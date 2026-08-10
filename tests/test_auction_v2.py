import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box, mapping
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services as services
import app.web as web
from app.auction_commercial import add_workspace_member, ensure_team_workspace
from app.auction_service import AuctionFilters, AuctionSyncResult
from app.auction_v2 import (
    AuctionV2Filters,
    AuctionV2FullSyncResult,
    AuctionV2SyncResult,
    auction_v2_analytics_payload,
    auction_v2_calendar_payload,
    auction_v2_dashboard,
    auction_v2_portfolio_payload,
    auction_v2_watchlist_matches,
    build_auction_v2_analysis,
    build_auction_v2_dossier_text,
    create_auction_v2_market_comparable,
    create_auction_v2_watchlist,
    dispatch_auction_v2_watchlist_notifications,
    eqazyna_history_publish_date_windows,
    format_auction_v2_telegram_card,
    get_auction_v2_payload,
    list_auction_v2_lots,
    list_auction_v2_map_markers,
    list_auction_v2_watchlists,
    list_auction_v2_web_notifications,
    mark_auction_v2_web_notifications_seen,
    prepare_auction_v2_worklist,
    refresh_auction_v2_infrastructure,
    seed_auction_v2_sources,
    sync_auction_v2_documents,
    sync_auction_v2_eqazyna_history_backfill,
    sync_auction_v2_full_cycle,
    sync_auction_v2_gov_kz_announcements,
    sync_auction_v2_sources,
    update_auction_v2_pipeline,
)
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import (
    Account,
    AuctionCrawlRun,
    AuctionDocument,
    AuctionEvidence,
    AuctionLot,
    AuctionLotChange,
    AuctionLotGeoCheck,
    AuctionLotV2Analysis,
    AuctionMarketComparable,
    AuctionSource,
    AuctionUserLotPipeline,
    AuctionWatchlist,
    AuctionWatchlistNotification,
    WebSession,
)
from app.providers.egkn import (
    CadastreLookupResult,
    DistrictInfo,
    EgknContextFeature,
    SettlementOption,
)
from app.providers.gov_kz import GovKzAnnouncement, GovKzAttachment, GovKzProvider
from app.providers.osm import Surroundings


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


@pytest.fixture(autouse=True)
def disable_live_auction_v2_osm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_live_osm_enabled", False)
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_live_gov_kz_enabled", False)
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_live_egkn_enabled", False)


@contextmanager
def client_for(session: Session) -> Iterator[TestClient]:
    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            client.headers.update(
                {"x-csrf-token": web.csrf_token_value("", "testclient")}
            )
            yield client
    finally:
        app.dependency_overrides.clear()


def authorize_client(client: TestClient, session: Session, account: Account) -> None:
    token = "test-web-session"
    session.add(
        WebSession(
            account_id=account.id,
            token_hash=web._hash(token),
            expires_at=web._now() + timedelta(days=1),
        )
    )
    session.commit()
    client.cookies.set("zhertap_session", token)
    client.headers.update(
        {"x-csrf-token": web.csrf_token_value(token, "testclient")}
    )


def make_admin_account() -> Account:
    return Account(
        phone="+77026669475",
        phone_verified_at=web._now(),
        password_hash=web._hash_password("password-1"),
    )


def make_non_admin_account() -> Account:
    return Account(
        phone="+77018854333",
        phone_verified_at=web._now(),
        password_hash=web._hash_password("password-1"),
    )


def make_lot(*, cadastre: str | None = "21-318-001-001", coordinates: bool = True) -> AuctionLot:
    payload = {"lat": 51.1282, "lon": 71.4304} if coordinates else {}
    lot = AuctionLot(
        source_lot_id="v2-lot-1",
        auction_number="A-100",
        title="Земельный участок под ИЖС",
        region="Астана",
        district="Есиль",
        locality="Астана",
        cadastre_number=cadastre,
        area_ha=0.10,
        land_rights="частная собственность",
        functional_purpose_level2="ИЖС",
        start_price_kzt=1_000_000,
        guarantee_kzt=150_000,
        auction_starts_at=web._now() + timedelta(days=7),
        source_url="https://sauda.e-qazyna.kz/ru/auction/100",
        raw_payload_json=json.dumps(payload),
        active=True,
    )
    lot.documents.append(
        AuctionDocument(
            title="Извещение о проведении торгов",
            source_url="https://sauda.e-qazyna.kz/doc/100.pdf",
            file_type="pdf",
        )
    )
    return lot


class FakeGovKzProvider:
    def __init__(self, announcements: list[GovKzAnnouncement]) -> None:
        self.announcements = announcements
        self.errors: list[str] = []

    def crawl_announcements(self, **_kwargs: object) -> list[GovKzAnnouncement]:
        return self.announcements


class FakeEgknProvider:
    def lookup_cadastre(
        self,
        cadastre: str,
        *,
        region: str | None = None,
        district: str | None = None,
        locality: str | None = None,
    ) -> CadastreLookupResult:
        assert cadastre == "21-318-001-001"
        assert region is not None
        return CadastreLookupResult(
            found=True,
            cadastre=cadastre,
            district=DistrictInfo(
                id=318,
                region_name="Астана",
                code="21-318",
                name="Есиль",
                display_name="р-н Есиль (21-318)",
                srs=32642,
                ate_code="",
                kato="",
            ),
            address="г. Астана, район Есиль",
            land_use="ИЖС",
            area_m2=1000.0,
            latitude=51.1282,
            longitude=71.4304,
            geometry=box(71.4299, 51.1279, 71.4309, 51.1286),
            raw_properties={"kad_nomer": cadastre, "address_ru": "г. Астана"},
        )

    def features_around(
        self,
        *,
        layer: str,
        latitude: float,
        longitude: float,
        radius_m: int,
        max_features: int = 25,
    ) -> list[EgknContextFeature]:
        assert latitude == pytest.approx(51.1282)
        assert longitude == pytest.approx(71.4304)
        assert radius_m > 0
        if layer == "egkn:freelands_view":
            return [
                EgknContextFeature(
                    layer=layer,
                    feature_id="free-1",
                    geometry=dict(mapping(box(71.4310, 51.1280, 71.4320, 51.1290))),
                    properties={
                        "gid": 1,
                        "lot_number": "FL-100",
                        "rent_condition_rus": "свободный участок",
                    },
                )
            ]
        if layer == "egkn:funczones_view":
            return [
                EgknContextFeature(
                    layer=layer,
                    feature_id="zone-1",
                    geometry=dict(mapping(box(71.4280, 51.1270, 71.4330, 51.1300))),
                    properties={"gid": 2, "category": "Жилая зона", "function": "ИЖС"},
                )
            ]
        return []


class FakeAuctionCatalogProvider:
    def regions(self) -> list[dict[str, str]]:
        return [{"name": "Region A", "nameRu": "Region A"}]

    def districts(self, region: str) -> list[DistrictInfo]:
        assert region == "Region A"
        return [
            DistrictInfo(
                id=101,
                region_name="Region A",
                code="01-101",
                name="District A",
                display_name="District A",
                srs=32642,
                ate_code="",
                kato="101000000",
            )
        ]

    def settlement_options(self, district_id: int) -> list[SettlementOption]:
        assert district_id == 101
        return [SettlementOption(gid="town-a", name="Town A", kato="101010000")]


class EmptyAuctionCatalogProvider:
    def regions(self) -> list[dict[str, str]]:
        return []

    def districts(self, region: str) -> list[DistrictInfo]:
        raise RuntimeError(f"EGKN districts unavailable for {region}")

    def settlement_options(self, district_id: int) -> list[SettlementOption]:
        raise RuntimeError(f"EGKN settlements unavailable for {district_id}")


def test_gov_kz_provider_extracts_land_auction_identifiers() -> None:
    provider = GovKzProvider(base_url="https://www.gov.kz")
    try:
        announcement = provider._announcement_from_item(
            {
                "id": 1256044,
                "projects": "vko-altai",
                "title": "Извещение о проведении земельного аукциона",
                "content": (
                    "<table><tr><td>№ лота</td><td>451657</td></tr>"
                    "<tr><td>Кадастровый номер</td><td>21-318-001-001</td></tr>"
                    "<tr><td>Право</td><td>право аренды земельного участка</td></tr></table>"
                    '<a href="/uploads/2026/auction-plan.pdf">Схема участка</a>'
                ),
                "created_date": "2026-07-31T06:00:00.000+00:00",
            },
            kind="documents",
            project="vko-altai",
        )
    finally:
        provider.close()

    assert announcement is not None
    assert announcement.source_kind == "documents"
    assert "451657" in announcement.lot_numbers
    assert "21-318-001-001" in announcement.cadastre_numbers
    assert announcement.attachments[0].url.endswith("/uploads/2026/auction-plan.pdf")


def test_auction_v2_gov_kz_announcements_create_evidence_and_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_live_gov_kz_enabled", True)
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_gov_kz_projects", "vko-altai")
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.source_lot_id = "451657"
        lot.auction_number = "451657"
        lot.source_url = "https://sauda.e-qazyna.kz/ru/auction/451657"
        session.add_all([account, lot])
        session.commit()
        build_auction_v2_analysis(session, lot, force=True)
        source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "gov_kz_akimat_announcements")
        )
        assert source is not None
        announcement = GovKzAnnouncement(
            source_url="https://www.gov.kz/memleket/entities/vko-altai/documents/details/1256044?lang=ru",
            source_kind="documents",
            project="vko-altai",
            title="Извещение о проведении земельного аукциона",
            body_text=(
                "Лот № 451657, кадастровый номер 21-318-001-001, город Астана, "
                "право аренды земельного участка."
            ),
            lot_numbers={"451657"},
            cadastre_numbers={"21-318-001-001"},
            eqazyna_urls={"https://sauda.e-qazyna.kz/ru/auction/451657"},
            attachments=[
                GovKzAttachment(
                    title="Схема земельного участка",
                    url="https://www.gov.kz/uploads/2026/schema-451657.pdf",
                    file_type="pdf",
                )
            ],
        )
        items_seen, matches, errors = sync_auction_v2_gov_kz_announcements(
            session,
            lots=[lot],
            source=source,
            provider=FakeGovKzProvider([announcement]),
        )
        session.commit()
        analysis = build_auction_v2_analysis(session, lot, force=True)
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Akimat watch",
            filters=AuctionV2Filters(base=AuctionFilters(), min_score=1),
        )
        notification_result = dispatch_auction_v2_watchlist_notifications(session)
        session.commit()
        evidence = session.scalar(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == "akimat_announcement")
        )
        gov_document = session.scalar(
            select(AuctionDocument).where(
                AuctionDocument.source_url == "https://www.gov.kz/uploads/2026/schema-451657.pdf"
            )
        )
        web_events = {
            row.event_type
            for row in session.scalars(
                select(AuctionWatchlistNotification).where(
                    AuctionWatchlistNotification.channel == "web"
                )
            ).all()
        }
        gov_status = next(
            item
            for item in json.loads(analysis.source_status_json)
            if item["code"] == "gov_kz_akimat_announcements"
        )

        assert items_seen == 1
        assert matches == 1
        assert errors == []
        assert evidence is not None
        assert evidence.confidence >= 0.9
        assert evidence.raw_payload_json is not None
        assert gov_document is not None
        assert gov_document.file_type == "pdf"
        assert gov_status["status"] == "ok"
        assert "akimat_announcement_found" in web_events
        assert notification_result.web_notifications_created == 2


