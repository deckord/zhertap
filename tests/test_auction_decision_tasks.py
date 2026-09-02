from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import tasks
from app.auction_decision_snapshot import read_current_decision_snapshot
from app.config import settings
from app.db import Base
from app.models import AuctionDecisionSnapshot, AuctionEvidence, AuctionLot

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


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
    def __init__(self, lock: _Lock) -> None:
        self._lock = lock

    def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> _Lock:
        assert name == "land-scout:lock:auction-decision-snapshots"
        assert timeout == 300
        assert blocking_timeout == 1
        return self._lock


def _factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _lot(source_id: str, purpose: str | None = None) -> AuctionLot:
    return AuctionLot(
        source="e-qazyna",
        source_lot_id=source_id,
        source_url=f"https://example.test/{source_id}",
        title="Земельный участок",
        purpose=purpose,
        object_type="land",
        start_price_kzt=100_000,
        updated_at=NOW,
    )


def _enable_task(monkeypatch, factory, *, acquired: bool = True) -> _Lock:
    lock = _Lock(acquired)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(settings, "auctions_enabled", True)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *args, **kwargs: _Redis(lock))
    return lock


def test_worker_task_persists_safe_requires_check_when_module_inputs_missing(
    monkeypatch,
) -> None:
    factory = _factory()
    lot = _lot("452662", "кемпинг")
    with factory() as session, session.begin():
        session.add(lot)
    lock = _enable_task(monkeypatch, factory)

    result = tasks.recompute_auction_decision_snapshots_task.run(batch_size=25)

    assert result["status"] == "ok"
    assert result["processed"] == 1
    assert result["has_more"] is False
    assert lock.released is True
    with factory() as session:
        snapshot = read_current_decision_snapshot(session, lot.id)
        assert snapshot is not None
        assert snapshot.verdict == "requires_check"
        assert snapshot.bid_ceiling_kzt is None


def test_batch_is_capped_and_continuation_keeps_high_water_and_checkpoint(
    monkeypatch,
) -> None:
    factory = _factory()
    lock = _enable_task(monkeypatch, factory)
    observed: dict[str, object] = {}
    worklist = [(f"lot-{index:03}", "resale") for index in range(100)]

    def fake_worklist(**kwargs):
        observed["limit"] = kwargs["limit"]
        return worklist, "lot-999"

    monkeypatch.setattr(tasks, "_decision_snapshot_worklist", fake_worklist)
    monkeypatch.setattr(tasks, "recompute_decision_snapshot", lambda *args, **kwargs: object())

    def fake_apply_async(*, kwargs, countdown):
        observed["continuation"] = kwargs
        observed["countdown"] = countdown

    monkeypatch.setattr(
        tasks.recompute_auction_decision_snapshots_task,
        "apply_async",
        fake_apply_async,
    )
    result = tasks.recompute_auction_decision_snapshots_task.run(batch_size=999, force=True)

    assert observed["limit"] == 100
    assert result["processed"] == 100
    assert result["has_more"] is True
    assert observed["continuation"] == {
        "after_lot_id": "lot-099",
        "high_water_lot_id": "lot-999",
        "batch_size": 100,
        "force": True,
    }
    assert observed["countdown"] == 2
    assert lock.released is True


