from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pyproj import CRS, Transformer
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from app.auction_parcel_geometry import (
    GeometryLimits,
    GeometryValidationError,
    validate_parcel_geojson,
)

RestrictionStatus = Literal["unknown", "partial", "clear", "restricted", "conflict", "error"]
LayerStatus = Literal["unknown", "partial", "clear", "restricted", "touch", "error"]
REQUIRED_RESTRICTION_LAYERS = (
    "red_lines",
    "szz",
    "power_protection",
    "water_protection",
    "flood",
    "engineering_corridors",
    "servitudes",
    "cadastral_restrictions",
)
DEFAULT_BLOCKER_LAYERS = {
    "red_lines",
    "szz",
    "power_protection",
    "water_protection",
    "flood",
    "engineering_corridors",
}
MIN_AREA_M2 = 0.01
MIN_LENGTH_M = 0.01


@dataclass(frozen=True, slots=True)
class RestrictionLimits:
    max_sources: int = 40
    max_features: int = 1_000
    max_vertices: int = 50_000
    max_text_length: int = 240


@dataclass(frozen=True, slots=True)
class RestrictionFact:
    layer: str
    restriction_id: str | None
    source_id: str
    version: str
    value: str | None
    impact: Literal["blocker", "warning"]
    claimed_reduces_usable_area: bool
    reduces_usable_area: bool
    geometry_mode: Literal["area", "line_fact"]
    intersects: bool
    touches_only: bool
    distance_m: float
    intersection_area_m2: float | None
    parcel_percent: float | None
    intersection_length_m: float | None
    provenance: str


