from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.purposes import GARDENING, LPH_FIELD_LAYER, LPH_HOUSEHOLD_LAYER

from .client import GgkClient, GgkClientError

CATALOG_URL = "https://map.gov.kz/services/"
SOURCE_AUTHORITY = "АИС государственного градостроительного кадастра Республики Казахстан"
MAPPING_VERSION = "ais-ggk-functional-zones-v1"
KAZAKHSTAN_BOUNDS = (45.0, 39.0, 88.5, 56.5)
PROFILE_CONFIG: dict[str, dict[str, Any]] = {
    "lph-household": {
        "purpose": LPH_HOUSEHOLD_LAYER,
        "allowed_codes": {"11010000"},
        "zone_name": "Территория усадебной застройки",
    },
    "lph-field": {
        "purpose": LPH_FIELD_LAYER,
        "allowed_codes": {"11024000", "11420000"},
        "zone_name": "Территория растениеводства или земель сельскохозяйственного назначения",
    },
    "gardening": {
        "purpose": GARDENING,
        "allowed_codes": {"11023000", "11500000"},
        "zone_name": "Территория садоводческих товариществ или садоводческих земель",
    },
}
PROHIBITED_CODES = {
    "11016000",
    "11017000",
    "11018000",
    "11019000",
    "11036000",
    "11037000",
    "11038000",
    "11039000",
    "11080000",
    "11090000",
    "11100000",
    "11110000",
    "11120000",
    "11130000",
    "11140000",
    "11150000",
    "11240000",
    "11250000",
    "11260000",
    "11280000",
    "11300000",
    "11310000",
    "11320000",
    "11330000",
    "11350000",
    "11360000",
    "11370000",
    "11380000",
    "11390000",
    "11400000",
    "11410000",
    "11440000",
    "11450000",
    "11460000",
    "11470000",
    "11510000",
    "11520000",
    "11530000",
}
REQUIRED_REVIEW_CHECKS = {
    "document_identity_verified",
    "legal_act_verified",
    "kato_scope_verified",
    "zone_mapping_verified",
    "geometry_bounds_verified",
    "random_visual_samples_verified",
}


class BuildError(ValueError):
    """Raised when an AIS GGK document cannot pass the release safety gate."""