def test_singleton_skip_does_not_compute(monkeypatch) -> None:
    factory = _factory()
    _enable_task(monkeypatch, factory, acquired=False)
    called = False

    def fail_worklist(**kwargs):
        nonlocal called
        called = True
        return [], None

    monkeypatch.setattr(tasks, "_decision_snapshot_worklist", fail_worklist)
    scheduled = []
    monkeypatch.setattr(
        tasks.recompute_auction_decision_snapshots_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    result = tasks.recompute_auction_decision_snapshots_task.run()
    assert result["status"] == "skipped_locked"
    assert result["continuation_scheduled"] is True
    assert len(scheduled) == 1
    assert called is False


def test_default_scenario_uses_canonical_classifier_mapping() -> None:
    assert tasks._default_decision_scenario(_lot("1", "кемпинг")) == "camping"
    assert tasks._default_decision_scenario(_lot("2", "строительство жилого комплекса")) == (
        "development"
    )
    assert tasks._default_decision_scenario(_lot("3", "строительство магазина")) == (
        "operating_business"
    )
    assert tasks._default_decision_scenario(_lot("4", None)) == "unclassified"


def test_unchanged_exact_policy_snapshot_is_not_selected_again(monkeypatch) -> None:
    factory = _factory()
    lot = _lot("stable", "кемпинг")
    with factory() as session, session.begin():
        session.add(lot)
    _enable_task(monkeypatch, factory)
    tasks.recompute_auction_decision_snapshots_task.run()

    worklist, _ = tasks._decision_snapshot_worklist(
        after_lot_id=None,
        high_water_lot_id=None,
        limit=25,
        force=False,
    )
    assert worklist == []
    with factory() as session:
        assert len(list(session.scalars(select(AuctionLot)))) == 1


def test_stale_snapshot_without_changed_input_quiesces(monkeypatch) -> None:
    factory = _factory()
    lot = _lot("stale", "кемпинг")
    with factory() as session, session.begin():
        session.add(lot)
    _enable_task(monkeypatch, factory)
    tasks.recompute_auction_decision_snapshots_task.run()
    with factory() as session, session.begin():
        snapshot = session.scalar(select(AuctionDecisionSnapshot))
        assert snapshot is not None
        snapshot.stale = True
    worklist, _ = tasks._decision_snapshot_worklist(
        after_lot_id=None,
        high_water_lot_id=None,
        limit=25,
        force=False,
    )
    assert worklist == []


def test_identical_new_evidence_is_validated_once_and_error_evidence_is_ignored(
    monkeypatch,
) -> None:
    factory = _factory()
    lot = _lot("watermark", "кемпинг")
    payload = '{"status":"clear"}'
    with factory() as session, session.begin():
        session.add(lot)
        session.flush()
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="decision_input:legal_passport",
                status="found",
                title="first",
                raw_payload_json=payload,
                observed_at=NOW,
            )
        )
    _enable_task(monkeypatch, factory)
    tasks.recompute_auction_decision_snapshots_task.run()
    with factory() as session, session.begin():
        session.add_all(
            [
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:legal_passport",
                    status="found",
                    title="same-payload-new-row",
                    raw_payload_json=payload,
                    observed_at=NOW,
                ),
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:geometry_context",
                    status="error",
                    title="ignored-error",
                    raw_payload_json='{"error":"temporary"}',
                    observed_at=NOW,
                ),
            ]
        )
    first_worklist, _ = tasks._decision_snapshot_worklist(
        after_lot_id=None,
        high_water_lot_id=None,
        limit=25,
        force=False,
    )
    assert first_worklist == [(lot.id, "camping")]
    tasks.recompute_auction_decision_snapshots_task.run()
    second_worklist, _ = tasks._decision_snapshot_worklist(
        after_lot_id=None,
        high_water_lot_id=None,
        limit=25,
        force=False,
    )
    assert second_worklist == []
    with factory() as session:
        snapshots = list(session.scalars(select(AuctionDecisionSnapshot)))
        assert len(snapshots) == 1
        assert snapshots[0].validated_evidence_id > 0


def test_active_history_generation_enqueues_decision_input_recompute(monkeypatch) -> None:
    factory = _factory()
    history_lock = _Lock()
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(settings, "auctions_enabled", True)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(
        tasks.Redis,
        "from_url",
        lambda *args, **kwargs: _RedisHistory(history_lock),
    )
    monkeypatch.setattr(
        tasks,
        "normalize_auction_history_step",
        lambda *args, **kwargs: {"status": "active", "has_more": False},
    )
    scheduled = []
    market_scheduled = []
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_input_recompute",
        lambda **kwargs: scheduled.append(kwargs),
    )
    monkeypatch.setattr(
        tasks,
        "_schedule_verified_market_sync",
        lambda **kwargs: market_scheduled.append(kwargs),
    )
    result = tasks.normalize_auction_history_task.run()
    assert result["status"] == "active"
    assert scheduled == [{"countdown": 2}]
    assert market_scheduled == [{"countdown": 1}]


class _RedisHistory:
    def __init__(self, lock: _Lock) -> None:
        self._lock = lock

    def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> _Lock:
        assert name == "land-scout:lock:auction-history-normalization"
        assert timeout == 300
        assert blocking_timeout == 1
        return self._lock
