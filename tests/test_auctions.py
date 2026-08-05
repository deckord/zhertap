import csv
from datetime import UTC, datetime
from io import StringIO

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.auction_access as auction_access
import app.services as services
from app.apipay import ApiPayQrInvoice
from app.auction_access import (
    apply_auction_apipay_invoice,
    can_view_auction_lot,
    claim_free_auction_lot,
    has_auction_paid_access,
    start_auction_payment,
)
from app.auction_exports import (
    auction_lot_publication_history,
    export_auction_lots_csv,
)
from app.auction_service import (
    AuctionFilters,
    active_auction_lots_geojson,
    auction_category_stats,
    auction_district_stats,
    auction_lot_changes,
    auction_lot_metrics,
    auction_market_snapshot,
    auction_region_stats,
    create_subscription,
    list_auction_functional_purposes,
    list_auction_lots,
    list_favorites,
    sync_current_auctions,
    toggle_favorite,
    upsert_auction_lot,
)
from app.config import settings
from app.db import Base
from app.models import (
    Candidate,
    PaymentStatus,
    ReviewStatus,
    SearchRequest,
    SearchStatus,
    UrbanPlanStatus,
)
from app.providers.eqazyna import (
    AuctionCrawlResult,
    AuctionDetailError,
    AuctionDocumentData,
    AuctionLotData,
    EqazynaProvider,
    configured_search_statuses,
    extract_lot_urls,
    parse_lot_detail,
)
from app.services import apply_apipay_webhook

DETAIL_HTML = """
<!doctype html>
<html><body>
  <main>
    <div>Земельный участок для строительства склада</div>
    <div>№ 408331</div>
    <div>Аукцион на повышение цены (зем. ресурсы)</div>
    <p>Стартовая цена</p><p>₸ 360 045,66</p>
    <p>Начало торгов</p><p>28.05.2025 11:00</p>
    <p>Гарантийный взнос</p><p>₸ 196 600,00</p>
    <p>Цена продажи</p><p>₸ 1 370 552,00</p>
    <p>Права на землю: Продажа права аренды земельного участка</p>
    <p>Статус торгов: Состоялся</p>
    <p>Обязательно ознакомтесь с правилами проведения</p>
    <a href="https://traderesources.e-qazyna.kz/ru/source-object-view?id=1">Объект</a>
    <h2>Описание объекта</h2>
    <p>Объект продажи</p>
    <p>Земельный участок для строительства склада, Описание: участок;
       Кадастровый номер: 15-229-046-001;
       Площадь земельного участка, га: 2.9188;
       Функциональное назначение земельного участка (уровень 2): Промышленности и производственная;
       Функциональное назначение земельного участка (уровень 3): Складов;
       Функциональное назначение земельного участка (уровень 4): Земли, объекты складов;
       Цель использования: Строительство;
       Целевое назначение земельного участка: для строительства склада;
       Делимость: Делимый;</p>
    <p>Расположение объекта</p>
    <p>Северо-Казахстанская область, Аккайынский район, с. Южное</p>
    <p>Продавец</p>
    <p>ОТДЕЛ ЗЕМЕЛЬНЫХ ОТНОШЕНИЙ; ИИН/БИН: 621240000018;</p>
    <p>Балансодержатель</p>
    <p>Электронные документы</p>
    <a href="/ru/MnuFileStoreFileDownload?FileId=one">Паспорт участка.pdf</a>
    <p>Публикация</p>
    <p>Извещение о продаже опубликовано: на веб-портале - 02-05-2025</p>
  </main>
</body></html>
"""


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def lot_data(**overrides) -> AuctionLotData:
    values = {
        "source_lot_id": "294585512097000000",
        "source_url": "https://sauda.e-qazyna.kz/ru/list/294585512097000000",
        "title": "Земельный участок для строительства склада",
        "auction_number": "408331",
        "status": "Прием заявок",
        "region": "Северо-Казахстанская область",
        "district": "Аккайынский район",
        "locality": "с. Южное",
        "area_ha": 2.9188,
        "functional_purpose_level2": "Промышленности и производственная",
        "functional_purpose_level3": "Складов",
        "functional_purpose_level4": "Земли, объекты складов",
        "use_goal": "Строительство",
        "purpose": "для строительства склада",
        "start_price_kzt": 360045.66,
        "auction_starts_at": datetime(2026, 8, 1, 11, 0, tzinfo=UTC),
        "documents": [
            AuctionDocumentData(
                title="Паспорт.pdf",
                source_url="https://sauda.e-qazyna.kz/document/one",
                file_type="pdf",
            )
        ],
    }
    values.update(overrides)
    return AuctionLotData(**values)