def test_auction_v2_gov_kz_announcements_do_not_match_by_location_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_live_gov_kz_enabled", True)
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_gov_kz_projects", "vko-altai")
    with build_session() as session:
        lot = make_lot()
        session.add(lot)
        session.commit()
        build_auction_v2_analysis(session, lot, force=True)
        source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "gov_kz_akimat_announcements")
        )
        assert source is not None
        unrelated = GovKzAnnouncement(
            source_url="https://www.gov.kz/memleket/entities/vko-altai/documents/details/900?lang=ru",
            source_kind="documents",
            project="vko-altai",
            title="Извещение о проведении земельного аукциона",
            body_text="Астана, Есиль, земельный аукцион по другому участку.",
            lot_numbers={"999999"},
            cadastre_numbers={"99-999-999-999"},
        )

        _items_seen, matches, _errors = sync_auction_v2_gov_kz_announcements(
            session,
            lots=[lot],
            source=source,
            provider=FakeGovKzProvider([unrelated]),
        )
        evidence_count = session.scalar(
            select(func.count(AuctionEvidence.id)).where(
                AuctionEvidence.evidence_type == "akimat_announcement"
            )
        )

        assert matches == 0
        assert evidence_count == 0


def test_auction_v2_sync_verifies_cadastre_with_egkn_before_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.auction_v2.settings.auction_v2_live_egkn_enabled", True)
    monkeypatch.setattr("app.auction_v2.EgknProvider", lambda: FakeEgknProvider())
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot(coordinates=False)
        session.add_all([account, lot])
        session.commit()

        result = sync_auction_v2_sources(session, limit=10, send_notifications=False)
        session.commit()

        geo_check = session.scalar(select(AuctionLotGeoCheck))
        analysis = session.scalar(select(AuctionLotV2Analysis))
        evidence = session.scalar(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == "cadastre_boundary")
        )
        context_evidence = list(
            session.scalars(
                select(AuctionEvidence).where(
                    AuctionEvidence.evidence_type == "egkn_context_layer"
                )
            ).all()
        )
        run_statuses = dict(
            session.execute(
                select(AuctionSource.code, AuctionCrawlRun.status).join(
                    AuctionCrawlRun,
                    AuctionCrawlRun.source_id == AuctionSource.id,
                )
            ).all()
        )
        source_statuses = json.loads(analysis.source_status_json) if analysis else []
        egkn_status = next(
            item for item in source_statuses if item["code"] == "egkn_public_map"
        )
        risk_codes = {item["code"] for item in json.loads(analysis.risk_flags_json)}
        readiness = {item["code"]: item for item in json.loads(analysis.readiness_json)}

        assert result.lots_checked == 1
        assert run_statuses["egkn_public_map"] == "success"
        assert geo_check is not None
        assert geo_check.cadastre_status == "verified"
        assert geo_check.coordinate_status == "found"
        assert geo_check.latitude == pytest.approx(51.1282)
        assert evidence is not None
        assert evidence.status == "found"
        evidence_payload = json.loads(evidence.raw_payload_json or "{}")
        assert evidence_payload["geometry_srs"] == "EPSG:4326"
        assert evidence_payload["geometry_geojson"]["type"] == "Polygon"
        assert len(context_evidence) == 4
        assert {row.status for row in context_evidence} == {"found", "missing"}
        assert "ИЖС" in (evidence.value_text or "")
        assert egkn_status["status"] == "ok"
        assert readiness["cadastre"]["status"] == "done"
        assert "no_coordinates" not in risk_codes
        map_data = list_auction_v2_map_markers(
            session,
            AuctionV2Filters(base=AuctionFilters()),
            account_id=account.id,
        )
        assert map_data["with_boundaries"] == 1
        assert map_data["markers"][0]["boundary"]["type"] == "Polygon"
        assert map_data["egkn_layer_total"] == 2
        assert map_data["egkn_layer_counts"]["free_lands"] == 1
        assert map_data["egkn_layer_counts"]["functional_zones"] == 1
        assert {
            item["layer_code"] for item in map_data["egkn_layers"]
        } == {"free_lands", "functional_zones"}


def test_prepare_auction_v2_worklist_builds_fast_user_layer() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.source_search_status = "ApplicationsAccept"
        session.add_all([account, lot])
        session.commit()

        result = prepare_auction_v2_worklist(session, limit=10, send_notifications=False)
        session.commit()

        analysis = session.scalar(select(AuctionLotV2Analysis))
        evidence_types = {
            row
            for row in session.scalars(
                select(AuctionEvidence.evidence_type).where(AuctionEvidence.lot_id == lot.id)
            ).all()
        }

        assert result.lots_checked == 1
        assert result.analyses_updated == 1
        assert analysis is not None
        assert "source_query" in evidence_types
        assert "market_query" in evidence_types


def test_prepare_auction_v2_worklist_limits_document_evidence() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        for index in range(30):
            lot.documents.append(
                AuctionDocument(
                    title=f"Дополнительный документ {index}",
                    source_url=f"https://sauda.e-qazyna.kz/doc/extra-{index}.pdf",
                    file_type="pdf",
                )
            )
        session.add_all([account, lot])
        session.commit()

        prepare_auction_v2_worklist(session, limit=10, send_notifications=False)
        session.commit()

        document_evidence_count = session.scalar(
            select(func.count(AuctionEvidence.id)).where(
                AuctionEvidence.lot_id == lot.id,
                AuctionEvidence.evidence_type == "official_document",
            )
        )
        summary = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.lot_id == lot.id,
                AuctionEvidence.evidence_type == "official_document_summary",
            )
        )

        assert document_evidence_count == 12
        assert summary is not None
        assert "Всего документов: 31" in (summary.value_text or "")


def test_auction_v2_admin_list_renders_prepared_analysis_sources_and_evidence() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.source_search_status = "ApplicationsAccept"
        session.add_all([account, lot])
        session.commit()
        build_auction_v2_analysis(session, lot, force=True)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2")

        analysis = session.scalar(select(AuctionLotV2Analysis))
        assert response.status_code == 200
        assert "<h1>Лоты</h1>" in response.text
        assert "Найдите участок, оцените срок" in response.text
        assert "Земельный участок под ИЖС" in response.text
        assert "Риск:" in response.text
        assert "Следующее действие:" in response.text
        assert "Открыть лот" in response.text
        assert "Показать лоты" in response.text
        assert "Рынок</span>" in response.text
        assert "/cabinet/auctions-v2/map" in response.text
        assert "Регионы</strong> загружаются сразу" in response.text
        eqazyna_source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "eqazyna_current_lots")
        )
        assert eqazyna_source is not None
        assert session.scalar(select(AuctionEvidence)) is not None
        assert analysis is not None
        assert analysis.score > 0
        payload = get_auction_v2_payload(session, lot.id, account_id=account.id)
        assert payload is not None
        assert payload.deadline_status == "normal"
        assert "торгов" in payload.deadline_label
        assert payload.eqazyna_status_label == "Прием заявок"
        assert payload.lot_scope == "future"
        assert payload.lot_scope_label == "Будущие"
        assert payload.map_embed_url is not None
        assert "openstreetmap.org/export/embed.html" in payload.map_embed_url
        assert payload.osm_map_url is not None
        assert analysis.recommended_action in {
            "prepare_official_review",
            "watch_and_check",
            "manual_check",
        }


def test_auction_v2_empty_admin_list_explains_empty_catalog_without_diagnostics() -> None:
    with build_session() as session:
        account = make_admin_account()
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2")

        assert response.status_code == 200
        assert "Лоты не найдены" in response.text
        assert "В базе v2 сейчас: 0 всего, 0 активных" in response.text
        assert "Показать все v2" in response.text
        assert "Состояние обновления источников" not in response.text
        assert "Здесь видно, работал ли сбор" not in response.text


def test_auction_v2_empty_search_explains_archive_match() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.active = False
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2?q=21318001001")

        assert response.status_code == 200
        assert "Номер найден не в активных, а в архиве" in response.text
        assert "Активные: <b>0</b>" in response.text
        assert "Архив: <b>1</b>" in response.text
        assert "Искать этот номер во всех лотах" in response.text
        assert "Проверить архив" in response.text


def test_auction_v2_empty_search_explains_filters_hide_active_match() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.source_search_status = "ApplicationsAccept"
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2?q=A-100&region=Алматы")

        assert response.status_code == 200
        assert "Номер есть в активных, но его скрыли фильтры" in response.text
        assert "Активные: <b>1</b>" in response.text
        assert "Убрать только поиск" in response.text


def test_auction_v2_map_markers_use_filtered_lots_and_geo_status() -> None:
    with build_session() as session:
        account = make_admin_account()
        mapped_lot = make_lot()
        missing_lot = make_lot(coordinates=False)
        missing_lot.source_lot_id = "lot-map-missing"
        missing_lot.title = "Лот без координат"
        session.add_all([account, mapped_lot, missing_lot])
        session.commit()
        build_auction_v2_analysis(session, mapped_lot, force=True)
        build_auction_v2_analysis(session, missing_lot, force=True)

        map_data = list_auction_v2_map_markers(
            session,
            AuctionV2Filters(base=AuctionFilters()),
            account_id=account.id,
            limit=20,
        )

        markers = map_data["markers"]
        assert map_data["total"] == 2
        assert map_data["mapped"] == 1
        assert map_data["without_coordinates"] == 1
        assert isinstance(markers, list)
        assert markers[0]["id"] == mapped_lot.id
        assert markers[0]["latitude"] == pytest.approx(51.1282)
        assert markers[0]["longitude"] == pytest.approx(71.4304)
        assert markers[0]["url"] == f"/cabinet/auctions-v2/{mapped_lot.id}"
        assert markers[0]["scope"] == "future"


def test_auction_v2_admin_can_open_map_view() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2/map")

        assert response.status_code == 200
        assert "Карта земельных аукционов" in response.text
        assert "auction-v2-map-data" in response.text
        assert "auction-v2-egkn-layer-data" in response.text
        assert "/static/leaflet.css" in response.text
        assert "/static/leaflet.js" in response.text
        assert "auction-v2-leaflet-map" in response.text
        assert "Границы ЕГКН" in response.text
        assert ">Список</a>" in response.text
        assert "auction-v2-map.js" in response.text
        assert "auction-v2-map.js?v=20260803d" in response.text
        assert "auction-v2-map-svg" not in response.text
        assert lot.id in response.text
        assert "51.1282" in response.text


def test_auction_v2_admin_list_renders_sort_control() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get(
                "/cabinet/auctions-v2",
                params={"sort_by": "price_per_sotka_asc"},
            )

    assert response.status_code == 200
    assert 'name="sort_by"' in response.text
    assert 'value="price_per_sotka_asc" selected' in response.text


def test_auction_v2_web_list_does_not_build_missing_analysis_in_request() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2")

        analysis_count = session.scalar(select(func.count(AuctionLotV2Analysis.id)))

    assert response.status_code == 200
    assert analysis_count == 0


def test_auction_v2_filters_match_legacy_district_prefixes() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.region = "Область Абай"
        lot.district = "УСТАРЕВШЕЕ - Бородулихинский район"
        lot.locality = "с. Новая Шульба"
        session.add_all([account, lot])
        session.commit()
        lot_id = lot.id
        build_auction_v2_analysis(session, lot, force=True)
        session.commit()

        rows, total = list_auction_v2_lots(
            session,
            AuctionV2Filters(
                base=AuctionFilters(
                    region="Область Абай",
                    district="Бородулихинский район",
                    locality="Новая Шульба",
                )
            ),
            account_id=account.id,
        )

    assert total == 1
    assert [item.lot.id for item in rows] == [lot_id]


