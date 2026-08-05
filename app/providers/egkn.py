import re
import time
import unicodedata
from dataclasses import dataclass
from math import cos, radians
from typing import Any

import httpx
from pyproj import Transformer
from shapely import make_valid, wkt
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as transform_geometry
from shapely.ops import unary_union

from app.config import settings


class EgknProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DistrictInfo:
    id: int
    region_name: str
    code: str
    name: str
    display_name: str
    srs: int
    ate_code: str
    kato: str = ""
    name_kz: str = ""
    display_name_kz: str = ""


@dataclass(frozen=True, slots=True)
class SettlementInfo:
    gid: str
    name: str
    kato: str
    district_id: int
    geometry: BaseGeometry


@dataclass(frozen=True, slots=True)
class SettlementOption:
    gid: str
    name: str
    kato: str


@dataclass(slots=True)
class ParcelRecord:
    geometry: BaseGeometry
    cadastre: str
    address: str
    land_use: str
    area_m2: float | None
    category_id: str | None = None


@dataclass(slots=True)
class CadastreLookupResult:
    found: bool
    cadastre: str
    source_layer: str = "egkn:u_view"
    district: DistrictInfo | None = None
    address: str = ""
    land_use: str = ""
    area_m2: float | None = None
    category_id: str | None = None
    right_type_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    geometry: BaseGeometry | None = None
    raw_properties: dict[str, Any] | None = None
    message: str | None = None


@dataclass(slots=True)
class EgknContextFeature:
    layer: str
    feature_id: str
    geometry: dict[str, Any]
    properties: dict[str, Any]


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\b(?:область|обл|район|р-н|рн|село|поселок|станция)\b", " ", value)
    value = re.sub(r"\b(?:с|п|ст|аул)\b", " ", value)
    return " ".join(re.findall(r"[а-яa-zқғңүұіөһ]+", value))


def normalize_cadastre(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).replace("–", "-").replace("—", "-")
    match = re.search(r"\b(\d{2})-(\d{3})-(\d{3})-(\d{3,})\b", normalized)
    if not match:
        return ""
    return "-".join(match.groups())


