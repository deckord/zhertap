from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import tasks
from app.db import Base
from app.models import AuctionLandIdentityBackfillCursor, AuctionLot


def _lot(*, lot_id: str, source_lot_id: str, land_object_id: str | None) -> AuctionLot:
    return AuctionLot(
        id=lot_id,
        source="e-qazyna",
        source_lot_id=source_lot_id,
        object_type="land",
        title=source_lot_id,
        source_url=f"https://sauda.e-qazyna.kz/ru/list/{source_lot_id}",
        land_object_id=land_object_id,
        last_seen_at=datetime.now(UTC),
    )


def test_canonical_identity_task_persists_cursor_and_schedules_bounded_continuation(
    tmp_path, monkeypatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'identity-task.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                _lot(
                    lot_id="00000000-0000-0000-0000-000000000001",
                    source_lot_id="cursor-first",
                    land_object_id="23340720260504000001",
                ),
                _lot(
                    lot_id="00000000-0000-0000-0000-000000000002",
                    source_lot_id="cursor-second",
                    land_object_id="23340720260504000002",
                ),
            ]
        )
        session.commit()

    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(
        tasks.backfill_canonical_land_identities_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )

    first = tasks.backfill_canonical_land_identities_task.run(batch_size=1)
    with factory() as session:
        cursor = session.get(AuctionLandIdentityBackfillCursor, "default")
        assert cursor is not None
        assert cursor.after_lot_id == "00000000-0000-0000-0000-000000000001"
        assert cursor.high_water_lot_id == "00000000-0000-0000-0000-000000000002"
        assert cursor.cycle_count == 0

    second = tasks.backfill_canonical_land_identities_task.run(batch_size=1)
    with factory() as session:
        cursor = session.get(AuctionLandIdentityBackfillCursor, "default")
        linked = list(
            session.scalars(
                select(AuctionLot).where(AuctionLot.land_object_ref_id.is_not(None))
            )
        )
        assert cursor is not None
        assert cursor.after_lot_id is None
        assert cursor.high_water_lot_id is None
        assert cursor.cycle_count == 1
        assert len(linked) == 2

    assert first["has_more"] is True
    assert second["has_more"] is False
    assert scheduled == [{"kwargs": {"batch_size": 1}, "countdown": 2}]


def test_canonical_identity_task_is_hourly_and_routed_to_auction_queue() -> None:
    assert tasks.beat_schedule["backfill-canonical-land-identities"] == {
        "task": "land_scout.backfill_canonical_land_identities",
        "schedule": 3600,
    }
    assert tasks.celery_app.conf.task_routes[
        "land_scout.backfill_canonical_land_identities"
    ] == {"queue": "auctions"}
