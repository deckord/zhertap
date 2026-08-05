from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pyproj import Transformer
from shapely import wkt
from shapely.ops import transform

from .models import BoundingBox

USER_AGENT = "LandScoutKZ-GenplanAutoreg/0.1 (operator-assisted map registration)"
KAZAKH_TRANSLATION = str.maketrans(
    {
        "ә": "а",
        "ғ": "г",
        "қ": "к",
        "ң": "н",
        "ө": "о",
        "ұ": "у",
        "ү": "у",
        "һ": "х",
        "і": "и",
        "ё": "е",
    }
)
ADMIN_WORDS_RE = re.compile(
    r"\b(?:область|обл|район|р-н|город|г|село|с|поселок|пос|п|ауыл|аудан|облысы)\b"
)
TOKEN_RE = re.compile(r"[0-9a-zа-яёәғқңөұүһі]+")
DISTRICT_SUFFIXES = (
    "инский",
    "ынский",
    "ский",
    "ская",
    "ское",
    "кого",
    "кий",
    "ный",
    "ная",
    "ное",
)
GENERIC_DOCUMENT_KEYS = {
    "генплан",
    "пдп",
    "проект",
    "схема",
    "основной чертеж",
    "новый",
    "нового",
    "юго запад",
    "юго восток",
    "северо запад",
    "северо восток",
    "новый город",
}
NAME_EQUIVALENTS = {
    "кажымукан": {"хаджимукана", "им хаджимукана"},
}
STATIC_BBOXES: tuple[dict[str, object], ...] = (
    {
        "region": "Акмолинская область",
        "district": "г. Акколь",
        "locality": "Акколь",
        "bbox": (70.85, 51.95, 71.15, 52.10),
    },
    {
        "region": "Акмолинская область",
        "district": "г. Косшы",
        "locality": "Косшы",
        "bbox": (71.23, 50.97, 71.48, 51.15),
    },
    {
        "region": "Акмолинская область",
        "district": "г. Кокшетау",
        "locality": "Кокшетау",
        "bbox": (69.10, 52.95, 69.65, 53.45),
    },
    {
        "region": "Акмолинская область",
        "district": "г. Щучинск",
        "locality": "Щучинск",
        "bbox": (70.15, 52.85, 70.45, 53.05),
    },
    {
        "region": "г. Астана",
        "district": "",
        "locality": "Астана",
        "bbox": (70.95, 50.95, 71.90, 51.45),
    },
    {
        "region": "г. Алматы",
        "district": "",
        "locality": "Алматы",
        "bbox": (76.70, 43.05, 77.15, 43.45),
    },
    {
        "region": "Алматинская область",
        "district": "г. Қонаев",
        "locality": "Конаев",
        "bbox": (76.65, 43.85, 77.15, 44.20),
    },
    {
        "region": "Область Жетісу",
        "district": "г. Талдыкорган",
        "locality": "Талдыкорган",
        "bbox": (78.20, 44.85, 78.60, 45.15),
    },
    {
        "region": "Карагандинская область",
        "district": "г. Караганда",
        "locality": "Караганда",
        "bbox": (72.75, 49.65, 73.50, 50.05),
    },
    {
        "region": "Карагандинская область",
        "district": "г. Темиртау",
        "locality": "Темиртау",
        "bbox": (72.75, 49.98, 73.30, 50.25),
    },
    {
        "region": "Карагандинская область",
        "district": "г. Балхаш",
        "locality": "Балхаш",
        "bbox": (74.75, 46.70, 75.15, 47.05),
    },
    {
        "region": "Павлодарская область",
        "district": "г. Павлодар",
        "locality": "Павлодар",
        "bbox": (76.75, 52.15, 77.25, 52.45),
    },
    {
        "region": "Костанайская область",
        "district": "г. Костанай",
        "locality": "Костанай",
        "bbox": (63.45, 53.05, 63.80, 53.35),
    },
    {
        "region": "Восточно-Казахстанская область",
        "district": "г. Усть-Каменогорск",
        "locality": "Усть-Каменогорск",
        "bbox": (82.45, 49.85, 82.85, 50.15),
    },
    {
        "region": "Западно-Казахстанская область",
        "district": "г. Уральск",
        "locality": "Уральск",
        "bbox": (51.05, 50.95, 51.55, 51.35),
    },
    {
        "region": "Актюбинская область",
        "district": "г. Актобе",
        "locality": "Актобе",
        "bbox": (56.95, 50.15, 57.45, 50.45),
    },
    {
        "region": "Актюбинская область",
        "district": "г. Хромтау",
        "locality": "Хромтау",
        "bbox": (58.35, 50.18, 58.70, 50.35),
    },
    {
        "region": "Алматинская область",
        "district": "г. Каскелен",
        "locality": "Каскелен",
        "bbox": (76.55, 43.15, 76.85, 43.35),
    },
    {
        "region": "Алматинская область",
        "district": "г. Есик",
        "locality": "Есик",
        "bbox": (77.35, 43.25, 77.55, 43.42),
    },
    {
        "region": "Область Абай",
        "district": "г. Семей",
        "locality": "Семей",
        "bbox": (80.05, 50.25, 80.45, 50.55),
    },
    {
        "region": "Северо-Казахстанская область",
        "district": "Кызылжарский район",
        "locality": "Кызылжарский район",
        "bbox": (68.25, 54.45, 69.55, 55.25),
    },
    {
        "region": "Мангистауская область",
        "district": "г. Актау",
        "locality": "Актау",
        "bbox": (51.00, 43.80, 51.25, 43.95),
    },
)