def test_auction_v2_analytics_groups_regions_districts_and_localities() -> None:
    with build_session() as session:
        first_lot = make_lot()
        first_lot.source_lot_id = "analytics-1"
        first_lot.region = "Область Абай"
        first_lot.district = "УСТАРЕВШЕЕ - Бородулихинский район"
        first_lot.locality = "с. Новая Шульба"
        first_lot.start_price_kzt = 900_000
        second_lot = make_lot(coordinates=False)
        second_lot.source_lot_id = "analytics-2"
        second_lot.region = "Область Абай"
        second_lot.district = "Бородулихинский район"
        second_lot.locality = "с. Новая Шульба"
        second_lot.start_price_kzt = 1_500_000
        second_lot.active = False
        session.add_all([first_lot, second_lot])
        session.flush()
        session.add(
            AuctionEvidence(
                lot_id=first_lot.id,
                evidence_type="cadastre_boundary",
                status="found",
                title="Граница участка",
            )
        )
        session.commit()
        build_auction_v2_analysis(session, first_lot, force=True)
        build_auction_v2_analysis(session, second_lot, force=True)

        payload = auction_v2_analytics_payload(
            session,
            region="Область Абай",
            district="Бородулихинский район",
        )

    assert payload["totals"]["total"] == 2
    assert payload["totals"]["archive"] == 1
    assert payload["totals"]["boundaries"] == 1
    assert payload["totals"]["median_price_per_sotka_text"] != "—"
    district_rows = payload["district_rows"]
    locality_rows = payload["locality_rows"]
    assert district_rows[0]["label"] == "Бородулихинский район"
    assert district_rows[0]["region"] == "Область Абай"
    assert district_rows[0]["total"] == 2
    assert "district=" in district_rows[0]["href"]
    assert locality_rows[0]["label"] == "с. Новая Шульба"


def test_auction_v2_admin_can_open_analytics_view() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.region = "Область Абай"
        lot.district = "УСТАРЕВШЕЕ - Бородулихинский район"
        lot.locality = "с. Новая Шульба"
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2/analytics")

    assert response.status_code == 200
    assert "<h1>Рынок</h1>" in response.text
    assert "Сравнение территорий" in response.text
    assert "Бородулихинский район" in response.text
    assert "Новая Шульба" in response.text
    assert "Открыть лоты" in response.text
    assert "site-catalogs.js?v=20260804b" in response.text


def test_auction_catalog_routes_use_egkn_catalog_when_lots_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "catalog_provider", FakeAuctionCatalogProvider())
    with build_session() as session:
        account = make_admin_account()
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            regions = client.get("/cabinet/auctions/catalog/regions")
            districts = client.get(
                "/cabinet/auctions/catalog/districts",
                params={"region": "Region A"},
            )
            localities = client.get(
                "/cabinet/auctions/catalog/localities",
                params={
                    "region": "Region A",
                    "district": "District A",
                    "district_id": 101,
                },
            )

    assert regions.status_code == 200
    assert districts.status_code == 200
    assert localities.status_code == 200
    assert regions.json() == [{"value": "Region A", "label": "Region A"}]
    assert districts.json() == [
        {"id": 101, "value": "District A", "label": "District A"}
    ]
    assert localities.json() == [
        {"value": "Town A", "label": "Town A · КАТО 101010000"}
    ]


def test_auction_catalog_routes_overlay_eqazyna_counts_on_egkn_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "catalog_provider", FakeAuctionCatalogProvider())
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.region = "Region A"
        lot.district = "District A"
        lot.locality = "Town A"
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            regions = client.get("/cabinet/auctions/catalog/regions").json()
            districts = client.get(
                "/cabinet/auctions/catalog/districts",
                params={"region": "Region A"},
            ).json()
            localities = client.get(
                "/cabinet/auctions/catalog/localities",
                params={
                    "region": "Region A",
                    "district": "District A",
                    "district_id": 101,
                },
            ).json()

    assert regions == [{"value": "Region A", "label": "Region A (1)"}]
    assert districts == [
        {"id": 101, "value": "District A", "label": "District A (1)"}
    ]
    assert localities == [
        {"value": "Town A", "label": "Town A · КАТО 101010000 (1)"}
    ]


def test_auction_catalog_routes_respect_lot_scope_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "catalog_provider", FakeAuctionCatalogProvider())
    with build_session() as session:
        account = make_admin_account()
        active = make_lot()
        active.region = "Region A"
        active.district = "District A"
        active.locality = "Town A"
        archived = make_lot()
        archived.source_lot_id = "catalog-archive"
        archived.auction_number = "CAT-2"
        archived.region = "Region A"
        archived.district = "District A"
        archived.locality = "Town A"
        archived.active = False
        archived.source_search_status = "FailureProtocolSigned"
        archived.auction_starts_at = web._now() - timedelta(days=3)
        session.add_all([account, active, archived])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            active_regions = client.get("/cabinet/auctions/catalog/regions").json()
            archive_regions = client.get(
                "/cabinet/auctions/catalog/regions",
                params={"lot_scope": "archive"},
            ).json()
            all_regions = client.get(
                "/cabinet/auctions/catalog/regions",
                params={"lot_scope": "all"},
            ).json()
            archive_districts = client.get(
                "/cabinet/auctions/catalog/districts",
                params={"region": "Region A", "lot_scope": "archive"},
            ).json()
            archive_localities = client.get(
                "/cabinet/auctions/catalog/localities",
                params={
                    "region": "Region A",
                    "district": "District A",
                    "district_id": 101,
                    "lot_scope": "archive",
                },
            ).json()

    assert active_regions == [{"value": "Region A", "label": "Region A (1)"}]
    assert archive_regions == [{"value": "Region A", "label": "Region A (1)"}]
    assert all_regions == [{"value": "Region A", "label": "Region A (2)"}]
    assert archive_districts == [
        {"id": 101, "value": "District A", "label": "District A (1)"}
    ]
    assert archive_localities == [
        {"value": "Town A", "label": "Town A · КАТО 101010000 (1)"}
    ]


def test_auction_catalog_routes_use_local_geo_fallback_when_egkn_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "catalog_provider", EmptyAuctionCatalogProvider())
    with build_session() as session:
        account = make_admin_account()
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            regions = client.get("/cabinet/auctions/catalog/regions")
            districts = client.get(
                "/cabinet/auctions/catalog/districts",
                params={"region": "Акмолинская область"},
            )
            localities = client.get(
                "/cabinet/auctions/catalog/localities",
                params={
                    "region": "Акмолинская область",
                    "district": "Бурабайский район",
                },
            )

    assert regions.status_code == 200
    assert districts.status_code == 200
    assert localities.status_code == 200
    assert any(row["value"] == "Акмолинская область" for row in regions.json())
    assert any(row["value"] == "Бурабайский район" for row in districts.json())
    assert any(row["value"] == "Златополье" for row in localities.json())


def test_auction_v2_detail_and_pipeline_update() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.source_search_status = "ApplicationsAccept"
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            detail_response = client.get(f"/cabinet/auctions-v2/{lot.id}")
            update_response = client.post(
                f"/cabinet/auctions-v2/{lot.id}/pipeline",
                data={
                    "stage": "decided_to_participate",
                    "max_bid_kzt": "1200000",
                    "road_cost_kzt": "250000",
                    "utilities_cost_kzt": "400000",
                    "project_cost_kzt": "",
                    "strategy": "resale",
                    "planned_purchase_price_kzt": "1200000",
                    "expected_exit_value_kzt": "2400000",
                    "holding_months": "12",
                    "financing_cost_kzt": "100000",
                    "contingency_percent": "10",
                    "inspection_status": "completed",
                    "inspection_access_ok": "true",
                    "inspection_flat_terrain": "true",
                    "inspection_conclusion": "Подъезд хороший, участок подходит",
                    "notes": "Проверить красные линии перед E-Qazyna",
                    "pinned": "true",
                },
                follow_redirects=False,
            )
            portfolio_response = client.get("/cabinet/auctions-v2/portfolio")
            activity_response = client.post(
                f"/cabinet/auctions-v2/{lot.id}/activity",
                data={
                    "kind": "expert_request",
                    "body": "Проверить технические условия по электричеству",
                },
                follow_redirects=False,
            )
            activity_detail_response = client.get(f"/cabinet/auctions-v2/{lot.id}")

        pipeline = session.scalar(select(AuctionUserLotPipeline))
        assert detail_response.status_code == 200
        assert "Решение перед участием" in detail_response.text
        assert "Главное сейчас" in detail_response.text
        assert "Что еще мешает идти на официальный портал" in detail_response.text
        assert "Официальный лот" in detail_response.text
        assert "Личный лимит" in detail_response.text
        assert "Зафиксировать решение" in detail_response.text
        assert "Сохранить решение" in detail_response.text
        assert "Моя работа" in detail_response.text
        assert "Изменить решение" in detail_response.text
        assert "Экономика" in detail_response.text
        assert "Документы" in detail_response.text
        assert "Извещение о проведении торгов" in detail_response.text
        assert "Ссылка найдена" in detail_response.text
        assert "Пока только ссылка" in detail_response.text
        assert "Файл хранится у источника" in detail_response.text
        assert "Открыть документ" in detail_response.text
        assert "Статус E-Qazyna" in detail_response.text
        assert "Прием заявок" in detail_response.text
        assert "Можно готовить проверку и заявку" in detail_response.text
        assert "E-Qazyna" in detail_response.text
        assert update_response.status_code == 303
        assert portfolio_response.status_code == 200
        assert "<h1>Сделки</h1>" in portfolio_response.text
        assert "Ожидаемая прибыль" in portfolio_response.text
        assert activity_response.status_code == 303
        assert "Комната сделки" in activity_detail_response.text
        assert "Проверить технические условия по электричеству" in activity_detail_response.text
        assert update_response.headers["location"] == (
            f"/cabinet/auctions-v2/{lot.id}?pipeline=saved"
        )
        assert pipeline is not None
        assert pipeline.stage == "decided_to_participate"
        assert pipeline.decision == "participate"
        assert pipeline.max_bid_kzt == 1_200_000
        assert json.loads(pipeline.costs_json) == {"road": 250000.0, "utilities": 400000.0}
        assert json.loads(pipeline.investment_json)["strategy"] == "resale"
        assert json.loads(pipeline.inspection_json)["status"] == "completed"
        assert json.loads(pipeline.activity_json)[0]["kind"] == "expert_request"
        assert pipeline.pinned is True
        payload = get_auction_v2_payload(session, lot.id, account_id=account.id)
        assert payload is not None
        assert payload.next_actions
        assert payload.cost_estimate["known_extra_costs_kzt"] == 650000.0
        assert payload.cost_estimate["cash_before_auction_kzt"] == 150000.0
        assert payload.cost_estimate["cash_after_win_kzt"] == 1700000.0
        assert payload.investment_case["all_in_cost_kzt"] == 2015000.0
        assert payload.investment_case["expected_profit_kzt"] == 385000.0
        assert payload.field_inspection["status"] == "completed"
        assert payload.field_inspection["checked_count"] == 2
        portfolio = auction_v2_portfolio_payload(session, account_id=account.id)
        assert portfolio["totals"]["tracked"] == 1
        assert portfolio["totals"]["total_budget_kzt"] == 2015000.0
        assert portfolio["totals"]["expected_profit_kzt"] == 385000.0
        assert payload.eqazyna_status_label == "Прием заявок"
        assert payload.next_actions[0]["status"] in {"manual", "missing", "warning", "external"}


