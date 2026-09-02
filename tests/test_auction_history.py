from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.auction_history import (
    auction_history_payload,
    auction_object_history,
    auction_object_identity,
    similar_auctions_aggregate,
)
from app.db import Base
from app.models import AuctionLot, AuctionLotHistory


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _lot(source_lot_id: str, **overrides: object) -> AuctionLot:
    values: dict[str, object] = {
        "source_lot_id": source_lot_id,
        "source_url": f"https://example.test/{source_lot_id}",
        "title": "Земельный участок для строительства магазина",
        "region": "Область Абай",
        "district": "Жаңасемей",
        "locality": "Новобаженово",
        "area_ha": 1.0,
        "land_rights": "Продажа права аренды земельного участка",
        "lease_term_years": 5,
        "purpose": "Для строительства магазина",
        "auction_starts_at": datetime(2026, 1, 1, tzinfo=UTC),
        "start_price_kzt": 1_000_000,
        "source_search_status": "ApplicationsAccept",
        "active": True,
    }
    values.update(overrides)
    return AuctionLot(**values)


def test_object_history_groups_attempts_by_land_object_id_and_not_snapshots(
    session: Session,
) -> None:
    first = _lot(
        "attempt-1",
        land_object_id="23340720260504000001",
        cadastre_number="21-318-001-001",
        auction_starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        start_price_kzt=1_000_000,
        source_search_status="FailureProtocolSigned",
    )
    first.history.append(
        AuctionLotHistory(
            status="Прием заявок",
            start_price_kzt=1_200_000,
            observed_at=datetime(2025, 12, 1, tzinfo=UTC),
        )
    )
    second = _lot(
        "attempt-2",
        land_object_id="23340720260504000001",
        cadastre_number="21-318-001-999",
        auction_starts_at=datetime(2026, 2, 1, tzinfo=UTC),
        start_price_kzt=800_000,
        sale_price_kzt=1_600_000,
        source_search_status="SuccessProtocolSigned",
    )
    same_cadastre_other_object = _lot(
        "different-object",
        land_object_id="DIFFERENT-LAND-ID",
        cadastre_number="21-318-001-001",
    )
    session.add_all((first, second, same_cadastre_other_object))
    session.flush()

    result = auction_object_history(session, first)

    assert result.identity.kind == "land_object_id"
    assert result.identity.confidence == "high"
    assert result.attempts_count == 2
    assert result.failed_count == 1
    assert result.successful_count == 1
    assert result.first_start_price_kzt == pytest.approx(1_000_000)
    assert result.last_start_price_kzt == pytest.approx(800_000)
    assert result.start_price_change_kzt == pytest.approx(-200_000)
    assert result.start_price_change_percent == pytest.approx(-20)
    assert result.sales_with_ratio_count == 1
    assert result.average_sale_to_start_ratio == pytest.approx(2)
    assert [attempt.source_lot_id for attempt in result.attempts] == [
        "attempt-1",
        "attempt-2",
    ]


