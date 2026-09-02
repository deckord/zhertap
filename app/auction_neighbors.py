"""Polygon-first classification of neighbouring EGKN parcels."""
from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import shape

from app.providers.egkn import EgknContextFeature


@dataclass(frozen=True, slots=True)
class NeighborLandUseResult:
    status: str
    counts: dict[str, int]


def _category(properties: dict[str, object]) -> str:
    text = " ".join(str(value or "") for value in properties.values()).casefold()
    if any(token in text for token in ("ижс", "жилищ", "индивидуальн")):
        return "residential"
    if any(token in text for token in ("лпх", "личн подсоб")):
        return "lph"
    if any(token in text for token in ("крестьян", "фермер", "сельск", "пашн", "аграр")):
        return "agriculture"
    if any(token in text for token in ("туризм", "рекреац", "баз отдыха", "гостиниц")):
        return "tourism"
    if any(token in text for token in ("магазин", "торгов", "коммерч", "бизнес")):
        return "commercial"
    return "other"


def analyze_neighbor_land_use(
    parcel_geojson: dict[str, object],
    features: tuple[EgknContextFeature, ...] | list[EgknContextFeature],
) -> NeighborLandUseResult:
    """Classify only geometrically adjacent EGKN parcels; no cadastral guesswork."""
    try:
        parcel = shape(parcel_geojson)
    except Exception:
        return NeighborLandUseResult("parcel_invalid", {})
    if parcel.is_empty:
        return NeighborLandUseResult("parcel_invalid", {})
    counts: dict[str, int] = {}
    for feature in features:
        try:
            neighbor = shape(feature.geometry)
        except Exception:
            continue
        if neighbor.is_empty or not parcel.touches(neighbor):
            continue
        category = _category(feature.properties)
        counts[category] = counts.get(category, 0) + 1
    return NeighborLandUseResult("found" if counts else "no_adjacent_features", counts)