def test_auction_v2_detail_hides_duplicate_documents_with_rotating_tokens() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.documents[0].title = "Plan.pdf"
        lot.documents[0].source_url = (
            "https://sauda.e-qazyna.kz/ru/MnuFileStoreFileDownload"
            "?FileId=K1_2607_DUP&Token=first"
        )
        lot.documents.append(
            AuctionDocument(
                title="Plan.pdf",
                source_url=(
                    "https://sauda.e-qazyna.kz/ru/MnuFileStoreFileDownload"
                    "?FileId=K1_2607_DUP&Token=second"
                ),
                file_type="pdf",
            )
        )
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            detail_response = client.get(f"/cabinet/auctions-v2/{lot.id}")

        assert detail_response.status_code == 200
        assert detail_response.text.count("auction-v2-document-row") == 1


def test_auction_v2_admin_can_add_market_comparable_from_detail() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post(
                f"/cabinet/auctions-v2/{lot.id}/market-comparables",
                data={
                    "source_name": "OLX",
                    "source_url": "https://www.olx.kz/d/obyavlenie/market-1",
                    "title": "Похожий участок",
                    "area_ha": "0.12",
                    "price_kzt": "2400000",
                    "listing_status": "active",
                },
                follow_redirects=False,
            )

        comparable = session.scalar(select(AuctionMarketComparable))
        analysis = session.scalar(select(AuctionLotV2Analysis))

        assert response.status_code == 303
        assert response.headers["location"] == f"/cabinet/auctions-v2/{lot.id}?market=saved"
        assert comparable is not None
        assert comparable.source_name == "OLX"
        assert comparable.price_per_sotka == pytest.approx(200_000)
        assert analysis is not None
        assert "Рыночные аналоги" in analysis.summary


def test_auction_v2_document_sync_downloads_found_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"%PDF-1.4 zhertap document"
    storage_dir = Path("var/test-auction-documents")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://sauda.e-qazyna.kz/doc/100.pdf"
        return httpx.Response(200, content=content)

    monkeypatch.setattr(
        "app.auction_v2.settings.auction_v2_document_storage_dir",
        str(storage_dir),
    )
    with build_session() as session:
        lot = make_lot()
        session.add(lot)
        session.commit()
        document_id = lot.documents[0].id

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = sync_auction_v2_documents(
                session,
                limit=10,
                enabled=True,
                client=client,
            )
        session.commit()

        document = session.get(AuctionDocument, document_id)
        assert document is not None
        assert result.checked == 1
        assert result.downloaded == 1
        assert result.errors == 0
        assert document.storage_status == "downloaded"
        assert document.download_error is None
        assert document.content_sha256 is not None
        assert document.downloaded_at is not None
        assert document.local_path is not None
        assert Path(document.local_path).read_bytes() == content


def test_auction_v2_official_readiness_tracks_external_steps_and_user_decision() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        initial = get_auction_v2_payload(session, lot.id, account_id=account.id, force=True)
        assert initial is not None
        initial_steps = {item["code"]: item for item in initial.official_readiness}

        assert initial_steps["external_ecp"]["status"] == "external"
        assert initial_steps["external_ncalayer"]["status"] == "external"
        assert initial_steps["external_bank_details"]["status"] == "external"
        assert initial_steps["guarantee"]["status"] == "done"
        assert initial_steps["deadline"]["status"] == "done"
        assert initial_steps["personal_limit"]["status"] == "manual"
        assert initial_steps["decision"]["status"] == "manual"
        assert initial_steps["official_boundary"]["status"] == "external"
        workflow_steps = {item["code"]: item for item in initial.buyer_workflow}
        assert workflow_steps["find_lot"]["status"] == "done"
        assert workflow_steps["cadastre_check"]["source"] == "ЕГКН / публичная кадастровая карта"
        assert workflow_steps["official_handoff"]["status"] == "external"
        process_rows = {item["code"]: item for item in initial.manual_process}
        assert process_rows["eqazyna"]["site"] == "E-Qazyna"
        assert process_rows["eqazyna"]["required"] is True
        assert process_rows["documents"]["status"] == "done"
        assert process_rows["market_comparison"]["importance"] == "optional"
        assert "не источник аукционов" in process_rows["market_comparison"]["note"]
        assert process_rows["official_handoff"]["status"] == "external"
        assert initial.manual_process_counts["required"] >= 6
        assert initial.manual_process_counts["external"] >= 1

        update_auction_v2_pipeline(
            session,
            account_id=account.id,
            lot_id=lot.id,
            stage="ready_for_official_site",
            max_bid_kzt=1_250_000,
            notes="готов к внешней проверке",
            pinned=True,
        )
        session.commit()

        ready = get_auction_v2_payload(session, lot.id, account_id=account.id, force=True)
        assert ready is not None
        ready_steps = {item["code"]: item for item in ready.official_readiness}

        assert ready_steps["personal_limit"]["status"] == "done"
        assert ready_steps["decision"]["status"] == "done"
        assert "1 250 000" in ready_steps["personal_limit"]["detail"]


def test_auction_v2_observer_catalog_and_paid_features_are_separated() -> None:
    with build_session() as session:
        account = make_non_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            catalog = client.get("/cabinet/auctions-v2")
            protected_responses = [
                client.get("/cabinet/auctions-v2/map", follow_redirects=False),
                client.get("/cabinet/auctions-v2/analytics", follow_redirects=False),
                client.post(
                    "/cabinet/auctions-v2/watchlists",
                    data={"name": "x"},
                    follow_redirects=False,
                ),
                client.post(
                    "/cabinet/auctions-v2/watchlists/1/active",
                    data={"active": "false"},
                    follow_redirects=False,
                ),
                client.post(
                    "/cabinet/auctions-v2/notifications/seen",
                    follow_redirects=False,
                ),
                client.get(
                    f"/cabinet/auctions-v2/{lot.id}/dossier.txt",
                    follow_redirects=False,
                ),
                client.get(
                    f"/cabinet/auctions-v2/{lot.id}",
                    follow_redirects=False,
                ),
                client.post(
                    f"/cabinet/auctions-v2/{lot.id}/pipeline",
                    data={"stage": "watching"},
                    follow_redirects=False,
                ),
                client.post(
                    f"/cabinet/auctions-v2/{lot.id}/market-comparables",
                    data={
                        "source_name": "Krisha",
                        "source_url": "https://krisha.kz/a/show/1",
                        "title": "analog",
                        "area_ha": "0.1",
                        "price_kzt": "1500000",
                    },
                    follow_redirects=False,
                ),
            ]
            admin_only = client.post(
                "/cabinet/auctions-v2/sync",
                follow_redirects=False,
            )

        assert catalog.status_code == 200
        assert "Режим «Наблюдатель»" in catalog.text
        assert [response.status_code for response in protected_responses] == [303] * len(
            protected_responses
        )
        assert all(
                response.headers["location"].startswith("/cabinet/auctions-v2/plans")
            for response in protected_responses
        )
        assert admin_only.status_code == 404


def test_paid_non_admin_can_use_auction_investor_workspace() -> None:
    with build_session() as session:
        account = make_non_admin_account()
        account.paid_access = True
        account.auction_plan = "investor"
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            detail = client.get(f"/cabinet/auctions-v2/{lot.id}")
            portfolio = client.get("/cabinet/auctions-v2/portfolio")
            pipeline = client.post(
                f"/cabinet/auctions-v2/{lot.id}/pipeline",
                data={"stage": "checking", "max_bid_kzt": "1300000"},
                follow_redirects=False,
            )
            sync = client.post(
                "/cabinet/auctions-v2/sync",
                follow_redirects=False,
            )

        assert detail.status_code == 200
        assert portfolio.status_code == 200
        assert pipeline.status_code == 303
        assert sync.status_code == 404
        stored = session.scalar(
            select(AuctionUserLotPipeline).where(
                AuctionUserLotPipeline.account_id == account.id,
                AuctionUserLotPipeline.lot_id == lot.id,
            )
        )
        assert stored is not None
        assert stored.stage == "checking"


def test_team_member_uses_owner_portfolio_and_authored_deal_room() -> None:
    with build_session() as session:
        owner = make_non_admin_account()
        owner.phone = "+77018854001"
        owner.paid_access = True
        owner.auction_plan = "team"
        member = make_non_admin_account()
        member.phone = "+77018854002"
        lot = make_lot()
        session.add_all([owner, member, lot])
        session.flush()
        workspace = ensure_team_workspace(session, owner)
        add_workspace_member(
            session,
            workspace=workspace,
            invited_by=owner,
            account=member,
            role="analyst",
        )
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, member)
            pipeline_response = client.post(
                f"/cabinet/auctions-v2/{lot.id}/pipeline",
                data={"stage": "decided_to_participate", "max_bid_kzt": "1400000"},
                follow_redirects=False,
            )
            activity_response = client.post(
                f"/cabinet/auctions-v2/{lot.id}/activity",
                data={"kind": "decision", "body": "Участвуем до лимита"},
                follow_redirects=False,
            )
            team_response = client.get("/cabinet/auctions-v2/team")
            detail_response = client.get(f"/cabinet/auctions-v2/{lot.id}")

        stored = session.scalar(
            select(AuctionUserLotPipeline).where(
                AuctionUserLotPipeline.account_id == owner.id,
                AuctionUserLotPipeline.lot_id == lot.id,
            )
        )
        assert pipeline_response.status_code == 303
        assert activity_response.status_code == 303
        assert team_response.status_code == 200
        assert detail_response.status_code == 200
        assert stored is not None
        assert stored.stage == "decided_to_participate"
        assert member.phone in detail_response.text
        assert "Участвуем до лимита" in detail_response.text


def test_team_viewer_cannot_change_shared_pipeline() -> None:
    with build_session() as session:
        owner = make_non_admin_account()
        owner.phone = "+77018854101"
        owner.paid_access = True
        owner.auction_plan = "team"
        viewer = make_non_admin_account()
        viewer.phone = "+77018854102"
        lot = make_lot()
        session.add_all([owner, viewer, lot])
        session.flush()
        workspace = ensure_team_workspace(session, owner)
        add_workspace_member(
            session,
            workspace=workspace,
            invited_by=owner,
            account=viewer,
            role="viewer",
        )
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, viewer)
            detail = client.get(f"/cabinet/auctions-v2/{lot.id}")
            update = client.post(
                f"/cabinet/auctions-v2/{lot.id}/pipeline",
                data={"stage": "won"},
                follow_redirects=False,
            )

        assert detail.status_code == 200
        assert "Режим наблюдателя" in detail.text
        assert update.status_code == 303
        assert session.scalar(select(AuctionUserLotPipeline)) is None


def test_auction_v2_analysis_flags_missing_cadastre_and_coordinates() -> None:
    with build_session() as session:
        lot = make_lot(cadastre=None, coordinates=False)
        session.add(lot)
        session.commit()

        analysis = build_auction_v2_analysis(session, lot, force=True)
        risks = json.loads(analysis.risk_flags_json)
        risk_codes = {item["code"] for item in risks}

        assert analysis.risk_level == "high"
        assert "no_cadastre" in risk_codes
        assert "no_coordinates" in risk_codes
        assert analysis.recommended_action in {"manual_check", "skip"}


def test_auction_v2_data_quality_is_available_in_web_payload_and_telegram_card() -> None:
    with build_session() as session:
        lot = make_lot()
        session.add(lot)
        session.commit()

        payload = get_auction_v2_payload(session, lot.id, force=True)

        assert payload is not None
        assert payload.data_quality["counts"]["total"] == 7
        assert payload.data_quality["counts"]["done"] >= 2
        card = format_auction_v2_telegram_card(payload)
        assert "Решение:" in card
        assert "Сейчас проверить:" in card
        assert "ключевых блоков подтверждено" in card


