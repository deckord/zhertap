import logging
import math
import time
from dataclasses import dataclass

import httpx
from shapely.geometry import LineString, Point, Polygon

from app.config import settings
from app.provider_backpressure import ProviderBackpressure
from app.provider_guard import bounded_http_request, guarded_http_call

logger = logging.getLogger(__name__)


class OsmProviderError(RuntimeError):
    pass


@dataclass(slots=True)
class Surroundings:
    cemetery_distance_m: float | None = None
    road_distance_m: float | None = None
    power_distance_m: float | None = None
    water_distance_m: float | None = None
    open_water_distance_m: float | None = None
    object_distance_m: float | None = None
    object_kind: str | None = None
    checked: bool = False


class OsmProvider:
    def __init__(self, *, backpressure: ProviderBackpressure | None = None) -> None:
        self.backpressure = backpressure

    def nearest_cemetery(self, lat: float, lon: float, radius_m: int) -> float | None:
        if not settings.enable_live_osm:
            return None

        query = f"""
        [out:json][timeout:25];
        (
          nwr(around:{radius_m},{lat},{lon})[amenity=grave_yard];
          nwr(around:{radius_m},{lat},{lon})[landuse=cemetery];
        );
        out center;
        """
        payload = self._request(query)
        distances = []
        for item in payload.get("elements", []):
            point_lat = item.get("lat") or item.get("center", {}).get("lat")
            point_lon = item.get("lon") or item.get("center", {}).get("lon")
            if point_lat is not None and point_lon is not None:
                distances.append(haversine_m(lat, lon, point_lat, point_lon))
        return min(distances) if distances else None

    def analyze_points(
        self,
        points: list[tuple[float, float]],
        radius_m: int = 2000,
        *,
        time_budget_seconds: int | float | None = None,
    ) -> list[Surroundings]:
        if not points or not settings.enable_live_osm:
            return [Surroundings() for _ in points]

        results = [Surroundings() for _ in points]
        budget = (
            settings.osm_time_budget_seconds
            if time_budget_seconds is None
            else max(0.0, float(time_budget_seconds))
        )
        deadline = time.monotonic() + budget
        successful_batches = 0
        batch_size = settings.osm_batch_size
        for start in range(0, len(points), batch_size):
            if time.monotonic() >= deadline:
                break
            batch = points[start : start + batch_size]
            query = build_point_query(batch, radius_m=radius_m)
            try:
                payload = self._request(query, deadline=deadline)
            except OsmProviderError as exc:
                logger.warning(
                    "OSM batch %s-%s was not checked: %s",
                    start,
                    start + len(batch) - 1,
                    exc,
                )
                continue
            checked = surroundings_from_payload(batch, payload, radius_m=radius_m)
            results[start : start + len(batch)] = checked
            successful_batches += 1

        if successful_batches == 0:
            logger.warning(
                "Public OSM servers did not answer within the time budget; "
                "continuing with unchecked OSM surroundings"
            )
            return results
            raise OsmProviderError(
                "Публичные серверы OSM временно не ответили; координаты не выдаются "
                "без проверки дорог и объектов"
            )
        return results

    def analyze_batch(
        self, points: list[tuple[float, float]], radius_m: int = 2000
    ) -> list[Surroundings]:
        """One durable workflow unit maps to exactly one Overpass request."""
        if not points or len(points) > settings.osm_batch_size:
            raise ValueError("OSM workflow batch is outside bounds")
        payload = self._request(build_point_query(points, radius_m=radius_m))
        return surroundings_from_payload(points, payload, radius_m=radius_m)

    def _request(
        self,
        query: str,
        *,
        deadline: float | None = None,
    ) -> dict:
        url = overpass_urls()[0]
        remaining = (
            settings.osm_query_timeout_seconds
            if deadline is None
            else min(
                settings.osm_query_timeout_seconds,
                max(0, deadline - time.monotonic()),
            )
        )
        if remaining < 1:
            raise OsmProviderError("OSM request deadline expired")
        try:
            def request() -> httpx.Response:
                with httpx.Client(timeout=remaining) as client:
                    return bounded_http_request(
                        client,
                        "POST",
                        url,
                        data={"data": query},
                        headers={
                            "User-Agent": "LandScoutKZ/0.3 (preliminary land research)"
                        },
                        total_deadline_seconds=min(remaining, 120.0),
                    )

            response = guarded_http_call(
                "osm_overpass",
                request,
                backpressure=self.backpressure,
            )
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OsmProviderError(f"Overpass response is unusable: {exc}") from exc


