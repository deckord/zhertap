from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auction_service import refresh_due_eqazyna_lot_statuses
from app.db import Base
from app.models import AuctionLot
from app.providers.eqazyna import AuctionLotData, EqazynaError


class _DetailProvider:
    def __init__(self, auction_starts_at: datetime) -> None:
        self.urls: list[str] = []
        self.auction_starts_at = auction_starts_at

    def lot_detail(self, url: str) -> AuctionLotData:
        self.urls.append(url)
        return AuctionLotData(
            source_lot_id="running-no-list-code",
            source_url=url,
            title="Земельный участок",
            object_type="land",
            status="Проводится",
            source_search_status=None,
            auction_starts_at=self.auction_starts_at,
        )


class _FailingProvider:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def lot_detail(self, url: str) -> AuctionLotData:
        self.urls.append(url)
        raise EqazynaError("official detail card unavailable")


def test_due_refresh_includes_running_detail_status_without_list_code() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    auction_starts_at = datetime.now(UTC) - timedelta(hours=2)
    provider = _DetailProvider(auction_starts_at)
    with Session(engine) as session:
        session.add(
            AuctionLot(
                source="e-qazyna",
                source_lot_id="running-no-list-code",
                source_search_status=None,
                object_type="land",
                status="Проводится",
                title="Земельный участок",
                source_url="https://example.test/running",
                active=True,
                auction_starts_at=auction_starts_at,
            )
        )
        session.commit()

        result = refresh_due_eqazyna_lot_statuses(session, provider=provider, limit=5)

    assert result["selected"] == 1
    assert result["terminal"] == 0
    assert result["errors"] == 0
    assert provider.urls == ["https://example.test/running"]


def test_due_detail_refresh_preserves_existing_list_status() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    auction_starts_at = datetime.now(UTC) - timedelta(hours=2)
    provider = _DetailProvider(auction_starts_at)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="running-no-list-code",
            source_search_status="Running",
            object_type="land",
            status="Проводится",
            title="Земельный участок",
            source_url="https://example.test/running",
            active=True,
            auction_starts_at=auction_starts_at,
        )
        session.add(lot)
        session.commit()

        result = refresh_due_eqazyna_lot_statuses(session, provider=provider, limit=5)
        session.refresh(lot)

        assert result["errors"] == 0
        assert lot.source_search_status == "Running"


def test_due_refresh_durable_cursor_prevents_failed_newest_batch_starvation() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    provider = _FailingProvider()
    now = datetime.now(UTC)
    with Session(engine) as session:
        for index in range(6):
            session.add(
                AuctionLot(
                    source="e-qazyna",
                    source_lot_id=f"running-{index}",
                    source_search_status="Running",
                    object_type="land",
                    status="Проводится",
                    title="Земельный участок",
                    source_url=f"https://example.test/running-{index}",
                    active=True,
                    auction_starts_at=now - timedelta(hours=index + 1),
                )
            )
        session.commit()

        first = refresh_due_eqazyna_lot_statuses(session, provider=provider, limit=5)

    # A fresh session proves cursor progress survives the worker process/session.
    with Session(engine) as session:
        second = refresh_due_eqazyna_lot_statuses(session, provider=provider, limit=5)

    assert first == {"selected": 5, "changed": 0, "terminal": 0, "errors": 5}
    assert second == {"selected": 1, "changed": 0, "terminal": 0, "errors": 1}
    assert provider.urls[-1] == "https://example.test/running-5"
