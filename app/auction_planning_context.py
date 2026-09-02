from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pyproj import CRS, Transformer
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from app.auction_parcel_geometry import (
    GeometryLimits,
    GeometryValidationError,
    validate_parcel_geojson,
)

PlanningStatus = Literal["unknown", "partial", "clear", "conflict", "error"]
CURRENT_KINDS = {"current_zone", "current_road"}
FUTURE_KINDS = {
    "future_zone",
    "planned_road",
    "red_line",
    "engineering_corridor",
    "szz",
}
FEATURE_KINDS = CURRENT_KINDS | FUTURE_KINDS
ADVERSE_KINDS = {"planned_road", "red_line", "engineering_corridor", "szz"}
KIND_LAYER_CONTRACT = {
    "current_zone": ("genplan", "current_zoning"),
    "current_road": ("genplan", "current_roads"),
    "future_zone": ("pdp", "future_zoning"),
    "planned_road": ("pdp", "planned_roads"),
    "red_line": ("pdp", "red_lines"),
    "engineering_corridor": ("pdp", "engineering_corridors"),
    "szz": ("pdp", "szz"),
}
MIN_INTERSECTION_AREA_M2 = 0.01
MIN_INTERSECTION_LENGTH_M = 0.01
REQUIRED_COVERAGE = (
    ("genplan", "current_zoning"),
    ("pdp", "future_zoning"),
    ("pdp", "planned_roads"),
    ("pdp", "red_lines"),
    ("pdp", "engineering_corridors"),
    ("pdp", "szz"),
)


@dataclass(frozen=True, slots=True)
class PlanningLimits:
    max_sources: int = 30
    max_features: int = 500
    max_vertices: int = 30_000
    max_text_length: int = 240


@dataclass(frozen=True, slots=True)
class CoverageCheck:
    document_type: str
    layer: str
    complete: bool
    source_ids: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    provenances: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningRelation:
    kind: str
    phase: Literal["current", "future"]
    value: str | None
    source_id: str
    source_version: str
    authoritative: bool
    intersects: bool
    touches_only: bool
    distance_m: float
    intersection_area_m2: float | None
    parcel_percent: float | None
    intersection_length_m: float | None
    allowed_use: bool | None
    provenance: str


