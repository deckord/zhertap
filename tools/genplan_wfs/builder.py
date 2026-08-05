from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from shapely import make_valid
from shapely.geometry import shape

WFS_URL = "https://geo-shym.kz/geoserver/gis_shymkent/ows"
CATALOG_URL = "https://map.gov.kz/services/"
APPROVAL_URL = "https://adilet.zan.kz/rus/docs/P2300000916"
APPROVAL_DOCUMENT = (
    "Постановление Правительства Республики Казахстан "
    "от 17 октября 2023 года № 916"
)
ALLOWED_INDEX = "Ж-1"
ALLOWED_FUNCTION = "усадебной застройки (1-3 этажа)"
ALLOWED_STYLE_LABEL = "Территория усадебной застройки"
EXPECTED_RED_LINE_NUMBER = "№916"
EXPECTED_RED_LINE_DATE = "2023/10/17"
EXPECTED_BOUNDS = (68.5, 41.5, 71.0, 43.5)
RAW_FILES = {
    "functional": "gpfunctionalzone_main.raw.geojson",
    "roads": "gpautotranrdc_main.raw.geojson",
    "red_line": "gpregredlinelin.raw.geojson",
    "capabilities": "wfs-capabilities.xml",
    "approval": "P2300000916.pdf",
    "functional_style": "gpfunctionalzone_main.sld",
    "roads_style": "gpautotranrdc_main.sld",
    "red_line_style": "gpregredlinelin.sld",
}


class BuildError(ValueError):
    """Raised when an official WFS snapshot does not match the reviewed schema."""