def test_extract_lot_urls_deduplicates_cards() -> None:
    html = """
    <a href="/ru/list/111111111111">№1</a>
    <a href="/ru/list/111111111111">Описание</a>
    <a href="/ru/list/222222222222?x=1">№2</a>
    <a href="/ru/auction-info">Справка</a>
    """
    assert extract_lot_urls(html, "https://sauda.e-qazyna.kz") == [
        "https://sauda.e-qazyna.kz/ru/list/111111111111",
        "https://sauda.e-qazyna.kz/ru/list/222222222222",
    ]


def test_parse_eqazyna_lot_detail() -> None:
    lot = parse_lot_detail(
        DETAIL_HTML,
        "https://sauda.e-qazyna.kz/ru/list/294585512097000000",
        "https://sauda.e-qazyna.kz",
    )

    assert lot.source_lot_id == "294585512097000000"
    assert lot.auction_number == "408331"
    assert lot.status == "Состоялся"
    assert lot.land_rights == "Продажа права аренды земельного участка"
    assert lot.cadastre_number == "15-229-046-001"
    assert lot.area_ha == pytest.approx(2.9188)
    assert lot.functional_purpose_level2 == "Промышленности и производственная"
    assert lot.functional_purpose_level3 == "Складов"
    assert lot.functional_purpose_level4 == "Земли, объекты складов"
    assert lot.use_goal == "Строительство"
    assert lot.start_price_kzt == pytest.approx(360045.66)
    assert lot.guarantee_kzt == pytest.approx(196600)
    assert lot.sale_price_kzt == pytest.approx(1370552)
    assert lot.region == "Северо-Казахстанская область"
    assert lot.district == "Аккайынский район"
    assert lot.seller_bin == "621240000018"
    assert lot.documents[0].title == "Паспорт участка.pdf"


def test_provider_follows_public_list_and_detail_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ru/list":
            if request.url.params.get("p") == "1":
                return httpx.Response(
                    200,
                    text='<a href="/ru/list/294585512097000000">№408331</a>',
                )
            return httpx.Response(200, text="<html><body>Нет новых лотов</body></html>")
        if request.url.path == "/ru/list/294585512097000000":
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(404)

    provider = EqazynaProvider(
        base_url="https://sauda.e-qazyna.kz",
        transport=httpx.MockTransport(handler),
    )
    lots = provider.current_lots(max_pages=3, max_lots=10)

    assert len(lots) == 1
    assert lots[0].auction_number == "408331"
    assert lots[0].source_search_status == "ApplicationsAccept"


def test_configured_eqazyna_statuses_include_historical_results(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "eqazyna_sync_statuses",
        "ApplicationsAccept,FailureProtocolSigned,CancelBeforeStart",
    )

    assert configured_search_statuses() == [
        "ApplicationsAccept",
        "FailureProtocolSigned",
        "CancelBeforeStart",
    ]


