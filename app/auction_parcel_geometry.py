from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from pyproj import CRS, Geod, Transformer
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import transform, unary_union
from shapely.validation import explain_validity

GeometryStatus = Literal["ok", "unknown", "error"]
RestrictionStatus = Literal["intersecting", "clear", "partial", "unknown", "error"]
FacadeStatus = Literal["ok", "unknown", "error"]
KAZAKHSTAN_LON_BOUNDS = (46.0, 88.0)
KAZAKHSTAN_LAT_BOUNDS = (40.0, 56.5)


@dataclass(frozen=True, slots=True)
class GeometryLimits:
    max_features: int = 100
    max_vertices: int = 20_000
    max_polygons: int = 200


@dataclass(frozen=True, slots=True)
class RestrictionIntersection:
    layer: str
    intersection_area_m2: float
    parcel_percent: float


@dataclass(frozen=True, slots=True)
class ParcelGeometryAnalysis:
    status: GeometryStatus
    error_code: str | None = None
    message: str | None = None
    area_m2: float | None = None
    perimeter_m: float | None = None
    compactness: float | None = None
    bbox_width_m: float | None = None
    bbox_height_m: float | None = None
    facade_status: FacadeStatus = "unknown"
    facade_error_code: str | None = None
    facade_m: float | None = None
    depth_m: float | None = None
    facade_confidence: float | None = None
    facade_provenance: str | None = None
    restrictions_status: RestrictionStatus = "unknown"
    restrictions_complete: bool = False
    restriction_error_code: str | None = None
    restriction_error_message: str | None = None
    restriction_intersections: tuple[RestrictionIntersection, ...] = field(default_factory=tuple)
    restricted_area_m2: float | None = None
    remaining_usable_area_m2: float | None = None


class GeometryValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryValidationError("invalid_coordinate", "Coordinate must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise GeometryValidationError("invalid_coordinate", "Coordinate must be finite")
    return number


def _point(value: object) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) < 2:
        raise GeometryValidationError("invalid_nesting", "Position must contain lon and lat")
    lon = _finite_number(value[0])
    lat = _finite_number(value[1])
    if not (KAZAKHSTAN_LON_BOUNDS[0] <= lon <= KAZAKHSTAN_LON_BOUNDS[1]):
        raise GeometryValidationError("outside_kazakhstan", "Longitude is outside Kazakhstan")
    if not (KAZAKHSTAN_LAT_BOUNDS[0] <= lat <= KAZAKHSTAN_LAT_BOUNDS[1]):
        raise GeometryValidationError("outside_kazakhstan", "Latitude is outside Kazakhstan")
    return lon, lat


def _ring(value: object, counter: list[int], limits: GeometryLimits) -> list[tuple[float, float]]:
    if not isinstance(value, list) or len(value) < 4:
        raise GeometryValidationError("invalid_ring", "Polygon ring needs at least four positions")
    points = [_point(item) for item in value]
    counter[0] += len(points)
    if counter[0] > limits.max_vertices:
        raise GeometryValidationError("too_many_vertices", "Geometry exceeds vertex limit")
    if points[0] != points[-1]:
        raise GeometryValidationError("open_ring", "Polygon rings must be closed")
    return points


def _polygon(value: object, counter: list[int], limits: GeometryLimits) -> Polygon:
    if not isinstance(value, list) or not value:
        raise GeometryValidationError("invalid_nesting", "Polygon coordinates are missing")
    rings = [_ring(item, counter, limits) for item in value]
    polygon = Polygon(rings[0], rings[1:])
    if polygon.is_empty or polygon.area <= 0:
        raise GeometryValidationError("empty_geometry", "Polygon has no area")
    if not polygon.is_valid:
        raise GeometryValidationError("invalid_geometry", explain_validity(polygon))
    return polygon


def validate_parcel_geojson(
    geojson: object,
    *,
    limits: GeometryLimits = GeometryLimits(),
) -> Polygon | MultiPolygon:
    if not isinstance(geojson, dict):
        raise GeometryValidationError("invalid_geojson", "Geometry must be a GeoJSON object")
    geometry_type = geojson.get("type")
    coordinates = geojson.get("coordinates")
    counter = [0]
    if geometry_type == "Polygon":
        return _polygon(coordinates, counter, limits)
    if geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise GeometryValidationError("invalid_nesting", "MultiPolygon coordinates are missing")
        if len(coordinates) > limits.max_polygons:
            raise GeometryValidationError("too_many_polygons", "Geometry exceeds polygon limit")
        polygons = [_polygon(item, counter, limits) for item in coordinates]
        geometry = MultiPolygon(polygons)
        if not geometry.is_valid:
            raise GeometryValidationError("invalid_geometry", explain_validity(geometry))
        return geometry
    raise GeometryValidationError("unsupported_type", "Only Polygon and MultiPolygon are supported")