@dataclass(frozen=True, slots=True)
class BuildResult:
    manifest_path: Path
    release_id: str
    document_id: int
    source_sha256: str
    scope: dict[str, str]
    layer_counts: dict[str, int]
    layer_sha256: dict[str, str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def list_ggk_documents(client: GgkClient | None = None) -> list[dict[str, Any]]:
    source = client or GgkClient()
    documents = source.features("gp_documents")
    result = []
    for feature in documents:
        properties = _properties(feature)
        result.append(
            {
                "id": _integer(properties.get("id"), "document id"),
                "locality": _text(properties.get("kato_name_ru")),
                "title": _text(properties.get("doc_name")),
                "number": _text(properties.get("doc_number")),
                "date": _text(properties.get("doc_date")),
                "status_id": properties.get("status_id"),
                "deactivation_date": properties.get("deactivation_date"),
            }
        )
    return sorted(result, key=lambda item: (item["locality"].casefold(), item["id"]))


def build_ggk_release(
    document_id: int,
    profile: str,
    output_dir: Path,
    review_input_path: Path | None,
    *,
    operator: str = "ggk-wfs-operator",
    client: GgkClient | None = None,
    release_mode: str = "search",
    shadow_source_url: str | None = None,
) -> BuildResult:
    if profile not in PROFILE_CONFIG:
        raise BuildError("Unknown profile: " + profile)
    release_mode = release_mode.lower().strip()
    if release_mode not in {"search", "shadow"}:
        raise BuildError("release_mode must be search or shadow")
    if release_mode == "search" and review_input_path is None:
        raise BuildError("Search release requires an independent review file")
    source = client or GgkClient()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_dir = output / "source"
    source_dir.mkdir(exist_ok=True)

    try:
        document = source.one("gp_documents", cql_filter=f"id={int(document_id)}")
        kato_rows = source.features("kato_ref")
        zone_rows = source.features("gp_func_zone_codes_ref")
        functional = source.features(
            "gp_functional_zones",
            cql_filter=f"creation_doc_id={int(document_id)} AND deactivation_doc_id IS NULL",
        )
        red_lines = source.features(
            "gp_red_lines",
            cql_filter=f"creation_doc_id={int(document_id)} AND deactivation_doc_id IS NULL",
        )
    except GgkClientError as exc:
        raise BuildError(str(exc)) from exc

    raw_document = copy.deepcopy(document)
    raw_functional = copy.deepcopy(functional)
    raw_red_lines = copy.deepcopy(red_lines)
    document_properties = _properties(document)
    _validate_document(document_properties, document_id)
    document_geometry = _geometry(document, {"Polygon", "MultiPolygon"}, "document boundary")
    kato_by_id = {
        _integer(_properties(row).get("id"), "KATO id"): _properties(row)
        for row in kato_rows
    }
    zone_by_id = {
        _integer(_properties(row).get("id"), "zone reference id"): _properties(row)
        for row in zone_rows
    }
    scope, ancestry = _infer_scope(document_properties, kato_by_id)
    profile_config = PROFILE_CONFIG[profile]

    allowed: list[dict[str, Any]] = []
    prohibited: list[dict[str, Any]] = []
    zone_counts: dict[str, int] = {}
    raw_zone_counts: dict[str, int] = {}
    discarded_degenerate: list[dict[str, Any]] = []
    discarded_red_lines: list[dict[str, Any]] = []
    for feature in functional:
        properties = _properties(feature)
        code = _zone_code(properties, zone_by_id)
        if not code:
            raise BuildError("Functional-zone feature has no resolvable official zone code")
        reference = next(
            (
                item
                for item in zone_by_id.values()
                if _text(item.get("code")) == code
            ),
            {},
        )
        zone_name = _text(reference.get("name_ru") or reference.get("name"))
        properties["ggk_zone_code"] = code
        properties["ggk_zone_name"] = zone_name
        feature["properties"] = properties
        raw_zone_counts[code] = raw_zone_counts.get(code, 0) + 1
        try:
            _geometry(feature, {"Polygon", "MultiPolygon"}, f"functional zone {code}")
        except BuildError as exc:
            if "geometry type" not in str(exc) and "contains invalid geometry" not in str(exc):
                raise
            discarded_degenerate.append(
                {
                    "feature_id": properties.get("id"),
                    "zone_code": code,
                    "reason": str(exc),
                }
            )
            continue
        _validate_near_document(feature, document_geometry, f"functional zone {code}")
        zone_counts[code] = zone_counts.get(code, 0) + 1
        if code in profile_config["allowed_codes"]:
            allowed.append(feature)
        if code in PROHIBITED_CODES:
            prohibited.append(feature)

    for feature in red_lines:
        try:
            red_line_geometry = _geometry(
                feature,
                {"LineString", "MultiLineString", "Polygon", "MultiPolygon"},
                "red line",
            )
        except BuildError as exc:
            if "geometry type" not in str(exc) and "contains invalid geometry" not in str(exc):
                raise
            discarded_red_lines.append(
                {
                    "feature_id": _properties(feature).get("id"),
                    "reason": str(exc),
                }
            )
            continue
        if red_line_geometry.geom_type in {"Polygon", "MultiPolygon"}:
            feature["geometry"] = mapping(red_line_geometry.boundary)
        _validate_near_document(feature, document_geometry, "red line")

    red_lines = [
        feature
        for feature in red_lines
        if _properties(feature).get("id")
        not in {row["feature_id"] for row in discarded_red_lines}
    ]

    max_discarded = max(25, round(len(functional) * 0.05))
    if len(discarded_degenerate) > max_discarded:
        raise BuildError(
            f"Document {document_id} has {len(discarded_degenerate)} degenerate "
            f"functional zones; safety limit is {max_discarded}"
        )
    allowed_discarded = sum(
        row["zone_code"] in profile_config["allowed_codes"]
        for row in discarded_degenerate
    )
    raw_allowed = sum(
        raw_zone_counts.get(code, 0) for code in profile_config["allowed_codes"]
    )
    max_allowed_discarded = max(2, round(raw_allowed * 0.005))
    if allowed_discarded > max_allowed_discarded:
        raise BuildError(
            f"Document {document_id} discarded {allowed_discarded} allowed-zone "
            f"features; safety limit is {max_allowed_discarded}"
        )
    if not allowed:
        expected = ", ".join(sorted(profile_config["allowed_codes"]))
        raise BuildError(
            f"Document {document_id} contains no allowed zones for {profile}: {expected}"
        )
    if not prohibited:
        raise BuildError(f"Document {document_id} contains no mapped prohibited zones")
    if not red_lines:
        raise BuildError(f"Document {document_id} contains no active red-line geometry")

    source_payloads = {
        "document.json": raw_document,
        "kato-ancestry.json": {"type": "KatoAncestry", "features": ancestry},
        "zone-catalog.json": _feature_collection(zone_rows, "AIS GGK zone catalogue"),
        "functional.raw.geojson": _feature_collection(
            raw_functional, "AIS GGK document functional zones"
        ),
        "red-line.raw.geojson": _feature_collection(
            raw_red_lines, "AIS GGK document red lines"
        ),
    }
    source_hashes: dict[str, str] = {}
    for filename, payload in source_payloads.items():
        path = source_dir / filename
        _write_json(path, payload)
        source_hashes[filename] = sha256_file(path)
    source_snapshot_sha = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    mapping_payload = {
        "version": MAPPING_VERSION,
        "profile": profile,
        "allowed_codes": sorted(profile_config["allowed_codes"]),
        "prohibited_codes": sorted(PROHIBITED_CODES),
        "polygon_red_lines_as_boundaries": True,
    }
    layers = {
        "allowed": _feature_collection(allowed, f"AIS GGK {profile} allowed zones"),
        "prohibited": _feature_collection(prohibited, "AIS GGK prohibited zones"),
        "red_line": _feature_collection(red_lines, "AIS GGK red lines"),
    }
    layer_paths: dict[str, Path] = {}
    layer_hashes: dict[str, str] = {}
    layer_counts: dict[str, int] = {}
    for kind, payload in layers.items():
        path = output / f"{kind}.geojson"
        _write_json(path, payload)
        layer_paths[kind] = path
        layer_hashes[kind] = sha256_file(path)
        layer_counts[kind] = len(payload["features"])

    doc_number = _text(document_properties.get("doc_number")) or "number unknown"
    wfs_doc_date = _text(document_properties.get("doc_date"))
    if release_mode == "search":
        review_input = _load_json(Path(review_input_path), "independent review")
        review_input = _validate_review_input(
            review_input,
            operator,
            document_properties,
        )
        legal_act = review_input["legal_act"]
        review_allow_shadow = False
    else:
        official_url = _text(shadow_source_url) or CATALOG_URL
        legal_act = {
            "number": doc_number,
            "date": wfs_doc_date,
            "url": official_url,
            "status": "unreviewed",
        }
        review_input = {
            "status": "WARNING",
            "independent_review": True,
            "reviewer": "ggk-shadow-a2",
            "reviewed_at_utc": datetime.now(UTC).isoformat(),
            "checks": {
                "document_identity_verified": True,
                "legal_act_verified": False,
                "kato_scope_verified": True,
                "zone_mapping_verified": False,
                "geometry_bounds_verified": True,
                "random_visual_samples_verified": False,
            },
            "legal_act": legal_act,
            "notes": [
                "Shadow release only. AIS GGK geometry was extracted and "
                "validated structurally, but legal/source and visual QA are "
                "required before search activation.",
            ],
        }
        review_allow_shadow = True
    mapping_payload["legal_act"] = legal_act
    mapping_sha = hashlib.sha256(
        json.dumps(mapping_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    source_sha = hashlib.sha256(
        f"{source_snapshot_sha}:{mapping_sha}".encode()
    ).hexdigest()
    release_id = (
        f"ggk-gp-{document_id}-{profile}-{source_sha[:12]}".lower().replace("_", "-")
    )
    review = {
        **review_input,
        "release_id": release_id,
        "source_sha256": source_sha,
        "reviewer_role": "A2",
        "operator": operator,
        "allow_shadow": review_allow_shadow,
        "layer_sha256": layer_hashes,
    }
    review_path = output / "review.json"
    _write_json(review_path, review)

    doc_number = _text(document_properties.get("doc_number")) or "номер не указан"
    wfs_doc_date = _text(document_properties.get("doc_date"))
    doc_date = legal_act["date"]
    approval_document = _approval_document(document_properties, legal_act)
    provenance = {
        "release_id": release_id,
        "source_sha256": source_sha,
        "review_sha256": sha256_file(review_path),
        "provenance_status": "verified_official",
        "identity_status": "matched",
        "official_url": legal_act["url"],
        "wfs_url": source.wfs_url,
        "catalog_url": CATALOG_URL,
        "document_id": document_id,
        "gp_ggk_number": document_properties.get("gp_ggk_number"),
        "kato_code_id": document_properties.get("kato_code_id"),
        "legal_act": legal_act,
        "wfs_document_date": wfs_doc_date,
        "legal_act_date_discrepancy": wfs_doc_date != doc_date,
        "source_files": source_hashes,
        "source_snapshot_sha256": source_snapshot_sha,
        "mapping": mapping_payload,
        "mapping_sha256": mapping_sha,
        "zone_counts": zone_counts,
        "raw_zone_counts": raw_zone_counts,
        "discarded_degenerate_features": discarded_degenerate,
        "discarded_red_line_features": discarded_red_lines,
        "selection": {
            "profile": profile,
            "allowed_codes": sorted(profile_config["allowed_codes"]),
            "prohibited_codes": sorted(PROHIBITED_CODES),
            "red_line_filter": (
                f"creation_doc_id={document_id} AND deactivation_doc_id IS NULL"
            ),
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
        "release_mode": release_mode,
        "source_sha256": source_sha,
        "source_version": (
            f"AIS GGK document {document_id}; {doc_number}; {doc_date or 'date unknown'}"
        ),
        "source_epsg": 4326,
        "released_by": operator,
        "purpose": profile_config["purpose"],
        "scope": scope,
        "document": {
            "title": _text(document_properties.get("doc_name"))
            or f"Генеральный план, документ {document_id}",
            "approval_document": approval_document,
            "approval_date": doc_date or None,
            "source_authority": SOURCE_AUTHORITY,
            "source_url": legal_act["url"],
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
                "zone_name": profile_config["zone_name"],
            },
            "prohibited": {
                "path": layer_paths["prohibited"].name,
                "sha256": layer_hashes["prohibited"],
                "zone_name": "Запрещающие и ограничивающие функциональные зоны",
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
        release_id=release_id,
        document_id=document_id,
        source_sha256=source_sha,
        scope=scope,
        layer_counts=layer_counts,
        layer_sha256=layer_hashes,
    )


def _validate_document(properties: dict[str, Any], document_id: int) -> None:
    if _integer(properties.get("id"), "document id") != document_id:
        raise BuildError("AIS GGK returned a different document id")
    if properties.get("status_id") != 1:
        raise BuildError(f"Document {document_id} is not active")
    if properties.get("deactivation_date") not in (None, ""):
        raise BuildError(f"Document {document_id} has a deactivation date")
    for field in ("doc_name", "doc_number", "doc_date", "kato_code_id", "kato_name_ru"):
        if properties.get(field) in (None, ""):
            raise BuildError(f"Document {document_id} is missing {field}")


def _infer_scope(
    document: dict[str, Any],
    kato_by_id: dict[int, dict[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    current_id = _integer(document.get("kato_code_id"), "document KATO id")
    ancestry: list[dict[str, Any]] = []
    seen: set[int] = set()
    while current_id not in seen:
        seen.add(current_id)
        row = kato_by_id.get(current_id)
        if row is None:
            raise BuildError(f"KATO ancestry is missing node {current_id}")
        ancestry.append(row)
        parent_id = row.get("parent_id")
        if parent_id in (None, ""):
            break
        current_id = _integer(parent_id, "KATO parent id")
    if not ancestry:
        raise BuildError("Document has no KATO ancestry")

    locality = _text(document.get("kato_name_ru")) or _text(ancestry[0].get("name_ru"))
    root_name = _text(ancestry[-1].get("name_ru"))
    if len(ancestry) == 1:
        return {"region": locality, "district": "*", "locality": "*"}, ancestry

    parent_name = _text(ancestry[1].get("name_ru"))
    parent_key = parent_name.casefold()
    if "район" in parent_key or "аудан" in parent_key:
        district = parent_name
    else:
        district = locality
    return {"region": root_name, "district": district, "locality": locality}, ancestry


def _zone_code(
    properties: dict[str, Any],
    zone_by_id: dict[int, dict[str, Any]],
) -> str:
    direct = _text(properties.get("gp_func_zone_code"))
    if direct:
        return direct
    reference_id = properties.get("gp_func_zone_code_id")
    if reference_id in (None, ""):
        return ""
    reference = zone_by_id.get(_integer(reference_id, "zone reference id"), {})
    return _text(reference.get("code"))


def _geometry(
    feature: dict[str, Any],
    allowed_types: set[str],
    label: str,
) -> BaseGeometry:
    geometry_payload = feature.get("geometry")
    if not isinstance(geometry_payload, dict):
        raise BuildError(f"{label} is missing geometry")
    try:
        geometry = make_valid(shape(geometry_payload))
    except Exception as exc:
        raise BuildError(f"{label} contains invalid geometry") from exc
    if geometry.geom_type == "GeometryCollection":
        compatible = _compatible_parts(geometry, allowed_types)
        if compatible:
            polygonal = [
                part
                for part in compatible
                if part.geom_type in {"Polygon", "MultiPolygon"}
            ]
            geometry = make_valid(unary_union(polygonal or compatible))
    if geometry.is_empty or geometry.geom_type not in allowed_types:
        raise BuildError(
            f"{label} has geometry type {geometry.geom_type!r}; "
            f"expected one of {sorted(allowed_types)}"
        )
    min_x, min_y, max_x, max_y = geometry.bounds
    kz_min_x, kz_min_y, kz_max_x, kz_max_y = KAZAKHSTAN_BOUNDS
    if min_x < kz_min_x or min_y < kz_min_y or max_x > kz_max_x or max_y > kz_max_y:
        raise BuildError(f"{label} lies outside Kazakhstan safety bounds: {geometry.bounds}")
    feature["geometry"] = mapping(geometry)
    return geometry


def _compatible_parts(
    geometry: BaseGeometry,
    allowed_types: set[str],
) -> list[BaseGeometry]:
    if geometry.is_empty:
        return []
    if geometry.geom_type in allowed_types:
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [
            part
            for child in geometry.geoms
            for part in _compatible_parts(child, allowed_types)
        ]
    return []


def _validate_near_document(
    feature: dict[str, Any],
    document_geometry: BaseGeometry,
    label: str,
) -> None:
    geometry = shape(feature["geometry"])
    min_x, min_y, max_x, max_y = document_geometry.bounds
    margin = 0.5
    if (
        geometry.bounds[2] < min_x - margin
        or geometry.bounds[0] > max_x + margin
        or geometry.bounds[3] < min_y - margin
        or geometry.bounds[1] > max_y + margin
    ):
        raise BuildError(f"{label} does not overlap the document safety extent")


def _validate_review_input(
    review: dict[str, Any],
    operator: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    if review.get("status") not in {"STRICT", "VERIFIED_STRICT"}:
        raise BuildError("Independent review status must be STRICT or VERIFIED_STRICT")
    if review.get("independent_review") is not True:
        raise BuildError("Independent review must explicitly set independent_review=true")
    reviewer = _text(review.get("reviewer"))
    if not reviewer:
        raise BuildError("Independent review must name the reviewer")
    if reviewer.casefold() == operator.casefold():
        raise BuildError("Independent reviewer must differ from the vector operator")
    reviewed_at = _text(review.get("reviewed_at_utc"))
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BuildError("reviewed_at_utc must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BuildError("reviewed_at_utc must include a timezone")
    if parsed > datetime.now(UTC):
        raise BuildError("reviewed_at_utc cannot be in the future")
    checks = review.get("checks")
    if not isinstance(checks, dict):
        raise BuildError("Independent review must include a checks object")
    missing = sorted(key for key in REQUIRED_REVIEW_CHECKS if checks.get(key) is not True)
    if missing:
        raise BuildError("Independent review checks are incomplete: " + ", ".join(missing))
    legal_act = review.get("legal_act")
    if not isinstance(legal_act, dict):
        raise BuildError("Independent review must include a legal_act object")
    legal_number = _text(legal_act.get("number"))
    legal_date = _text(legal_act.get("date"))
    legal_url = _text(legal_act.get("url"))
    legal_status = _text(legal_act.get("status")).lower()
    if not legal_number or not legal_date or not legal_url:
        raise BuildError("legal_act must include number, date and url")
    base_legal_act = _validate_base_legal_act(legal_act)
    if not _legal_act_matches_wfs_document(legal_number, base_legal_act, document):
        raise BuildError("legal_act number does not match the AIS GGK document number")
    try:
        datetime.fromisoformat(legal_date)
    except ValueError as exc:
        raise BuildError("legal_act date must use YYYY-MM-DD") from exc
    parsed_url = urlsplit(legal_url)
    allowed_hosts = {"adilet.zan.kz", "www.adilet.zan.kz", "zan.gov.kz", "www.gov.kz"}
    if parsed_url.scheme != "https" or parsed_url.hostname not in allowed_hosts:
        raise BuildError("legal_act url must point to an official Adilet, Zan or gov.kz page")
    if legal_status != "active":
        raise BuildError("legal_act status must be active for a strict release")
    review["legal_act"] = {
        "number": legal_number,
        "date": legal_date,
        "url": legal_url,
        "status": legal_status,
    }
    if base_legal_act is not None:
        review["legal_act"]["base_legal_act"] = base_legal_act
    return review


def _legal_act_matches_wfs_document(
    legal_number: str,
    base_legal_act: dict[str, str] | None,
    document: dict[str, Any],
) -> bool:
    wfs_number_digits = "".join(re.findall(r"\d+", _text(document.get("doc_number"))))
    legal_number_digits = "".join(re.findall(r"\d+", legal_number))
    if wfs_number_digits and wfs_number_digits == legal_number_digits:
        return True
    if base_legal_act is None:
        return False
    base_number_digits = "".join(re.findall(r"\d+", base_legal_act["number"]))
    return bool(wfs_number_digits and wfs_number_digits == base_number_digits)


def _validate_base_legal_act(legal_act: dict[str, Any]) -> dict[str, str] | None:
    base = legal_act.get("base_legal_act")
    if base is None:
        return None
    if not isinstance(base, dict):
        raise BuildError("base_legal_act must be an object")
    base_number = _text(base.get("number"))
    base_date = _text(base.get("date"))
    base_url = _text(base.get("url"))
    if not base_number or not base_date or not base_url:
        raise BuildError("base_legal_act must include number, date and url")
    try:
        datetime.fromisoformat(base_date)
    except ValueError as exc:
        raise BuildError("base_legal_act date must use YYYY-MM-DD") from exc
    parsed_url = urlsplit(base_url)
    allowed_hosts = {"adilet.zan.kz", "www.adilet.zan.kz", "zan.gov.kz", "www.gov.kz"}
    if parsed_url.scheme != "https" or parsed_url.hostname not in allowed_hosts:
        raise BuildError("base_legal_act url must point to an official Adilet, Zan or gov.kz page")
    return {
        "number": base_number,
        "date": base_date,
        "url": base_url,
        "status": _text(base.get("status")).lower() or "active",
    }


def _approval_document(document: dict[str, Any], legal_act: dict[str, Any]) -> str:
    authority = _text(document.get("approved_by"))
    legal_number = _text(legal_act.get("number"))
    base = legal_act.get("base_legal_act")
    if isinstance(base, dict):
        base_number = _text(base.get("number"))
        if legal_number and base_number and legal_number != base_number:
            suffix = f"{legal_number} (изменяет {base_number})"
        else:
            suffix = legal_number or base_number
    else:
        suffix = legal_number or _text(document.get("doc_number"))
    return " ".join(part for part in (authority, suffix) if part)


def _feature_collection(
    features: list[dict[str, Any]],
    name: str,
) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "name": name,
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": features,
    }


def _properties(feature: dict[str, Any]) -> dict[str, Any]:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise BuildError("AIS GGK feature is missing properties")
    return dict(properties)


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise BuildError(f"{label} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BuildError(f"{label} must be an integer") from exc


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"Cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BuildError(f"{label} root must be an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
