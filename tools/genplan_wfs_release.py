from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from shapely import make_valid
from shapely.geometry import mapping, shape

from app.purposes import GARDENING, LPH_FIELD_LAYER, LPH_HOUSEHOLD_LAYER

LAYER_KINDS = ("allowed", "prohibited", "red_line")
PURPOSES = {
    "lph-household": LPH_HOUSEHOLD_LAYER,
    "lph-field": LPH_FIELD_LAYER,
    "gardening": GARDENING,
}
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
RED_LINE_TYPES = {"LineString", "MultiLineString", "Polygon", "MultiPolygon"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a shadow urban-plan release from generic WFS/GeoServer layers."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--profile", required=True, choices=sorted(PURPOSES))
    parser.add_argument("--region", required=True)
    parser.add_argument("--district", required=True)
    parser.add_argument("--locality", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--approval-document", required=True)
    parser.add_argument("--source-authority", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-version", default="")
    parser.add_argument("--operator", default="generic-wfs-release-operator")
    parser.add_argument("--reviewer", default="generic-wfs-release-reviewer")
    parser.add_argument("--reviewed-at-utc")
    parser.add_argument("--qa-status", default="WARNING")
    parser.add_argument("--release-mode", choices=("search", "shadow"))
    parser.add_argument("--allowed", action="append", default=[])
    parser.add_argument("--allowed-search", action="append", default=[])
    parser.add_argument("--prohibited", action="append", default=[])
    parser.add_argument("--red-line", action="append", default=[])
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-features-per-layer", type=int, default=250_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allowed or not args.prohibited or not args.red_line:
        raise SystemExit("--allowed, --prohibited and --red-line are required")
    qa_status = args.qa_status.strip().upper()
    release_mode = args.release_mode or ("shadow" if qa_status == "WARNING" else "search")
    if qa_status == "WARNING" and release_mode != "shadow":
        raise SystemExit("WARNING releases must use --release-mode shadow")
    if release_mode == "search" and qa_status not in {"STRICT", "VERIFIED_STRICT"}:
        raise SystemExit("search releases require --qa-status STRICT or VERIFIED_STRICT")
    if args.operator.casefold() == args.reviewer.casefold():
        raise SystemExit("--reviewer must differ from --operator for independent review")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    allowed_search = _parse_search(args.allowed_search)

    source = _build_source(
        base_url=args.base_url,
        allowed=args.allowed,
        allowed_search=allowed_search,
        prohibited=args.prohibited,
        red_line=args.red_line,
        page_size=args.page_size,
        max_features_per_layer=args.max_features_per_layer,
    )
    layer_paths: dict[str, Path] = {}
    layer_hashes: dict[str, str] = {}
    layer_counts: dict[str, int] = {}
    for kind in LAYER_KINDS:
        source["layers"][kind]["features"].sort(
            key=lambda feature: (
                str((feature.get("properties") or {}).get("_wfs_layer") or ""),
                str(feature.get("id") or ""),
            )
        )
        path = output / f"{kind}.geojson"
        _write_json(path, source["layers"][kind])
        layer_paths[kind] = path
        layer_hashes[kind] = _sha256_file(path)
        layer_counts[kind] = len(source["layers"][kind]["features"])

    source_manifest = {
        "schema_version": "generic-wfs-source/v1",
        "base_url": args.base_url,
        "layers": source["source_layers"],
        "filters": {"allowed": allowed_search},
        "counts": source["counts"],
        "discarded": source["discarded"],
        "layer_sha256": layer_hashes,
        "release_policy": {
            "qa_status": qa_status,
            "release_mode": release_mode,
        },
    }
    source_manifest_path = output / "source-manifest.json"
    _write_json(source_manifest_path, source_manifest)
    source_sha = _sha256_file(source_manifest_path)

    review = {
        "release_id": args.release_id,
        "source_sha256": source_sha,
        "status": qa_status,
        "independent_review": True,
        "reviewer": args.reviewer,
        "reviewer_role": "A2",
        "operator": args.operator,
        "reviewed_at_utc": args.reviewed_at_utc or datetime.now(UTC).isoformat(),
        "allow_shadow": qa_status == "WARNING",
        "layer_sha256": layer_hashes,
        "notes": [
            "WFS geometry was exported structurally. Search activation is allowed "
            "only when the official source, zone mapping and visual samples were "
            "independently reviewed.",
        ],
    }
    review_path = output / "review.json"
    _write_json(review_path, review)

    provenance = {
        "release_id": args.release_id,
        "source_sha256": source_sha,
        "review_sha256": _sha256_file(review_path),
        "provenance_status": "verified_official",
        "identity_status": "matched",
        "official_url": args.source_url,
        "source_manifest": {
            "path": source_manifest_path.name,
            "sha256": source_sha,
        },
        "layers": {
            kind: {"sha256": digest, "feature_count": layer_counts[kind]}
            for kind, digest in layer_hashes.items()
        },
    }
    provenance_path = output / "provenance.json"
    _write_json(provenance_path, provenance)

    manifest = {
        "schema_version": "1.0",
        "release_id": args.release_id,
        "release_mode": release_mode,
        "source_sha256": source_sha,
        "source_version": args.source_version or f"Generic WFS snapshot {datetime.now(UTC).date()}",
        "source_epsg": 4326,
        "released_by": args.operator,
        "purpose": PURPOSES[args.profile],
        "scope": {
            "region": args.region,
            "district": args.district,
            "locality": args.locality,
        },
        "document": {
            "title": args.title,
            "approval_document": args.approval_document,
            "approval_date": None,
            "source_authority": args.source_authority,
            "source_url": args.source_url,
        },
        "review": {"path": review_path.name, "sha256": _sha256_file(review_path)},
        "provenance": {
            "path": provenance_path.name,
            "sha256": _sha256_file(provenance_path),
        },
        "layers": {
            "allowed": {
                "path": layer_paths["allowed"].name,
                "sha256": layer_hashes["allowed"],
                "zone_name": "WFS allowed functional zones",
            },
            "prohibited": {
                "path": layer_paths["prohibited"].name,
                "sha256": layer_hashes["prohibited"],
                "zone_name": "WFS prohibited functional zones",
            },
            "red_line": {
                "path": layer_paths["red_line"].name,
                "sha256": layer_hashes["red_line"],
                "zone_name": "WFS red lines",
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
                "layer_counts": layer_counts,
                "discarded": source["discarded"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _build_source(
    *,
    base_url: str,
    allowed: list[str],
    allowed_search: dict[str, str],
    prohibited: list[str],
    red_line: list[str],
    page_size: int,
    max_features_per_layer: int,
) -> dict[str, Any]:
    layers = {kind: _feature_collection(f"WFS {kind}") for kind in LAYER_KINDS}
    counts: dict[str, int] = {}
    discarded: dict[str, list[dict[str, Any]]] = {kind: [] for kind in LAYER_KINDS}
    source_layers = {
        "allowed": allowed,
        "prohibited": prohibited,
        "red_line": red_line,
    }
    for kind, names in source_layers.items():
        search = allowed_search if kind == "allowed" else {}
        for name in names:
            fetched = _fetch_features(
                base_url,
                name,
                page_size=page_size,
                max_features=max_features_per_layer,
            )
            counts[name] = len(fetched)
            for feature in fetched:
                if search and not _matches_properties(feature, search):
                    continue
                normalized = _normalize_feature(feature, kind)
                if normalized is None:
                    discarded[kind].append(
                        {
                            "layer": name,
                            "id": feature.get("id"),
                            "geometry_type": (feature.get("geometry") or {}).get("type"),
                        }
                    )
                    continue
                normalized.setdefault("properties", {})["_wfs_layer"] = name
                layers[kind]["features"].append(normalized)
    for kind in LAYER_KINDS:
        if not layers[kind]["features"]:
            raise SystemExit(f"No usable {kind} features were exported")
    return {
        "layers": layers,
        "counts": counts,
        "discarded": discarded,
        "source_layers": source_layers,
    }


def _fetch_features(
    base_url: str,
    layer: str,
    *,
    page_size: int,
    max_features: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        while len(rows) < max_features:
            response = client.get(
                base_url,
                params={
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetFeature",
                    "typeNames": layer,
                    "count": page_size,
                    "startIndex": start,
                    "srsName": "EPSG:4326",
                    "outputFormat": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
            features = payload.get("features") if isinstance(payload, dict) else None
            if not isinstance(features, list) or not features:
                return rows
            rows.extend(feature for feature in features if isinstance(feature, dict))
            if len(features) < page_size:
                return rows[:max_features]
            start += len(features)
    return rows[:max_features]


def _normalize_feature(feature: dict[str, Any], layer_kind: str) -> dict[str, Any] | None:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return None
    allowed_types = RED_LINE_TYPES if layer_kind == "red_line" else POLYGON_TYPES
    if geometry.get("type") not in allowed_types:
        return None
    try:
        valid = make_valid(shape(geometry))
    except Exception:
        return None
    if valid.is_empty:
        return None
    if layer_kind == "red_line" and valid.geom_type in POLYGON_TYPES:
        valid = valid.boundary
    if valid.geom_type not in (RED_LINE_TYPES if layer_kind == "red_line" else POLYGON_TYPES):
        return None
    result = dict(feature)
    result["geometry"] = mapping(valid)
    return result


def _matches_properties(feature: dict[str, Any], search: dict[str, str]) -> bool:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return False
    for field, value in search.items():
        if str(properties.get(field) or "").strip().casefold() != value.casefold():
            return False
    return True


def _parse_search(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        field, separator, value = item.partition("=")
        if not separator or not field.strip() or not value.strip():
            raise SystemExit("--allowed-search must use field=value format")
        result[field.strip()] = value.strip()
    return result


def _feature_collection(name: str) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": name,
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": [],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
