from __future__ import annotations

from app import tasks
from app.auction_eqazyna_verified_sales import EqazynaSaleBatchResult
from app.auction_market_dirty_worker import MarketDirtyWorkerResult
from app.config import settings


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
        assert name == "land-scout:lock:auction-verified-market"
        assert timeout == 360
        assert blocking_timeout == 1
        return self._lock


def _enable(monkeypatch, *, acquired: bool = True) -> _Lock:
    lock = _Lock(acquired)
    monkeypatch.setattr(settings, "auctions_enabled", True)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *args, **kwargs: _Redis(lock))
    return lock


def _batch(*, has_more: bool, generation: int | None = 1) -> EqazynaSaleBatchResult:
    return EqazynaSaleBatchResult(
        status="ok",
        selected=100,
        ingested=3,
        unchanged=96,
        rejected=1,
        rejection_reasons={"coordinates_unknown_or_conflict": 1},
        last_lot_id="lot-100",
        high_water_lot_id="lot-999",
        has_more=has_more,
        duration_ms=120,
        max_source_lag_seconds=3600,
        inventory_generation=generation,
    )


def test_ingest_batch_caps_100_and_continues_keyset(monkeypatch) -> None:
    lock = _enable(monkeypatch)
    observed = {}

    def fake_ingest(*args, **kwargs):
        observed["limit"] = kwargs["limit"]
        return _batch(has_more=True)

    monkeypatch.setattr(tasks, "ingest_eqazyna_verified_sales_batch", fake_ingest)
    scheduled = []
    monkeypatch.setattr(
        tasks.sync_auction_verified_market_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    result = tasks.sync_auction_verified_market_task.run(batch_size=999)
    assert observed["limit"] == 100
    assert result["ingested"] == 3
    assert result["duration_ms"] == 120
    assert scheduled == [
        {
            "kwargs": {
                "phase": "ingest",
                "after_lot_id": "lot-100",
                "high_water_lot_id": "lot-999",
                "batch_size": 100,
                "inventory_changed": True,
            },
            "countdown": 2,
        }
    ]
    assert lock.released is True


def test_finished_ingest_starts_bounded_market_sweep(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "ingest_eqazyna_verified_sales_batch",
        lambda *args, **kwargs: _batch(has_more=False),
    )
    scheduled = []
    monkeypatch.setattr(
        tasks.sync_auction_verified_market_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    tasks.sync_auction_verified_market_task.run(batch_size=100)
    assert scheduled == [{"kwargs": {"phase": "market", "batch_size": 25}, "countdown": 2}]


def test_market_sweep_changed_evidence_enqueues_w14_post_commit_and_continues(
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "recompute_market_dirty_page",
        lambda *args, **kwargs: MarketDirtyWorkerResult("ok", 25, 3, 22, 3, 0, True, "lot-024", 7),
    )
    w14 = []
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_input_recompute",
        lambda **kwargs: w14.append(kwargs),
    )
    continuations = []
    monkeypatch.setattr(
        tasks.sync_auction_verified_market_task,
        "apply_async",
        lambda **kwargs: continuations.append(kwargs),
    )
    result = tasks.sync_auction_verified_market_task.run(phase="market", batch_size=25)
    assert result["changed"] == 3
    assert w14 == [{"countdown": 1}]
    assert continuations[0]["kwargs"] == {
        "phase": "market",
        "batch_size": 25,
    }


def test_unchanged_ingest_quiesces_without_full_market_sweep(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "ingest_eqazyna_verified_sales_batch",
        lambda *args, **kwargs: _batch(has_more=False, generation=None),
    )
    scheduled = []
    monkeypatch.setattr(
        tasks.sync_auction_verified_market_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    result = tasks.sync_auction_verified_market_task.run(batch_size=100)
    assert result["phase"] == "ingest"
    assert scheduled == []


def test_busy_singleton_drops_duplicate_without_amplifying_chain(monkeypatch) -> None:
    _enable(monkeypatch, acquired=False)
    scheduled = []
    monkeypatch.setattr(
        tasks.sync_auction_verified_market_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    result = tasks.sync_auction_verified_market_task.run(phase="market", batch_size=25)
    assert result["status"] == "skipped_locked"
    assert result["continuation_scheduled"] is False
    assert scheduled == []


def test_verified_market_task_is_routed_to_auctions_queue() -> None:
    assert tasks.celery_app.conf.task_routes["land_scout.sync_auction_verified_market"] == {
        "queue": "auctions"
    }
