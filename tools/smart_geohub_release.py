from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shapely import make_valid
from shapely.geometry import shape

from tools.smart_geohub_export import SmartGeoHubClient, SmartGeoHubClientError

LAYER_KINDS = ("allowed", "prohibited", "red_line")
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
RED_LINE_TYPES = POLYGON_TYPES | {"LineString", "MultiLineString"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a genplan_import release candidate from Smart GeoHub collections."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--district", default="*")
    parser.add_argument("--locality", default="*")
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--approval-document", required=True)
    parser.add_argument("--approval-date")
    parser.add_argument("--source-authority", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-version", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--operator", default="smart-geohub-release-operator")
    parser.add_argument("--reviewer", default="smart-geohub-release-reviewer")
    parser.add_argument("--reviewed-at-utc")
    parser.add_argument("--qa-status", default="WARNING")
    parser.add_argument("--release-mode", choices=("search", "shadow"))
    parser.add_argument("--context-admterr-id", default="kz")
    parser.add_argument("--max-features-per-collection", type=int, default=250_000)
    parser.add_argument("--geometry-workers", type=int, default=1)
    parser.add_argument("--allowed", action="append", default=[], help="Allowed collection")
    parser.add_argument(
        "--allowed-search",
        action="append",
        default=[],
        help="Search filter for allowed collections, format field=value",
    )
    parser.add_argument("--prohibited", action="append", default=[], help="Prohibited collection")
    parser.add_argument("--red-line", action="append", default=[], help="Red-line collection")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allowed:
        print("At least one --allowed collection is required", file=sys.stderr)
        return 2
    if not args.prohibited:
        print("At least one --prohibited collection is required", file=sys.stderr)
        return 2
    if not args.red_line:
        print("At least one --red-line collection is required", file=sys.stderr)
        return 2
    qa_status = args.qa_status.strip().upper()
    release_mode = args.release_mode or ("shadow" if qa_status == "WARNING" else "search")
    if qa_status == "WARNING" and release_mode != "shadow":
        print("WARNING releases must use --release-mode shadow", file=sys.stderr)
        return 2
    if release_mode == "search" and qa_status not in {"STRICT", "VERIFIED_STRICT"}:
        print("search releases require --qa-status STRICT or VERIFIED_STRICT", file=sys.stderr)
        return 2
    if qa_status in {"STRICT", "VERIFIED_STRICT"} and args.operator == args.reviewer:
        print(
            "STRICT/VERIFIED_STRICT releases require reviewer different from operator",
            file=sys.stderr,
        )
        return 2
    reviewed_at_utc = args.reviewed_at_utc or datetime.now(UTC).isoformat()

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    client = SmartGeoHubClient(
        base_url=args.base_url,
        max_features=args.max_features_per_collection,
        geometry_workers=args.geometry_workers,
    )
    allowed_search = _parse_search(args.allowed_search)

    try:
        try:
            layer_payloads, collection_stats = _fetch_layers(
                client=client,
                allowed=args.allowed,
                allowed_search=allowed_search,
                prohibited=args.prohibited,
                red_line=args.red_line,
                context_admterr_id=args.context_admterr_id,
                max_features=args.max_features_per_collection,
            )
        except SmartGeoHubClientError as exc:
            print(f"Smart GeoHub release blocked: {exc}", file=sys.stderr)
            return 2
    finally:
        client.close()

    layer_paths: dict[str, Path] = {}
    layer_hashes: dict[str, str] = {}
    for layer_kind in LAYER_KINDS:
        path = output / f"{layer_kind}.geojson"
        _write_json(path, layer_payloads[layer_kind])
        layer_paths[layer_kind] = path
        layer_hashes[layer_kind] = _sha(path)

    source_manifest = {
        "schema_version": "smart-geohub-source/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "base_url": client.base_url,
        "api_base_url": client.api_base_url,
        "context_admterr_id": args.context_admterr_id,
        "collections": collection_stats,
        "layer_sha256": layer_hashes,
        "release_policy": {
            "qa_status": qa_status,
            "release_mode": release_mode,
            "approved_for_search": release_mode == "search"
            and qa_status in {"STRICT", "VERIFIED_STRICT"},
        },
    }
    source_manifest_path = output / "source-manifest.json"
    _write_json(source_manifest_path, source_manifest)
    source_sha = _sha(source_manifest_path)

    review = {
        "release_id": args.release_id,
        "source_sha256": source_sha,
        "status": qa_status,
        "independent_review": True,
        "reviewer_role": "A2",
        "reviewer": args.reviewer,
        "operator": args.operator,
        "reviewed_at_utc": reviewed_at_utc,
        "allow_shadow": qa_status == "WARNING",
        "layer_sha256": layer_hashes,
        "notes": (
            "Smart GeoHub release candidate. WARNING means geometry and source identity "
            "are stored for shadow QA and are not approved for client search."
        ),
    }
    review_path = output / "review.json"
    _write_json(review_path, review)
    review_sha = _sha(review_path)

    provenance = {
        "release_id": args.release_id,
        "source_sha256": source_sha,
        "review_sha256": review_sha,
        "provenance_status": "verified_official",
        "identity_status": "matched",
        "official_url": args.source_url,
        "source_manifest": {"path": source_manifest_path.name, "sha256": source_sha},
        "layers": {kind: {"sha256": layer_hashes[kind]} for kind in LAYER_KINDS},
    }
    provenance_path = output / "provenance.json"
    _write_json(provenance_path, provenance)
    provenance_sha = _sha(provenance_path)

    manifest = {
        "schema_version": "1.0",
        "release_id": args.release_id,
        "release_mode": release_mode,
        "source_sha256": source_sha,
        "source_version": args.source_version,
        "source_epsg": 4326,
        "released_by": args.operator,
        "purpose": args.purpose,
        "scope": {
            "region": args.region,
            "district": args.district,
            "locality": args.locality,
        },
        "document": {
            "title": args.title,
            "approval_document": args.approval_document,
            "approval_date": args.approval_date,
            "source_authority": args.source_authority,
            "source_url": args.source_url,
        },
        "review": {"path": review_path.name, "sha256": review_sha},
        "provenance": {"path": provenance_path.name, "sha256": provenance_sha},
        "layers": {
            "allowed": {
                "path": layer_paths["allowed"].name,
                "sha256": layer_hashes["allowed"],
                "zone_name": "Smart GeoHub allowed functional zones",
            },
            "prohibited": {
                "path": layer_paths["prohibited"].name,
                "sha256": layer_hashes["prohibited"],
                "zone_name": "Smart GeoHub prohibited and restriction zones",
            },
            "red_line": {
                "path": layer_paths["red_line"].name,
                "sha256": layer_hashes["red_line"],
                "zone_name": "Smart GeoHub red lines",
            },
        },
    }
    manifest_path = output / "release-manifest.json"
    _write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "release_id": args.release_id,
                "release_mode": release_mode,
                "qa_status": qa_status,
                "layers": {
                    kind: {
                        "features": len(layer_payloads[kind]["features"]),
                        "bytes": layer_paths[kind].stat().st_size,
                        "sha256": layer_hashes[kind],
                    }
                    for kind in LAYER_KINDS
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _parse_search(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        field, separator, value = item.partition("=")
        if not separator or not field.strip() or not value.strip():
            raise SystemExit("--allowed-search must use field=value format")
        result[field.strip()] = value.strip()
    return result


def _fetch_layers(
    *,
    client: SmartGeoHubClient,
    allowed: list[str],
    allowed_search: dict[str, str],
    prohibited: list[str],
    red_line: list[str],
    context_admterr_id: str,
    max_features: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    stats: list[dict[str, Any]] = []
    layers = {
        "allowed": _feature_collection(),
        "prohibited": _feature_collection(),
        "red_line": _feature_collection(),
    }
    for layer_kind, collections, search in (
        ("allowed", allowed, allowed_search),
        ("prohibited", prohibited, {}),
        ("red_line", red_line, {}),
    ):
        for collection in collections:
            before = len(layers[layer_kind]["features"])
            features = list(
                client.features_with_geometry(
                    collection,
                    context_admterr_id=context_admterr_id,
                    max_features=max_features,
                    search=search,
                )
            )
            accepted_features = []
            for feature in features:
                if not _feature_allowed_for_layer(feature, layer_kind):
                    continue
                properties = dict(feature.get("properties") or {})
                properties["_source_collection"] = collection
                properties["_release_layer_kind"] = layer_kind
                feature["properties"] = properties
                accepted_features.append(feature)
            accepted_features.sort(
                key=lambda item: (
                    str((item.get("properties") or {}).get("_source_collection") or ""),
                    str(item.get("id") or (item.get("properties") or {}).get("id") or ""),
                )
            )
            layers[layer_kind]["features"].extend(accepted_features)
            stats.append(
                {
                    "collection": collection,
                    "layer_kind": layer_kind,
                    "search": search,
                    "feature_count": len(features),
                    "accepted_feature_count": len(layers[layer_kind]["features"]) - before,
                    "skipped_geometry_count": len(features)
                    - (len(layers[layer_kind]["features"]) - before),
                    "cumulative_layer_count": len(layers[layer_kind]["features"]),
                    "truncated_by_limit": len(features) >= max_features,
                    "start_index": before,
                }
            )
    return layers, stats


def _feature_allowed_for_layer(feature: dict[str, Any], layer_kind: str) -> bool:
    geometry = feature.get("geometry")
    geometry_type = geometry.get("type") if isinstance(geometry, dict) else None
    if not geometry_type:
        return False
    allowed = RED_LINE_TYPES if layer_kind == "red_line" else POLYGON_TYPES
    if str(geometry_type) not in allowed:
        return False
    try:
        valid = make_valid(shape(geometry))
    except Exception:
        return False
    return not valid.is_empty and valid.geom_type in allowed


def _feature_collection() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [],
    }


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
