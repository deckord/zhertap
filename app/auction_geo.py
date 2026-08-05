from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from app.models import AuctionLot

EARTH_RADIUS_M = 6_371_000


@dataclass(frozen=True, slots=True)
class GeoPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class AuctionGeoObject:
    kind: str
    latitude: float
    longitude: float
    name: str | None = None


@dataclass(frozen=True, slots=True)
class AuctionGeoMetrics:
    status: str
    latitude: float | None
    longitude: float | None
    distance_to_city_m: float | None = None
    road_m: float | None = None
    school_m: float | None = None
    hospital_m: float | None = None
    fuel_m: float | None = None
    railway_m: float | None = None
    power_line_m: float | None = None


_LAT_KEYS = {"lat", "latitude", "y"}
_LON_KEYS = {"lon", "lng", "long", "longitude", "x"}
_REFERENCE_LIST_KEYS = {
    "geo_reference_objects",
    "reference_objects",
    "nearby_objects",
    "osm_objects",
    "objects",
}
_GROUPED_REFERENCE_KEYS = {
    "cities": "city",
    "city_centers": "city",
    "roads": "road",
    "highways": "road",
    "schools": "school",
    "hospitals": "hospital",
    "fuel_stations": "fuel",
    "railways": "railway",
    "power_lines": "power_line",
}
_CATEGORY_ALIASES = {
    "city": {"city", "town", "village", "settlement", "locality", "city_center", "district_center"},
    "road": {"road", "highway", "asphalt", "street", "motorway", "trunk", "primary"},
    "school": {"school", "college", "kindergarten", "education"},
    "hospital": {"hospital", "clinic", "doctors", "healthcare", "medical"},
    "fuel": {"fuel", "gas_station", "petrol_station", "amenity:fuel"},
    "railway": {"railway", "rail", "station"},
    "power_line": {"power_line", "power", "line", "transmission", "electricity"},
}
_DISTANCE_FIELDS = {
    "city": "distance_to_city_m",
    "road": "road_m",
    "school": "school_m",
    "hospital": "hospital_m",
    "fuel": "fuel_m",
    "railway": "railway_m",
    "power_line": "power_line_m",
}


