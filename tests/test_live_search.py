import pytest
from pyproj import Transformer
from shapely.affinity import translate
from shapely.geometry import box, mapping
from shapely.ops import transform as transform_geometry
from shapely.ops import unary_union

from app.config import settings
from app.live_search import LiveSearchEngine
from app.providers.egkn import DistrictInfo, EgknProviderError, ParcelRecord, SettlementInfo
from app.providers.osm import Surroundings
from app.schemas import ALL_DISTRICTS, SearchCreate


class FakeEgkn:
    district = DistrictInfo(
        id=17,
        region_name="Акмолинская область (01)",
        code="01-016",
        name="Зерендинский",
        display_name="р-н Зерендинский (01-016)",
        srs=32642,
        ate_code="115600000",
    )
    settlement = SettlementInfo(
        gid="399",
        name="Зеренда",
        kato="115630100",
        district_id=17,
        geometry=box(500_000, 5_800_000, 500_300, 5_800_300),
    )
    parcel_rows = [
        ParcelRecord(
            geometry=box(500_130, 5_800_130, 500_170, 5_800_170),
            cadastre="01160005623",
            address="Зеренда",
            land_use="ведение личного подсобного хозяйства",
            area_m2=1600,
        ),
        ParcelRecord(
            geometry=box(500_190, 5_800_130, 500_230, 5_800_170),
            cadastre="01160005645",
            address="Зеренда",
            land_use="ЛПХ",
            area_m2=1600,
        ),
    ]

    def find_district(self, region: str, district: str) -> DistrictInfo:
        return self.district

    def find_settlement(self, district_id: int, locality: str) -> SettlementInfo:
        return self.settlement

    def parcels(self, district: DistrictInfo, settlement: SettlementInfo) -> list[ParcelRecord]:
        return self.parcel_rows


class FakeOsm:
    def analyze_points(
        self, points: list[tuple[float, float]], radius_m: int
    ) -> list[Surroundings]:
        return [
            Surroundings(
                road_distance_m=40,
                power_distance_m=120,
                water_distance_m=300,
                checked=True,
            )
            for _ in points
        ]


class RecordingEgkn(FakeEgkn):
    def __init__(self) -> None:
        self.parcel_area_m2: list[float] = []
        self.parcel_gids: list[str] = []

    def parcels(self, district: DistrictInfo, settlement: SettlementInfo) -> list[ParcelRecord]:
        self.parcel_area_m2.append(settlement.geometry.area)
        self.parcel_gids.append(settlement.gid)
        return self.parcel_rows


class FakeGardenEgkn(FakeEgkn):
    parcel_rows = [
        ParcelRecord(
            geometry=box(500_130, 5_800_130, 500_170, 5_800_170),
            cadastre="011710171103",
            address="Зеленый Бор",
            land_use="для ведения садоводства",
            area_m2=1200,
            category_id="01",
        ),
        ParcelRecord(
            geometry=box(500_190, 5_800_130, 500_230, 5_800_170),
            cadastre="01171017965",
            address="Зеленый Бор",
            land_use="для ведения садоводства и дачного строительства",
            area_m2=1200,
            category_id="02",
        ),
    ]


class FakeNoPurposeAnchorEgkn(FakeEgkn):
    parcel_rows = [
        ParcelRecord(
            geometry=box(500_130, 5_800_130, 500_170, 5_800_170),
            cadastre="01160005623",
            address="Зеренда",
            land_use="индивидуальное жилищное строительство",
            area_m2=1600,
        ),
        ParcelRecord(
            geometry=box(500_190, 5_800_130, 500_230, 5_800_170),
            cadastre="01160005645",
            address="Зеренда",
            land_use="обслуживание жилого дома",
            area_m2=1600,
        ),
    ]


class FakeDistrictOnlyEgkn(FakeEgkn):
    def district_search_area(self, district: DistrictInfo) -> SettlementInfo:
        return self.settlement


class LargeLayerThenTilesEgkn(FakeEgkn):
    def parcels(self, district: DistrictInfo, settlement: SettlementInfo) -> list[ParcelRecord]:
        if settlement.gid == self.settlement.gid:
            raise EgknProviderError(
                "Слой ЕГКН превысил лимит объектов; сузьте поиск до другого населенного пункта"
            )
        return self.parcel_rows