def test_provider_crawls_multiple_statuses_and_marks_source_status() -> None:
    def detail_html(source_id: str, status: str) -> str:
        return DETAIL_HTML.replace("294585512097000000", source_id).replace(
            "Состоялся",
            status,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        status = request.url.params.get("searchStatus")
        page = request.url.params.get("p")
        if request.url.path == "/ru/list":
            if status == "ApplicationsAccept" and page == "1":
                return httpx.Response(200, text='<a href="/ru/list/111111">Активный</a>')
            if status == "FailureProtocolSigned" and page == "1":
                return httpx.Response(200, text='<a href="/ru/list/222222">Не состоялся</a>')
            return httpx.Response(200, text="<html><body>Нет лотов</body></html>")
        if request.url.path == "/ru/list/111111":
            return httpx.Response(200, text=detail_html("111111", "Прием заявок"))
        if request.url.path == "/ru/list/222222":
            return httpx.Response(200, text=detail_html("222222", "Не состоялся"))
        return httpx.Response(404)

    provider = EqazynaProvider(
        base_url="https://sauda.e-qazyna.kz",
        transport=httpx.MockTransport(handler),
    )
    result = provider.current_lots_with_report(
        statuses=["ApplicationsAccept", "FailureProtocolSigned"],
        max_pages=2,
        max_lots=10,
    )

    assert result.complete is True
    assert result.status_counts == {
        "ApplicationsAccept": 1,
        "FailureProtocolSigned": 1,
    }
    assert {lot.source_lot_id: lot.source_search_status for lot in result.lots} == {
        "111111": "ApplicationsAccept",
        "222222": "FailureProtocolSigned",
    }


def test_provider_applies_max_lots_across_all_statuses() -> None:
    detail_requests: list[str] = []

    def detail_html(source_id: str, status: str) -> str:
        return DETAIL_HTML.replace("294585512097000000", source_id).replace(
            "Состоялся",
            status,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        status = request.url.params.get("searchStatus")
        page = request.url.params.get("p")
        if request.url.path == "/ru/list" and page == "1":
            if status == "ApplicationsAccept":
                return httpx.Response(
                    200,
                    text=(
                        '<a href="/ru/list/111111">A1</a>'
                        '<a href="/ru/list/111112">A2</a>'
                        '<a href="/ru/list/111113">A3</a>'
                    ),
                )
            if status == "FailureProtocolSigned":
                return httpx.Response(
                    200,
                    text=(
                        '<a href="/ru/list/222221">F1</a>'
                        '<a href="/ru/list/222222">F2</a>'
                        '<a href="/ru/list/222223">F3</a>'
                    ),
                )
        if request.url.path == "/ru/list":
            return httpx.Response(200, text="<html><body>Нет лотов</body></html>")
        source_id = request.url.path.rsplit("/", 1)[-1]
        detail_requests.append(source_id)
        status_text = "Прием заявок" if source_id.startswith("111") else "Не состоялся"
        return httpx.Response(200, text=detail_html(source_id, status_text))

    provider = EqazynaProvider(
        base_url="https://sauda.e-qazyna.kz",
        transport=httpx.MockTransport(handler),
    )
    result = provider.current_lots_with_report(
        statuses=["ApplicationsAccept", "FailureProtocolSigned"],
        max_pages=2,
        max_lots=3,
    )

    assert detail_requests == ["111111", "222221", "111112"]
    assert len(result.lots) == 3
    assert result.url_count == 6
    assert result.complete is False


class StubAuctionProvider:
    def __init__(self, result: AuctionCrawlResult) -> None:
        self.result = result

    def current_lots_with_report(self, **_: object) -> AuctionCrawlResult:
        return self.result


def test_upsert_tracks_changes_without_duplicate_documents(session: Session) -> None:
    lot, created, changed = upsert_auction_lot(session, lot_data())
    session.commit()

    assert created is True
    assert changed is False
    assert len(lot.history) == 1
    assert len(lot.documents) == 1

    updated, created, changed = upsert_auction_lot(
        session,
        lot_data(status="Состоялся", sale_price_kzt=700000),
    )
    session.commit()

    assert created is False
    assert changed is True
    assert len(updated.history) == 2
    assert len(updated.documents) == 1
    assert updated.sale_price_kzt == 700000


def test_upsert_deduplicates_eqazyna_documents_with_rotating_token(
    session: Session,
) -> None:
    first_url = (
        "https://sauda.e-qazyna.kz/ru/MnuFileStoreFileDownload"
        "?FileId=K1_2607_DOC&Token=first"
    )
    second_url = (
        "https://sauda.e-qazyna.kz/ru/MnuFileStoreFileDownload"
        "?FileId=K1_2607_DOC&Token=second"
    )
    lot, _created, _changed = upsert_auction_lot(
        session,
        lot_data(
            documents=[
                AuctionDocumentData(
                    title="Схема.pdf",
                    source_url=first_url,
                    file_type="pdf",
                )
            ],
        ),
    )
    session.commit()

    updated, _created, _changed = upsert_auction_lot(
        session,
        lot_data(
            documents=[
                AuctionDocumentData(
                    title="Схема.pdf",
                    source_url=second_url,
                    file_type="pdf",
                )
            ],
        ),
    )
    session.commit()

    assert lot.id == updated.id
    assert len(updated.documents) == 1
    assert "FileId=K1_2607_DOC" in updated.documents[0].source_url


def test_upsert_tracks_eqazyna_search_status_change(session: Session) -> None:
    lot, created, changed = upsert_auction_lot(
        session,
        lot_data(source_search_status="Pending"),
    )
    session.commit()

    assert created is True
    assert changed is False
    assert len(lot.history) == 1

    updated, created, changed = upsert_auction_lot(
        session,
        lot_data(source_search_status="ApplicationsAccept"),
    )
    session.commit()
    changes = auction_lot_changes(session, updated.id)

    assert created is False
    assert changed is True
    assert len(updated.history) == 2
    assert changes[0].field_name == "source_search_status"
    assert changes[0].old_value == "Pending"
    assert changes[0].new_value == "ApplicationsAccept"


def test_export_auction_lots_csv_includes_prices_and_source_status(session: Session) -> None:
    lot, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_search_status="ApplicationsAccept",
            cadastre_number="15-001-001-001",
            area_ha=2,
            start_price_kzt=4_000_000,
            sale_price_kzt=4_500_000,
        ),
    )
    session.commit()

    rows = list(csv.DictReader(StringIO(export_auction_lots_csv([lot]))))

    assert len(rows) == 1
    assert rows[0]["source_lot_id"] == lot.source_lot_id
    assert rows[0]["source_search_status"] == "ApplicationsAccept"
    assert rows[0]["cadastre_number"] == "15-001-001-001"
    assert float(rows[0]["area_sotka"]) == pytest.approx(200)
    assert float(rows[0]["price_per_sotka_kzt"]) == pytest.approx(20_000)
    assert float(rows[0]["price_per_square_meter_kzt"]) == pytest.approx(200)