def test_auction_v2_rejects_coordinates_outside_kazakhstan() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.raw_payload_json = json.dumps({"lat": 0, "lon": 45})
        session.add_all([account, lot])
        session.commit()

        analysis = build_auction_v2_analysis(session, lot, force=True)
        payload = get_auction_v2_payload(session, lot.id, account_id=account.id)
        geo_check = session.scalar(select(AuctionLotGeoCheck))
        map_data = list_auction_v2_map_markers(
            session,
            AuctionV2Filters(base=AuctionFilters()),
            account_id=account.id,
            limit=20,
        )

        risks = json.loads(analysis.risk_flags_json)
        risk_codes = {item["code"] for item in risks}
        assert payload is not None
        assert geo_check is not None
        assert geo_check.coordinate_status == "unconfirmed"
        assert geo_check.latitude is None
        assert geo_check.longitude is None
        assert payload.map_embed_url is None
        assert payload.coordinate_label == "Координаты не подтверждены"
        assert "coordinates_unconfirmed" in risk_codes
        assert map_data["mapped"] == 0
        assert map_data["without_coordinates"] == 1


def test_auction_v2_osm_infrastructure_is_saved_and_used_in_risk_analysis() -> None:
    class FakeOsmProvider:
        def analyze_points(
            self,
            points: list[tuple[float, float]],
            radius_m: int = 1200,
        ) -> list[Surroundings]:
            assert points == [(51.1282, 71.4304)]
            assert radius_m == 1200
            return [
                Surroundings(
                    road_distance_m=42,
                    power_distance_m=180,
                    water_distance_m=260,
                    open_water_distance_m=20,
                    cemetery_distance_m=None,
                    object_distance_m=85,
                    object_kind="mapped object",
                    checked=True,
                )
            ]

    with build_session() as session:
        lot = make_lot()
        session.add(lot)
        session.commit()

        geo_check = refresh_auction_v2_infrastructure(
            session,
            lot,
            provider=FakeOsmProvider(),
            force=True,
        )
        analysis = build_auction_v2_analysis(session, lot, force=True)
        risks = json.loads(analysis.risk_flags_json)
        source_statuses = json.loads(analysis.source_status_json)
        osm_status = next(item for item in source_statuses if item["code"] == "osm_overpass")

        assert geo_check.osm_status == "checked"
        assert geo_check.engineering_status == "warning"
        assert geo_check.road_distance_m == 42
        assert geo_check.open_water_distance_m == 20
        assert analysis.confidence_level == "high"
        assert {item["code"] for item in risks} >= {
            "open_water_nearby",
            "manual_genplan_required",
        }
        assert osm_status["status"] == "warning"
        assert "20 м" in osm_status["detail"]


def test_auction_v2_osm_infrastructure_skips_lot_without_coordinates() -> None:
    class FailingOsmProvider:
        def analyze_points(
            self,
            points: list[tuple[float, float]],
            radius_m: int = 1200,
        ) -> list[Surroundings]:
            raise AssertionError("OSM provider must not be called without coordinates")

    with build_session() as session:
        lot = make_lot(coordinates=False)
        session.add(lot)
        session.commit()

        geo_check = refresh_auction_v2_infrastructure(
            session,
            lot,
            provider=FailingOsmProvider(),
            force=True,
        )

        assert geo_check.osm_status == "missing_coordinates"
        assert geo_check.road_distance_m is None
        assert session.scalar(select(AuctionLotGeoCheck)) is not None


def test_auction_v2_lot_list_filters_by_deadline_status() -> None:
    with build_session() as session:
        account = make_admin_account()
        urgent = make_lot()
        urgent.source_lot_id = "deadline-urgent"
        urgent.auction_number = "D-1"
        urgent.auction_starts_at = web._now() + timedelta(hours=12)
        soon = make_lot()
        soon.source_lot_id = "deadline-soon"
        soon.auction_number = "D-2"
        soon.auction_starts_at = web._now() + timedelta(days=2)
        normal = make_lot()
        normal.source_lot_id = "deadline-normal"
        normal.auction_number = "D-3"
        normal.auction_starts_at = web._now() + timedelta(days=7)
        session.add_all([account, urgent, soon, normal])
        session.commit()
        for lot in (urgent, soon, normal):
            build_auction_v2_analysis(session, lot, force=True)
        session.commit()

        urgent_rows, urgent_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), deadline_status="urgent"),
            account_id=account.id,
        )
        soon_rows, soon_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), deadline_status="soon"),
            account_id=account.id,
        )

        assert urgent_total == 1
        assert [item.lot.id for item in urgent_rows] == [urgent.id]
        assert urgent_rows[0].deadline_status == "urgent"
        assert soon_total == 1
        assert [item.lot.id for item in soon_rows] == [soon.id]


def test_auction_v2_lot_list_sorts_by_price_per_sotka() -> None:
    with build_session() as session:
        account = make_admin_account()
        cheap_per_sotka = make_lot()
        cheap_per_sotka.source_lot_id = "sort-cheap-sotka"
        cheap_per_sotka.auction_number = "P-1"
        cheap_per_sotka.start_price_kzt = 2_000_000
        cheap_per_sotka.area_ha = 2.0
        expensive_per_sotka = make_lot()
        expensive_per_sotka.source_lot_id = "sort-expensive-sotka"
        expensive_per_sotka.auction_number = "P-2"
        expensive_per_sotka.start_price_kzt = 500_000
        expensive_per_sotka.area_ha = 0.01
        session.add_all([account, expensive_per_sotka, cheap_per_sotka])
        session.commit()
        for lot in (expensive_per_sotka, cheap_per_sotka):
            build_auction_v2_analysis(session, lot, force=True)
        session.commit()

        rows, total = list_auction_v2_lots(
            session,
            AuctionV2Filters(
                base=AuctionFilters(),
                sort_by="price_per_sotka_asc",
            ),
            account_id=account.id,
        )

        assert total == 2
        assert [item.lot.id for item in rows] == [
            cheap_per_sotka.id,
            expensive_per_sotka.id,
        ]


def test_auction_v2_lot_list_filters_by_eqazyna_status() -> None:
    with build_session() as session:
        account = make_admin_account()
        accepting = make_lot()
        accepting.source_lot_id = "status-accepting"
        accepting.auction_number = "STATUS-1"
        accepting.title = "Лот с приемом заявок"
        accepting.source_search_status = "ApplicationsAccept"
        pending = make_lot()
        pending.source_lot_id = "status-pending"
        pending.auction_number = "STATUS-2"
        pending.title = "Лот ожидает начала"
        pending.source_search_status = "Pending"
        unknown = make_lot()
        unknown.source_lot_id = "status-unknown"
        unknown.auction_number = "STATUS-3"
        unknown.title = "Лот без официального статуса"
        unknown.source_search_status = None
        session.add_all([account, accepting, pending, unknown])
        session.commit()

        accepting_rows, accepting_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(
                base=AuctionFilters(),
                eqazyna_status="ApplicationsAccept",
            ),
            account_id=account.id,
        )
        pending_rows, pending_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), eqazyna_status="Pending"),
            account_id=account.id,
        )
        unknown_rows, unknown_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), eqazyna_status="unknown"),
            account_id=account.id,
        )
        map_data = list_auction_v2_map_markers(
            session,
            AuctionV2Filters(
                base=AuctionFilters(),
                eqazyna_status="ApplicationsAccept",
            ),
            account_id=account.id,
            limit=20,
        )

        assert accepting_total == 1
        assert [item.lot.id for item in accepting_rows] == [accepting.id]
        assert accepting_rows[0].eqazyna_status_label == "Прием заявок"
        assert pending_total == 1
        assert [item.lot.id for item in pending_rows] == [pending.id]
        assert pending_rows[0].eqazyna_status_label == "Ожидает начала"
        assert unknown_total == 1
        assert [item.lot.id for item in unknown_rows] == [unknown.id]
        assert unknown_rows[0].eqazyna_status_label == "Активный лот"
        assert map_data["total"] == 1
        assert [marker["id"] for marker in map_data["markers"]] == [accepting.id]

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get(
                "/cabinet/auctions-v2",
                params={"eqazyna_status": "ApplicationsAccept"},
            )
            map_response = client.get(
                "/cabinet/auctions-v2/map",
                params={"eqazyna_status": "ApplicationsAccept"},
            )

        assert response.status_code == 200
        assert "Лот с приемом заявок" in response.text
        assert "Лоты: 1" in response.text
        assert 'name="eqazyna_status"' in response.text
        assert 'value="ApplicationsAccept" selected' in response.text
        assert map_response.status_code == 200
        assert 'name="eqazyna_status"' in map_response.text
        assert 'value="ApplicationsAccept" selected' in map_response.text


def test_auction_v2_lot_list_searches_by_cadastre_and_archive_scope() -> None:
    with build_session() as session:
        account = make_admin_account()
        active = make_lot(cadastre="21-318-001-001")
        active.source_lot_id = "scope-active"
        active.auction_number = "S-1"
        archived = make_lot(cadastre="99-777-123-456")
        archived.source_lot_id = "scope-archive"
        archived.auction_number = "S-2"
        archived.active = False
        archived.source_search_status = "FailureProtocolSigned"
        archived.auction_starts_at = web._now() - timedelta(days=3)
        session.add_all([account, active, archived])
        session.commit()
        for lot in (active, archived):
            build_auction_v2_analysis(session, lot, force=True)
        session.commit()

        all_rows, all_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), search_query="99-777", lot_scope="all"),
            account_id=account.id,
        )
        active_rows, active_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), search_query="99-777"),
            account_id=account.id,
        )
        compact_rows, compact_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), search_query="21318001001"),
            account_id=account.id,
        )
        colon_rows, colon_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), search_query="99:777:123:456", lot_scope="all"),
            account_id=account.id,
        )
        archive_rows, archive_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), lot_scope="archive"),
            account_id=account.id,
        )

        assert all_total == 1
        assert [item.lot.id for item in all_rows] == [archived.id]
        assert active_total == 0
        assert active_rows == []
        assert compact_total == 1
        assert [item.lot.id for item in compact_rows] == [active.id]
        assert colon_total == 1
        assert [item.lot.id for item in colon_rows] == [archived.id]
        assert archive_total == 1
        assert [item.lot.id for item in archive_rows] == [archived.id]
        assert archive_rows[0].eqazyna_status_label == "Не состоялся, протокол подписан"
        assert archive_rows[0].lot_scope == "archive"
        assert archive_rows[0].lot_scope_label == "Архив"


def test_auction_v2_lot_list_creates_missing_analysis_for_legacy_lots() -> None:
    with build_session() as session:
        account = make_admin_account()
        active = make_lot(cadastre="01-005-061-079")
        active.source_lot_id = "legacy-active"
        active.auction_number = "LEG-1"
        archived = make_lot(cadastre="99-777-123-456")
        archived.source_lot_id = "legacy-archive"
        archived.auction_number = "LEG-2"
        archived.active = False
        archived.source_search_status = "FailureProtocolSigned"
        archived.auction_starts_at = web._now() - timedelta(days=5)
        session.add_all([account, active, archived])
        session.commit()

        active_rows, active_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), search_query="01005061079"),
            account_id=account.id,
        )
        archive_rows, archive_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(
                base=AuctionFilters(),
                search_query="99777123456",
                lot_scope="all",
            ),
            account_id=account.id,
        )
        analysis_count = session.scalar(select(func.count(AuctionLotV2Analysis.id)))

    assert active_total == 1
    assert [item.lot.id for item in active_rows] == [active.id]
    assert archive_total == 1
    assert [item.lot.id for item in archive_rows] == [archived.id]
    assert analysis_count == 2