@dataclass(frozen=True, slots=True)
class RestrictionLayerResult:
    layer: str
    status: LayerStatus
    coverage_complete: bool
    source_ids: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    observed_area_m2: float | None = None
    parcel_percent: float | None = None
    intersection_length_m: float | None = None
    touches: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RestrictionConflict:
    layer: str
    code: str
    message: str
    source_ids: tuple[str, ...]
    versions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestrictionContextAnalysis:
    status: RestrictionStatus
    parcel_area_m2: float | None = None
    layers: tuple[RestrictionLayerResult, ...] = ()
    facts: tuple[RestrictionFact, ...] = ()
    conflicts: tuple[RestrictionConflict, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    observed_restricted_area_m2: float | None = None
    restricted_area_m2: float | None = None
    usable_area_m2: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class RestrictionValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, limits: RestrictionLimits) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RestrictionValidationError("invalid_string", f"{field} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > limits.max_text_length:
        raise RestrictionValidationError("string_too_long", f"{field} exceeds text limit")
    return cleaned


def _timestamp(value: object, limits: RestrictionLimits) -> str:
    observed_at = _text(value, "observed_at", limits)
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RestrictionValidationError(
            "invalid_timestamp", "observed_at must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RestrictionValidationError("timezone_required", "observed_at needs a UTC offset")
    return observed_at


def _position(value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) < 2:
        raise RestrictionValidationError("invalid_geometry", "Position must contain lon and lat")
    lon, lat = value[0], value[1]
    if (
        isinstance(lon, bool)
        or isinstance(lat, bool)
        or not isinstance(lon, (int, float))
        or not isinstance(lat, (int, float))
    ):
        raise RestrictionValidationError("invalid_geometry", "Coordinates must be numeric")
    lon_float, lat_float = float(lon), float(lat)
    if not math.isfinite(lon_float) or not math.isfinite(lat_float):
        raise RestrictionValidationError("invalid_geometry", "Coordinates must be finite")
    if not 46 <= lon_float <= 88 or not 40 <= lat_float <= 56.5:
        raise RestrictionValidationError("outside_kazakhstan", "Geometry is outside Kazakhstan")
    return lon_float, lat_float


def _line_geometry(value: object, counter: list[int], limits: RestrictionLimits) -> BaseGeometry:
    if not isinstance(value, dict):
        raise RestrictionValidationError("invalid_geometry", "Line fact must be GeoJSON")
    geometry_type = value.get("type")
    coordinates = value.get("coordinates")

    def line(items: object) -> LineString:
        if not isinstance(items, list) or len(items) < 2:
            raise RestrictionValidationError("invalid_geometry", "Line needs two positions")
        points = [_position(item) for item in items]
        counter[0] += len(points)
        if counter[0] > limits.max_vertices:
            raise RestrictionValidationError(
                "too_many_vertices", "Restriction vertex limit exceeded"
            )
        return LineString(points)

    if geometry_type == "LineString":
        return line(coordinates)
    if geometry_type == "MultiLineString":
        if not isinstance(coordinates, list) or not coordinates:
            raise RestrictionValidationError("invalid_geometry", "MultiLineString is empty")
        return MultiLineString([line(items) for items in coordinates])
    raise RestrictionValidationError("geometry_mode_mismatch", "line_fact requires line geometry")


def _raw_vertex_count(value: object, depth: int = 0) -> int:
    if depth > 8 or not isinstance(value, list):
        raise RestrictionValidationError("invalid_geometry", "Invalid coordinate nesting")
    if value and isinstance(value[0], (int, float)) and not isinstance(value[0], bool):
        return 1
    return sum(_raw_vertex_count(item, depth + 1) for item in value)


def _area_geometry(value: object, counter: list[int], limits: RestrictionLimits) -> BaseGeometry:
    try:
        geometry = validate_parcel_geojson(
            value,
            limits=GeometryLimits(max_vertices=limits.max_vertices - counter[0]),
        )
    except GeometryValidationError as exc:
        raise RestrictionValidationError(exc.code, str(exc)) from exc
    if not isinstance(value, dict):
        raise RestrictionValidationError("invalid_geometry", "Area restriction must be GeoJSON")
    counter[0] += _raw_vertex_count(value.get("coordinates"))
    if counter[0] > limits.max_vertices:
        raise RestrictionValidationError("too_many_vertices", "Restriction vertex limit exceeded")
    return geometry


def _transformer(parcel: Polygon | MultiPolygon) -> Transformer:
    centroid = parcel.centroid
    zone = max(1, min(60, int((centroid.x + 180) // 6) + 1))
    return Transformer.from_crs("EPSG:4326", CRS.from_epsg(32600 + zone), always_xy=True)


def analyze_restriction_context(
    parcel_geojson: object | None,
    *,
    restriction_sources: object | None,
    restriction_features: object | None,
    expected_layers: tuple[str, ...] = REQUIRED_RESTRICTION_LAYERS,
    limits: RestrictionLimits = RestrictionLimits(),
) -> RestrictionContextAnalysis:
    """Combine authoritative precomputed restriction facts without I/O.

    Non-authoritative observations are retained as warnings/facts but cannot
    reduce usable area or degrade an otherwise complete authoritative result.
    Version conflicts require the same stable ``restriction_id``; different
    objects in one layer are never treated as competing versions.
    """
    if parcel_geojson is None or restriction_sources is None:
        return RestrictionContextAnalysis(
            status="unknown",
            error_code="restriction_input_missing",
            error_message="Parcel geometry or restriction coverage checklist is missing",
        )
    try:
        if not expected_layers:
            raise RestrictionValidationError(
                "expected_layers_missing",
                "At least one expected restriction layer is required",
            )
        if len(expected_layers) > len(REQUIRED_RESTRICTION_LAYERS) or any(
            layer not in REQUIRED_RESTRICTION_LAYERS for layer in expected_layers
        ):
            raise RestrictionValidationError("invalid_expected_layers", "Unknown expected layer")
        if len(set(expected_layers)) != len(expected_layers):
            raise RestrictionValidationError("duplicate_expected_layer", "Expected layers repeat")
        try:
            parcel = validate_parcel_geojson(
                parcel_geojson,
                limits=GeometryLimits(max_vertices=limits.max_vertices),
            )
        except GeometryValidationError as exc:
            raise RestrictionValidationError(exc.code, str(exc)) from exc
        if (
            not isinstance(restriction_sources, list)
            or len(restriction_sources) > limits.max_sources
        ):
            raise RestrictionValidationError(
                "invalid_sources", "Restriction sources invalid or oversized"
            )
        if not restriction_sources:
            return RestrictionContextAnalysis(status="unknown")
        if (
            not isinstance(restriction_features, list)
            or len(restriction_features) > limits.max_features
        ):
            raise RestrictionValidationError(
                "invalid_features", "Restriction features invalid or oversized"
            )

        sources: dict[str, dict[str, object]] = {}
        for raw in restriction_sources:
            if not isinstance(raw, dict):
                raise RestrictionValidationError(
                    "invalid_source", "Restriction source must be an object"
                )
            source_id = _text(raw.get("id"), "source.id", limits)
            if source_id in sources:
                raise RestrictionValidationError(
                    "duplicate_source", "Restriction source IDs repeat"
                )
            version = _text(raw.get("version"), "version", limits)
            provenance = _text(raw.get("provenance"), "provenance", limits)
            observed_at = _timestamp(raw.get("observed_at"), limits)
            authoritative = raw.get("authoritative")
            if not isinstance(authoritative, bool):
                raise RestrictionValidationError(
                    "invalid_authority", "authoritative must be boolean"
                )
            coverage = raw.get("coverage")
            if not isinstance(coverage, dict):
                raise RestrictionValidationError(
                    "coverage_missing", "Coverage checklist is missing"
                )
            normalized_coverage = {}
            for layer, complete in coverage.items():
                layer_name = _text(layer, "coverage.layer", limits)
                if layer_name not in REQUIRED_RESTRICTION_LAYERS or not isinstance(complete, bool):
                    raise RestrictionValidationError(
                        "invalid_coverage", "Coverage layer or flag invalid"
                    )
                normalized_coverage[layer_name] = complete
            sources[source_id] = {
                "version": version,
                "provenance": f"{provenance} @ {observed_at}",
                "authoritative": authoritative,
                "coverage": normalized_coverage,
            }

        transformer = _transformer(parcel)
        parcel_metric = transform(transformer.transform, parcel)
        parcel_area = float(parcel_metric.area)
        counter = [0]
        facts = []
        blockers = []
        warnings = []
        conflicts = []
        layer_areas: dict[str, list[BaseGeometry]] = {layer: [] for layer in expected_layers}
        layer_unusable_areas: dict[str, list[BaseGeometry]] = {
            layer: [] for layer in expected_layers
        }
        layer_lengths: dict[str, float] = {layer: 0.0 for layer in expected_layers}
        layer_touches: dict[str, int] = {layer: 0 for layer in expected_layers}
        layer_errors: dict[str, list[str]] = {layer: [] for layer in expected_layers}
        layer_values: dict[str, dict[str, list[tuple[str, str, str]]]] = {
            layer: {} for layer in expected_layers
        }
        positive_line_fact = False
        positive_authoritative_warning = False

        for raw in restriction_features:
            if not isinstance(raw, dict):
                raise RestrictionValidationError(
                    "invalid_feature", "Restriction feature must be an object"
                )
            layer = _text(raw.get("layer"), "feature.layer", limits)
            if layer not in expected_layers:
                raise RestrictionValidationError(
                    "unexpected_layer", "Feature layer is not expected"
                )
            source_id = _text(raw.get("source_id"), "feature.source_id", limits)
            source = sources.get(source_id)
            if source is None:
                raise RestrictionValidationError("unknown_source", "Feature source is unknown")
            if layer not in source["coverage"]:  # type: ignore[operator]
                layer_errors[layer].append(f"{source_id}: source does not own layer coverage")
                continue
            mode = raw.get("geometry_mode", "area")
            if mode not in {"area", "line_fact"}:
                layer_errors[layer].append(f"{source_id}: invalid geometry_mode")
                continue
            impact = raw.get("impact") or (
                "blocker" if layer in DEFAULT_BLOCKER_LAYERS else "warning"
            )
            if impact not in {"blocker", "warning"}:
                layer_errors[layer].append(f"{source_id}: invalid impact")
                continue
            reduces_usable_area = raw.get("reduces_usable_area")
            if reduces_usable_area is None:
                reduces_usable_area = impact == "blocker"
            if not isinstance(reduces_usable_area, bool):
                layer_errors[layer].append(f"{source_id}: invalid reduces_usable_area")
                continue
            value = raw.get("value")
            restriction_id_raw = raw.get("restriction_id")
            try:
                normalized_value = (
                    _text(value, "feature.value", limits) if value is not None else None
                )
                restriction_id = (
                    _text(restriction_id_raw, "feature.restriction_id", limits)
                    if restriction_id_raw is not None
                    else None
                )
                geometry = (
                    _area_geometry(raw.get("geometry"), counter, limits)
                    if mode == "area"
                    else _line_geometry(raw.get("geometry"), counter, limits)
                )
                geometry_metric = transform(transformer.transform, geometry)
                intersection = parcel_metric.intersection(geometry_metric)
                topological = not intersection.is_empty
                area = float(intersection.area) if mode == "area" and topological else None
                length = float(intersection.length) if mode == "line_fact" and topological else None
                positive = (
                    area is not None and area > MIN_AREA_M2
                    if mode == "area"
                    else length is not None and length > MIN_LENGTH_M
                )
                touches = topological and not positive
                if positive and mode == "area" and source["authoritative"] is True:
                    layer_areas[layer].append(intersection)
                    if reduces_usable_area:
                        layer_unusable_areas[layer].append(intersection)
                if positive and mode == "line_fact" and source["authoritative"] is True:
                    layer_lengths[layer] += length or 0.0
                    positive_line_fact = True
                if touches:
                    if source["authoritative"] is True:
                        layer_touches[layer] += 1
                        warnings.append(
                            f"{layer}: boundary touch from {source_id}, not positive overlap"
                        )
                    else:
                        warnings.append(
                            f"Non-authoritative {layer} boundary touch from {source_id}"
                        )
                fact = RestrictionFact(
                    layer=layer,
                    restriction_id=restriction_id,
                    source_id=source_id,
                    version=str(source["version"]),
                    value=normalized_value,
                    impact=impact,
                    claimed_reduces_usable_area=reduces_usable_area,
                    reduces_usable_area=(
                        reduces_usable_area and source["authoritative"] is True
                    ),
                    geometry_mode=mode,
                    intersects=positive,
                    touches_only=touches,
                    distance_m=float(parcel_metric.distance(geometry_metric)),
                    intersection_area_m2=area,
                    parcel_percent=area / parcel_area * 100 if area is not None else None,
                    intersection_length_m=length,
                    provenance=str(source["provenance"]),
                )
                facts.append(fact)
                if positive:
                    message = f"{layer}: positive {mode} intersection from {source_id}"
                    if source["authoritative"] is True:
                        (blockers if impact == "blocker" else warnings).append(message)
                        positive_authoritative_warning |= impact == "warning"
                    else:
                        warnings.append("Non-authoritative observation: " + message)
                    if restriction_id and normalized_value and source["authoritative"] is True:
                        layer_values[layer].setdefault(restriction_id, []).append(
                            (normalized_value, source_id, str(source["version"]))
                        )
            except RestrictionValidationError as exc:
                message = f"{source_id}: {exc.code}: {exc}"
                if source["authoritative"] is True:
                    layer_errors[layer].append(message)
                else:
                    warnings.append("Non-authoritative feature error: " + message)

        all_area_parts = [part for parts in layer_areas.values() for part in parts]
        unusable_area_parts = [part for parts in layer_unusable_areas.values() for part in parts]
        observed_area = float(unary_union(all_area_parts).area) if all_area_parts else 0.0
        observed_unusable_area = (
            float(unary_union(unusable_area_parts).area) if unusable_area_parts else 0.0
        )
        layer_results = []
        all_complete = True
        any_touch = False
        any_layer_error = False
        for layer in expected_layers:
            coverage_sources = [
                (source_id, source)
                for source_id, source in sources.items()
                if source["authoritative"] is True and source["coverage"].get(layer) is True  # type: ignore[union-attr]
            ]
            complete = bool(coverage_sources)
            all_complete &= complete
            errors = tuple(layer_errors[layer])
            any_layer_error |= bool(errors)
            parts = layer_areas[layer]
            layer_area = float(unary_union(parts).area) if parts else 0.0
            touches = layer_touches[layer]
            any_touch |= touches > 0
            if errors:
                layer_status: LayerStatus = "error"
            elif not complete:
                layer_status = "partial"
            elif layer_area > MIN_AREA_M2 or layer_lengths[layer] > MIN_LENGTH_M:
                layer_status = "restricted"
            elif touches:
                layer_status = "touch"
            else:
                layer_status = "clear"
            for restriction_id, values in layer_values[layer].items():
                if len({item[0].casefold() for item in values}) > 1:
                    conflicts.append(
                        RestrictionConflict(
                            layer=layer,
                            code="source_version_conflict",
                            message=(
                                f"Authoritative versions disagree for {layer}:{restriction_id}"
                            ),
                            source_ids=tuple(item[1] for item in values),
                            versions=tuple(item[2] for item in values),
                        )
                    )
            layer_results.append(
                RestrictionLayerResult(
                    layer=layer,
                    status=layer_status,
                    coverage_complete=complete,
                    source_ids=tuple(item[0] for item in coverage_sources),
                    versions=tuple(str(item[1]["version"]) for item in coverage_sources),
                    observed_area_m2=layer_area if complete or parts else None,
                    parcel_percent=layer_area / parcel_area * 100 if complete or parts else None,
                    intersection_length_m=layer_lengths[layer] or None,
                    touches=touches,
                    errors=errors,
                )
            )

        definitive = (
            all_complete
            and not any_layer_error
            and not conflicts
            and not positive_line_fact
            and not any_touch
        )
        restricted_area = min(parcel_area, observed_unusable_area) if definitive else None
        usable_area = (
            max(0.0, parcel_area - restricted_area) if restricted_area is not None else None
        )
        if conflicts:
            status: RestrictionStatus = "conflict"
        elif blockers or positive_authoritative_warning:
            status = "restricted"
        elif not all_complete or any_layer_error or positive_line_fact or any_touch:
            status = "partial"
        else:
            status = "clear"
        return RestrictionContextAnalysis(
            status=status,
            parcel_area_m2=parcel_area,
            layers=tuple(layer_results),
            facts=tuple(facts),
            conflicts=tuple(conflicts),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            observed_restricted_area_m2=(observed_area if all_complete or all_area_parts else None),
            restricted_area_m2=restricted_area,
            usable_area_m2=usable_area,
        )
    except RestrictionValidationError as exc:
        return RestrictionContextAnalysis(
            status="error",
            error_code=exc.code,
            error_message=str(exc),
        )