def test_auction_lot_publication_history_by_cadastre_and_source_id(
    session: Session,
) -> None:
    first, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="first-publication",
            source_url="https://sauda.e-qazyna.kz/ru/list/first-publication",
            cadastre_number="15-001-001-777",
            start_price_kzt=3_200_000,
            status="РќРµ СЃРѕСЃС‚РѕСЏР»СЃСЏ",
            source_search_status="FailureProtocolSigned",
        ),
    )
    second, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="second-publication",
            source_url="https://sauda.e-qazyna.kz/ru/list/second-publication",
            auction_number="500777",
            cadastre_number="15-001-001-777",
            start_price_kzt=2_500_000,
            status="РџСЂРёРµРј Р·Р°СЏРІРѕРє",
            source_search_status="ApplicationsAccept",
        ),
    )
    session.commit()

    cadastre_history = auction_lot_publication_history(
        session,
        cadastre_number="15-001-001-777",
    )
    source_history = auction_lot_publication_history(
        session,
        source_lot_id="second-publication",
    )

    assert cadastre_history.identifier_type == "cadastre_number"
    assert cadastre_history.lot_count == 2
    assert cadastre_history.publication_count == 2
    assert cadastre_history.failed_count == 1
    assert cadastre_history.first_start_price_kzt == pytest.approx(3_200_000)
    assert cadastre_history.last_start_price_kzt == pytest.approx(2_500_000)
    assert cadastre_history.start_price_change_kzt == pytest.approx(-700_000)
    assert cadastre_history.start_price_change_percent == pytest.approx(-21.875)
    assert [item["source_lot_id"] for item in cadastre_history.publications] == [
        first.source_lot_id,
        second.source_lot_id,
    ]
    assert source_history.identifier_type == "source_lot_id"
    assert source_history.lot_count == 1
    assert source_history.publication_count == 1
    assert source_history.publications[0]["source_lot_id"] == second.source_lot_id


def test_upsert_keeps_historical_lots_but_marks_them_inactive(session: Session) -> None:
    active, _, _ = upsert_auction_lot(
        session,
        lot_data(status=None, source_search_status="ApplicationsAccept"),
    )
    failed, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="failed-status",
            source_url="https://sauda.e-qazyna.kz/ru/list/failed-status",
            auction_number="500020",
            status=None,
            source_search_status="FailureProtocolSigned",
        ),
    )
    session.commit()

    assert active.active is True
    assert failed.active is False
    assert failed.source_search_status == "FailureProtocolSigned"


def test_auction_metrics_and_change_log_are_calculated(session: Session) -> None:
    cheap, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="cheap",
            source_url="https://sauda.e-qazyna.kz/ru/list/cheap",
            cadastre_number="15-001-001-001",
            area_ha=1,
            start_price_kzt=1_000_000,
            status="Прием заявок",
        ),
    )
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="expensive",
            source_url="https://sauda.e-qazyna.kz/ru/list/expensive",
            auction_number="500011",
            cadastre_number="15-001-001-002",
            area_ha=1,
            start_price_kzt=3_000_000,
        ),
    )
    session.commit()

    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="cheap",
            source_url="https://sauda.e-qazyna.kz/ru/list/cheap",
            cadastre_number="15-001-001-001",
            area_ha=1,
            start_price_kzt=900_000,
            status="Не состоялся",
        ),
    )
    session.commit()
    session.refresh(cheap)

    metrics = auction_lot_metrics(session, cheap)
    changes = auction_lot_changes(session, cheap.id)

    assert metrics.price_per_sotka == pytest.approx(9_000)
    assert metrics.price_per_square_meter == pytest.approx(90)
    assert metrics.district_average_price_per_sotka == pytest.approx(19_500)
    assert metrics.district_difference_percent == pytest.approx(-53.846, rel=0.01)
    assert metrics.rating > 50
    assert {change.field_name for change in changes} >= {"status", "start_price_kzt"}