def haversine_m(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    lat_a = math.radians(latitude_a)
    lon_a = math.radians(longitude_a)
    lat_b = math.radians(latitude_b)
    lon_b = math.radians(longitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = lon_b - lon_a
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(value))


def auction_geo_metrics(
    lot: AuctionLot,
    *,
    reference_objects: list[AuctionGeoObject | dict[str, Any]] | None = None,
) -> AuctionGeoMetrics:
    payload = _load_payload(lot.raw_payload_json)
    point = extract_lot_point(lot, payload)
    if point is None:
        return AuctionGeoMetrics(status="no_coordinates", latitude=None, longitude=None)

    references = _normalize_reference_objects(reference_objects or [])
    references.extend(extract_reference_objects(payload))
    if not references:
        return AuctionGeoMetrics(
            status="no_reference_objects",
            latitude=point.latitude,
            longitude=point.longitude,
        )

    values: dict[str, float | None] = dict.fromkeys(_DISTANCE_FIELDS.values())
    for category, field_name in _DISTANCE_FIELDS.items():
        matching = [
            item for item in references if _canonical_category(item.kind) == category
        ]
        if not matching:
            continue
        values[field_name] = min(
            haversine_m(point.latitude, point.longitude, item.latitude, item.longitude)
            for item in matching
        )

    return AuctionGeoMetrics(
        status="ok",
        latitude=point.latitude,
        longitude=point.longitude,
        **values,
    )


def extract_lot_point(lot: AuctionLot, payload: Any | None = None) -> GeoPoint | None:
    payload = _load_payload(lot.raw_payload_json) if payload is None else payload
    point = _point_from_lot_payload(payload)
    if point is not None:
        return point
    for value in (lot.location_text, lot.description, lot.title):
        point = _point_from_text(value)
        if point is not None:
            return point
    return None


def _point_from_lot_payload(payload: Any) -> GeoPoint | None:
    if not isinstance(payload, dict):
        return _point_from_value(payload)

    for key in ("geometry", "coordinates", "center", "location", "point"):
        point = _point_from_value(payload.get(key))
        if point is not None:
            return point

    point = _point_from_coordinate_keys(payload)
    if point is not None:
        return point

    excluded = _REFERENCE_LIST_KEYS | set(_GROUPED_REFERENCE_KEYS)
    for key, child in payload.items():
        if key in excluded:
            continue
        point = _point_from_value(child)
        if point is not None:
            return point
    return None


def extract_reference_objects(payload: Any) -> list[AuctionGeoObject]:
    if not isinstance(payload, dict):
        return []

    objects: list[AuctionGeoObject] = []
    for key in _REFERENCE_LIST_KEYS:
        objects.extend(_normalize_reference_objects(payload.get(key)))

    for key, kind in _GROUPED_REFERENCE_KEYS.items():
        grouped = payload.get(key)
        if not isinstance(grouped, list):
            continue
        for item in grouped:
            point = _point_from_value(item)
            if point is None:
                continue
            name = item.get("name") if isinstance(item, dict) else None
            objects.append(
                AuctionGeoObject(
                    kind=kind,
                    latitude=point.latitude,
                    longitude=point.longitude,
                    name=str(name) if name else None,
                )
            )
    return objects


def _normalize_reference_objects(value: Any) -> list[AuctionGeoObject]:
    if not isinstance(value, list):
        return []
    objects: list[AuctionGeoObject] = []
    for item in value:
        if isinstance(item, AuctionGeoObject):
            objects.append(item)
            continue
        if not isinstance(item, dict):
            continue
        point = _point_from_value(item)
        kind = item.get("kind") or item.get("type") or item.get("category")
        if point is None or not kind:
            continue
        name = item.get("name")
        objects.append(
            AuctionGeoObject(
                kind=str(kind),
                latitude=point.latitude,
                longitude=point.longitude,
                name=str(name) if name else None,
            )
        )
    return objects


def _load_payload(raw_payload_json: str | None) -> Any:
    if not raw_payload_json:
        return None
    try:
        return json.loads(raw_payload_json)
    except json.JSONDecodeError:
        return None


def _point_from_value(value: Any) -> GeoPoint | None:
    if isinstance(value, dict):
        geometry = value.get("geometry")
        if isinstance(geometry, dict):
            point = _point_from_geojson(geometry)
            if point is not None:
                return point

        point = _point_from_coordinate_keys(value)
        if point is not None:
            return point

        coordinates = value.get("coordinates")
        point = _point_from_sequence(coordinates)
        if point is not None:
            return point

        for child in value.values():
            point = _point_from_value(child)
            if point is not None:
                return point
    elif isinstance(value, list):
        point = _point_from_sequence(value)
        if point is not None:
            return point
        for child in value:
            point = _point_from_value(child)
            if point is not None:
                return point
    elif isinstance(value, str):
        return _point_from_text(value)
    return None


def _point_from_coordinate_keys(value: dict[str, Any]) -> GeoPoint | None:
    lower = {str(key).lower(): item for key, item in value.items()}
    latitude = next((_to_float(lower[key]) for key in _LAT_KEYS if key in lower), None)
    longitude = next((_to_float(lower[key]) for key in _LON_KEYS if key in lower), None)
    if latitude is None or longitude is None:
        return None
    return _valid_point(latitude, longitude)


def _point_from_geojson(value: dict[str, Any]) -> GeoPoint | None:
    if value.get("type") != "Point":
        return None
    return _point_from_sequence(value.get("coordinates"))


def _point_from_sequence(value: Any) -> GeoPoint | None:
    if not isinstance(value, list | tuple) or len(value) < 2:
        return None
    first = _to_float(value[0])
    second = _to_float(value[1])
    if first is None or second is None:
        return None
    return _valid_point(second, first) or _valid_point(first, second)


def _point_from_text(value: str | None) -> GeoPoint | None:
    if not value:
        return None
    match = re.search(r"@(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)", value)
    if match:
        return _valid_point(float(match.group(1)), float(match.group(2)))
    for first, second in re.findall(r"(-?\d{1,3}(?:\.\d+)?)[,\s]+(-?\d{1,3}(?:\.\d+)?)", value):
        point = _valid_point(float(first), float(second)) or _valid_point(
            float(second), float(first)
        )
        if point is not None:
            return point
    return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None
    return None


def _valid_point(latitude: float, longitude: float) -> GeoPoint | None:
    if -90 <= latitude <= 90 and -180 <= longitude <= 180:
        return GeoPoint(latitude=latitude, longitude=longitude)
    return None


def _canonical_category(value: str) -> str | None:
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    for category, aliases in _CATEGORY_ALIASES.items():
        if normalized == category or normalized in aliases:
            return category
    return None