class FakeAllDistrictEgkn(FakeDistrictOnlyEgkn):
    second_district = DistrictInfo(
        id=18,
        region_name="Акмолинская область (01)",
        code="01-017",
        name="Второй",
        display_name="р-н Второй (01-017)",
        srs=32642,
        ate_code="115700000",
    )

    def districts(self, region: str) -> list[DistrictInfo]:
        return [self.district, self.second_district]

    def district_search_area(self, district: DistrictInfo) -> SettlementInfo:
        offset = 0 if district.id == self.district.id else 1000
        return SettlementInfo(
            gid=f"district:{district.id}",
            name=district.display_name,
            kato="",
            district_id=district.id,
            geometry=translate(self.settlement.geometry, xoff=offset),
        )

    def parcels(
        self,
        district: DistrictInfo,
        settlement: SettlementInfo,
    ) -> list[ParcelRecord]:
        offset = 0 if district.id == self.district.id else 1000
        return [
            ParcelRecord(
                geometry=translate(row.geometry, xoff=offset),
                cadastre=f"{district.id}{row.cadastre}",
                address=settlement.name,
                land_use=row.land_use,
                area_m2=row.area_m2,
            )
            for row in self.parcel_rows
        ]


class BlockingRoadOsm:
    def analyze_points(
        self, points: list[tuple[float, float]], radius_m: int
    ) -> list[Surroundings]:
        return [Surroundings(road_distance_m=5, checked=True) for _ in points]


class BlockingWaterOsm:
    def analyze_points(
        self, points: list[tuple[float, float]], radius_m: int
    ) -> list[Surroundings]:
        return [Surroundings(open_water_distance_m=1, checked=True) for _ in points]


class LargePlotOsm(FakeOsm):
    def analyze_points(
        self, points: list[tuple[float, float]], radius_m: int
    ) -> list[Surroundings]:
        return [
            Surroundings(road_distance_m=80, water_distance_m=250, checked=True) for _ in points
        ]


def test_live_search_fits_ten_sotok_and_uses_neighbor_cadastre() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        area_ha=0.10,
        result_limit=5,
    )

    results = LiveSearchEngine(FakeEgkn(), FakeOsm()).search(query)

    assert 1 <= len(results) <= 5
    assert {item.nearby_cadastre for item in results} <= {
        "01160005623",
        "01160005645",
    }
    assert all(item.locality == "Зеренда" for item in results)
    assert all("Живой ЕГКН" in item.risk_notes for item in results)
    assert all(item.road_distance_m == 40 for item in results)
    assert all(item.nearby_distance_m <= 15 for item in results)


def test_live_search_prefilters_area_by_allowed_urban_plan_geometry() -> None:
    transformer = Transformer.from_crs("EPSG:32642", "EPSG:4326", always_xy=True)
    allowed_metric = box(500_000, 5_800_000, 500_180, 5_800_300)
    allowed_wgs84 = transform_geometry(transformer.transform, allowed_metric)
    query = SearchCreate(
        region="РђРєРјРѕР»РёРЅСЃРєР°СЏ РѕР±Р»Р°СЃС‚СЊ",
        district="Р—РµСЂРµРЅРґРёРЅСЃРєРёР№ СЂР°Р№РѕРЅ",
        locality="Р—РµСЂРµРЅРґР°",
        area_ha=0.10,
        result_limit=5,
        urban_plan_allowed_geojsons=[mapping(allowed_wgs84)],
    )
    egkn = RecordingEgkn()

    LiveSearchEngine(egkn, FakeOsm()).search(query)

    assert egkn.parcel_gids == ["399:urban-plan"]
    assert egkn.parcel_area_m2
    assert egkn.parcel_area_m2[0] < FakeEgkn.settlement.geometry.area


def test_genplan_first_search_does_not_require_same_purpose_anchor() -> None:
    transformer = Transformer.from_crs("EPSG:32642", "EPSG:4326", always_xy=True)
    allowed_wgs84 = transform_geometry(transformer.transform, FakeEgkn.settlement.geometry)
    query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        area_ha=0.10,
        result_limit=5,
        urban_plan_allowed_geojsons=[mapping(allowed_wgs84)],
    )

    results = LiveSearchEngine(FakeNoPurposeAnchorEgkn(), FakeOsm()).search(query)

    assert results
    assert all("Живой ЕГКН + генплан" in item.risk_notes for item in results)
    assert all("рядом не найдено участков назначения" in item.risk_notes for item in results)
    assert {item.nearby_land_use for item in results} <= {
        "индивидуальное жилищное строительство",
        "обслуживание жилого дома",
    }