class EgknProvider:
    def __init__(self, *, verify_tls: bool | None = None) -> None:
        self.verify_tls = settings.egkn_verify_tls if verify_tls is None else verify_tls

    def _client(self) -> httpx.Client:
        return httpx.Client(
            verify=self.verify_tls,
            timeout=settings.egkn_timeout_seconds,
            headers={"User-Agent": "LandScoutKZ/0.2 (preliminary cadastral research)"},
        )

    def _get(self, url: str, *, params: dict[str, Any]) -> httpx.Response:
        """Fetch a public EGKN endpoint with one short retry for transient outages."""
        last_error: Exception | None = None
        for attempt in range(settings.egkn_request_attempts):
            try:
                with self._client() as client:
                    response = client.get(url, params=params)
                status_code = getattr(response, "status_code", 200)
                if status_code == 429 or status_code >= 500:
                    last_error = EgknProviderError(
                        f"Public EGKN service temporarily returned HTTP {status_code}"
                    )
                else:
                    if hasattr(response, "raise_for_status"):
                        response.raise_for_status()
                    return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
            if attempt + 1 < settings.egkn_request_attempts:
                time.sleep(1 + attempt)
        raise EgknProviderError(
            "Public EGKN service did not respond in time. Please retry the search later."
        ) from last_error

    def _json(self, url: str, *, params: dict[str, Any]) -> Any:
        response = self._get(url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            preview = (response.text or "")[:160].replace("\n", " ").strip()
            message = "ЕГКН вернул не JSON"
            if preview:
                message += f": {preview}"
            raise EgknProviderError(message) from exc

    def healthcheck(self) -> bool:
        params = {"service": "WFS", "version": "1.0.0", "request": "GetCapabilities"}
        try:
            response = self._get(settings.egkn_wfs_url, params=params)
        except EgknProviderError:
            return False
        return response.is_success and "WFS_Capabilities" in response.text

    def lookup_cadastre(
        self,
        cadastre: str,
        *,
        region: str | None = None,
        district: str | None = None,
        locality: str | None = None,
    ) -> CadastreLookupResult:
        cadastre_key = normalize_cadastre(cadastre)
        if not cadastre_key:
            return CadastreLookupResult(
                found=False,
                cadastre=cadastre,
                message="Кадастровый номер не указан",
            )

        districts = self._candidate_districts_for_cadastre(
            cadastre_key,
            region=region,
            district=district,
        )
        if not districts:
            return CadastreLookupResult(
                found=False,
                cadastre=cadastre_key,
                message="Район ЕГКН не найден по региону/району или коду кадастра",
            )

        last_message = "Участок не найден в публичном слое ЕГКН"
        for district_info in districts:
            try:
                search_area = self._lookup_search_area(district_info, locality=locality)
                result = self._lookup_cadastre_in_area(
                    cadastre_key,
                    district=district_info,
                    search_area=search_area,
                )
            except EgknProviderError as exc:
                last_message = str(exc)
                continue
            if result.found:
                return result
            last_message = result.message or last_message
        return CadastreLookupResult(
            found=False,
            cadastre=cadastre_key,
            message=last_message,
        )

    def _lookup_search_area(
        self,
        district: DistrictInfo,
        *,
        locality: str | None = None,
    ) -> SettlementInfo:
        if not locality:
            return self.district_search_area(district)
        try:
            return self.find_settlement(district.id, locality)
        except EgknProviderError:
            return self.district_search_area(district)

    def regions(self) -> list[dict[str, Any]]:
        return self._json(
            f"{settings.egkn_rest_url}/map/districts", params={"lang": "ru"}
        )

    def districts(self, region: str) -> list[DistrictInfo]:
        region_key = normalize_name(region)
        result: list[DistrictInfo] = []
        for region_row in self.regions():
            current_region = region_row.get("nameRu") or region_row.get("name") or ""
            current_region_key = normalize_name(current_region)
            if region_key and not (
                region_key == current_region_key
                or region_key in current_region_key
                or current_region_key in region_key
            ):
                continue
            for row in region_row.get("districts", []):
                current_name = row.get("nameRu") or row.get("name") or ""
                if not normalize_name(current_name):
                    continue
                result.append(
                    DistrictInfo(
                        id=int(row["id"]),
                        region_name=current_region,
                        code=f"{row['regionCode']}-{row['code']}",
                        name=current_name.split("(")[0].strip(),
                        display_name=f"{row.get('type', 'р-н')} {current_name}".strip(),
                        srs=int(row["srs"]),
                        ate_code=str(row.get("ate_code") or ""),
                        kato=str(row.get("kato") or ""),
                        name_kz=str(row.get("nameKz") or "").split("(")[0].strip(),
                        display_name_kz=str(row.get("nameKz") or current_name).strip(),
                    )
                )
        return result

    def find_district(self, region: str, district: str) -> DistrictInfo:
        district_key = normalize_name(district)
        if not district_key:
            raise EgknProviderError("Укажите район из справочника ЕГКН")
        matches: list[DistrictInfo] = []
        for row in self.districts(region):
            current_key = normalize_name(row.name)
            if (
                district_key == current_key
                or district_key in current_key
                or current_key in district_key
            ):
                matches.append(row)
        if not matches:
            raise EgknProviderError(f"Район не найден в каталоге ЕГКН: {district}")
        return matches[0]

    def _candidate_districts_for_cadastre(
        self,
        cadastre: str,
        *,
        region: str | None,
        district: str | None,
    ) -> list[DistrictInfo]:
        candidates: list[DistrictInfo] = []
        seen_ids: set[int] = set()

        if region and district:
            try:
                matched = self.find_district(region, district)
                candidates.append(matched)
                seen_ids.add(matched.id)
            except EgknProviderError:
                pass

        code_parts = cadastre.split("-")
        if len(code_parts) < 2:
            return candidates

        region_code, district_code = code_parts[0], code_parts[1]
        for region_row in self.regions():
            current_region = str(region_row.get("nameRu") or region_row.get("name") or "")
            for row in region_row.get("districts", []):
                if (
                    str(row.get("regionCode") or "").zfill(2) != region_code.zfill(2)
                    or str(row.get("code") or "").zfill(3) != district_code.zfill(3)
                ):
                    continue
                district_id = int(row["id"])
                if district_id in seen_ids:
                    continue
                current_name = str(row.get("nameRu") or row.get("name") or "")
                candidates.append(
                    DistrictInfo(
                        id=district_id,
                        region_name=current_region,
                        code=f"{row['regionCode']}-{row['code']}",
                        name=current_name.split("(")[0].strip(),
                        display_name=f"{row.get('type', 'р-н')} {current_name}".strip(),
                        srs=int(row["srs"]),
                        ate_code=str(row.get("ate_code") or ""),
                        kato=str(row.get("kato") or ""),
                        name_kz=str(row.get("nameKz") or "").split("(")[0].strip(),
                        display_name_kz=str(row.get("nameKz") or current_name).strip(),
                    )
                )
                seen_ids.add(district_id)
        return candidates

    def _settlement_rows(
        self, district_id: int, language: str = "ru"
    ) -> list[dict[str, Any]]:
        return self._json(
            f"{settings.egkn_rest_url}/map/ate",
            params={"lang": language, "districtId": district_id},
        )

    def settlement_options(
        self, district_id: int, language: str = "ru"
    ) -> list[SettlementOption]:
        return [
            SettlementOption(
                gid=str(row.get("gid") or ""),
                name=str(row.get("name") or "").strip(),
                kato=str(row.get("kato") or ""),
            )
            for row in self._settlement_rows(district_id, language)
            if str(row.get("name") or "").strip()
        ]

    def settlements(self, district_id: int) -> list[SettlementInfo]:
        rows = self._settlement_rows(district_id)
        result = []
        for row in rows:
            try:
                geometry = make_valid(wkt.loads(row["geom"]))
            except Exception:
                continue
            if geometry.is_empty:
                continue
            result.append(
                SettlementInfo(
                    gid=str(row["gid"]),
                    name=row["name"],
                    kato=str(row.get("kato") or ""),
                    district_id=int(row["district_id"]),
                    geometry=geometry,
                )
            )
        return result

    def find_settlement(self, district_id: int, locality: str) -> SettlementInfo:
        locality_key = normalize_name(locality)
        rows = self.settlements(district_id)
        exact = [row for row in rows if normalize_name(row.name) == locality_key]
        if exact:
            return exact[0]
        partial = [
            row
            for row in rows
            if locality_key in normalize_name(row.name) or normalize_name(row.name) in locality_key
        ]
        if not partial:
            raise EgknProviderError(f"Населенный пункт не найден в ЕГКН: {locality}")
        partial.sort(key=lambda row: abs(len(normalize_name(row.name)) - len(locality_key)))
        return partial[0]

    def district_search_area(self, district: DistrictInfo) -> SettlementInfo:
        filters = []
        if district.kato.isdigit():
            filters.append(f"kato='{district.kato}'")
        if district.ate_code.isdigit():
            filters.append(f"ate_code_ar='{district.ate_code}'")
        if not filters:
            raise EgknProviderError(
                f"Для района нет кода поиска границы ЕГКН: {district.display_name}"
            )

        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": "egkn:districts",
            "outputFormat": "application/json",
            "CQL_FILTER": " OR ".join(filters),
            "srsName": f"EPSG:{district.srs}",
            "maxFeatures": 5,
        }
        payload = self._json(settings.egkn_wfs_url, params=params)

        geometries = []
        for feature in payload.get("features", []):
            raw_geometry = feature.get("geometry")
            if not raw_geometry:
                continue
            try:
                geometry = make_valid(shape(raw_geometry))
            except Exception:
                continue
            if not geometry.is_empty:
                geometries.append(geometry)
        if not geometries:
            raise EgknProviderError(
                f"Граница района не найдена в публичном слое ЕГКН: {district.display_name}"
            )
        return SettlementInfo(
            gid=f"district:{district.id}",
            name=district.display_name,
            kato=district.kato,
            district_id=district.id,
            geometry=make_valid(unary_union(geometries)),
        )

    def parcels(self, district: DistrictInfo, settlement: SettlementInfo) -> list[ParcelRecord]:
        minx, miny, maxx, maxy = settlement.geometry.bounds
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": "egkn:u_view",
            "outputFormat": "application/json",
            "viewparams": f"district_id:{district.id}",
            "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:{district.srs}",
            "maxFeatures": settings.live_max_features,
        }
        payload = self._json(settings.egkn_wfs_url, params=params)

        features = payload.get("features", [])
        if len(features) >= settings.live_max_features:
            raise EgknProviderError(
                "Слой ЕГКН превысил лимит объектов; сузьте поиск до другого населенного пункта"
            )

        result: list[ParcelRecord] = []
        for feature in features:
            raw_geometry = feature.get("geometry")
            properties = feature.get("properties") or {}
            cadastre = str(properties.get("kad_nomer") or "").strip()
            if not raw_geometry or not cadastre:
                continue
            try:
                geometry = make_valid(shape(raw_geometry))
            except Exception:
                continue
            if geometry.is_empty or not geometry.intersects(settlement.geometry):
                continue
            area = properties.get("squ") or properties.get("shape_area")
            result.append(
                ParcelRecord(
                    geometry=geometry,
                    cadastre=cadastre,
                    address=str(properties.get("address_ru") or ""),
                    land_use=str(properties.get("tsn_ru") or properties.get("tsn") or ""),
                    area_m2=float(area) if area is not None else None,
                    category_id=str(properties.get("category_id") or "") or None,
                )
            )
        return result

    def _lookup_cadastre_in_area(
        self,
        cadastre: str,
        *,
        district: DistrictInfo,
        search_area: SettlementInfo,
    ) -> CadastreLookupResult:
        minx, miny, maxx, maxy = search_area.geometry.bounds
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": "egkn:u_view",
            "outputFormat": "application/json",
            "viewparams": f"district_id:{district.id}",
            "CQL_FILTER": f"kad_nomer='{cadastre}'",
            "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:{district.srs}",
            "srsName": f"EPSG:{district.srs}",
            "maxFeatures": 5,
        }
        payload = self._json(settings.egkn_wfs_url, params=params)
        for feature in payload.get("features", []):
            raw_geometry = feature.get("geometry")
            properties = feature.get("properties") or {}
            found_cadastre = normalize_cadastre(str(properties.get("kad_nomer") or ""))
            if not raw_geometry or found_cadastre != cadastre:
                continue
            try:
                geometry = make_valid(shape(raw_geometry))
            except Exception as exc:
                raise EgknProviderError("ЕГКН вернул некорректную геометрию участка") from exc
            if geometry.is_empty:
                continue
            transformer = Transformer.from_crs(
                f"EPSG:{district.srs}",
                "EPSG:4326",
                always_xy=True,
            )
            geometry_wgs84 = make_valid(transform_geometry(transformer.transform, geometry))
            centroid = geometry_wgs84.representative_point()
            longitude = centroid.x
            latitude = centroid.y
            area = properties.get("squ") or properties.get("shape_area")
            return CadastreLookupResult(
                found=True,
                cadastre=found_cadastre,
                source_layer="egkn:u_view",
                district=district,
                address=str(properties.get("address_ru") or ""),
                land_use=str(properties.get("tsn_ru") or properties.get("tsn") or ""),
                area_m2=float(area) if area is not None else None,
                category_id=str(properties.get("category_id") or "") or None,
                right_type_id=str(properties.get("right_type_id") or "") or None,
                latitude=float(latitude),
                longitude=float(longitude),
                geometry=geometry_wgs84,
                raw_properties=dict(properties),
            )
        return CadastreLookupResult(
            found=False,
            cadastre=cadastre,
            source_layer="egkn:u_view",
            district=district,
            message="Кадастровый номер не найден в границах района/населенного пункта",
        )

    def get_features(
        self,
        *,
        layer: str,
        bbox: tuple[float, float, float, float],
        viewparams: str | None = None,
        srs_name: str | None = None,
        max_features: int = 5000,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": layer,
            "outputFormat": "application/json",
            "bbox": ",".join(map(str, bbox)),
            "maxFeatures": max_features,
        }
        if viewparams:
            params["viewparams"] = viewparams
        if srs_name:
            params["srsName"] = srs_name
        return self._json(settings.egkn_wfs_url, params=params)

    def features_around(
        self,
        *,
        layer: str,
        latitude: float,
        longitude: float,
        radius_m: int,
        max_features: int = 25,
    ) -> list[EgknContextFeature]:
        bbox = _wgs84_bbox(latitude=latitude, longitude=longitude, radius_m=radius_m)
        payload = self.get_features(
            layer=layer,
            bbox=(*bbox, "EPSG:4326"),
            srs_name="EPSG:4326",
            max_features=max_features,
        )
        result: list[EgknContextFeature] = []
        for feature in payload.get("features", []):
            raw_geometry = feature.get("geometry")
            properties = feature.get("properties") or {}
            if not raw_geometry or not isinstance(properties, dict):
                continue
            try:
                geometry = make_valid(shape(raw_geometry))
            except Exception:
                continue
            if geometry.is_empty:
                continue
            geometry_payload = mapping(geometry)
            geometry_type = geometry_payload.get("type")
            if geometry_type not in {
                "Point",
                "MultiPoint",
                "LineString",
                "MultiLineString",
                "Polygon",
                "MultiPolygon",
            }:
                continue
            feature_id = (
                str(feature.get("id") or "")
                or str(properties.get("gid") or "")
                or str(properties.get("id") or "")
            )
            result.append(
                EgknContextFeature(
                    layer=layer,
                    feature_id=feature_id,
                    geometry=dict(geometry_payload),
                    properties=dict(properties),
                )
            )
        return result


def _wgs84_bbox(
    *,
    latitude: float,
    longitude: float,
    radius_m: int,
) -> tuple[float, float, float, float]:
    lat_delta = radius_m / 110_574
    lon_scale = max(0.2, cos(radians(latitude)))
    lon_delta = radius_m / (111_320 * lon_scale)
    return (
        longitude - lon_delta,
        latitude - lat_delta,
        longitude + lon_delta,
        latitude + lat_delta,
    )