def test_cadastre_fallback_is_explicit_and_conservative(session: Session) -> None:
    current = _lot("current", cadastre_number="21-318-001-001")
    prior = _lot(
        "prior",
        cadastre_number="21-318-001-001",
        auction_starts_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    identified = _lot(
        "identified",
        cadastre_number="21-318-001-001",
        land_object_id="KNOWN-OBJECT",
    )
    session.add_all((current, prior, identified))
    session.flush()

    identity = auction_object_identity(current)
    result = auction_object_history(session, current)

    assert identity.kind == "cadastre_number"
    assert identity.confidence == "medium"
    assert result.attempts_count == 2
    assert {item.source_lot_id for item in result.attempts} == {"current", "prior"}


def test_official_source_object_url_groups_repeat_auctions_before_cadastre(
    session: Session,
) -> None:
    source_object_url = (
        "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/934/view"
    )
    first = _lot(
        "first",
        source_object_url=source_object_url,
        cadastre_number=None,
        auction_starts_at=datetime(2024, 3, 1, tzinfo=UTC),
        start_price_kzt=1_000_000,
        source_search_status="FailureProtocolSigned",
    )
    second = _lot(
        "second",
        source_object_url=source_object_url,
        cadastre_number="04:061:003:1326",
        auction_starts_at=datetime(2025, 3, 1, tzinfo=UTC),
        start_price_kzt=800_000,
        source_search_status="FailureProtocolSigned",
    )
    third = _lot(
        "third",
        source_object_url=source_object_url,
        cadastre_number="04-061-003-1326",
        auction_starts_at=datetime(2026, 3, 1, tzinfo=UTC),
        start_price_kzt=700_000,
    )
    session.add_all((first, second, third))
    session.flush()

    result = auction_object_history(session, third)

    assert result.identity.kind == "source_object_url"
    assert result.identity.confidence == "high"
    assert result.identity.value == "jerler:934"
    assert result.attempts_count == 3
    assert result.distinct_years_count == 3
    assert result.first_event_year == 2024
    assert result.last_event_year == 2026
    assert result.start_price_trend == "decreased"
    assert result.price_decrease_count == 2
    assert result.failed_count == 2
    assert result.start_price_change_percent == pytest.approx(-30)


def test_incomplete_cadastre_does_not_merge_different_publications(session: Session) -> None:
    first = _lot(
        "first",
        cadastre_number="04:061:001:",
        auction_starts_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    second = _lot(
        "second",
        cadastre_number="04:061:001:",
        auction_starts_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    session.add_all((first, second))
    session.flush()

    identity = auction_object_identity(second)
    result = auction_object_history(session, second)

    assert identity.kind == "none"
    assert identity.confidence == "none"
    assert result.attempts_count == 0


def test_repeat_source_object_is_not_counted_as_a_market_comparable(session: Session) -> None:
    source_object_url = (
        "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/934/view"
    )
    current = _lot(
        "current",
        land_object_id=None,
        cadastre_number=None,
        source_object_url=source_object_url,
    )
    repeat = _lot(
        "repeat",
        land_object_id=None,
        cadastre_number="04:061:003:1326",
        source_object_url=source_object_url,
        sale_price_kzt=10_000_000,
    )
    comparable = _lot(
        "comparable",
        land_object_id=None,
        cadastre_number=None,
        source_object_url=(
            "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/935/view"
        ),
        sale_price_kzt=5_000_000,
    )
    session.add_all((current, repeat, comparable))
    session.flush()

    result = similar_auctions_aggregate(session, current)

    assert result.lots_count == 1
    assert result.sale_price_observation_count == 1
    assert result.average_sale_price_kzt == pytest.approx(5_000_000)


def test_similar_auctions_require_right_scenario_territory_and_area(
    session: Session,
) -> None:
    current = _lot("current", land_object_id="OBJECT-1")
    valid_sale = _lot(
        "valid-sale",
        land_object_id="OBJECT-2",
        area_ha=1.5,
        start_price_kzt=2_000_000,
        sale_price_kzt=5_000_000,
        source_search_status="SuccessProtocolSigned",
    )
    valid_failure = _lot(
        "valid-failure",
        land_object_id="OBJECT-3",
        area_ha=0.5,
        start_price_kzt=3_000_000,
        source_search_status="FailureProtocolSigned",
    )
    wrong_right = _lot(
        "wrong-right",
        land_rights="Частная собственность",
        sale_price_kzt=50_000_000,
    )
    wrong_scenario = _lot(
        "wrong-scenario",
        title="Земельный участок под склад",
        purpose="Строительство склада",
        sale_price_kzt=40_000_000,
    )
    wrong_territory = _lot(
        "wrong-territory",
        locality="Другое село",
        sale_price_kzt=30_000_000,
    )
    wrong_area = _lot("wrong-area", area_ha=3.0, sale_price_kzt=20_000_000)
    wrong_lease_term = _lot(
        "wrong-lease-term",
        lease_term_years=49,
        sale_price_kzt=25_000_000,
    )
    expired = _lot(
        "expired",
        auction_starts_at=datetime(2024, 1, 1, tzinfo=UTC),
        sale_price_kzt=35_000_000,
    )
    repeat = _lot(
        "repeat",
        land_object_id="OBJECT-1",
        sale_price_kzt=10_000_000,
    )
    session.add_all(
        (
            current,
            valid_sale,
            valid_failure,
            wrong_right,
            wrong_scenario,
            wrong_territory,
            wrong_area,
            wrong_lease_term,
            expired,
            repeat,
        )
    )
    session.flush()

    result = similar_auctions_aggregate(session, current)

    assert result.available is True
    assert result.territory_scope == "locality"
    assert result.right_kind == "lease"
    assert result.scenario == "retail"
    assert result.lots_count == 2
    assert result.successful_count == 1
    assert result.failed_count == 1
    assert result.start_price_observation_count == 2
    assert result.average_start_price_kzt == pytest.approx(2_500_000)
    assert result.sale_price_observation_count == 1
    assert result.average_sale_price_kzt == pytest.approx(5_000_000)
    assert result.average_sale_to_start_ratio == pytest.approx(2.5)
    assert result.median_sale_to_start_ratio == pytest.approx(2.5)
    assert result.lease_term_min_years == pytest.approx(3)
    assert result.lease_term_max_years == pytest.approx(10)


def test_cyrillic_failed_status_is_not_also_counted_as_success(
    session: Session,
) -> None:
    current = _lot("current", land_object_id="OBJECT-1")
    failed = _lot(
        "failed",
        land_object_id="OBJECT-2",
        status="Аукцион не состоялся",
        source_search_status=None,
    )
    session.add_all((current, failed))
    session.flush()

    result = similar_auctions_aggregate(session, current)

    assert result.lots_count == 1
    assert result.completed_count == 1
    assert result.successful_count == 0
    assert result.failed_count == 1
    assert result.unresolved_count == 0


def test_nullified_result_with_protocol_price_is_not_a_success(session: Session) -> None:
    current = _lot(
        "current",
        source_object_url=(
            "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/934/view"
        ),
        cadastre_number=None,
        auction_starts_at=datetime(2024, 1, 23, tzinfo=UTC),
        sale_price_kzt=2_489_260,
        status="Состоялся",
        source_search_status="SuccessProtocolSigned",
    )
    nullified = _lot(
        "nullified",
        source_object_url=current.source_object_url,
        cadastre_number=None,
        auction_starts_at=datetime(2023, 1, 25, tzinfo=UTC),
        sale_price_kzt=6_800_790,
        status="Результат торга отменен",
        source_search_status="NullifyResultProtocolSigned",
    )
    session.add_all((current, nullified))
    session.flush()

    result = auction_object_history(session, current)

    assert result.successful_count == 1
    assert result.failed_count == 1
    assert [attempt.outcome for attempt in result.attempts] == ["failure", "success"]
    assert result.attempts[0].sale_price_kzt is None
    assert result.attempts[0].sale_to_start_ratio is None
    assert result.sales_with_ratio_count == 1
    assert result.average_sale_to_start_ratio == pytest.approx(2.48926)


def test_similar_auctions_do_not_guess_without_required_dimensions(
    session: Session,
) -> None:
    current = _lot("current", land_rights=None)
    session.add(current)
    session.flush()

    result = similar_auctions_aggregate(session, current)

    assert result.available is False
    assert result.reason == "unknown_land_right"
    assert result.lots_count == 0


def test_complete_payload_uses_two_queries_without_n_plus_one(session: Session) -> None:
    current = _lot("current", land_object_id="OBJECT-1")
    prior = _lot(
        "prior",
        land_object_id="OBJECT-1",
        auction_starts_at=datetime.now(UTC) - timedelta(days=30),
    )
    comparable = _lot("comparable", land_object_id="OBJECT-2")
    session.add_all((current, prior, comparable))
    session.flush()

    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(session.bind, "before_cursor_execute", record_statement)
    try:
        payload = auction_history_payload(session, current)
    finally:
        event.remove(session.bind, "before_cursor_execute", record_statement)

    assert payload.object_history.attempts_count == 2
    assert payload.similar_auctions.lots_count == 0
    assert payload.similar_auctions.reason == "insufficient_data"
    assert payload.similar_auctions.normalized_status == "insufficient_data"
    assert len(statements) == 2