def test_live_search_retries_large_egkn_layer_by_tiles() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        area_ha=0.10,
        result_limit=5,
    )

    results = LiveSearchEngine(LargeLayerThenTilesEgkn(), FakeOsm()).search(query)

    assert results
    assert {item.nearby_cadastre for item in results} <= {
        "01160005623",
        "01160005645",
    }


def test_live_search_rejects_road_crossing_ten_sotok_square() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        area_ha=0.10,
        result_limit=5,
    )

    results = LiveSearchEngine(FakeEgkn(), BlockingRoadOsm()).search(query)

    assert results == []


def test_live_search_rejects_open_water_crossing_plot() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        area_ha=0.10,
        result_limit=5,
    )

    results = LiveSearchEngine(FakeEgkn(), BlockingWaterOsm()).search(query)

    assert results == []


def test_live_search_accepts_district_boundary_without_settlement() -> None:
    query = SearchCreate(
        region="г. Шымкент",
        district="Абайский район",
        locality=None,
        area_ha=0.10,
        result_limit=3,
    )

    results = LiveSearchEngine(FakeDistrictOnlyEgkn(), FakeOsm()).search(query)

    assert results
    assert all(item.locality == FakeEgkn.settlement.name for item in results)


def test_all_district_search_returns_at_most_one_candidate_per_district() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district=ALL_DISTRICTS,
        locality=None,
        area_ha=0.10,
        result_limit=10,
    )

    results = LiveSearchEngine(FakeAllDistrictEgkn(), FakeOsm()).search(query)

    assert len(results) == 2
    assert len({item.region_chain for item in results}) == 2


def test_gardening_search_uses_only_gardening_anchors_and_twelve_sotok() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district="Бурабайский район",
        locality="Зеленый Бор",
        purpose="Садоводство",
        area_ha=0.12,
        result_limit=5,
    )

    results = LiveSearchEngine(FakeGardenEgkn(), FakeOsm()).search(query)

    assert results
    assert all("садоводства" in (item.nearby_land_use or "") for item in results)
    assert {item.nearby_category_id for item in results} <= {"01", "02"}
    assert all("0.12 га" in item.risk_notes for item in results)


def test_new_field_lph_search_uses_twenty_five_sotok_and_field_warnings() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        purpose="ЛПХ (новый поиск)",
        allotment_type="field",
        irrigation_type="non_irrigated",
        result_limit=5,
    )

    results = LiveSearchEngine(FakeEgkn(), LargePlotOsm()).search(query)

    assert query.area_ha == 0.25
    assert results
    assert all("Геометрический расчет выполнен для 0.25 га" in item.risk_notes for item in results)
    assert all("вид надела и наличие орошения" in item.risk_notes for item in results)
    assert all("Центральная канализация" in item.sewer_evidence for item in results)


def test_draft_square_is_full_ten_sotok_and_does_not_overlap_egkn_parcels() -> None:
    query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        area_ha=0.10,
        result_limit=5,
    )
    engine = LiveSearchEngine(FakeEgkn(), FakeOsm())

    drafts = engine._build_drafts(
        query,
        FakeEgkn.settlement,
        FakeEgkn.parcel_rows,
        FakeEgkn.parcel_rows,
    )
    occupied = unary_union([row.geometry for row in FakeEgkn.parcel_rows])

    assert drafts
    assert all(draft.plot.area == pytest.approx(1000, rel=1e-9) for draft in drafts)
    assert all(draft.plot.disjoint(occupied) for draft in drafts)
    assert all(FakeEgkn.settlement.geometry.covers(draft.plot) for draft in drafts)


def test_next_live_batch_excludes_coordinates_from_first_batch(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_lph_neighbor_distance_m", 500)
    engine = LiveSearchEngine(FakeEgkn(), FakeOsm())
    first_query = SearchCreate(
        region="Акмолинская область",
        district="Зерендинский район",
        locality="Зеренда",
        area_ha=0.10,
        result_limit=5,
    )
    first = engine.search(first_query)
    second_query = first_query.model_copy(
        update={"excluded_coordinates": [(item.latitude, item.longitude) for item in first]}
    )

    second = engine.search(second_query)

    first_coordinates = {(round(item.latitude, 7), round(item.longitude, 7)) for item in first}
    second_coordinates = {(round(item.latitude, 7), round(item.longitude, 7)) for item in second}
    assert len(first) == 5
    assert len(second) == 5
    assert first_coordinates.isdisjoint(second_coordinates)