def test_auction_rating_prefers_discounted_documented_lot_with_history(
    session: Session,
) -> None:
    cheap, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="cheap-current",
            source_url="https://sauda.e-qazyna.kz/ru/list/cheap-current",
            auction_number="600001",
            cadastre_number="15-777-001-001",
            region="Rating region",
            district="Rating district",
            area_ha=1,
            start_price_kzt=800_000,
            source_search_status="ApplicationsAccept",
        ),
    )
    for index, price in enumerate((1_000_000, 1_100_000), start=1):
        upsert_auction_lot(
            session,
            lot_data(
                source_lot_id=f"cheap-failed-{index}",
                source_url=f"https://sauda.e-qazyna.kz/ru/list/cheap-failed-{index}",
                auction_number=f"60000{index + 1}",
                cadastre_number="15-777-001-001",
                region="Rating region",
                district="Rating district",
                area_ha=1,
                start_price_kzt=price,
                source_search_status="FailureProtocolSigned",
            ),
        )
    weak, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="weak-current",
            source_url="https://sauda.e-qazyna.kz/ru/list/weak-current",
            auction_number="600010",
            cadastre_number="15-777-001-010",
            region="Rating region",
            district="Rating district",
            area_ha=1,
            start_price_kzt=4_000_000,
            documents=[],
            source_search_status="ApplicationsAccept",
        ),
    )
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="market-success",
            source_url="https://sauda.e-qazyna.kz/ru/list/market-success",
            auction_number="600020",
            cadastre_number="15-777-001-020",
            region="Rating region",
            district="Rating district",
            area_ha=1,
            start_price_kzt=3_000_000,
            source_search_status="SuccessProtocolSigned",
        ),
    )
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="market-failed",
            source_url="https://sauda.e-qazyna.kz/ru/list/market-failed",
            auction_number="600021",
            cadastre_number="15-777-001-021",
            region="Rating region",
            district="Rating district",
            area_ha=1,
            start_price_kzt=2_500_000,
            source_search_status="FailureProtocolSigned",
        ),
    )
    session.commit()

    cheap_metrics = auction_lot_metrics(session, cheap)
    weak_metrics = auction_lot_metrics(session, weak)

    assert cheap_metrics.price_per_sotka < weak_metrics.price_per_sotka
    assert cheap_metrics.publication_count == 3
    assert cheap_metrics.failed_count == 2
    assert cheap_metrics.document_count == 1
    assert cheap_metrics.district_lot_count == 6
    assert cheap_metrics.district_successful_count == 1
    assert cheap_metrics.district_failed_count == 3
    assert cheap_metrics.district_liquidity_percent == pytest.approx(25)
    assert cheap_metrics.rating > weak_metrics.rating
    assert cheap_metrics.rating >= 80
    assert weak_metrics.rating <= 40


def test_auction_region_district_and_category_stats(session: Session) -> None:
    upsert_auction_lot(session, lot_data())
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="business",
            source_url="https://sauda.e-qazyna.kz/ru/list/business",
            auction_number="500010",
            region="Акмолинская область",
            district="Целиноградский район",
            functional_purpose_level2="Деловая",
            area_ha=0.2,
            start_price_kzt=2_000_000,
        ),
    )
    session.commit()

    assert auction_region_stats(session)[0]["total"] == 1
    assert auction_district_stats(session, "Акмолинская область")[0]["district"] == (
        "Целиноградский район"
    )
    categories = {item["category"]: item["total"] for item in auction_category_stats(session)}
    assert categories["Деловая"] == 1
    assert categories["Промышленности и производственная"] == 1

def test_active_auction_lots_geojson_uses_payload_coordinates(session: Session) -> None:
    active_lot, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="geo-active",
            source_url="https://sauda.e-qazyna.kz/ru/list/geo-active",
            cadastre_number="01-001-001-001",
            start_price_kzt=1_200_000,
            area_ha=1.2,
            district="Geo district",
        ),
    )
    active_lot.raw_payload_json = (
        '{"object": {"latitude": 51.1282, "longitude": 71.4307}}'
    )
    inactive_lot, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="geo-inactive",
            source_url="https://sauda.e-qazyna.kz/ru/list/geo-inactive",
            source_search_status="FailureProtocolSigned",
            status=None,
        ),
    )
    inactive_lot.raw_payload_json = (
        '{"object": {"latitude": 51.2, "longitude": 71.5}}'
    )
    session.commit()

    geojson = active_auction_lots_geojson(session, AuctionFilters())

    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) == 1
    feature = geojson["features"][0]
    assert feature["geometry"] == {
        "type": "Point",
        "coordinates": [71.4307, 51.1282],
    }
    assert feature["properties"]["id"] == active_lot.id
    assert feature["properties"]["price"] == 1_200_000
    assert feature["properties"]["area"] == 1.2
    assert feature["properties"]["cadastre"] == "01-001-001-001"
    assert feature["properties"]["rating"] == auction_lot_metrics(session, active_lot).rating
    assert feature["properties"]["source_url"] == active_lot.source_url


