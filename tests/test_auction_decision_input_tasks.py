from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import tasks
from app.auction_decision_input_store import RecomputeResult
from app.auction_object_enrichment import (
    JerlerEnrichmentDeferred,
    SourceObjectSyncResult,
)
from app.config import settings
from app.db import Base
from app.models import AuctionLot, AuctionLotGeoCheck

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


class _Lock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.released = False

    def acquire(self, *, blocking: bool) -> bool:
        assert blocking is False
        return self.acquired

    def release(self) -> None:
        self.released = True


class _Redis:
    def __init__(self, locks: dict[str, _Lock]) -> None:
        self.locks = locks

    def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> _Lock:
        assert timeout in {300, 720}
        assert blocking_timeout == 1
        return self.locks[name]


def _factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _enable(monkeypatch, locks: dict[str, _Lock], factory=None) -> None:
    monkeypatch.setattr(settings, "auctions_enabled", True)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *args, **kwargs: _Redis(locks))
    if factory is not None:
        monkeypatch.setattr(tasks, "SessionLocal", factory)


def test_w14_batch_caps_continues_and_enqueues_w13_only_after_changed(monkeypatch) -> None:
    lock = _Lock()
    _enable(
        monkeypatch,
        {"land-scout:lock:auction-decision-inputs": lock},
    )
    observed: dict[str, object] = {}
    results = [
        RecomputeResult(
            lot_id=f"lot-{index}",
            status="insufficient",
            changed=index == 0,
            input_hash=str(index) * 64,
        )
        for index in range(100)
    ]

    def fake_batch(_factory, *, limit):
        observed["limit"] = limit
        return results

    monkeypatch.setattr(tasks, "recompute_decision_input_batch", fake_batch)
    monkeypatch.setattr(
        tasks.recompute_auction_decision_inputs_task,
        "apply_async",
        lambda **kwargs: observed.setdefault("continuation", kwargs),
    )
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_snapshot_recompute",
        lambda **kwargs: observed.setdefault("snapshot", kwargs),
    )

    result = tasks.recompute_auction_decision_inputs_task.run(batch_size=999)

    assert observed["limit"] == 100
    assert result == {
        "status": "ok",
        "selected": 100,
        "processed": 100,
        "changed": 1,
        "errors": 0,
        "busy": 0,
        "has_more": True,
    }
    assert observed["snapshot"] == {"countdown": 1}
    assert observed["continuation"] == {
        "kwargs": {"batch_size": 100},
        "countdown": 2,
    }
    assert lock.released is True


def test_w14_unchanged_batch_quiesces_without_w13(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, {"land-scout:lock:auction-decision-inputs": lock})
    monkeypatch.setattr(tasks, "recompute_decision_input_batch", lambda *a, **k: [])
    snapshot_calls = []
    continuation_calls = []
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_snapshot_recompute",
        lambda **kwargs: snapshot_calls.append(kwargs),
    )
    monkeypatch.setattr(
        tasks.recompute_auction_decision_inputs_task,
        "apply_async",
        lambda **kwargs: continuation_calls.append(kwargs),
    )

    result = tasks.recompute_auction_decision_inputs_task.run()

    assert result["selected"] == 0
    assert result["has_more"] is False
    assert snapshot_calls == []
    assert continuation_calls == []
    assert lock.released is True


def test_w14_locked_batch_reschedules_without_computation(monkeypatch) -> None:
    lock = _Lock(acquired=False)
    _enable(monkeypatch, {"land-scout:lock:auction-decision-inputs": lock})
    called = False

    def fail_batch(*args, **kwargs):
        nonlocal called
        called = True
        return []

    scheduled = []
    monkeypatch.setattr(tasks, "recompute_decision_input_batch", fail_batch)
    monkeypatch.setattr(
        tasks.recompute_auction_decision_inputs_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    result = tasks.recompute_auction_decision_inputs_task.run(batch_size=25)
    assert result["status"] == "skipped_locked"
    assert result["continuation_scheduled"] is True
    assert scheduled[0]["kwargs"] == {"batch_size": 25}
    assert 5 <= scheduled[0]["countdown"] <= 15
    assert called is False


def test_real_w14_task_second_run_is_quiescent(monkeypatch) -> None:
    factory = _factory()
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-452662",
                source="e-qazyna",
                source_lot_id="452662",
                source_url="https://example.test/452662",
                title="Кемпинг",
                purpose="строительство кемпинга",
                object_type="land",
                updated_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            )
        )
    lock = _Lock()
    _enable(
        monkeypatch,
        {"land-scout:lock:auction-decision-inputs": lock},
        factory,
    )
    snapshots = []
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_snapshot_recompute",
        lambda **kwargs: snapshots.append(kwargs),
    )
    first = tasks.recompute_auction_decision_inputs_task.run()
    second = tasks.recompute_auction_decision_inputs_task.run()
    assert first["changed"] == 1, first
    assert second["selected"] == 0
    assert snapshots == [{"countdown": 1}]