def _validate_road_geojson(
    geojson: object,
    *,
    limits: GeometryLimits,
) -> LineString | MultiLineString:
    if not isinstance(geojson, dict):
        raise GeometryValidationError("invalid_road_edge", "Road edge must be GeoJSON")
    geometry_type = geojson.get("type")
    coordinates = geojson.get("coordinates")
    counter = 0

    def line(value: object) -> LineString:
        nonlocal counter
        if not isinstance(value, list) or len(value) < 2:
            raise GeometryValidationError("invalid_road_edge", "Road line needs two positions")
        points = [_point(item) for item in value]
        counter += len(points)
        if counter > limits.max_vertices:
            raise GeometryValidationError("too_many_vertices", "Road edge exceeds vertex limit")
        return LineString(points)

    if geometry_type == "LineString":
        return line(coordinates)
    if geometry_type == "MultiLineString":
        if not isinstance(coordinates, list) or not coordinates:
            raise GeometryValidationError("invalid_road_edge", "Road lines are missing")
        if len(coordinates) > limits.max_features:
            raise GeometryValidationError("too_many_features", "Road edge exceeds feature limit")
        return MultiLineString([line(value) for value in coordinates])
    raise GeometryValidationError(
        "invalid_road_edge",
        "Road edge must be LineString or MultiLineString",
    )