class BboxResolutionError(RuntimeError):
    pass


class BboxResolver(Protocol):
    def resolve(
        self,
        locality: str,
        *,
        region: str = "",
        district: str = "",
    ) -> BoundingBox: ...


def _normal(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"\([^)]*\)", " ", value)
    value = ADMIN_WORDS_RE.sub(" ", value)
    return " ".join(TOKEN_RE.findall(value))


def _fold(value: str) -> str:
    return _normal(value).translate(KAZAKH_TRANSLATION)


def _compact(value: str) -> str:
    return _fold(value).replace(" ", "")


def _root_key(value: str) -> str:
    key = _compact(value)
    for suffix in DISTRICT_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix) + 3:
            key = key[: -len(suffix)]
            break
    return key.rstrip("ийы")


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) <= 1
    if len(left) > len(right):
        left, right = right, left
    offset = 0
    differences = 0
    for index, char in enumerate(left):
        if char == right[index + offset]:
            continue
        differences += 1
        offset = 1
        if differences > 1 or char != right[index + offset]:
            return False
    return True


def _is_name_match(wanted: str, actual: str) -> bool:
    wanted_key = _fold(wanted)
    actual_key = _fold(actual)
    if not wanted_key or not actual_key:
        return False
    if wanted_key == actual_key or wanted_key in actual_key or actual_key in wanted_key:
        return True
    if actual_key in NAME_EQUIVALENTS.get(wanted_key, set()):
        return True
    if wanted_key in NAME_EQUIVALENTS.get(actual_key, set()):
        return True
    wanted_compact = _compact(wanted)
    actual_compact = _compact(actual)
    if (
        wanted_compact == actual_compact
        or wanted_compact in actual_compact
        or actual_compact in wanted_compact
    ):
        return True
    wanted_root = _root_key(wanted)
    actual_root = _root_key(actual)
    if (
        len(wanted_root) >= 4
        and len(actual_root) >= 4
        and _edit_distance_at_most_one(wanted_root, actual_root)
    ):
        return True
    return bool(
        wanted_root
        and actual_root
        and (
            wanted_root == actual_root
            or wanted_root in actual_root
            or actual_root in wanted_root
        )
    )


def _is_generic_document_name(value: str) -> bool:
    key = _fold(value)
    compact = key.replace(" ", "")
    if key in GENERIC_DOCUMENT_KEYS:
        return True
    return any(item.replace(" ", "") in compact for item in GENERIC_DOCUMENT_KEYS)


@dataclass(slots=True)
class NominatimResolver:
    base_url: str = "https://nominatim.openstreetmap.org"
    timeout: float = 30.0
    client: httpx.Client | None = None

    def resolve(
        self,
        locality: str,
        *,
        region: str = "",
        district: str = "",
    ) -> BoundingBox:
        query = ", ".join(part for part in (locality, district, region, "Kazakhstan") if part)
        own_client = self.client is None
        client = self.client or httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        try:
            response = client.get(
                f"{self.base_url.rstrip('/')}/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 5,
                    "countrycodes": "kz",
                    "addressdetails": 1,
                },
            )
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BboxResolutionError(f"Nominatim request failed: {exc}") from exc
        finally:
            if own_client:
                client.close()

        ranked: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            raw_bbox = row.get("boundingbox")
            if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
                continue
            name = str(row.get("name") or row.get("display_name") or "")
            score = 2 if _is_name_match(locality, name) else int(_fold(locality) in _fold(name))
            ranked.append((score, row))
        if not ranked:
            raise BboxResolutionError(f"No Nominatim bbox found for {query}")
        row = max(ranked, key=lambda item: item[0])[1]
        south, north, west, east = (float(value) for value in row["boundingbox"])
        return BoundingBox(
            west=west,
            south=south,
            east=east,
            north=north,
            source="nominatim",
            label=str(row.get("display_name") or locality),
        )