def test_auction_v2_lot_list_filters_by_geo_and_osm_status() -> None:
    class StaticOsmProvider:
        def __init__(self, surroundings: Surroundings) -> None:
            self.surroundings = surroundings

        def analyze_points(
            self,
            points: list[tuple[float, float]],
            radius_m: int = 1200,
        ) -> list[Surroundings]:
            return [self.surroundings for _point in points]

    with build_session() as session:
        account = make_admin_account()
        checked = make_lot()
        checked.source_lot_id = "geo-checked"
        checked.auction_number = "G-1"
        warning = make_lot()
        warning.source_lot_id = "geo-warning"
        warning.auction_number = "G-2"
        warning.raw_payload_json = json.dumps({"lat": 51.131, "lon": 71.436})
        missing = make_lot(coordinates=False)
        missing.source_lot_id = "geo-missing"
        missing.auction_number = "G-3"
        session.add_all([account, checked, warning, missing])
        session.commit()

        refresh_auction_v2_infrastructure(
            session,
            checked,
            provider=StaticOsmProvider(
                Surroundings(
                    road_distance_m=40,
                    power_distance_m=120,
                    water_distance_m=180,
                    open_water_distance_m=120,
                    object_distance_m=70,
                    checked=True,
                )
            ),
            force=True,
        )
        refresh_auction_v2_infrastructure(
            session,
            warning,
            provider=StaticOsmProvider(
                Surroundings(
                    road_distance_m=40,
                    power_distance_m=120,
                    water_distance_m=180,
                    open_water_distance_m=12,
                    object_distance_m=70,
                    checked=True,
                )
            ),
            force=True,
        )
        for lot in (checked, warning, missing):
            build_auction_v2_analysis(session, lot, force=True)
        session.commit()

        checked_rows, checked_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), geo_status="osm_checked"),
            account_id=account.id,
        )
        warning_rows, warning_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), geo_status="osm_warning"),
            account_id=account.id,
        )
        missing_rows, missing_total = list_auction_v2_lots(
            session,
            AuctionV2Filters(base=AuctionFilters(), geo_status="coordinates_missing"),
            account_id=account.id,
        )

        assert checked_total == 1
        assert [item.lot.id for item in checked_rows] == [checked.id]
        assert checked_rows[0].geo_check.osm_status == "checked"
        assert checked_rows[0].geo_check.engineering_status == "checked"
        assert warning_total == 1
        assert [item.lot.id for item in warning_rows] == [warning.id]
        assert warning_rows[0].geo_check.engineering_status == "warning"
        assert missing_total == 1
        assert [item.lot.id for item in missing_rows] == [missing.id]


def test_auction_v2_market_comparable_updates_analysis_limits_and_dossier() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()
        base_analysis = build_auction_v2_analysis(session, lot, force=True)
        base_market_limit = base_analysis.max_bid_market_kzt
        assert base_market_limit is None

        comparable = create_auction_v2_market_comparable(
            session,
            lot_id=lot.id,
            source_name="Krisha",
            source_url="https://krisha.kz/a/show/market-1",
            title="Похожий участок рядом",
            area_ha=0.10,
            price_kzt=2_000_000,
        )
        session.commit()
        analysis = session.scalar(
            select(AuctionLotV2Analysis).where(AuctionLotV2Analysis.lot_id == lot.id)
        )
        payload = get_auction_v2_payload(session, lot.id, account_id=account.id)
        dossier = build_auction_v2_dossier_text(session, lot.id, account_id=account.id)
        saved = session.scalar(select(AuctionMarketComparable))

        assert saved is not None
        assert comparable.price_per_sotka == pytest.approx(200_000)
        assert analysis is not None
        assert analysis.max_bid_market_kzt is not None
        assert base_market_limit is None or analysis.max_bid_market_kzt >= base_market_limit
        assert payload is not None
        market_status = next(
            item for item in payload.source_statuses if item["code"] == "krisha_land_market"
        )
        assert market_status["status"] == "ok"
        assert "Krisha" in market_status["detail"]
        assert any(item["code"] == "olx_land_market" for item in payload.source_statuses)
        assert dossier is not None
        assert "РЫНОЧНЫЕ АНАЛОГИ" in dossier
        assert "https://krisha.kz/a/show/market-1" in dossier


def test_auction_v2_source_sync_creates_runs_and_query_evidence() -> None:
    with build_session() as session:
        lot = make_lot()
        session.add(lot)
        session.commit()

        result = sync_auction_v2_sources(session, limit=10)
        session.commit()

        evidence_types = {
            row[0]
            for row in session.execute(select(AuctionEvidence.evidence_type)).all()
        }
        data_source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "data_egov_open_data")
        )
        eqazyna_source = session.scalar(
            select(AuctionSource).where(AuctionSource.code == "eqazyna_current_lots")
        )
        run_statuses = dict(
            session.execute(
                select(AuctionSource.code, AuctionCrawlRun.status).join(
                    AuctionCrawlRun,
                    AuctionCrawlRun.source_id == AuctionSource.id,
                )
            ).all()
        )

        assert result.lots_checked == 1
        assert result.sources_checked >= 6
        assert result.crawl_runs_created == result.sources_checked
        assert session.scalar(select(AuctionCrawlRun)) is not None
        assert eqazyna_source is not None
        assert eqazyna_source.last_success_at is not None
        assert data_source is not None
        assert data_source.last_success_at is None
        assert run_statuses["eqazyna_current_lots"] == "success"
        assert run_statuses["data_egov_open_data"] == "query_ready"
        assert run_statuses["egkn_public_map"] == "manual_required"
        assert {"source_query", "market_query", "official_boundary"}.issubset(evidence_types)


def test_auction_v2_source_sync_prioritizes_lots_without_analysis() -> None:
    with build_session() as session:
        ready = make_lot()
        ready.source_lot_id = "sync-ready"
        ready.auction_number = "SYNC-1"
        ready.last_seen_at = web._now()
        missing = make_lot()
        missing.source_lot_id = "sync-missing"
        missing.auction_number = "SYNC-2"
        missing.last_seen_at = web._now() - timedelta(days=2)
        session.add_all([ready, missing])
        session.commit()
        ready_id = ready.id
        missing_id = missing.id
        build_auction_v2_analysis(session, ready, force=True)
        session.commit()

        result = sync_auction_v2_sources(session, limit=1, send_notifications=False)
        session.commit()
        analysed_lot_ids = {
            row[0]
            for row in session.execute(select(AuctionLotV2Analysis.lot_id)).all()
        }

    assert result.lots_checked == 1
    assert missing_id in analysed_lot_ids
    assert ready_id in analysed_lot_ids


def test_auction_v2_sync_dispatches_watchlist_notifications_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_payloads: list[dict] = []
    monkeypatch.setattr(
        services,
        "telegram_request",
        lambda _method, payload: sent_payloads.append(payload) or {"ok": True},
    )
    with build_session() as session:
        account = make_admin_account()
        account.telegram_chat_id = "chat-1"
        account.telegram_user_id = "777"
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Telegram watch",
            filters=AuctionV2Filters(base=AuctionFilters(), min_score=1),
        )
        session.commit()

        first = sync_auction_v2_sources(session, limit=10)
        session.commit()
        second = sync_auction_v2_sources(session, limit=10)
        session.commit()

        rows = session.scalars(select(AuctionWatchlistNotification)).all()

        assert first.web_notifications_created == 1
        assert first.telegram_notifications_sent == 1
        assert first.notification_errors == 0
        assert second.web_notifications_created == 0
        assert second.telegram_notifications_sent == 0
        assert {row.channel for row in rows} == {"web", "telegram"}
        assert len(rows) == 2
        assert len(sent_payloads) == 1
        assert sent_payloads[0]["chat_id"] == "chat-1"


def test_auction_v2_watchlist_dispatches_price_change_event_once() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Price watch",
            filters=AuctionV2Filters(base=AuctionFilters(), min_score=1),
        )
        session.commit()

        first = sync_auction_v2_sources(session, limit=10)
        session.add(
            AuctionLotChange(
                lot_id=lot.id,
                field_name="start_price_kzt",
                old_value="1000000",
                new_value="900000",
                changed_at=web._now(),
            )
        )
        session.commit()
        second = sync_auction_v2_sources(session, limit=10)
        session.commit()
        third = sync_auction_v2_sources(session, limit=10)
        session.commit()

        web_rows = list(
            session.scalars(
                select(AuctionWatchlistNotification)
                .where(AuctionWatchlistNotification.channel == "web")
                .order_by(AuctionWatchlistNotification.id)
            ).all()
        )

        assert first.web_notifications_created == 1
        assert second.web_notifications_created == 1
        assert third.web_notifications_created == 0
        assert [row.event_type for row in web_rows] == ["new_lot", "price_changed"]
        assert web_rows[1].event_key.startswith("change:")
        assert "900 000" in (web_rows[1].detail or "")


def test_auction_v2_watchlist_dispatches_eqazyna_status_change_once() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Status watch",
            filters=AuctionV2Filters(base=AuctionFilters(), min_score=1),
        )
        session.commit()

        first = sync_auction_v2_sources(session, limit=10)
        session.add(
            AuctionLotChange(
                lot_id=lot.id,
                field_name="source_search_status",
                old_value="Pending",
                new_value="ApplicationsAccept",
                changed_at=web._now(),
            )
        )
        session.commit()
        second = sync_auction_v2_sources(session, limit=10)
        session.commit()
        third = sync_auction_v2_sources(session, limit=10)
        session.commit()

        web_rows = list(
            session.scalars(
                select(AuctionWatchlistNotification)
                .where(AuctionWatchlistNotification.channel == "web")
                .order_by(AuctionWatchlistNotification.id)
            ).all()
        )

        assert first.web_notifications_created == 1
        assert second.web_notifications_created == 1
        assert third.web_notifications_created == 0
        assert [row.event_type for row in web_rows] == [
            "new_lot",
            "eqazyna_status_changed",
        ]
        assert web_rows[1].event_key.startswith("change:")
        assert "Ожидает начала -> Прием заявок" in (web_rows[1].detail or "")


def test_auction_v2_watchlist_dispatches_deadline_event_once() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.auction_starts_at = web._now() + timedelta(minutes=90)
        session.add_all([account, lot])
        session.commit()
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Deadline watch",
            filters=AuctionV2Filters(base=AuctionFilters(), min_score=1),
        )
        session.commit()

        first = sync_auction_v2_sources(session, limit=10)
        session.commit()
        second = sync_auction_v2_sources(session, limit=10)
        session.commit()
        web_rows = list(
            session.scalars(
                select(AuctionWatchlistNotification)
                .where(AuctionWatchlistNotification.channel == "web")
                .order_by(AuctionWatchlistNotification.event_type)
            ).all()
        )

        assert first.web_notifications_created == 2
        assert second.web_notifications_created == 0
        assert {row.event_type for row in web_rows} == {"auction_2h", "new_lot"}


def test_auction_v2_web_notification_inbox_marks_seen() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Web watch",
            filters=AuctionV2Filters(base=AuctionFilters(), min_score=1),
        )
        session.commit()

        result = sync_auction_v2_sources(session, limit=10)
        session.commit()
        inbox = list_auction_v2_web_notifications(session, account_id=account.id)
        watchlists = list_auction_v2_watchlists(session, account.id)

        assert result.web_notifications_created == 1
        assert len(inbox) == 1
        assert inbox[0].watchlist.name == "Web watch"
        assert inbox[0].item.lot.id == lot.id
        assert watchlists[0].web_notification_count == 1

        seen = mark_auction_v2_web_notifications_seen(
            session,
            account_id=account.id,
            lot_id=lot.id,
        )
        session.commit()
        rows = session.scalars(select(AuctionWatchlistNotification)).all()

        assert seen == 1
        assert rows[0].status == "opened"
        assert rows[0].seen_at is not None
        assert list_auction_v2_web_notifications(session, account_id=account.id) == []
        assert list_auction_v2_watchlists(session, account.id)[0].web_notification_count == 0


