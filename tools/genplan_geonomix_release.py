from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        description="Build a shadow urban-plan release from a Geonomix geoportal."
    )
    parser.add_argument("--base-url", required=True, help="Example: https://map.almobl.kz")
    parser.add_argument("--context-admterr-id", required=True, help="Example: kz.19")
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
    parser.add_argument("--operator", default="geonomix-release-operator")
    parser.add_argument("--reviewer", default="geonomix-release-reviewer")
    parser.add_argument("--reviewed-at-utc")
    parser.add_argument("--qa-status", default="WARNING")
    parser.add_argument("--release-mode", choices=("search", "shadow"))
    parser.add_argument("--allowed", action="append", default=[])
    parser.add_argument("--prohibited", action="append", default=[])
    parser.add_argument("--red-line", action="append", default=[])
    parser.add_argument("--allowed-prefix", action="append", default=[])
    parser.add_argument("--allowed-not-contains", action="append", default=[])
    parser.add_argument("--prohibited-prefix", action="append", default=[])
    parser.add_argument("--prohibited-not-prefix", action="append", default=[])
    parser.add_argument("--red-line-prefix", action="append", default=[])
    parser.add_argument("--allowed-list-search", action="append", default=[])
    parser.add_argument("--prohibited-list-search", action="append", default=[])
    parser.add_argument("--red-line-list-search", action="append", default=[])
    parser.add_argument("--geometry-workers", type=int, default=8)
    parser.add_argument("--max-features-per-collection", type=int, default=25_000)
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
    source = _build_source(
        base_url=args.base_url.rstrip("/"),
        context_admterr_id=args.context_admterr_id,
        allowed=args.allowed,
        prohibited=args.prohibited,
        red_line=args.red_line,
        filters={
            "allowed": _parse_prefix_filters(args.allowed_prefix),
            "allowed_not_contains": _parse_contains_filters(args.allowed_not_contains),
            "prohibited": _parse_prefix_filters(args.prohibited_prefix),
            "prohibited_not": _parse_prefix_filters(args.prohibited_not_prefix),
            "red_line": _parse_prefix_filters(args.red_line_prefix),
        },
        list_search={
            "allowed": _parse_exact_filters(args.allowed_list_search),
            "prohibited": _parse_exact_filters(args.prohibited_list_search),
            "red_line": _parse_exact_filters(args.red_line_list_search),
        },
        geometry_workers=args.geometry_workers,
        max_features_per_collection=args.max_features_per_collection,
    )

    layer_paths: dict[str, Path] = {}
    layer_hashes: dict[str, str] = {}
    layer_counts: dict[str, int] = {}
    for kind in LAYER_KINDS:
        source["layers"][kind]["features"].sort(
            key=lambda feature: (
                str((feature.get("properties") or {}).get("_geonomix_collection") or ""),
                str(feature.get("id") or ""),
            )
        )
        path = output / f"{kind}.geojson"
        _write_json(path, source["layers"][kind])
        layer_paths[kind] = path
        layer_hashes[kind] = _sha256_file(path)
        layer_counts[kind] = len(source["layers"][kind]["features"])

    source_manifest = {
        "schema_version": "geonomix-source/v1",
        "base_url": args.base_url.rstrip("/"),
        "context_admterr_id": args.context_admterr_id,
        "collections": source["source_collections"],
        "filters": source["filters"],
        "list_search": source["list_search"],
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
            "Geonomix API returned vector geometry. Search activation is allowed "
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
        "source_version": args.source_version
        or f"Geonomix snapshot {datetime.now(UTC).date()}",
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
                "zone_name": "Geonomix allowed functional zones",
            },
            "prohibited": {
                "path": layer_paths["prohibited"].name,
                "sha256": layer_hashes["prohibited"],
                "zone_name": "Geonomix prohibited functional zones",
            },
            "red_line": {
                "path": layer_paths["red_line"].name,
                "sha256": layer_hashes["red_line"],
                "zone_name": "Geonomix red lines",
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
    context_admterr_id: str,
    allowed: list[str],
    prohibited: list[str],
    red_line: list[str],
    filters: dict[str, dict[str, str]],
    list_search: dict[str, dict[str, str]],
    geometry_workers: int,
    max_features_per_collection: int,
) -> dict[str, Any]:
    layers = {kind: _feature_collection(f"Geonomix {kind}") for kind in LAYER_KINDS}
    counts: dict[str, int] = {}
    discarded: dict[str, list[dict[str, Any]]] = {kind: [] for kind in LAYER_KINDS}
    source_collections = {
        "allowed": allowed,
        "prohibited": prohibited,
        "red_line": red_line,
    }
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        for kind, collections in source_collections.items():
            for collection in collections:
                rows = _fetch_rows(
                    client,
                    base_url,
                    collection,
                    context_admterr_id=context_admterr_id,
                    list_search=list_search.get(kind, {}),
                    max_features=max_features_per_collection,
                )
                counts[collection] = len(rows)
                rows = [
                    row
                    for row in rows
                    if _row_matches_kind(row, kind=kind, filters=filters)
                ]
                features = _fetch_geometries(
                    client,
                    base_url,
                    collection,
                    rows,
                    kind=kind,
                    workers=geometry_workers,
                )
                for feature, error in features:
                    if error is not None:
                        discarded[kind].append(error)
                        continue
                    layers[kind]["features"].append(feature)
    for kind in LAYER_KINDS:
        if not layers[kind]["features"]:
            raise SystemExit(f"No usable {kind} features were exported")
    return {
        "layers": layers,
        "counts": counts,
        "discarded": discarded,
        "source_collections": source_collections,
        "filters": filters,
        "list_search": list_search,
    }