def test_auction_market_snapshot_has_created_monthly_and_district_rankings(
    session: Session,
) -> None:
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="market-active",
            source_url="https://sauda.e-qazyna.kz/ru/list/market-active",
            district="Cheap district",
            start_price_kzt=100_000,
            area_ha=1.0,
        ),
    )
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="market-sold",
            source_url="https://sauda.e-qazyna.kz/ru/list/market-sold",
            source_search_status="SuccessProtocolSigned",
            status=None,
            district="Expensive district",
            start_price_kzt=900_000,
            area_ha=1.0,
        ),
    )
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="market-failed",
            source_url="https://sauda.e-qazyna.kz/ru/list/market-failed",
            source_search_status="FailureProtocolSigned",
            status=None,
            district="Cheap district",
            start_price_kzt=200_000,
            area_ha=1.0,
        ),
    )
    session.commit()

    snapshot = auction_market_snapshot(session)

    assert snapshot["monthly"][-1]["created"] == 3
    assert snapshot["monthly"][-1]["active"] == 1
    assert snapshot["monthly"][-1]["sold"] == 1
    assert snapshot["monthly"][-1]["failed"] == 1
    rankings = snapshot["district_price_rankings"]
    assert rankings["cheapest"][0]["district"] == "Cheap district"
    assert rankings["most_expensive"][0]["district"] == "Expensive district"


def test_full_sync_deactivates_lots_missing_from_successful_crawl(
    session: Session,
) -> None:
    current, _, _ = upsert_auction_lot(session, lot_data(source_lot_id="current"))
    stale, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="stale",
            source_url="https://sauda.e-qazyna.kz/ru/list/stale",
            auction_number="500099",
        ),
    )
    session.commit()

    result = sync_current_auctions(
        session,
        provider=StubAuctionProvider(
            AuctionCrawlResult(
                lots=[
                    lot_data(
                        source_lot_id="current",
                        auction_number="408332",
                    )
                ],
                source_lot_ids={"current"},
                url_count=1,
                pages_scanned=2,
                complete=True,
            )
        ),
        send_notifications=False,
    )

    session.refresh(current)
    session.refresh(stale)
    assert result.deactivated == 1
    assert result.crawl_complete is True
    assert current.active is True
    assert stale.active is False


def test_incomplete_sync_keeps_missing_lots_active(session: Session) -> None:
    upsert_auction_lot(session, lot_data(source_lot_id="current"))
    stale, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="stale",
            source_url="https://sauda.e-qazyna.kz/ru/list/stale",
            auction_number="500099",
        ),
    )
    session.commit()

    result = sync_current_auctions(
        session,
        provider=StubAuctionProvider(
            AuctionCrawlResult(
                lots=[lot_data(source_lot_id="current")],
                source_lot_ids={"current"},
                url_count=100,
                pages_scanned=10,
                complete=False,
            )
        ),
        send_notifications=False,
    )

    session.refresh(stale)
    assert result.deactivated == 0
    assert result.crawl_complete is False
    assert stale.active is True


def test_detail_errors_are_reported_and_do_not_deactivate_lots(
    session: Session,
) -> None:
    upsert_auction_lot(session, lot_data(source_lot_id="current"))
    stale, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="stale",
            source_url="https://sauda.e-qazyna.kz/ru/list/stale",
            auction_number="500099",
        ),
    )
    session.commit()

    result = sync_current_auctions(
        session,
        provider=StubAuctionProvider(
            AuctionCrawlResult(
                lots=[lot_data(source_lot_id="current")],
                source_lot_ids={"current", "broken"},
                url_count=2,
                pages_scanned=2,
                complete=False,
                detail_errors=[
                    AuctionDetailError(
                        source_url="https://sauda.e-qazyna.kz/ru/list/broken",
                        source_lot_id="broken",
                        message="timeout",
                    )
                ],
            )
        ),
        send_notifications=False,
    )

    session.refresh(stale)
    assert result.errors == 1
    assert result.detail_errors == 1
    assert result.deactivated == 0
    assert stale.active is True


def test_filters_favorites_and_subscriptions_are_user_scoped(session: Session) -> None:
    first, _, _ = upsert_auction_lot(session, lot_data())
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="other",
            source_url="https://sauda.e-qazyna.kz/ru/list/other",
            auction_number="500000",
            region="Акмолинская область",
            area_ha=0.15,
            start_price_kzt=5_000_000,
        ),
    )
    session.commit()

    lots, total = list_auction_lots(
        session,
        AuctionFilters(
            region="Северо-Казахстанская область",
            max_price_kzt=1_000_000,
            min_area_ha=1,
        ),
    )
    assert total == 1
    assert lots[0].id == first.id

    assert toggle_favorite(session, "100", first.id) is True
    assert [item.id for item in list_favorites(session, "100")] == [first.id]
    assert list_favorites(session, "200") == []
    assert toggle_favorite(session, "100", first.id) is False

    subscription = create_subscription(
        session,
        telegram_user_id="100",
        telegram_chat_id="100",
        language="ru",
        filters=AuctionFilters(region="Северо-Казахстанская область"),
    )
    duplicate = create_subscription(
        session,
        telegram_user_id="100",
        telegram_chat_id="100",
        language="ru",
        filters=AuctionFilters(region="Северо-Казахстанская область"),
    )
    assert duplicate.id == subscription.id