def overpass_urls() -> list[str]:
    values = [settings.overpass_url, *settings.overpass_fallback_urls.split(",")]
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def build_point_query(
    points: list[tuple[float, float]],
    *,
    radius_m: int,
) -> str:
    selectors: list[str] = []
    cemetery_radius = min(radius_m, 2000)
    for lat, lon in points:
        center = f"{lat:.7f},{lon:.7f}"
        selectors.extend(
            [
                f"nwr(around:100,{center});",
                f"nwr(around:{cemetery_radius},{center})[amenity=grave_yard];",
                f"nwr(around:{cemetery_radius},{center})[landuse=cemetery];",
                f"nwr(around:300,{center})[highway];",
                f"nwr(around:500,{center})[power];",
                f'nwr(around:600,{center})[man_made~"water_tower|water_well"];',
                f"nwr(around:600,{center})[amenity=drinking_water];",
                f"nwr(around:500,{center})[natural=water];",
                f"nwr(around:500,{center})[natural=wetland];",
                f"nwr(around:500,{center})[waterway];",
                f"nwr(around:500,{center})[landuse=reservoir];",
                f"nwr(around:500,{center})[water];",
            ]
        )
    return (
        f"[out:json][timeout:{settings.osm_query_timeout_seconds}];\n(\n"
        + "\n".join(selectors)
        + "\n);\nout center geom;"
    )


def surroundings_from_payload(
    points: list[tuple[float, float]],
    payload: dict,
    *,
    radius_m: int,
) -> list[Surroundings]:
    features: list[tuple[dict, set[str]]] = []
    seen: set[tuple[str, int]] = set()
    for item in payload.get("elements", []):
        key = (str(item.get("type") or ""), int(item.get("id") or 0))
        if key in seen:
            continue
        seen.add(key)
        tags = item.get("tags") or {}
        kinds: set[str] = set()
        if tags.get("amenity") == "grave_yard" or tags.get("landuse") == "cemetery":
            kinds.add("cemetery")
        if tags.get("highway") and tags.get("highway") not in {
            "footway",
            "path",
            "steps",
        }:
            kinds.add("road")
        if tags.get("power"):
            kinds.add("power")
        if (
            tags.get("man_made") in {"water_tower", "water_well"}
            or tags.get("amenity") == "drinking_water"
        ):
            kinds.add("water")
        if (
            tags.get("natural") in {"water", "wetland"}
            or tags.get("waterway")
            or tags.get("landuse") == "reservoir"
            or tags.get("water")
        ):
            kinds.add("open_water")
        if (
            tags.get("building")
            or tags.get("railway") not in {None, "abandoned", "disused"}
            or tags.get("aeroway")
            or tags.get("amenity") in {"parking", "fuel"}
            or tags.get("landuse") in {"industrial", "commercial", "retail"}
            or tags.get("landuse") in {"landfill", "quarry", "military"}
            or tags.get("boundary") == "protected_area"
            or tags.get("leisure") == "nature_reserve"
            or tags.get("man_made") in {"pipeline", "wastewater_plant"}
            or tags.get("amenity") in {"waste_disposal", "grave_yard"}
            or tags.get("natural") == "water"
            or tags.get("waterway")
            or tags.get("power") in {"substation", "transformer", "plant"}
        ):
            kinds.add("object")
        if not kinds:
            continue
        features.append((item, kinds))

    results = []
    for point_lat, point_lon in points:
        distances: dict[str, list[float]] = {
            "cemetery": [],
            "road": [],
            "power": [],
            "water": [],
            "open_water": [],
            "object": [],
        }
        for item, kinds in features:
            distance = feature_distance_m(point_lat, point_lon, item)
            if distance is None or distance > radius_m:
                continue
            for kind in kinds:
                distances[kind].append(distance)
        object_distance = min(distances["object"], default=None)
        results.append(
            Surroundings(
                cemetery_distance_m=min(distances["cemetery"], default=None),
                road_distance_m=min(distances["road"], default=None),
                power_distance_m=min(distances["power"], default=None),
                water_distance_m=min(distances["water"], default=None),
                open_water_distance_m=min(distances["open_water"], default=None),
                object_distance_m=object_distance,
                object_kind="mapped object" if object_distance is not None else None,
                checked=True,
            )
        )
    return results


def feature_distance_m(lat: float, lon: float, item: dict) -> float | None:
    raw_geometry = item.get("geometry") or []
    coordinates = [
        (float(row["lon"]), float(row["lat"]))
        for row in raw_geometry
        if row.get("lat") is not None and row.get("lon") is not None
    ]
    if not coordinates:
        item_lat = item.get("lat") or item.get("center", {}).get("lat")
        item_lon = item.get("lon") or item.get("center", {}).get("lon")
        if item_lat is None or item_lon is None:
            return None
        return haversine_m(lat, lon, float(item_lat), float(item_lon))

    cos_lat = math.cos(math.radians(lat))
    local = [
        ((item_lon - lon) * 111_320 * cos_lat, (item_lat - lat) * 111_320)
        for item_lon, item_lat in coordinates
    ]
    origin = Point(0, 0)
    if len(local) == 1:
        return origin.distance(Point(local[0]))
    if len(local) >= 4 and local[0] == local[-1]:
        polygon = Polygon(local)
        if polygon.is_valid:
            return origin.distance(polygon)
    return origin.distance(LineString(local))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + (
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))
