import pytest
from shapely.geometry import box, mapping

from app.providers.egkn import (
    CadastreLookupResult,
    DistrictInfo,
    EgknProvider,
    EgknProviderError,
    SettlementInfo,
    normalize_cadastre,
)


def test_find_district_uses_matching_catalog_entry() -> None:
    provider = EgknProvider(verify_tls=False)
    provider.regions = lambda: [  # type: ignore[method-assign]
        {
            "name": "Акмолинская область",
            "nameRu": "Акмолинская область (01)",
            "districts": [
                {
                    "id": 1,
                    "regionCode": "01",
                    "code": "001",
                    "type": "р-н",
                    "nameRu": "Аккольский (01-001)",
                    "srs": 32642,
                    "ate_code": "157354",
                },
                {
                    "id": 18,
                    "regionCode": "01",
                    "code": "171",
                    "type": "р-н.",
                    "nameRu": "Бурабайский (01-171)",
                    "srs": 32642,
                    "ate_code": "153382",
                },
            ],
        }
    ]

    district = provider.find_district("Акмолинская область", "Бурабайский район")

    assert district.id == 18
    assert district.display_name == "р-н. Бурабайский (01-171)"


def test_settlement_options_do_not_require_heavy_geometry() -> None:
    provider = EgknProvider(verify_tls=False)
    provider._settlement_rows = lambda district_id, language="ru": [  # type: ignore[method-assign]
        {"name": "Бурабай", "kato": "117035100"},
        {"name": "Зеленый Бор", "kato": "117055100"},
    ]

    rows = provider.settlement_options(18)

    assert [row.name for row in rows] == ["Бурабай", "Зеленый Бор"]
    assert rows[0].kato == "117035100"


def test_normalize_cadastre_extracts_standard_number() -> None:
    assert normalize_cadastre(" кадастр № 21-318-001-001 ") == "21-318-001-001"
    assert normalize_cadastre("21–318–001–001") == "21-318-001-001"
    assert normalize_cadastre("bad") == ""


def test_egkn_non_json_response_raises_provider_error() -> None:
    provider = EgknProvider(verify_tls=False)

    class Response:
        text = "<html>temporary proxy error</html>"

        def json(self) -> dict:
            raise ValueError("not json")

    provider._get = lambda url, *, params: Response()  # type: ignore[method-assign]

    with pytest.raises(EgknProviderError, match="ЕГКН вернул не JSON") as exc_info:
        provider.regions()

    assert "temporary proxy error" in str(exc_info.value)


def test_lookup_cadastre_uses_district_code_and_u_view() -> None:
    provider = EgknProvider(verify_tls=False)
    district = DistrictInfo(
        id=318,
        region_name="г. Астана (21)",
        code="21-318",
        name="Есиль",
        display_name="р-н Есиль (21-318)",
        srs=32642,
        ate_code="",
        kato="",
    )
    provider.regions = lambda: [  # type: ignore[method-assign]
        {
            "nameRu": "г. Астана (21)",
            "districts": [
                {
                    "id": district.id,
                    "regionCode": "21",
                    "code": "318",
                    "type": "р-н",
                    "nameRu": "Есиль (21-318)",
                    "srs": district.srs,
                    "ate_code": "",
                    "kato": "",
                }
            ],
        }
    ]
    provider.district_search_area = lambda _district: SettlementInfo(  # type: ignore[method-assign]
        gid="district:318",
        name="р-н Есиль (21-318)",
        kato="",
        district_id=318,
        geometry=box(500_000, 5_660_000, 501_000, 5_661_000),
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "features": [
                    {
                        "geometry": mapping(box(500_100, 5_660_100, 500_200, 5_660_200)),
                        "properties": {
                            "kad_nomer": "21-318-001-001",
                            "address_ru": "г. Астана",
                            "tsn_ru": "ИЖС",
                            "squ": 1000,
                        },
                    }
                ]
            }

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url, params):
            assert params["typeName"] == "egkn:u_view"
            assert params["viewparams"] == "district_id:318"
            assert params["CQL_FILTER"] == "kad_nomer='21-318-001-001'"
            return Response()

    provider._client = lambda: Client()  # type: ignore[method-assign]

    result = provider.lookup_cadastre("21-318-001-001")

    assert result.found is True
    assert result.district is not None
    assert result.district.id == 318
    assert result.address == "г. Астана"
    assert result.land_use == "ИЖС"
    assert result.latitude is not None
    assert result.longitude is not None
    assert result.geometry is not None
    minx, miny, maxx, maxy = result.geometry.bounds
    assert -180 <= minx <= maxx <= 180
    assert -90 <= miny <= maxy <= 90