def test_functional_purpose_filter_uses_official_level_two_value(
    session: Session,
) -> None:
    industrial, _, _ = upsert_auction_lot(session, lot_data())
    upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="business",
            source_url="https://sauda.e-qazyna.kz/ru/list/business",
            auction_number="500010",
            functional_purpose_level2="Деловая",
            purpose="для строительства склада",
        ),
    )
    session.commit()

    purposes = list_auction_functional_purposes(session)
    lots, total = list_auction_lots(
        session,
        AuctionFilters(
            purpose_query="Промышленности и производственная",
        ),
    )

    assert purposes == [
        ("Деловая", 1),
        ("Промышленности и производственная", 1),
    ]
    assert total == 1
    assert lots[0].id == industrial.id


def test_free_auction_user_can_view_all_lot_cards_but_not_paid_actions(
    session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_admin_user_ids", "")
    monkeypatch.setattr(settings, "auction_free_preview_lots", 1)
    first, _, _ = upsert_auction_lot(session, lot_data())
    second, _, _ = upsert_auction_lot(
        session,
        lot_data(
            source_lot_id="second",
            source_url="https://sauda.e-qazyna.kz/ru/list/second",
            auction_number="500001",
        ),
    )
    session.commit()

    access = claim_free_auction_lot(
        session,
        telegram_user_id="preview-user",
        telegram_chat_id="preview-user",
        language="ru",
        lot_id=first.id,
    )
    repeated = claim_free_auction_lot(
        session,
        telegram_user_id="preview-user",
        telegram_chat_id="preview-user",
        language="ru",
        lot_id=second.id,
    )

    assert access.free_lot_id == first.id
    assert repeated.free_lot_id == first.id
    assert can_view_auction_lot(session, "preview-user", first.id) is True
    assert can_view_auction_lot(session, "preview-user", second.id) is True
    assert can_view_auction_lot(session, "another-user", first.id) is True


def test_auction_payment_activates_permanent_platform_access(
    session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_admin_user_ids", "")
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(
        auction_access,
        "dispatch_auction_payment_reconciliation",
        lambda _: None,
    )
    captured: dict = {}

    def fake_invoice(**kwargs) -> ApiPayQrInvoice:
        captured.update(kwargs)
        return ApiPayQrInvoice(
            invoice_id="9001",
            status="pending",
            payment_url="https://qr.kaspi.kz/auction",
        )

    monkeypatch.setattr(auction_access, "create_qr_invoice", fake_invoice)
    access = start_auction_payment(
        session,
        telegram_user_id="paid-user",
        telegram_chat_id="paid-user",
        language="ru",
    )

    assert captured["amount_kzt"] == 4990
    assert captured["request_id"] == f"auction-{access.id}"
    assert access.payment_status == PaymentStatus.awaiting_transfer.value
    assert has_auction_paid_access(session, "paid-user") is False

    result = apply_apipay_webhook(
        session,
        {
            "event": "invoice.status_changed",
            "invoice": {
                "id": "9001",
                "external_order_id": f"auction-{access.id}",
                "status": "paid",
                "amount": "4990.00",
            },
        },
    )

    assert result.activate_auction_access is True
    assert result.auction_access_id == access.id
    assert has_auction_paid_access(session, "paid-user") is True
    assert services.has_paid_access(session, "paid-user") is True
    assert has_auction_paid_access(session, "another-user") is False


def test_auction_payment_reuses_pending_land_invoice(
    session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_admin_user_ids", "")
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    created: list[str] = []
    monkeypatch.setattr(
        auction_access,
        "create_qr_invoice",
        lambda **kwargs: created.append(kwargs["request_id"])
        or ApiPayQrInvoice(
            invoice_id="auction-new",
            status="pending",
            payment_url="https://qr.kaspi.kz/auction-new",
        ),
    )

    request = SearchRequest(
        region="region",
        district="district",
        locality="locality",
        telegram_user_id="cross-user",
        telegram_chat_id="cross-chat",
        status=SearchStatus.ready.value,
        payment_status=PaymentStatus.awaiting_transfer.value,
        payment_amount_kzt=4990,
        payment_provider="apipay",
        payment_provider_invoice_id="land-900",
        payment_provider_status="pending",
        payment_provider_url="https://qr.kaspi.kz/land-existing",
    )
    session.add(request)
    session.commit()

    access = start_auction_payment(
        session,
        telegram_user_id="cross-user",
        telegram_chat_id="cross-chat",
        language="ru",
    )

    assert created == []
    assert access.payment_status == PaymentStatus.awaiting_transfer.value
    assert access.payment_provider == "apipay"
    assert access.payment_provider_invoice_id is None
    assert access.payment_provider_url == "https://qr.kaspi.kz/land-existing"


def test_paid_land_search_unlocks_auction_access(session: Session) -> None:
    request = SearchRequest(
        region="Акмолинская область",
        district="Бурабайский район",
        locality="Бурабай",
        telegram_user_id="search-paid-user",
        telegram_chat_id="search-paid-user",
        status=SearchStatus.delivered.value,
        payment_status=PaymentStatus.paid.value,
    )
    session.add(request)
    session.commit()

    assert services.has_paid_access(session, "search-paid-user") is True
    assert has_auction_paid_access(session, "search-paid-user") is True


def test_land_payment_reuses_pending_auction_invoice_and_delivers_after_payment(
    session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_admin_user_ids", "")
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "paid_search_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    sent: list[tuple[str, dict]] = []
    created: list[str] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda method, payload: sent.append((method, payload)) or {"ok": True},
    )
    monkeypatch.setattr(
        services,
        "create_qr_invoice",
        lambda **kwargs: created.append(kwargs["request_id"])
        or ApiPayQrInvoice(
            invoice_id="land-new",
            status="pending",
            payment_url="https://qr.kaspi.kz/land-new",
        ),
    )
    monkeypatch.setattr(
        auction_access,
        "dispatch_auction_payment_reconciliation",
        lambda _: None,
    )
    monkeypatch.setattr(
        auction_access,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="auction-901",
            status="pending",
            payment_url="https://qr.kaspi.kz/auction-existing",
        ),
    )

    access = start_auction_payment(
        session,
        telegram_user_id="auction-to-land",
        telegram_chat_id="auction-to-land",
        language="ru",
    )
    request = SearchRequest(
        region="region",
        district="district",
        locality="locality",
        telegram_user_id="auction-to-land",
        telegram_chat_id="auction-to-land",
        status=SearchStatus.ready.value,
    )
    session.add(request)
    session.flush()
    session.add(
        Candidate(
            request_id=request.id,
            rank=1,
            region_chain="region",
            locality="locality",
            latitude=52.0,
            longitude=71.0,
            nearby_cadastre="010000000001",
            nearby_distance_m=10,
            nearby_land_use="LPH",
            requested_area_ha=0.10,
            road_distance_m=20,
            power_evidence="ok",
            water_evidence="ok",
            sewer_evidence="ok",
            cemetery_distance_m=None,
            score=80,
            google_maps_url="https://maps.google.com/",
            review_status=ReviewStatus.approved.value,
            urban_plan_status=UrbanPlanStatus.passed.value,
        )
    )
    session.commit()

    returned = services.request_payment(session, request.id)

    assert returned.id == request.id
    assert created == []
    session.refresh(request)
    assert request.payment_provider_invoice_id is None
    assert request.payment_provider_url is None
    assert sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["url"] == (
        "https://qr.kaspi.kz/auction-existing"
    )

    result = services.apply_apipay_webhook(
        session,
        {
            "event": "invoice.status_changed",
            "invoice": {
                "id": "auction-901",
                "external_order_id": f"auction-{access.id}",
                "status": "paid",
                "amount": "4990.00",
            },
        },
    )

    session.refresh(request)
    assert result.activate_auction_access is True
    assert services.has_paid_access(session, "auction-to-land") is True
    assert request.status == SearchStatus.delivered.value
    assert any("010000000001" in payload.get("text", "") for _, payload in sent)


def test_auction_payment_rejects_wrong_amount(
    session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "telegram_admin_user_ids", "")
    monkeypatch.setattr(settings, "apipay_enabled", True)
    monkeypatch.setattr(settings, "platform_access_price_kzt", 4990)
    monkeypatch.setattr(
        auction_access,
        "dispatch_auction_payment_reconciliation",
        lambda _: None,
    )
    monkeypatch.setattr(
        auction_access,
        "create_qr_invoice",
        lambda **_: ApiPayQrInvoice(
            invoice_id="9002",
            status="pending",
            payment_url="https://qr.kaspi.kz/auction-two",
        ),
    )
    access = start_auction_payment(
        session,
        telegram_user_id="wrong-amount",
        telegram_chat_id="wrong-amount",
        language="ru",
    )

    with pytest.raises(ValueError, match="не совпадает"):
        apply_auction_apipay_invoice(
            session,
            {
                "id": "9002",
                "external_order_id": f"auction-{access.id}",
                "status": "paid",
                "amount": "1490.00",
            },
        )

    session.refresh(access)
    assert access.paid_access is False