@dataclass(frozen=True, slots=True)
class BuildResult:
    manifest_path: Path
    source_sha256: str
    layer_counts: dict[str, int]
    layer_sha256: dict[str, str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_geojson(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BuildError(f"{label} must be a GeoJSON FeatureCollection")
    features = payload.get("features")
    if not isinstance(features, list):
        raise BuildError(f"{label} features must be an array")
    return payload


def _property(feature: dict[str, Any], key: str) -> str:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return ""
    value = properties.get(key)
    return str(value).strip() if value is not None else ""


def _validate_geometry(feature: dict[str, Any], allowed_types: set[str]) -> None:
    geometry_payload = feature.get("geometry")
    if not isinstance(geometry_payload, dict):
        raise BuildError("WFS feature is missing geometry")
    try:
        geometry = make_valid(shape(geometry_payload))
    except Exception as exc:
        raise BuildError("WFS feature contains invalid geometry") from exc
    if geometry.is_empty or geometry.geom_type not in allowed_types:
        raise BuildError(
            f"Unexpected WFS geometry type {geometry.geom_type!r}; "
            f"expected one of {sorted(allowed_types)}"
        )
    min_x, min_y, max_x, max_y = geometry.bounds
    expected_min_x, expected_min_y, expected_max_x, expected_max_y = EXPECTED_BOUNDS
    if (
        min_x < expected_min_x
        or min_y < expected_min_y
        or max_x > expected_max_x
        or max_y > expected_max_y
    ):
        raise BuildError(f"WFS geometry lies outside the Shymkent safety bounds: {geometry.bounds}")


def _feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": "Shymkent official urban-plan WFS extract",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": features,
    }


def extract_shymkent_layers(source_dir: Path) -> dict[str, dict[str, Any]]:
    source = Path(source_dir).resolve()
    functional = _read_geojson(source / RAW_FILES["functional"], "functional-zone WFS")
    roads = _read_geojson(source / RAW_FILES["roads"], "road WFS")
    red_lines = _read_geojson(source / RAW_FILES["red_line"], "red-line WFS")

    allowed = [
        feature
        for feature in functional["features"]
        if _property(feature, "index") == ALLOWED_INDEX
        and _property(feature, "functional").casefold() == ALLOWED_FUNCTION.casefold()
        and _property(feature, "name_usl_1").casefold() == ALLOWED_STYLE_LABEL.casefold()
    ]
    if len(allowed) < 1000:
        raise BuildError(
            f"Expected at least 1000 Ж-1 features, received {len(allowed)}; "
            "the remote schema or dataset may have changed"
        )
    if not roads["features"]:
        raise BuildError("Official road layer is empty")
    if not red_lines["features"]:
        raise BuildError("Official red-line layer is empty")

    for feature in allowed:
        _validate_geometry(feature, {"Polygon", "MultiPolygon"})
    for feature in roads["features"]:
        if _property(feature, "name_usl") != "Дороги и проезды":
            raise BuildError("Road layer contains an unexpected classification")
        _validate_geometry(feature, {"Polygon", "MultiPolygon"})
    for feature in red_lines["features"]:
        if _property(feature, "number_post") != EXPECTED_RED_LINE_NUMBER:
            raise BuildError("Red-line layer is not tied to Government Resolution №916")
        if not _property(feature, "approved_date").startswith(EXPECTED_RED_LINE_DATE):
            raise BuildError("Red-line approval date does not match Resolution №916")
        _validate_geometry(
            feature,
            {"LineString", "MultiLineString", "Polygon", "MultiPolygon"},
        )

    return {
        "allowed": _feature_collection(allowed),
        "prohibited": _feature_collection(list(roads["features"])),
        "red_line": _feature_collection(list(red_lines["features"])),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _source_bundle(source_dir: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    entries: dict[str, dict[str, Any]] = {}
    for key, filename in RAW_FILES.items():
        path = source_dir / filename
        if not path.is_file():
            raise BuildError(f"Required official source snapshot is missing: {path}")
        entries[key] = {
            "file": filename,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest(), entries


def _validate_review_input(review: dict[str, Any], operator: str) -> dict[str, Any]:
    if review.get("status") not in {"STRICT", "VERIFIED_STRICT"}:
        raise BuildError("Independent review status must be STRICT or VERIFIED_STRICT")
    if review.get("independent_review") is not True:
        raise BuildError("Independent review must explicitly set independent_review=true")
    reviewer = str(review.get("reviewer", "")).strip()
    if not reviewer:
        raise BuildError("Independent review must name the reviewer")
    if reviewer.casefold() == operator.casefold():
        raise BuildError("Independent reviewer must differ from the vector operator")
    reviewed_at = str(review.get("reviewed_at_utc", "")).strip()
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BuildError("reviewed_at_utc must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BuildError("reviewed_at_utc must include a timezone")
    checks = review.get("checks")
    if not isinstance(checks, dict):
        raise BuildError("Independent review must include a checks object")
    required_checks = {
        "wfs_schema_verified",
        "resolution_916_verified",
        "geometry_bounds_verified",
        "random_visual_samples_verified",
    }
    missing = sorted(key for key in required_checks if checks.get(key) is not True)
    if missing:
        raise BuildError("Independent review checks are incomplete: " + ", ".join(missing))
    return review


def build_shymkent_release(
    source_dir: Path,
    output_dir: Path,
    review_input_path: Path,
    *,
    operator: str = "genplan-wfs-operator",
) -> BuildResult:
    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)

    source_sha, source_files = _source_bundle(source)
    layers = extract_shymkent_layers(source)
    layer_paths: dict[str, Path] = {}
    layer_hashes: dict[str, str] = {}
    layer_counts: dict[str, int] = {}
    for kind, payload in layers.items():
        path = output / f"{kind}.geojson"
        _write_json(path, payload)
        layer_paths[kind] = path
        layer_hashes[kind] = sha256_file(path)
        layer_counts[kind] = len(payload["features"])

    try:
        review_input = json.loads(Path(review_input_path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read independent review: {exc}") from exc
    if not isinstance(review_input, dict):
        raise BuildError("Independent review root must be an object")
    review_input = _validate_review_input(review_input, operator)

    release_id = "shymkent-genplan-916-wfs-v1"
    review = {
        **review_input,
        "release_id": release_id,
        "source_sha256": source_sha,
        "reviewer_role": "A2",
        "operator": operator,
        "allow_shadow": False,
        "layer_sha256": layer_hashes,
    }
    review_path = output / "review.json"
    _write_json(review_path, review)

    provenance = {
        "release_id": release_id,
        "source_sha256": source_sha,
        "review_sha256": sha256_file(review_path),
        "provenance_status": "verified_official",
        "identity_status": "matched",
        "official_url": WFS_URL,
        "catalog_url": CATALOG_URL,
        "approval_url": APPROVAL_URL,
        "source_files": source_files,
        "selection": {
            "allowed": {
                "source_layer": "gis_shymkent:gpfunctionalzone_main",
                "index": ALLOWED_INDEX,
                "functional": ALLOWED_FUNCTION,
                "style_label": ALLOWED_STYLE_LABEL,
            },
            "prohibited": {
                "source_layer": "gis_shymkent:gpautotranrdc_main",
                "name_usl": "Дороги и проезды",
            },
            "red_line": {
                "source_layer": "gis_shymkent:gpregredlinelin",
                "number_post": EXPECTED_RED_LINE_NUMBER,
                "approved_date": EXPECTED_RED_LINE_DATE,
            },
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
        "release_id": release_id,
        "release_mode": "search",
        "source_sha256": source_sha,
        "source_version": "Government Resolution №916 / live WFS snapshot",
        "source_epsg": 4326,
        "released_by": operator,
        "purpose": "ЛПХ",
        "scope": {
            "region": "г. Шымкент (79)",
            "district": "*",
            "locality": "*",
        },
        "document": {
            "title": "Генеральный план города Шымкента",
            "approval_document": APPROVAL_DOCUMENT,
            "approval_date": "2023-10-17",
            "source_authority": "Геопортал города Шымкента / Правительство Республики Казахстан",
            "source_url": WFS_URL,
        },
        "review": {"path": review_path.name, "sha256": sha256_file(review_path)},
        "provenance": {
            "path": provenance_path.name,
            "sha256": sha256_file(provenance_path),
        },
        "layers": {
            "allowed": {
                "path": layer_paths["allowed"].name,
                "sha256": layer_hashes["allowed"],
                "zone_name": "Ж-1: усадебная застройка (1-3 этажа)",
            },
            "prohibited": {
                "path": layer_paths["prohibited"].name,
                "sha256": layer_hashes["prohibited"],
                "zone_name": "Дороги и проезды",
            },
            "red_line": {
                "path": layer_paths["red_line"].name,
                "sha256": layer_hashes["red_line"],
                "zone_name": "Красные линии",
            },
        },
    }
    manifest_path = output / "release-manifest.json"
    _write_json(manifest_path, manifest)
    return BuildResult(
        manifest_path=manifest_path,
        source_sha256=source_sha,
        layer_counts=layer_counts,
        layer_sha256=layer_hashes,
    )
