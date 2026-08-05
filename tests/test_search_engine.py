from app.schemas import SearchCreate
from app.search_engine import SearchEngine


class OfflineOsm:
    def nearest_cemetery(self, lat: float, lon: float, radius_m: int) -> None:
        return None


class DistrictLiveSearch:
    def __init__(self) -> None:
        self.queries: list[SearchCreate] = []

    def search(self, query: SearchCreate) -> list:
        self.queries.append(query)
        return []


def test_keeps_candidates_near_cemeteries_but_lowers_their_score() -> None:
    query = SearchCreate(
        district="Бурабайский район",
        area_ha=0.10,
        result_limit=15,
    )

    results = SearchEngine(osm_provider=OfflineOsm(), mode="demo").search(query)
    by_cadastre = {item.nearby_cadastre: item for item in results}

    assert "01171014273" in by_cadastre
    assert "01171003218" in by_cadastre
    assert "01171008003" in by_cadastre
    assert by_cadastre["01171014273"].score < by_cadastre["01171008003"].score


def test_filters_by_locality() -> None:
    query = SearchCreate(
        district="Зерендинский район",
        locality="Симферополь",
        result_limit=10,
        cemetery_buffer_m=500,
    )

    results = SearchEngine(osm_provider=OfflineOsm(), mode="demo").search(query)

    assert len(results) == 2
    assert all("Симферополь" in item.locality for item in results)


def test_live_search_accepts_city_district_without_separate_locality() -> None:
    live = DistrictLiveSearch()
    query = SearchCreate(
        region="г. Шымкент (79)",
        district="Абайский район",
        locality=None,
    )

    results = SearchEngine(
        osm_provider=OfflineOsm(),
        mode="live",
        live_engine=live,
    ).search(query)

    assert results == []
    assert live.queries == [query]