def test_auction_v2_full_cycle_passes_limit_to_eqazyna_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(settings, "eqazyna_sync_max_pages", 7)
    monkeypatch.setattr(settings, "eqazyna_sync_max_lots", 120)

    def fake_sync_current_auctions(session: Session, **kwargs: object) -> AuctionSyncResult:
        seen.update(kwargs)
        return AuctionSyncResult(
            fetched=0,
            created=0,
            updated=0,
            notifications_sent=0,
            errors=0,
        )

    def fake_sync_sources(
        session: Session,
        *,
        limit: int | None = None,
        force: bool = True,
        send_notifications: bool = True,
    ) -> AuctionV2SyncResult:
        seen["v2_limit"] = limit
        return AuctionV2SyncResult()

    monkeypatch.setattr("app.auction_v2.sync_current_auctions", fake_sync_current_auctions)
    monkeypatch.setattr("app.auction_v2.sync_auction_v2_sources", fake_sync_sources)

    with build_session() as session:
        result = sync_auction_v2_full_cycle(session, limit=30)

    assert result.lots_fetched == 0
    assert seen["max_pages"] == 7
    assert seen["max_lots"] == 120
    assert seen["v2_limit"] == 30
    assert seen["send_notifications"] is False


def test_eqazyna_history_backfill_uses_safe_archive_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(settings, "eqazyna_history_sync_max_pages", 12)
    monkeypatch.setattr(settings, "eqazyna_history_sync_max_lots", 250)
    monkeypatch.setattr(
        settings,
        "eqazyna_history_sync_statuses",
        "SuccessProtocolSigned,FailureProtocolSigned",
    )

    window_results = [
        AuctionSyncResult(
            fetched=20,
            created=7,
            updated=13,
            notifications_sent=0,
            errors=0,
            deactivated=0,
            crawl_complete=True,
            url_count=40,
            pages_scanned=24,
            status_counts={"SuccessProtocolSigned": 30, "FailureProtocolSigned": 10},
        ),
        AuctionSyncResult(
            fetched=5,
            created=2,
            updated=3,
            notifications_sent=0,
            errors=0,
            deactivated=0,
            crawl_complete=True,
            url_count=8,
            pages_scanned=6,
            status_counts={"SuccessProtocolSigned": 3, "FailureProtocolSigned": 5},
        ),
    ]

    def fake_sync_current_auctions(session: Session, **kwargs: object) -> AuctionSyncResult:
        calls.append(kwargs)
        return window_results[len(calls) - 1]

    monkeypatch.setattr("app.auction_v2.sync_current_auctions", fake_sync_current_auctions)

    with build_session() as session:
        result = sync_auction_v2_eqazyna_history_backfill(
            session,
            publish_date_windows=[
                ("01.01.2020", "31.12.2020"),
                ("01.01.2021", "31.12.2021"),
            ],
        )
        row = session.execute(
            select(AuctionCrawlRun, AuctionSource)
            .join(AuctionSource, AuctionSource.id == AuctionCrawlRun.source_id)
            .where(AuctionSource.code == "eqazyna_history_backfill")
        ).one()
        run, source = row
        payload = json.loads(run.raw_payload_json or "{}")

    assert result.fetched == 25
    assert result.created == 9
    assert result.updated == 16
    assert [call["publish_date_windows"] for call in calls] == [
        [("01.01.2020", "31.12.2020")],
        [("01.01.2021", "31.12.2021")],
    ]
    for call in calls:
        assert call["max_pages"] == 12
        assert call["max_lots"] == 250
        assert call["statuses"] == ["SuccessProtocolSigned", "FailureProtocolSigned"]
        assert call["deactivate_missing"] is False
        assert call["send_notifications"] is False
    assert source.name == "E-Qazyna: архив торгов"
    assert run.status == "success"
    assert run.items_seen == 48
    assert run.items_created == 9
    assert run.items_updated == 16
    assert payload["mode"] == "auction_v2_eqazyna_history_backfill"
    assert payload["deactivated"] == 0
    assert payload["publish_date_windows_count"] == 2
    assert payload["status_counts"]["SuccessProtocolSigned"] == 33


def test_eqazyna_history_publish_date_windows_use_configured_start_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "eqazyna_history_sync_start_year", 2024)
    monkeypatch.setattr(settings, "eqazyna_history_sync_window_days", 90)

    assert eqazyna_history_publish_date_windows(today=date(2024, 5, 1)) == [
        ("01.01.2024", "30.03.2024"),
        ("31.03.2024", "01.05.2024"),
    ]


def test_auction_v2_full_cycle_records_empty_eqazyna_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_sync_current_auctions(session: Session, **kwargs: object) -> AuctionSyncResult:
        return AuctionSyncResult(
            fetched=0,
            created=0,
            updated=0,
            notifications_sent=0,
            errors=0,
            crawl_complete=True,
            url_count=0,
            pages_scanned=3,
            status_counts={
                "ApplicationsAccept": 0,
                "Pending": 0,
                "Running": 0,
            },
        )

    monkeypatch.setattr("app.auction_v2.sync_current_auctions", fake_sync_current_auctions)
    monkeypatch.setattr(
        "app.auction_v2.sync_auction_v2_sources",
        lambda *args, **kwargs: AuctionV2SyncResult(),
    )

    with build_session() as session:
        result = sync_auction_v2_full_cycle(session, limit=30)
        row = session.execute(
            select(AuctionCrawlRun, AuctionSource)
            .join(AuctionSource, AuctionSource.id == AuctionCrawlRun.source_id)
            .where(AuctionSource.code == "eqazyna_current_lots")
        ).one()
        run, source = row
        payload = json.loads(run.raw_payload_json or "{}")
        dashboard = auction_v2_dashboard(session)

    assert result.lots_fetched == 0
    assert run.status == "missing"
    assert run.items_seen == 0
    assert run.finished_at is not None
    assert source.last_success_at is not None
    assert payload["url_count"] == 0
    assert payload["pages_scanned"] == 3
    assert payload["status_counts"]["ApplicationsAccept"] == 0
    assert dashboard["empty_diagnostics"]["reason"] == "eqazyna_no_urls"
    assert "ссылок 0" in dashboard["empty_diagnostics"]["summary"]


def test_auction_v2_full_cycle_commits_fast_layer_before_slow_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_sync_current_auctions(session: Session, **kwargs: object) -> AuctionSyncResult:
        return AuctionSyncResult(
            fetched=1,
            created=0,
            updated=1,
            notifications_sent=0,
            errors=0,
            crawl_complete=False,
            url_count=1,
            pages_scanned=1,
        )

    def failing_sync_sources(
        session: Session,
        *,
        limit: int | None = None,
        force: bool = True,
        send_notifications: bool = True,
    ) -> AuctionV2SyncResult:
        raise RuntimeError("slow source timeout")

    monkeypatch.setattr("app.auction_v2.sync_current_auctions", fake_sync_current_auctions)
    monkeypatch.setattr("app.auction_v2.sync_auction_v2_sources", failing_sync_sources)

    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with pytest.raises(RuntimeError, match="slow source timeout"):
            sync_auction_v2_full_cycle(session, limit=10)

        analysis_count = session.scalar(select(func.count(AuctionLotV2Analysis.id)))
        evidence_count = session.scalar(select(func.count(AuctionEvidence.id)))
        run = session.scalar(
            select(AuctionCrawlRun)
            .join(AuctionSource, AuctionSource.id == AuctionCrawlRun.source_id)
            .where(AuctionSource.code == "eqazyna_current_lots")
        )

    assert analysis_count == 1
    assert evidence_count and evidence_count > 0
    assert run is not None
    assert run.status == "success"


def test_auction_v2_full_cycle_records_eqazyna_error_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_sync_current_auctions(session: Session, **kwargs: object) -> AuctionSyncResult:
        raise RuntimeError("E-Qazyna timeout")

    monkeypatch.setattr("app.auction_v2.sync_current_auctions", failing_sync_current_auctions)

    with build_session() as session:
        with pytest.raises(RuntimeError, match="E-Qazyna timeout"):
            sync_auction_v2_full_cycle(session, limit=5)

        row = session.execute(
            select(AuctionCrawlRun, AuctionSource)
            .join(AuctionSource, AuctionSource.id == AuctionCrawlRun.source_id)
            .where(AuctionSource.code == "eqazyna_current_lots")
        ).one()
        run, source = row

    assert run.status == "error"
    assert run.finished_at is not None
    assert "E-Qazyna timeout" in (run.error_message or "")
    assert "E-Qazyna timeout" in (source.last_error or "")


def test_auction_v2_source_admin_shows_runs_errors_and_raw_payload() -> None:
    with build_session() as session:
        account = make_admin_account()
        session.add(account)
        sources = seed_auction_v2_sources(session)
        eqazyna = next(source for source in sources if source.code == "eqazyna_current_lots")
        eqazyna.last_error = "E-Qazyna timeout"
        session.add(
            AuctionCrawlRun(
                source_id=eqazyna.id,
                status="error",
                items_seen=12,
                items_created=3,
                items_updated=2,
                finished_at=web._now(),
                error_message="E-Qazyna timeout",
                raw_payload_json=json.dumps({"status": "broken", "items": [1, 2, 3]}),
            )
        )
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2/sources")

        assert response.status_code == 200
        assert "Состояние данных аукционов v2" in response.text
        assert "служебный режим внутри аукционов v2" in response.text
        assert "← К лотам" in response.text
        assert "служебная проверка" not in response.text
        assert "E-Qazyna: текущие торги" in response.text
        assert "E-Qazyna timeout" in response.text
        assert "Данные для диагностики" in response.text
        assert "broken" in response.text
        assert "увидел 12" in response.text


def test_auction_v2_source_admin_hidden_from_non_admin_phone() -> None:
    with build_session() as session:
        account = make_non_admin_account()
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2/sources")

        assert response.status_code == 404


def test_auction_v2_empty_search_explains_cadastre_and_scope_links() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot(cadastre="21-318-001-001")
        session.add_all([account, lot])
        session.commit()
        build_auction_v2_analysis(session, lot, force=True)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2?q=01005061079")

    assert response.status_code == 200
    assert "Лоты не найдены" in response.text
    assert "В базе v2 такого номера нет" in response.text
    assert "Система проверила активные, будущие и архивные лоты v2" in response.text
    assert "Активные: <b>0</b>" in response.text
    assert "Все: <b>0</b>" in response.text
    assert "Искать этот номер во всех лотах" in response.text
    assert "lot_scope=all" in response.text
    assert "Проверить архив" in response.text
    assert "lot_scope=archive" in response.text
    assert "Служебное обновление каталога" in response.text


def test_auction_v2_empty_list_shows_eqazyna_empty_reason() -> None:
    with build_session() as session:
        account = make_admin_account()
        session.add(account)
        sources = seed_auction_v2_sources(session)
        eqazyna = next(source for source in sources if source.code == "eqazyna_current_lots")
        session.add(
            AuctionCrawlRun(
                source_id=eqazyna.id,
                status="missing",
                items_seen=0,
                items_created=0,
                items_updated=0,
                finished_at=web._now(),
                raw_payload_json=json.dumps(
                    {
                        "mode": "auction_v2_eqazyna_crawl",
                        "fetched": 0,
                        "url_count": 0,
                        "pages_scanned": 2,
                        "crawl_complete": True,
                        "detail_errors": 0,
                        "status_counts": {"ApplicationsAccept": 0},
                    }
                ),
            )
        )
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get("/cabinet/auctions-v2")

    assert response.status_code == 200
    assert "Лоты не найдены" in response.text
    assert "E-Qazyna ответил, но не дал ссылок на земельные лоты" in response.text
    assert "Последний E-Qazyna: Нет данных; ссылок 0; карточек 0; страниц 2" in response.text
    assert "Проверить источники" in response.text