def test_lookup_cadastre_falls_back_to_district_when_locality_is_not_found() -> None:
    provider = EgknProvider(verify_tls=False)
    district = DistrictInfo(
        id=318,
        region_name="г. Астана (21)",
        code="21-318",
        name="Есиль",
        display_name="р-н Есиль (21-318)",
        srs=32642,
        ate_code="",
        kato="",
    )
    search_area = SettlementInfo(
        gid="district:318",
        name="р-н Есиль (21-318)",
        kato="",
        district_id=318,
        geometry=box(500_000, 5_660_000, 501_000, 5_661_000),
    )
    provider.regions = lambda: [  # type: ignore[method-assign]
        {
            "nameRu": "г. Астана (21)",
            "districts": [
                {
                    "id": district.id,
                    "regionCode": "21",
                    "code": "318",
                    "type": "р-н",
                    "nameRu": "Есиль (21-318)",
                    "srs": district.srs,
                    "ate_code": "",
                    "kato": "",
                }
            ],
        }
    ]
    provider.find_settlement = (  # type: ignore[method-assign]
        lambda _district_id, _locality: (_ for _ in ()).throw(
            EgknProviderError("settlement not found")
        )
    )
    provider.district_search_area = lambda _district: search_area  # type: ignore[method-assign]
    provider._lookup_cadastre_in_area = (  # type: ignore[method-assign]
        lambda cadastre, *, district, search_area: CadastreLookupResult(
            found=True,
            cadastre=cadastre,
            district=district,
            address=search_area.name,
        )
    )

    result = provider.lookup_cadastre("21-318-001-001", locality="Новая строка")

    assert result.found is True
    assert result.address == "р-н Есиль (21-318)"


def test_features_around_queries_context_layer_in_wgs84() -> None:
    provider = EgknProvider(verify_tls=False)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "features": [
                    {
                        "id": "freelands.1",
                        "geometry": mapping(box(71.43, 51.12, 71.44, 51.13)),
                        "properties": {
                            "gid": 1,
                            "lot_number": "FL-1",
                            "rent_condition_rus": "свободный участок",
                        },
                    }
                ]
            }

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url, params):
            assert params["typeName"] == "egkn:freelands_view"
            assert params["srsName"] == "EPSG:4326"
            assert params["bbox"].endswith(",EPSG:4326")
            assert params["maxFeatures"] == 5
            return Response()

    provider._client = lambda: Client()  # type: ignore[method-assign]

    features = provider.features_around(
        layer="egkn:freelands_view",
        latitude=51.1282,
        longitude=71.4304,
        radius_m=1200,
        max_features=5,
    )

    assert len(features) == 1
    assert features[0].feature_id == "freelands.1"
    assert features[0].geometry["type"] == "Polygon"
    assert features[0].properties["lot_number"] == "FL-1"


def test_district_search_area_uses_official_wfs_boundary() -> None:
    provider = EgknProvider(verify_tls=False)
    geometry = box(500_000, 4_600_000, 501_000, 4_601_000)

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"features": [{"geometry": mapping(geometry)}]}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url, params):
            assert params["typeName"] == "egkn:districts"
            assert "791110000" in params["CQL_FILTER"]
            return Response()

    provider._client = lambda: Client()  # type: ignore[method-assign]
    district = DistrictInfo(
        id=241,
        region_name="г. Шымкент (22)",
        code="22-327",
        name="Абайский",
        display_name="р-н. Абайский (22-327)",
        srs=32642,
        ate_code="179926",
        kato="791110000",
    )

    area = provider.district_search_area(district)

    assert area.gid == "district:241"
    assert area.name == "р-н. Абайский (22-327)"
    assert area.geometry.equals(geometry)