def test_jerler_typed_deferral_uses_ceil_continuation_not_retry(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, {"land-scout:lock:auction-source-objects": lock})
    monkeypatch.setattr(
        tasks,
        "sync_auction_source_objects_detached",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            JerlerEnrichmentDeferred("rate_limited", 2.01)
        ),
    )
    scheduled = []
    monkeypatch.setattr(
        tasks.sync_auction_source_objects_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    monkeypatch.setattr(
        tasks.sync_auction_source_objects_task,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("self.retry must not run")),
    )

    result = tasks.sync_auction_source_objects_task.run(
        limit=120,
        ttl_minutes=2,
        error_retry_minutes=999,
    )

    assert result["deferred"] == 1
    assert result["retry_after_seconds"] == 3
    assert scheduled == [
        {
            "kwargs": {
                "limit": 100,
                "ttl_minutes": 5,
                "error_retry_minutes": 120,
                "deferred_attempt": 1,
            },
            "countdown": 3,
        }
    ]
    assert lock.released is True


def test_jerler_partial_deferral_reports_progress_and_schedules_dependents(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, {"land-scout:lock:auction-source-objects": lock})
    monkeypatch.setattr(
        tasks,
        "sync_auction_source_objects_detached",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            JerlerEnrichmentDeferred(
                "rate_limited",
                2.01,
                partial_result=SourceObjectSyncResult(
                    selected=2,
                    fetched=1,
                    updated=1,
                ),
            )
        ),
    )
    continuations = []
    source_refreshes = []
    decision_refreshes = []
    monkeypatch.setattr(
        tasks.sync_auction_source_objects_task,
        "apply_async",
        lambda **kwargs: continuations.append(kwargs),
    )
    monkeypatch.setattr(
        tasks,
        "_schedule_auction_v2_sources_refresh",
        lambda **kwargs: source_refreshes.append(kwargs),
    )
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_input_recompute",
        lambda **kwargs: decision_refreshes.append(kwargs),
    )
    monkeypatch.setattr(
        tasks.sync_auction_source_objects_task,
        "retry",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("self.retry must not run")),
    )

    result = tasks.sync_auction_source_objects_task.run(
        limit=120,
        ttl_minutes=2,
        error_retry_minutes=999,
    )

    assert result == {
        "selected": 2,
        "fetched": 1,
        "updated": 1,
        "skipped_fresh": 0,
        "errors": 0,
        "deferred": 1,
        "retry_after_seconds": 3,
        "continuation_exhausted": 0,
    }
    assert continuations == [
        {
            "kwargs": {
                "limit": 100,
                "ttl_minutes": 5,
                "error_retry_minutes": 120,
                "deferred_attempt": 1,
            },
            "countdown": 3,
        }
    ]
    assert source_refreshes == [{"countdown": 3}]
    assert decision_refreshes == [{"countdown": 2}]
    assert lock.released is True


def test_jerler_typed_deferral_stops_after_bounded_continuations(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, {"land-scout:lock:auction-source-objects": lock})
    monkeypatch.setattr(
        tasks,
        "sync_auction_source_objects_detached",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            JerlerEnrichmentDeferred("rate_limited", 1)
        ),
    )
    scheduled = []
    monkeypatch.setattr(
        tasks.sync_auction_source_objects_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )

    result = tasks.sync_auction_source_objects_task.run(
        limit=20,
        deferred_attempt=tasks.MAX_JERLER_DEFERRED_CONTINUATIONS,
    )

    assert result["deferred"] == 1
    assert result["continuation_exhausted"] == 1
    assert scheduled == []
    assert lock.released is True


def test_jerler_update_schedules_spatial_sources_and_decision_input(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, {"land-scout:lock:auction-source-objects": lock})
    monkeypatch.setattr(
        tasks,
        "sync_auction_source_objects_detached",
        lambda *args, **kwargs: SimpleNamespace(
            selected=1,
            fetched=1,
            updated=1,
            errors=0,
            as_dict=lambda: {
                "selected": 1,
                "fetched": 1,
                "updated": 1,
                "errors": 0,
            },
        ),
    )
    source_runs = []
    decisions = []
    monkeypatch.setattr(
        tasks,
        "_schedule_auction_v2_sources_refresh",
        lambda **kwargs: source_runs.append(kwargs),
    )
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_input_recompute",
        lambda **kwargs: decisions.append(kwargs),
    )

    result = tasks.sync_auction_source_objects_task.run(limit=20)

    assert result["updated"] == 1
    assert source_runs == [{"countdown": 3}]
    assert decisions == [{"countdown": 2}]


def test_refresh_auction_v2_infrastructure_task_selects_due_geo_checks(monkeypatch) -> None:
    factory = _factory()
    with factory() as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_url="https://example.test/lot",
            source_lot_id="lot-1",
            title="lot",
            region="region",
            object_type="land",
            active=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
            auction_starts_at=NOW,
        )
        session.add(lot)
        session.flush()
        session.add(
            AuctionLotGeoCheck(
                lot_id=lot.id,
                coordinate_status="found",
                osm_status="not_checked",
                latitude=43.2,
                longitude=74.4,
            )
        )
        session.commit()
    monkeypatch.setattr(settings, "auctions_enabled", True)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    decisions = []
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_input_recompute",
        lambda **kwargs: decisions.append(kwargs),
    )

    def fake_refresh_batch(_session, lots, *, force=False):
        assert len(lots) == 1
        geo = _session.scalar(
            select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lots[0].id)
        )
        assert geo is not None
        geo.osm_status = "checked"
        return 1, 0

    monkeypatch.setattr(tasks, "_refresh_auction_v2_infrastructure_batch", fake_refresh_batch)

    result = tasks.refresh_auction_v2_infrastructure_task.run(limit=10)

    assert result == {"selected": 1, "checked": 1, "errors": 0}
    assert decisions == [{"countdown": 2}]