def test_auction_v2_detail_marks_web_notifications_seen() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Detail watch",
            filters=AuctionV2Filters(base=AuctionFilters(), min_score=1),
        )
        session.commit()
        sync_auction_v2_sources(session, limit=10)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get(f"/cabinet/auctions-v2/{lot.id}")

        notification = session.scalar(select(AuctionWatchlistNotification))

        assert response.status_code == 200
        assert notification is not None
        assert notification.status == "opened"
        assert notification.seen_at is not None


def test_auction_v2_builds_pre_purchase_dossier_text() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        lot.cadastre_number = "21-318-001-001"
        session.add_all([account, lot])
        session.commit()

        text = build_auction_v2_dossier_text(
            session,
            lot.id,
            account_id=account.id,
        )

        assert text is not None
        assert "ZHERTAP AUCTIONS V2 DOSSIER" in text
        assert "ГРАНИЦА СИСТЕМЫ" in text
        assert "Индекс преимущества" in text
        assert "РАБОЧИЙ ПРОЦЕСС ДО УЧАСТИЯ" in text
        assert "Найти официальный лот" in text
        assert "ПЕРЕД ПЕРЕХОДОМ НА E-QAZYNA" in text
        assert "E-Qazyna" in text
        assert "Заявка, ЭЦП" in text
        assert lot.cadastre_number in text


def test_auction_v2_admin_can_download_dossier_from_cabinet() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.get(f"/cabinet/auctions-v2/{lot.id}/dossier.txt")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "attachment" in response.headers["content-disposition"]
        assert "ZHERTAP AUCTIONS V2 DOSSIER" in response.text
        assert "Zhertap доводит только до решения" in response.text


def test_auction_v2_admin_can_trigger_full_cycle_sync_from_cabinet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_full_cycle(_session: Session) -> AuctionV2FullSyncResult:
        nonlocal calls
        calls += 1
        return AuctionV2FullSyncResult(
            lots_fetched=3,
            lots_created=1,
            lots_updated=2,
            v2=AuctionV2SyncResult(
                lots_checked=3,
                sources_checked=10,
                web_notifications_created=1,
            ),
        )

    monkeypatch.setattr(web, "sync_auction_v2_full_cycle", fake_full_cycle)
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post("/cabinet/auctions-v2/sync", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/cabinet/auctions-v2?sync=done")
        assert "lots_fetched=3" in response.headers["location"]
        assert "lots_created=1" in response.headers["location"]
        assert "web_notifications=1" in response.headers["location"]
        assert calls == 1


def test_auction_v2_admin_can_trigger_history_backfill_from_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_history_backfill(_session: Session) -> AuctionSyncResult:
        nonlocal calls
        calls += 1
        return AuctionSyncResult(
            fetched=40,
            created=12,
            updated=28,
            notifications_sent=0,
            errors=0,
            url_count=80,
            pages_scanned=24,
        )

    monkeypatch.setattr(web, "sync_auction_v2_eqazyna_history_backfill", fake_history_backfill)
    with build_session() as session:
        account = make_admin_account()
        session.add(account)
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post(
                "/cabinet/auctions-v2/history-backfill",
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "/cabinet/auctions-v2/sources?backfill=done"
        )
        assert "lots_fetched=40" in response.headers["location"]
        assert "lots_created=12" in response.headers["location"]
        assert "pages_scanned=24" in response.headers["location"]
        assert calls == 1


def test_auction_v2_full_cycle_is_not_scheduled_twice() -> None:
    from app.config import settings
    from app.tasks import celery_app

    schedule = celery_app.conf.beat_schedule

    assert "sync-eqazyna-auctions" in schedule
    assert schedule["sync-eqazyna-auctions"]["schedule"] == (
        min(
            settings.eqazyna_sync_interval_minutes,
            settings.auction_v2_full_cycle_interval_minutes,
        )
        * 60
    )
    assert "sync-auction-v2-sources" not in schedule
    assert "land_scout.sync_auction_v2_full_cycle" in celery_app.tasks
    assert "land_scout.sync_auction_v2_eqazyna_history_backfill" in celery_app.tasks


def test_auction_v2_watchlist_matches_scored_lots() -> None:
    with build_session() as session:
        account = make_admin_account()
        target = make_lot()
        other = make_lot()
        other.source_lot_id = "v2-lot-2"
        other.auction_number = "A-200"
        other.region = "Алматы"
        other.district = "Бостандык"
        session.add_all([account, target, other])
        session.commit()
        build_auction_v2_analysis(session, target, force=True)
        build_auction_v2_analysis(session, other, force=True)
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Астана 70+",
            filters=AuctionV2Filters(
                base=AuctionFilters(region="Астана"),
                min_score=70,
            ),
        )
        session.commit()

        watchlists = list_auction_v2_watchlists(session, account.id)
        matches = auction_v2_watchlist_matches(session, account_id=account.id, limit=10)

        assert len(watchlists) == 1
        assert watchlists[0].match_count == 1
        assert [item.lot.id for item in matches] == [target.id]


def test_auction_v2_watchlist_filters_by_eqazyna_status() -> None:
    with build_session() as session:
        account = make_admin_account()
        accepting = make_lot()
        accepting.source_lot_id = "watch-status-accept"
        accepting.auction_number = "WS-1"
        accepting.source_search_status = "ApplicationsAccept"
        pending = make_lot()
        pending.source_lot_id = "watch-status-pending"
        pending.auction_number = "WS-2"
        pending.source_search_status = "Pending"
        session.add_all([account, accepting, pending])
        session.commit()
        build_auction_v2_analysis(session, accepting, force=True)
        build_auction_v2_analysis(session, pending, force=True)
        create_auction_v2_watchlist(
            session,
            account_id=account.id,
            name="Прием заявок",
            filters=AuctionV2Filters(
                base=AuctionFilters(),
                eqazyna_status="ApplicationsAccept",
                min_score=1,
            ),
        )
        session.commit()

        watchlists = list_auction_v2_watchlists(session, account.id)
        matches = auction_v2_watchlist_matches(session, account_id=account.id, limit=10)

        assert len(watchlists) == 1
        assert watchlists[0].watchlist.eqazyna_status == "ApplicationsAccept"
        assert "E-Qazyna: прием заявок" in watchlists[0].filter_description
        assert watchlists[0].match_count == 1
        assert [item.lot.id for item in matches] == [accepting.id]


def test_auction_v2_admin_can_create_and_pause_watchlist_from_cabinet() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            create_response = client.post(
                "/cabinet/auctions-v2/watchlists",
                data={
                    "name": "ИЖС Астана",
                    "lot_scope": "future",
                    "region": "Астана",
                    "district": "Есиль",
                    "locality": "Астана",
                    "purpose": "ИЖС",
                    "min_price_kzt": "500000",
                    "max_price_kzt": "2000000",
                    "min_area_ha": "0.05",
                    "max_area_ha": "0.20",
                    "min_score": "70",
                    "eqazyna_status": "ApplicationsAccept",
                    "risk_level": "low",
                    "confidence_level": "high",
                    "stage": "watching",
                    "deadline_status": "normal",
                    "geo_status": "coordinates_found",
                },
                follow_redirects=False,
            )
            watchlist = session.scalar(
                select(AuctionWatchlist).where(AuctionWatchlist.name == "ИЖС Астана")
            )
            assert watchlist is not None
            pause_response = client.post(
                f"/cabinet/auctions-v2/watchlists/{watchlist.id}/active",
                data={"active": "false"},
                follow_redirects=False,
            )

        session.refresh(watchlist)
        assert create_response.status_code == 303
        assert create_response.headers["location"] == (
            "/cabinet/auctions-v2/subscriptions?watchlist=saved"
        )
        assert watchlist.lot_scope == "future"
        assert watchlist.region == "Астана"
        assert watchlist.district == "Есиль"
        assert watchlist.locality == "Астана"
        assert watchlist.purpose_query == "ИЖС"
        assert watchlist.min_price_kzt == 500_000
        assert watchlist.max_price_kzt == 2_000_000
        assert watchlist.min_area_ha == 0.05
        assert watchlist.max_area_ha == 0.20
        assert watchlist.min_score == 70
        assert watchlist.eqazyna_status == "ApplicationsAccept"
        assert watchlist.risk_level == "low"
        assert watchlist.confidence_level == "high"
        assert watchlist.stage == "watching"
        assert watchlist.deadline_status == "normal"
        assert watchlist.geo_status == "coordinates_found"
        assert pause_response.status_code == 303
        assert pause_response.headers["location"] == (
            "/cabinet/auctions-v2/subscriptions?watchlist=disabled"
        )
        assert watchlist.active is False


def test_auction_v2_internal_navigation_subscriptions_and_calendar() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        now = web._now()
        lot.auction_starts_at = now + timedelta(days=2)
        session.add_all([account, lot])
        session.commit()
        update_auction_v2_pipeline(
            session,
            account_id=account.id,
            lot_id=lot.id,
            stage="checking",
            max_bid_kzt=1_400_000,
            reminder_at=now + timedelta(days=1),
            notes="Проверить допуск",
            inspection={
                "status": "planned",
                "planned_at": (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
            },
        )
        session.commit()

        payload = auction_v2_calendar_payload(
            session,
            account_id=account.id,
            now=now,
        )
        with client_for(session) as client:
            authorize_client(client, session, account)
            catalog = client.get("/cabinet/auctions-v2")
            subscriptions = client.get("/cabinet/auctions-v2/subscriptions")
            calendar = client.get("/cabinet/auctions-v2/calendar")
            plans = client.get("/cabinet/auctions-v2/plans")
            legacy_plans = client.get("/cabinet/auction-plans", follow_redirects=False)
            legacy_subscriptions = client.get(
                "/cabinet/auctions/subscriptions",
                follow_redirects=False,
            )

        assert len(payload["events"]) == 3
        assert payload["totals"]["next_7_days"] == 3
        assert {event["kind"] for event in payload["events"]} == {
            "auction",
            "inspection",
            "reminder",
        }
        assert catalog.status_code == 200
        assert "Лента важных событий" not in catalog.text
        assert "Все аукционы" not in catalog.text
        assert subscriptions.status_code == 200
        assert "Создать условия поиска" in subscriptions.text
        assert calendar.status_code == 200
        assert "Просрочено и сегодня" in calendar.text
        assert plans.status_code == 200
        assert "Тариф и доступ" in plans.text
        assert legacy_plans.headers["location"] == "/cabinet/auctions-v2/plans"
        assert (
            legacy_subscriptions.headers["location"]
            == "/cabinet/auctions-v2/subscriptions"
        )


def test_auction_v2_pipeline_saves_control_date_from_detail_form() -> None:
    with build_session() as session:
        account = make_admin_account()
        lot = make_lot()
        session.add_all([account, lot])
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            response = client.post(
                f"/cabinet/auctions-v2/{lot.id}/pipeline",
                data={
                    "stage": "checking",
                    "reminder_at": "2026-08-15T10:30",
                },
                follow_redirects=False,
            )

        pipeline = session.scalar(
            select(AuctionUserLotPipeline).where(
                AuctionUserLotPipeline.account_id == account.id,
                AuctionUserLotPipeline.lot_id == lot.id,
            )
        )
        assert response.status_code == 303
        assert pipeline is not None
        assert pipeline.reminder_at is not None
        assert pipeline.reminder_at.hour == 5