@dataclass(frozen=True, slots=True)
class PlanningConflict:
    code: str
    message: str
    source_ids: tuple[str, ...]
    versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanningContextAnalysis:
    status: PlanningStatus
    coverage: tuple[CoverageCheck, ...] = ()
    current_relations: tuple[PlanningRelation, ...] = ()
    future_relations: tuple[PlanningRelation, ...] = ()
    conflicts: tuple[PlanningConflict, ...] = ()
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class PlanningValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, limits: PlanningLimits) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningValidationError("invalid_string", f"{field} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > limits.max_text_length:
        raise PlanningValidationError("string_too_long", f"{field} exceeds text limit")
    return cleaned


def _timestamp(value: object, limits: PlanningLimits) -> str:
    observed_at = _text(value, "observed_at", limits)
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanningValidationError("invalid_timestamp", "observed_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlanningValidationError(
            "timezone_required",
            "observed_at must include a UTC offset",
        )
    return observed_at


def _position(value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) < 2:
        raise PlanningValidationError("invalid_geometry", "Line position must contain lon and lat")
    lon, lat = value[0], value[1]
    if (
        isinstance(lon, bool)
        or isinstance(lat, bool)
        or not isinstance(lon, (int, float))
        or not isinstance(lat, (int, float))
    ):
        raise PlanningValidationError("invalid_geometry", "Coordinates must be numeric")
    lon_float, lat_float = float(lon), float(lat)
    if not math.isfinite(lon_float) or not math.isfinite(lat_float):
        raise PlanningValidationError("invalid_geometry", "Coordinates must be finite")
    if not 46 <= lon_float <= 88 or not 40 <= lat_float <= 56.5:
        raise PlanningValidationError("outside_kazakhstan", "Geometry is outside Kazakhstan")
    return lon_float, lat_float


def _line_geometry(value: object, counter: list[int], limits: PlanningLimits) -> BaseGeometry:
    if not isinstance(value, dict):
        raise PlanningValidationError("invalid_geometry", "Feature geometry must be GeoJSON")
    geometry_type = value.get("type")
    coordinates = value.get("coordinates")

    def line(items: object) -> LineString:
        if not isinstance(items, list) or len(items) < 2:
            raise PlanningValidationError("invalid_geometry", "Line needs at least two positions")
        points = [_position(item) for item in items]
        counter[0] += len(points)
        if counter[0] > limits.max_vertices:
            raise PlanningValidationError("too_many_vertices", "Planning geometry exceeds limit")
        return LineString(points)

    if geometry_type == "LineString":
        return line(coordinates)
    if geometry_type == "MultiLineString":
        if not isinstance(coordinates, list) or not coordinates:
            raise PlanningValidationError("invalid_geometry", "MultiLineString is empty")
        return MultiLineString([line(items) for items in coordinates])
    raise PlanningValidationError("unsupported_geometry", "Expected line planning geometry")


def _feature_geometry(
    value: object,
    counter: list[int],
    limits: PlanningLimits,
) -> BaseGeometry:
    if isinstance(value, dict) and value.get("type") in {"LineString", "MultiLineString"}:
        return _line_geometry(value, counter, limits)
    before = counter[0]
    try:
        geometry = validate_parcel_geojson(
            value,
            limits=GeometryLimits(
                max_features=limits.max_features,
                max_vertices=limits.max_vertices - before,
                max_polygons=limits.max_features,
            ),
        )
    except GeometryValidationError as exc:
        raise PlanningValidationError(exc.code, str(exc)) from exc
    counter[0] += _polygon_vertex_count(value)
    if counter[0] > limits.max_vertices:
        raise PlanningValidationError("too_many_vertices", "Planning geometry exceeds limit")
    return geometry


def _polygon_vertex_count(value: object, depth: int = 0) -> int:
    if depth > 8 or not isinstance(value, dict):
        raise PlanningValidationError("invalid_geometry", "Invalid polygon nesting")
    coordinates = value.get("coordinates")

    def count(items: object, nested: int) -> int:
        if nested > 8 or not isinstance(items, list):
            raise PlanningValidationError("invalid_geometry", "Invalid polygon coordinates")
        if items and isinstance(items[0], (int, float)) and not isinstance(items[0], bool):
            return 1
        return sum(count(item, nested + 1) for item in items)

    return count(coordinates, 0)


def _transformer(parcel: Polygon | MultiPolygon) -> Transformer:
    centroid = parcel.centroid
    zone = max(1, min(60, int((centroid.x + 180) // 6) + 1))
    return Transformer.from_crs("EPSG:4326", CRS.from_epsg(32600 + zone), always_xy=True)


def analyze_planning_context(
    parcel_geojson: object | None,
    *,
    planning_sources: object | None,
    planning_features: object | None,
    limits: PlanningLimits = PlanningLimits(),
) -> PlanningContextAnalysis:
    """Analyze already-stored planning layers; performs no I/O or online fanout."""
    if parcel_geojson is None or planning_sources is None:
        return PlanningContextAnalysis(
            status="unknown",
            error_code="planning_input_missing",
            error_message="Parcel geometry or planning source checklist is missing",
        )
    try:
        try:
            parcel = validate_parcel_geojson(
                parcel_geojson,
                limits=GeometryLimits(max_vertices=limits.max_vertices),
            )
        except GeometryValidationError as exc:
            raise PlanningValidationError(exc.code, str(exc)) from exc
        if not isinstance(planning_sources, list) or len(planning_sources) > limits.max_sources:
            raise PlanningValidationError(
                "invalid_sources", "Planning sources are invalid or oversized"
            )
        if not planning_sources:
            return PlanningContextAnalysis(status="unknown", warnings=("No planning sources",))
        if not isinstance(planning_features, list) or len(planning_features) > limits.max_features:
            raise PlanningValidationError(
                "invalid_features", "Planning features are invalid or oversized"
            )

        sources: dict[str, dict[str, object]] = {}
        for raw in planning_sources:
            if not isinstance(raw, dict):
                raise PlanningValidationError("invalid_source", "Planning source must be an object")
            source_id = _text(raw.get("id"), "source.id", limits)
            if source_id in sources:
                raise PlanningValidationError(
                    "duplicate_source", "Planning source IDs must be unique"
                )
            document_type = _text(raw.get("document_type"), "document_type", limits).casefold()
            if document_type not in {"genplan", "pdp"}:
                raise PlanningValidationError("invalid_document_type", "Expected genplan or pdp")
            version = _text(raw.get("version"), "version", limits)
            provenance = _text(raw.get("provenance"), "provenance", limits)
            observed_at = _timestamp(raw.get("observed_at"), limits)
            authoritative = raw.get("authoritative")
            if not isinstance(authoritative, bool):
                raise PlanningValidationError("invalid_authority", "authoritative must be boolean")
            coverage = raw.get("coverage")
            if not isinstance(coverage, dict):
                raise PlanningValidationError(
                    "coverage_missing", "Source coverage checklist is missing"
                )
            normalized_coverage = {}
            for layer, complete in coverage.items():
                layer_name = _text(layer, "coverage.layer", limits)
                if not isinstance(complete, bool):
                    raise PlanningValidationError(
                        "invalid_coverage", "Coverage flags must be boolean"
                    )
                normalized_coverage[layer_name] = complete
            not_applicable_raw = raw.get("not_applicable", [])
            if not isinstance(not_applicable_raw, list):
                raise PlanningValidationError(
                    "invalid_not_applicable",
                    "not_applicable must be a layer-name list",
                )
            not_applicable = {
                _text(layer, "not_applicable.layer", limits) for layer in not_applicable_raw
            }
            sources[source_id] = {
                "document_type": document_type,
                "version": version,
                "provenance": f"{provenance} @ {observed_at}",
                "authoritative": authoritative,
                "coverage": normalized_coverage,
                "not_applicable": not_applicable,
            }

        coverage_results = []
        for document_type, layer in REQUIRED_COVERAGE:
            matches = [
                (source_id, source)
                for source_id, source in sources.items()
                if source["document_type"] == document_type
                and source["authoritative"] is True
                and source["coverage"].get(layer) is True  # type: ignore[union-attr]
            ]
            coverage_results.append(
                CoverageCheck(
                    document_type=document_type,
                    layer=layer,
                    complete=bool(matches),
                    source_ids=tuple(item[0] for item in matches),
                    versions=tuple(str(item[1]["version"]) for item in matches),
                    provenances=tuple(str(item[1]["provenance"]) for item in matches),
                )
            )

        transformer = _transformer(parcel)
        parcel_metric = transform(transformer.transform, parcel)
        vertex_counter = [0]
        current_relations = []
        future_relations = []
        warnings = []
        conflicts = []
        intersecting_values: dict[str, list[tuple[str, str, str]]] = {}
        for raw in planning_features:
            if not isinstance(raw, dict):
                raise PlanningValidationError(
                    "invalid_feature", "Planning feature must be an object"
                )
            kind = _text(raw.get("kind"), "feature.kind", limits).casefold()
            if kind not in FEATURE_KINDS:
                raise PlanningValidationError(
                    "unsupported_kind", "Unsupported planning feature kind"
                )
            source_id = _text(raw.get("source_id"), "feature.source_id", limits)
            source = sources.get(source_id)
            if source is None:
                raise PlanningValidationError(
                    "unknown_source", "Feature references an unknown source"
                )
            required_document, required_layer = KIND_LAYER_CONTRACT[kind]
            if (
                source["document_type"] != required_document
                or required_layer not in source["coverage"]  # type: ignore[operator]
            ):
                raise PlanningValidationError(
                    "feature_source_layer_mismatch",
                    f"{kind} must reference {required_document}:{required_layer}",
                )
            geometry = _feature_geometry(raw.get("geometry"), vertex_counter, limits)
            geometry_metric = transform(transformer.transform, geometry)
            intersection = parcel_metric.intersection(geometry_metric)
            topological_intersection = not intersection.is_empty
            distance = float(parcel_metric.distance(geometry_metric))
            value = raw.get("value")
            normalized_value = _text(value, "feature.value", limits) if value is not None else None
            allowed_use = raw.get("allowed_use")
            if allowed_use is not None and not isinstance(allowed_use, bool):
                raise PlanningValidationError("invalid_allowed_use", "allowed_use must be boolean")
            is_polygon = isinstance(geometry_metric, (Polygon, MultiPolygon))
            area = float(intersection.area) if topological_intersection and is_polygon else None
            percent = area / float(parcel_metric.area) * 100 if area is not None else None
            length = (
                float(intersection.length) if topological_intersection and not is_polygon else None
            )
            intersects = (
                area is not None and area > MIN_INTERSECTION_AREA_M2
                if is_polygon
                else length is not None and length > MIN_INTERSECTION_LENGTH_M
            )
            touches_only = topological_intersection and not intersects
            phase: Literal["current", "future"] = "current" if kind in CURRENT_KINDS else "future"
            relation = PlanningRelation(
                kind=kind,
                phase=phase,
                value=normalized_value,
                source_id=source_id,
                source_version=str(source["version"]),
                authoritative=bool(source["authoritative"]),
                intersects=intersects,
                touches_only=touches_only,
                distance_m=distance,
                intersection_area_m2=area,
                parcel_percent=percent,
                intersection_length_m=length,
                allowed_use=allowed_use,
                provenance=str(source["provenance"]),
            )
            (current_relations if phase == "current" else future_relations).append(relation)
            if intersects and normalized_value and source["authoritative"] is True:
                intersecting_values.setdefault(kind, []).append(
                    (normalized_value, source_id, str(source["version"]))
                )
            if intersects and allowed_use is False and source["authoritative"] is True:
                conflicts.append(
                    PlanningConflict(
                        code="current_use_conflict"
                        if phase == "current"
                        else "future_use_conflict",
                        message=f"{kind} does not allow the proposed use",
                        source_ids=(source_id,),
                        versions=(str(source["version"]),),
                    )
                )
            if intersects and kind in ADVERSE_KINDS:
                if source["authoritative"] is True:
                    conflicts.append(
                        PlanningConflict(
                            code=f"{kind}_intersection",
                            message=f"Parcel intersects authoritative {kind}",
                            source_ids=(source_id,),
                            versions=(str(source["version"]),),
                        )
                    )
                else:
                    warnings.append(f"Parcel intersects non-authoritative {kind} from {source_id}")

        for kind, values in sorted(intersecting_values.items()):
            distinct = {item[0].casefold() for item in values}
            if len(distinct) > 1:
                conflicts.append(
                    PlanningConflict(
                        code="source_version_conflict",
                        message=f"Authoritative sources disagree on intersecting {kind}",
                        source_ids=tuple(item[1] for item in values),
                        versions=tuple(item[2] for item in values),
                    )
                )

        missing = [
            f"{item.document_type}:{item.layer}" for item in coverage_results if not item.complete
        ]
        authoritative_current_zone = any(
            relation.kind == "current_zone" and relation.authoritative and relation.intersects
            for relation in current_relations
        )
        if not authoritative_current_zone:
            missing.append("genplan:current_zone_relation")
        future_coverage_complete = any(
            item.document_type == "pdp" and item.layer == "future_zoning" and item.complete
            for item in coverage_results
        )
        authoritative_future_zone = any(
            relation.kind == "future_zone" and relation.authoritative and relation.intersects
            for relation in future_relations
        )
        future_not_applicable = any(
            source["document_type"] == "pdp"
            and source["authoritative"] is True
            and source["coverage"].get("future_zoning") is True  # type: ignore[union-attr]
            and "future_zoning" in source["not_applicable"]  # type: ignore[operator]
            for source in sources.values()
        )
        if future_coverage_complete and not authoritative_future_zone and not future_not_applicable:
            missing.append("pdp:future_zone_relation_or_not_applicable")
        if missing:
            warnings.append("Missing authoritative planning coverage: " + ", ".join(missing))
        status: PlanningStatus = (
            "conflict" if conflicts else "partial" if missing or warnings else "clear"
        )
        return PlanningContextAnalysis(
            status=status,
            coverage=tuple(coverage_results),
            current_relations=tuple(current_relations),
            future_relations=tuple(future_relations),
            conflicts=tuple(conflicts),
            warnings=tuple(warnings),
        )
    except PlanningValidationError as exc:
        return PlanningContextAnalysis(
            status="error",
            error_code=exc.code,
            error_message=str(exc),
        )