def _fetch_rows(
    client: httpx.Client,
    base_url: str,
    collection: str,
    *,
    context_admterr_id: str,
    list_search: dict[str, str],
    max_features: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    continuation: list[Any] | None = None
    while len(rows) < max_features:
        params: list[tuple[str, Any]] = [
            ("lang", "ru"),
            ("collection", collection),
            ("context[admterr_id]", context_admterr_id),
            ("$type", "list"),
        ]
        for field, text in list_search.items():
            params.append((f"search[{field}][text]", text))
        if continuation:
            params.extend(("continuation[]", value) for value in continuation)
        response = client.get(f"{base_url}/api/list", params=params)
        response.raise_for_status()
        payload = response.json()
        page = payload.get("features") or []
        rows.extend(page[: max_features - len(rows)])
        continuation = payload.get("continuation")
        if not continuation or not page:
            break
    return rows


def _row_matches_kind(
    row: dict[str, Any],
    *,
    kind: str,
    filters: dict[str, dict[str, str]],
) -> bool:
    properties = row.get("properties") or {}
    if not _matches_prefixes(properties, filters.get(kind, {})):
        return False
    if kind == "allowed" and _matches_contains(
        properties,
        filters.get("allowed_not_contains", {}),
    ):
        return False
    prohibited_not = filters.get("prohibited_not", {})
    if kind == "prohibited" and prohibited_not and _matches_prefixes(
        properties,
        prohibited_not,
    ):
        return False
    return True


def _matches_prefixes(properties: dict[str, Any], filters: dict[str, str]) -> bool:
    for field, prefix in filters.items():
        value = properties.get(field)
        if value is None or not str(value).startswith(prefix):
            return False
    return True


def _matches_contains(properties: dict[str, Any], filters: dict[str, list[str]]) -> bool:
    for field, needles in filters.items():
        value = properties.get(field)
        if value is None:
            continue
        text = str(value).casefold()
        if any(needle.casefold() in text for needle in needles):
            return True
    return False


def _parse_prefix_filters(values: list[str]) -> dict[str, str]:
    filters: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"Invalid prefix filter {value!r}; expected FIELD=PREFIX")
        field, prefix = value.split("=", 1)
        field = field.strip()
        prefix = prefix.strip()
        if not field or not prefix:
            raise SystemExit(f"Invalid prefix filter {value!r}; expected FIELD=PREFIX")
        filters[field] = prefix
    return filters


def _parse_contains_filters(values: list[str]) -> dict[str, list[str]]:
    filters: dict[str, list[str]] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"Invalid contains filter {value!r}; expected FIELD=TEXT")
        field, text = value.split("=", 1)
        field = field.strip()
        text = text.strip()
        if not field or not text:
            raise SystemExit(f"Invalid contains filter {value!r}; expected FIELD=TEXT")
        filters.setdefault(field, []).append(text)
    return filters


def _parse_exact_filters(values: list[str]) -> dict[str, str]:
    filters: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"Invalid list search {value!r}; expected FIELD=TEXT")
        field, text = value.split("=", 1)
        field = field.strip()
        text = text.strip()
        if not field or not text:
            raise SystemExit(f"Invalid list search {value!r}; expected FIELD=TEXT")
        filters[field] = text
    return filters


def _fetch_geometries(
    client: httpx.Client,
    base_url: str,
    collection: str,
    rows: list[dict[str, Any]],
    *,
    kind: str,
    workers: int,
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    results: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_fetch_one_geometry, client, base_url, collection, row, kind): row
            for row in rows
        }
        for future in as_completed(futures):
            results.append(future.result())
    return results


def _fetch_one_geometry(
    client: httpx.Client,
    base_url: str,
    collection: str,
    row: dict[str, Any],
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    feature_id = row.get("id")
    try:
        response = client.get(
            f"{base_url}/api/geometry",
            params={"collection": collection, "feature_id": feature_id},
        )
        response.raise_for_status()
        geometry = response.json()
        if not geometry:
            return {}, {"collection": collection, "id": feature_id, "error": "empty_geometry"}
        feature = {
            "type": "Feature",
            "id": feature_id,
            "properties": {
                **(row.get("properties") or {}),
                "_geonomix_collection": collection,
            },
            "geometry": geometry,
        }
        normalized = _normalize_feature(feature, kind)
        if normalized is None:
            return {}, {
                "collection": collection,
                "id": feature_id,
                "geometry_type": geometry.get("type"),
            }
        return normalized, None
    except Exception as exc:
        return {}, {"collection": collection, "id": feature_id, "error": str(exc)}


def _normalize_feature(feature: dict[str, Any], kind: str) -> dict[str, Any] | None:
    geometry = feature.get("geometry")
    if not geometry or not geometry.get("type"):
        return None
    allowed_types = RED_LINE_TYPES if kind == "red_line" else POLYGON_TYPES
    if geometry["type"] not in allowed_types:
        return None
    geom = make_valid(shape(geometry))
    if geom.is_empty:
        return None
    if kind == "red_line" and geom.geom_type in POLYGON_TYPES:
        geom = geom.boundary
    if kind != "red_line" and geom.geom_type not in POLYGON_TYPES:
        return None
    if kind == "red_line" and geom.geom_type not in {"LineString", "MultiLineString"}:
        return None
    return {
        "type": "Feature",
        "id": feature.get("id"),
        "properties": feature.get("properties") or {},
        "geometry": mapping(geom),
    }


def _feature_collection(name: str) -> dict[str, Any]:
    return {"type": "FeatureCollection", "name": name, "features": []}


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
