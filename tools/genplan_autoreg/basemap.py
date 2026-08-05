from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image

from .models import BoundingBox
from .providers import USER_AGENT

WEB_MERCATOR_MAX_LAT = 85.05112878


@dataclass(frozen=True, slots=True)
class ReferenceRaster:
    image: Image.Image
    bbox: BoundingBox
    source: str
    attribution: str

    def pixel_to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        width, height = self.image.size
        if width <= 1 or height <= 1:
            raise ValueError("Reference raster is too small")
        lon = self.bbox.west + (x / (width - 1)) * (self.bbox.east - self.bbox.west)
        north_y = _lat_to_mercator_y(self.bbox.north)
        south_y = _lat_to_mercator_y(self.bbox.south)
        mercator_y = north_y + (y / (height - 1)) * (south_y - north_y)
        lat = _mercator_y_to_lat(mercator_y)
        return lon, lat


class BasemapProvider(Protocol):
    def fetch(self, bbox: BoundingBox, output_dir: Path) -> ReferenceRaster: ...


BASEMAPS = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
    },
    "arcgis": {
        "url": (
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        "attribution": "Esri World Imagery and its listed data providers",
    },
}


@dataclass(slots=True)
class WebTileProvider:
    source: str = "arcgis"
    zoom: int = 15
    max_tiles: int = 144
    tile_size: int = 256
    timeout: float = 30.0
    client: httpx.Client | None = None

    def fetch(self, bbox: BoundingBox, output_dir: Path) -> ReferenceRaster:
        if self.source not in BASEMAPS:
            raise ValueError(f"Unsupported basemap: {self.source}")
        if not 0 <= self.zoom <= 19:
            raise ValueError("Zoom must be between 0 and 19")
        output_dir.mkdir(parents=True, exist_ok=True)

        zoom = self.zoom
        while True:
            min_x, min_y = _lonlat_to_tile(bbox.west, bbox.north, zoom)
            max_x, max_y = _lonlat_to_tile(bbox.east, bbox.south, zoom)
            tile_count = (max_x - min_x + 1) * (max_y - min_y + 1)
            if tile_count <= self.max_tiles or zoom == 0:
                break
            zoom -= 1

        mosaic = Image.new(
            "RGB",
            (
                (max_x - min_x + 1) * self.tile_size,
                (max_y - min_y + 1) * self.tile_size,
            ),
        )
        own_client = self.client is None
        client = self.client or httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        try:
            for tile_y in range(min_y, max_y + 1):
                for tile_x in range(min_x, max_x + 1):
                    tile = self._load_tile(client, tile_x, tile_y, zoom, output_dir)
                    mosaic.paste(
                        tile,
                        ((tile_x - min_x) * self.tile_size, (tile_y - min_y) * self.tile_size),
                    )
        finally:
            if own_client:
                client.close()

        tile_west, tile_north = _tile_to_lonlat(min_x, min_y, zoom)
        tile_east, tile_south = _tile_to_lonlat(max_x + 1, max_y + 1, zoom)
        tile_bbox = BoundingBox(
            west=tile_west,
            south=tile_south,
            east=tile_east,
            north=tile_north,
            source=bbox.source,
            label=bbox.label,
        )
        crop_left, crop_top = _lonlat_to_mosaic_pixel(
            bbox.west,
            bbox.north,
            tile_bbox,
            mosaic.size,
        )
        crop_right, crop_bottom = _lonlat_to_mosaic_pixel(
            bbox.east,
            bbox.south,
            tile_bbox,
            mosaic.size,
        )
        cropped = mosaic.crop(
            (
                max(0, int(math.floor(crop_left))),
                max(0, int(math.floor(crop_top))),
                min(mosaic.width, int(math.ceil(crop_right))),
                min(mosaic.height, int(math.ceil(crop_bottom))),
            )
        )
        if cropped.width < 2 or cropped.height < 2:
            raise ValueError("Basemap crop is empty")
        return ReferenceRaster(
            image=cropped,
            bbox=bbox,
            source=f"{self.source}:z{zoom}",
            attribution=BASEMAPS[self.source]["attribution"],
        )

    def _load_tile(
        self,
        client: httpx.Client,
        tile_x: int,
        tile_y: int,
        zoom: int,
        output_dir: Path,
    ) -> Image.Image:
        cache = output_dir / "tile_cache" / self.source / str(zoom)
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / f"{tile_x}_{tile_y}.png"
        if path.exists():
            with Image.open(path) as image:
                return image.convert("RGB")
        url = BASEMAPS[self.source]["url"].format(z=zoom, x=tile_x, y=tile_y)
        response = client.get(url)
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        if image.size != (self.tile_size, self.tile_size):
            raise ValueError(f"Unexpected tile size from {url}: {image.size}")
        image.save(path, format="PNG")
        return image


def _lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    lat = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat))
    scale = 2**zoom
    x = int(math.floor((lon + 180.0) / 360.0 * scale))
    lat_rad = math.radians(lat)
    y = int(
        math.floor(
            (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * scale
        )
    )
    return max(0, min(scale - 1, x)), max(0, min(scale - 1, y))


def _tile_to_lonlat(x: int, y: int, zoom: int) -> tuple[float, float]:
    scale = 2**zoom
    lon = x / scale * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / scale))))
    return lon, lat


def _lat_to_mercator_y(lat: float) -> float:
    lat = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat))
    return math.asinh(math.tan(math.radians(lat)))


def _mercator_y_to_lat(value: float) -> float:
    return math.degrees(math.atan(math.sinh(value)))


def _lonlat_to_mosaic_pixel(
    lon: float,
    lat: float,
    bbox: BoundingBox,
    size: tuple[int, int],
) -> tuple[float, float]:
    x = (lon - bbox.west) / (bbox.east - bbox.west) * size[0]
    north_y = _lat_to_mercator_y(bbox.north)
    south_y = _lat_to_mercator_y(bbox.south)
    y = (_lat_to_mercator_y(lat) - north_y) / (south_y - north_y) * size[1]
    return x, y