def _metric_transformer(geometry: BaseGeometry) -> Transformer:
    centroid = geometry.centroid
    zone = max(1, min(60, int((centroid.x + 180) // 6) + 1))
    crs = CRS.from_epsg(32600 + zone)
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True)


def _geodesic_metrics(geometry: Polygon | MultiPolygon) -> tuple[float, float]:
    oriented = (
        orient(geometry, sign=1.0)
        if isinstance(geometry, Polygon)
        else MultiPolygon([orient(part, sign=1.0) for part in geometry.geoms])
    )
    area, perimeter = Geod(ellps="WGS84").geometry_area_perimeter(oriented)
    return abs(float(area)), abs(float(perimeter))


def _restriction_items(value: object, limits: GeometryLimits) -> list[tuple[str, object]]:
    if isinstance(value, dict) and value.get("type") == "FeatureCollection":
        features = value.get("features")
    elif isinstance(value, list):
        features = value
    else:
        raise GeometryValidationError(
            "invalid_restrictions",
            "Restrictions must be a FeatureCollection or feature list",
        )
    if not isinstance(features, list):
        raise GeometryValidationError("invalid_restrictions", "Restriction features are missing")
    if len(features) > limits.max_features:
        raise GeometryValidationError("too_many_features", "Restriction feature limit exceeded")
    items: list[tuple[str, object]] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise GeometryValidationError(
                "invalid_restrictions",
                "Restriction item is not a Feature",
            )
        properties = feature.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        layer = str(properties.get("layer") or properties.get("code") or f"restriction_{index + 1}")
        items.append((layer[:120], feature.get("geometry")))
    return items


def _raw_vertex_count(value: object, *, depth: int = 0) -> int:
    if depth > 8:
        raise GeometryValidationError("invalid_nesting", "Coordinate nesting is too deep")
    if not isinstance(value, list):
        raise GeometryValidationError("invalid_nesting", "Coordinates must be arrays")
    if value and isinstance(value[0], (int, float)) and not isinstance(value[0], bool):
        return 1
    return sum(_raw_vertex_count(item, depth=depth + 1) for item in value)


def analyze_parcel_geometry(
    parcel_geojson: object | None,
    *,
    restriction_features: object | None = None,
    restrictions_complete: bool = False,
    road_edge_geojson: object | None = None,
    road_edge_confidence: float | None = None,
    road_edge_provenance: str | None = None,
    limits: GeometryLimits = GeometryLimits(),
) -> ParcelGeometryAnalysis:
    """Analyze parcel polygons.

    Restriction overlays must be Polygon/MultiPolygon areas. Buffer red-line or other
    line datasets upstream and pass the resulting polygon; raw LineString overlays
    are rejected explicitly.
    """
    if parcel_geojson is None:
        return ParcelGeometryAnalysis(
            status="unknown",
            error_code="geometry_missing",
            message="Parcel geometry is not available",
        )
    try:
        parcel = validate_parcel_geojson(parcel_geojson, limits=limits)
        area_m2, perimeter_m = _geodesic_metrics(parcel)
        transformer = _metric_transformer(parcel)
        parcel_metric = transform(transformer.transform, parcel)
        min_x, min_y, max_x, max_y = parcel_metric.bounds
        compactness = 4 * math.pi * area_m2 / (perimeter_m * perimeter_m)
    except GeometryValidationError as exc:
        return ParcelGeometryAnalysis(status="error", error_code=exc.code, message=str(exc))
    except Exception as exc:  # defensive boundary for malformed third-party payloads
        return ParcelGeometryAnalysis(
            status="error",
            error_code="geometry_processing_error",
            message=str(exc)[:500],
        )

    facade_m = None
    depth_m = None
    facade_status: FacadeStatus = "unknown"
    facade_error_code = None
    facade_confidence = None
    facade_provenance = None
    if road_edge_geojson is not None:
        try:
            road = _validate_road_geojson(road_edge_geojson, limits=limits)
            road_metric = transform(transformer.transform, road)
            intersection_length = float(parcel_metric.boundary.intersection(road_metric).length)
            if intersection_length <= 0.01:
                facade_status = "unknown"
                facade_error_code = "road_edge_not_intersecting"
            else:
                confidence = 0.5 if road_edge_confidence is None else float(road_edge_confidence)
                if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                    raise GeometryValidationError(
                        "invalid_road_confidence",
                        "Road-edge confidence must be finite and between zero and one",
                    )
                facade_m = intersection_length
                depth_m = area_m2 / facade_m
                facade_status = "ok"
                facade_confidence = confidence
                facade_provenance = road_edge_provenance or "explicit_road_edge"
        except GeometryValidationError as exc:
            facade_m = None
            depth_m = None
            facade_status = "error"
            facade_error_code = exc.code
        except (TypeError, ValueError):
            facade_status = "error"
            facade_error_code = "invalid_road_edge"

    restrictions_status: RestrictionStatus = "unknown"
    intersections: tuple[RestrictionIntersection, ...] = ()
    restricted_area_m2 = None
    remaining_area_m2 = None
    restriction_error_code = None
    restriction_error_message = None
    if restriction_features is not None:
        try:
            items = _restriction_items(restriction_features, limits)
            by_layer: dict[str, list[BaseGeometry]] = {}
            all_intersections: list[BaseGeometry] = []
            restriction_vertex_count = 0
            for layer, geometry_json in items:
                if not isinstance(geometry_json, dict):
                    raise GeometryValidationError(
                        "invalid_restrictions",
                        "Restriction geometry is missing",
                    )
                restriction_vertex_count += _raw_vertex_count(geometry_json.get("coordinates"))
                if restriction_vertex_count > limits.max_vertices:
                    raise GeometryValidationError(
                        "too_many_vertices",
                        "Restriction geometries exceed total vertex limit",
                    )
                overlay = validate_parcel_geojson(geometry_json, limits=limits)
                overlay_metric = transform(transformer.transform, overlay)
                intersection = parcel_metric.intersection(overlay_metric)
                by_layer.setdefault(layer, []).append(intersection)
                if not intersection.is_empty:
                    all_intersections.append(intersection)
            layer_results = []
            area_scale = area_m2 / float(parcel_metric.area)
            for layer, parts in sorted(by_layer.items()):
                layer_area = float(unary_union(parts).area) * area_scale
                layer_results.append(
                    RestrictionIntersection(
                        layer=layer,
                        intersection_area_m2=layer_area,
                        parcel_percent=(layer_area / area_m2 * 100) if area_m2 else 0.0,
                    )
                )
            observed_restricted_area_m2 = (
                min(area_m2, float(unary_union(all_intersections).area) * area_scale)
                if all_intersections
                else 0.0
            )
            intersections = tuple(layer_results)
            if restrictions_complete:
                restricted_area_m2 = observed_restricted_area_m2
                remaining_area_m2 = max(0.0, area_m2 - restricted_area_m2)
                restrictions_status = (
                    "intersecting" if restricted_area_m2 > 0 else "clear"
                )
            else:
                # Per-layer observations remain useful, but total burden and usable
                # area are unknown until authoritative layer coverage is complete.
                restricted_area_m2 = None
                remaining_area_m2 = None
                restrictions_status = "partial"
        except GeometryValidationError as exc:
            restrictions_status = "error"
            restriction_error_code = exc.code
            restriction_error_message = str(exc)
            intersections = ()
            restricted_area_m2 = None
            remaining_area_m2 = None

    return ParcelGeometryAnalysis(
        status="ok",
        area_m2=area_m2,
        perimeter_m=perimeter_m,
        compactness=compactness,
        bbox_width_m=float(max_x - min_x),
        bbox_height_m=float(max_y - min_y),
        facade_status=facade_status,
        facade_error_code=facade_error_code,
        facade_m=facade_m,
        depth_m=depth_m,
        facade_confidence=facade_confidence,
        facade_provenance=facade_provenance,
        restrictions_status=restrictions_status,
        restrictions_complete=restrictions_complete,
        restriction_error_code=restriction_error_code,
        restriction_error_message=restriction_error_message,
        restriction_intersections=intersections,
        restricted_area_m2=restricted_area_m2,
        remaining_usable_area_m2=remaining_area_m2,
    )
