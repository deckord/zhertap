"""Bounded public NSDI WFS reader for water-protection evidence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class NsdiProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NsdiFeature:
    feature_id: str
    source_layer: str
    geometry: dict[str, object]
    properties: dict[str, object]


class NationalWaterProtectionProvider:
    """Read the published Kostanay NSDI layer; absence never means legal clearance.

    The historical class name is retained for import compatibility.  GeoServer
    capabilities identify this layer as regional, not national, so callers must
    not turn an empty response outside the declared extent into non-intersection.
    """

    WFS_URL = "https://map.gov.kz/geoserver/ows"
    WATER_PROTECTION_ZONE_LAYER = "geonode:waterprotectionzone"
    DATASET_URL = "https://map.gov.kz/api/v2/datasets/1633"
    COVERAGE_AREA = "Костанайская область"
    PUBLISHED_EXTENT = (
        60.587555404441,
        48.7392173714443,
        68.1003205621784,
        54.646679811658,
    )
    MAX_FEATURES = 100
    # A single official zone polygon can legitimately contain several megabytes of
    # vertices even for a tiny parcel bbox. Keep a hard cap, but above the observed
    # 2.9–3.5 MB responses from the published national layer.
    MAX_RESPONSE_BYTES = 5_000_000

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    @property
    def coverage_area(self) -> str:
        return self.COVERAGE_AREA

    @property
    def source_layer(self) -> str:
        return self.WATER_PROTECTION_ZONE_LAYER

    def covers_bbox(self, bbox: tuple[float, float, float, float]) -> bool:
        min_lon, min_lat, max_lon, max_lat = bbox
        extent_min_lon, extent_min_lat, extent_max_lon, extent_max_lat = (
            self.PUBLISHED_EXTENT
        )
        return (
            extent_min_lon <= min_lon < max_lon <= extent_max_lon
            and extent_min_lat <= min_lat < max_lat <= extent_max_lat
        )

    def covers_region(self, region: str | None) -> bool:
        normalized = " ".join(str(region or "").split()).casefold()
        return normalized == self.COVERAGE_AREA.casefold()

    def features_for_bbox(
        self, bbox: tuple[float, float, float, float]
    ) -> tuple[NsdiFeature, ...]:
        min_lon, min_lat, max_lon, max_lat = bbox
        if not (46 <= min_lon < max_lon <= 88 and 40 <= min_lat < max_lat <= 56.5):
            raise NsdiProviderError("bbox outside Kazakhstan bounds")
        if not self.covers_bbox(bbox):
            raise NsdiProviderError("bbox outside published layer extent")
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": self.WATER_PROTECTION_ZONE_LAYER,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat},EPSG:4326",
            "count": str(self.MAX_FEATURES),
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(30.0, connect=10.0), transport=self.transport
            ) as client:
                response = client.get(self.WFS_URL, params=params)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NsdiProviderError("NSDI water-protection layer unavailable") from exc
        if len(response.content) > self.MAX_RESPONSE_BYTES:
            raise NsdiProviderError("NSDI response exceeds byte limit")
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise NsdiProviderError("NSDI returned non-JSON data") from exc
        if payload.get("type") != "FeatureCollection" or not isinstance(
            payload.get("features"), list
        ):
            raise NsdiProviderError(
                "NSDI response is not a GeoJSON feature collection"
            )
        results: list[NsdiFeature] = []
        for item in payload["features"][: self.MAX_FEATURES]:
            if not isinstance(item, dict):
                continue
            geometry = item.get("geometry")
            properties = item.get("properties")
            feature_id = str(item.get("id") or "")
            if (
                not feature_id
                or not isinstance(geometry, dict)
                or geometry.get("type") not in {"Polygon", "MultiPolygon"}
                or not isinstance(geometry.get("coordinates"), list)
                or not isinstance(properties, dict)
            ):
                continue
            results.append(
                NsdiFeature(
                    feature_id=feature_id,
                    source_layer=self.WATER_PROTECTION_ZONE_LAYER,
                    geometry=geometry,
                    properties=properties,
                )
            )
        return tuple(results)
