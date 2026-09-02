"""Evidence-only intersection checks for published official NSDI layers."""
from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

from app.providers.nsdi import NsdiFeature


@dataclass(frozen=True, slots=True)
class WaterProtectionIntersection:
    status: str
    feature_count: int
    intersection_percent: float | None
    requires_manual_review: bool


_TO_METRIC = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True).transform


def analyze_water_protection_intersection(
    parcel_geojson: dict[str, object],
    features: tuple[NsdiFeature, ...],
) -> WaterProtectionIntersection:
    """Report only actual geometry intersection; never infer legal clearance."""
    try:
        parcel = shape(parcel_geojson)
        parcel_metric = transform(_TO_METRIC, parcel)
    except Exception:
        return WaterProtectionIntersection("parcel_invalid", 0, None, True)
    if parcel.is_empty or parcel_metric.is_empty or parcel_metric.area <= 0:
        return WaterProtectionIntersection("parcel_invalid", 0, None, True)
    intersection_area = 0.0
    intersecting = 0
    for feature in features:
        try:
            zone = transform(_TO_METRIC, shape(feature.geometry))
        except Exception:
            continue
        if zone.is_empty or not parcel_metric.intersects(zone):
            continue
        intersecting += 1
        intersection_area += parcel_metric.intersection(zone).area
    if not intersecting:
        return WaterProtectionIntersection(
            "no_intersection_in_published_layer", 0, 0.0, True
        )
    percent = min(100.0, max(0.0, intersection_area / parcel_metric.area * 100.0))
    return WaterProtectionIntersection(
        "intersection_found", intersecting, round(percent, 3), True
    )
