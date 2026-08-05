from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import SmartGeoHubClient

SUMMARY_FIELDS = (
    "functional",
    "name_usl",
    "usl_i32",
    "gpsubtype_id",
    "object_id_i32",
)


@dataclass(frozen=True, slots=True)
class ExportResult:
    manifest_path: Path
    geojson_path: Path
    collection: str
    feature_count: int
    source_sha256: str
    geometry_types: dict[str, int]


@dataclass(frozen=True, slots=True)
class SummaryResult:
    manifest_path: Path
    collection: str
    feature_count: int
    truncated_by_limit: bool


@dataclass(frozen=True, slots=True)
class CountResult:
    manifest_path: Path
    collection: str
    feature_count: int


def export_collection(
    *,
    base_url: str,
    collection: str,
    output_dir: Path,
    context_admterr_id: str = "kz",
    max_features: int = 10_000,
    operator: str = "smart-geohub-exporter",
    client: SmartGeoHubClient | None = None,
    search: dict[str, str] | None = None,
    geometry_workers: int = 1,
) -> ExportResult:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    source = client or SmartGeoHubClient(
        base_url=base_url,
        max_features=max_features,
        geometry_workers=geometry_workers,
    )
    try:
        features = list(
            source.features_with_geometry(
                collection,
                context_admterr_id=context_admterr_id,
                max_features=max_features,
                search=search,
            )
        )
    finally:
        if own_client:
            source.close()
    feature_collection = {
        "type": "FeatureCollection",
        "name": collection,
        "features": features,
    }
    geojson_path = output / "features.geojson"
    _write_json(geojson_path, feature_collection)
    source_sha256 = sha256_file(geojson_path)
    manifest = {
        "schema_version": "smart-geohub-export/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "generated_by": operator,
        "base_url": source.base_url,
        "api_base_url": source.api_base_url,
        "collection": collection,
        "context_admterr_id": context_admterr_id,
        "source_sha256": source_sha256,
        "feature_count": len(features),
        "max_features": max_features,
        "truncated_by_limit": len(features) >= max_features,
        "search": search or {},
        "geometry_types": dict(_geometry_type_counts(features)),
        "property_counts": _property_counts(features),
        "files": {
            "features": {
                "path": geojson_path.name,
                "sha256": source_sha256,
            }
        },
        "next_step": (
            "Map official zone semantics, split allowed/prohibited/red_line layers, "
            "run independent QA, then create a genplan_import release."
        ),
    }
    manifest_path = output / "export-manifest.json"
    _write_json(manifest_path, manifest)
    return ExportResult(
        manifest_path=manifest_path,
        geojson_path=geojson_path,
        collection=collection,
        feature_count=len(features),
        source_sha256=source_sha256,
        geometry_types=manifest["geometry_types"],
    )


def summarize_collection(
    *,
    base_url: str,
    collection: str,
    output_dir: Path,
    context_admterr_id: str = "kz",
    max_features: int = 250_000,
    operator: str = "smart-geohub-summary",
    client: SmartGeoHubClient | None = None,
    search: dict[str, str] | None = None,
) -> SummaryResult:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    source = client or SmartGeoHubClient(base_url=base_url, max_features=max_features)
    try:
        features = source.features(
            collection,
            context_admterr_id=context_admterr_id,
            max_features=max_features,
            search=search,
        )
    finally:
        if own_client:
            source.close()
    manifest = {
        "schema_version": "smart-geohub-summary/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "generated_by": operator,
        "base_url": source.base_url,
        "api_base_url": source.api_base_url,
        "collection": collection,
        "context_admterr_id": context_admterr_id,
        "feature_count": len(features),
        "max_features": max_features,
        "truncated_by_limit": len(features) >= max_features,
        "search": search or {},
        "property_counts": _property_counts(features),
        "next_step": (
            "Use this summary to choose candidate allowed/prohibited/red-line "
            "collections before exporting full geometry."
        ),
    }
    manifest_path = output / "summary-manifest.json"
    _write_json(manifest_path, manifest)
    return SummaryResult(
        manifest_path=manifest_path,
        collection=collection,
        feature_count=len(features),
        truncated_by_limit=manifest["truncated_by_limit"],
    )


def count_collection(
    *,
    base_url: str,
    collection: str,
    output_dir: Path,
    context_admterr_id: str = "kz",
    operator: str = "smart-geohub-count",
    client: SmartGeoHubClient | None = None,
    search: dict[str, str] | None = None,
) -> CountResult:
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    source = client or SmartGeoHubClient(base_url=base_url)
    try:
        feature_count = source.count(
            collection,
            context_admterr_id=context_admterr_id,
            search=search,
        )
    finally:
        if own_client:
            source.close()
    manifest = {
        "schema_version": "smart-geohub-count/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "generated_by": operator,
        "base_url": source.base_url,
        "api_base_url": source.api_base_url,
        "collection": collection,
        "context_admterr_id": context_admterr_id,
        "feature_count": feature_count,
        "search": search or {},
        "next_step": "Use positive counts to choose focused geometry exports.",
    }
    manifest_path = output / "count-manifest.json"
    _write_json(manifest_path, manifest)
    return CountResult(
        manifest_path=manifest_path,
        collection=collection,
        feature_count=feature_count,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _geometry_type_counts(features: list[dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for feature in features:
        geometry = feature.get("geometry") or {}
        counter[str(geometry.get("type") or "unknown")] += 1
    return counter


def _property_counts(features: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for field in SUMMARY_FIELDS:
        counter: Counter[str] = Counter()
        for feature in features:
            properties = feature.get("properties") or {}
            value = str(properties.get(field) or "").strip()
            if value:
                counter[value] += 1
        if counter:
            result[field] = dict(counter.most_common(50))
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