@dataclass(slots=True)
class StaticBboxResolver:
    entries: tuple[dict[str, object], ...] = STATIC_BBOXES

    def resolve(
        self,
        locality: str,
        *,
        region: str = "",
        district: str = "",
    ) -> BoundingBox:
        request_values = [value for value in (locality, district, region) if value]
        best: tuple[int, dict[str, object]] | None = None
        for entry in self.entries:
            entry_region = str(entry.get("region") or "")
            entry_district = str(entry.get("district") or "")
            entry_locality = str(entry.get("locality") or "")
            region_matches = not region or _is_name_match(region, entry_region)
            score = 0
            if district and entry_district and _is_name_match(district, entry_district):
                score += 3
            if locality and _is_name_match(locality, entry_locality):
                score += 3
            if locality and _is_generic_document_name(locality) and score:
                score += 1
            if not score and any(
                _is_name_match(value, entry_locality) for value in request_values
            ):
                score = 1
            if not region_matches and score < 6:
                continue
            if not region_matches:
                score -= 1
            if score and (best is None or score > best[0]):
                best = (score, entry)
        if best is None:
            raise BboxResolutionError(
                f"No static bbox found for {locality}, {district}, {region}"
            )
        entry = best[1]
        west, south, east, north = entry["bbox"]  # type: ignore[misc]
        label = str(entry.get("locality") or locality)
        return BoundingBox(
            west=float(west),
            south=float(south),
            east=float(east),
            north=float(north),
            source="static_bbox",
            label=label,
        )


@dataclass(slots=True)
class EgknResolver:
    rest_url: str = "https://map.gov4c.kz/egkn/rest"
    timeout: float = 45.0
    verify_tls: bool = False
    client: httpx.Client | None = None

    def resolve(
        self,
        locality: str,
        *,
        region: str = "",
        district: str = "",
    ) -> BoundingBox:
        if not region or not district:
            raise BboxResolutionError("EGKN resolver requires region and district")
        own_client = self.client is None
        client = self.client or httpx.Client(
            timeout=self.timeout,
            verify=self.verify_tls,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            regions = self._get_json(client, "/map/districts", {"lang": "ru"})
            district_row = self._find_district(regions, region, district)
            settlements = self._get_json(
                client,
                "/map/ate",
                {"lang": "ru", "districtId": int(district_row["id"])},
            )
            locality_row = self._find_locality(settlements, locality)
            geometry = wkt.loads(str(locality_row["geom"]))
            if geometry.is_empty:
                raise BboxResolutionError("EGKN locality geometry is empty")
            source_srs = int(district_row.get("srs") or 4326)
            if source_srs != 4326:
                transformer = Transformer.from_crs(
                    f"EPSG:{source_srs}",
                    "EPSG:4326",
                    always_xy=True,
                )
                geometry = transform(transformer.transform, geometry)
            west, south, east, north = geometry.bounds
            return BoundingBox(
                west=float(west),
                south=float(south),
                east=float(east),
                north=float(north),
                source="egkn",
                label=str(locality_row.get("name") or locality),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, BboxResolutionError):
                raise
            raise BboxResolutionError(f"EGKN request failed: {exc}") from exc
        finally:
            if own_client:
                client.close()

    def _get_json(
        self,
        client: httpx.Client,
        path: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        response = client.get(f"{self.rest_url.rstrip('/')}{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise BboxResolutionError("Unexpected EGKN response")
        return payload

    @staticmethod
    def _find_district(
        regions: list[dict[str, Any]],
        region: str,
        district: str,
    ) -> dict[str, Any]:
        for region_row in regions:
            region_name = str(region_row.get("nameRu") or region_row.get("name") or "")
            if not _is_name_match(region, region_name):
                continue
            for row in region_row.get("districts") or []:
                name = str(row.get("nameRu") or row.get("name") or "")
                if _is_name_match(district, name):
                    return row
        raise BboxResolutionError(f"District not found in EGKN: {district}")

    @staticmethod
    def _find_locality(
        rows: list[dict[str, Any]],
        locality: str,
    ) -> dict[str, Any]:
        exact = [
            row for row in rows if _is_name_match(locality, str(row.get("name") or ""))
        ]
        if exact:
            return exact[0]
        raise BboxResolutionError(f"Locality not found in EGKN: {locality}")


@dataclass(slots=True)
class FallbackResolver:
    resolvers: list[BboxResolver]

    def resolve(
        self,
        locality: str,
        *,
        region: str = "",
        district: str = "",
    ) -> BoundingBox:
        errors: list[str] = []
        for resolver in self.resolvers:
            try:
                return resolver.resolve(locality, region=region, district=district)
            except BboxResolutionError as exc:
                errors.append(str(exc))
        raise BboxResolutionError("; ".join(errors) or "No bbox resolver configured")
