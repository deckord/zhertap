from __future__ import annotations

from types import SimpleNamespace

from app import tasks
from app.auction_spatial_evidence_writer import (
    SpatialFeedIdentity,
    SpatialWorkClaim,
    SpatialWorklistResult,
)
from app.auction_spatial_outbox import (
    SpatialDispatchReport,
    SpatialOutboxClaim,
)
from app.auction_spatial_worker import SpatialClaimResult, SpatialSeedResult
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
        self.lock_value = lock

    def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> _Lock:
        assert name == "land-scout:lock:auction-spatial-feeds"
        assert timeout == 360
        assert blocking_timeout == 1
        return self.lock_value


def _enable(monkeypatch, lock: _Lock | None = None) -> None:
    monkeypatch.setattr(settings, "auctions_enabled", True)
    monkeypatch.setattr(settings, "auction_spatial_feed_enabled", True)
    monkeypatch.setattr(settings, "auction_spatial_batch_size", 10)
    monkeypatch.setattr(settings, "auction_spatial_providers_json", "{}")
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    if lock is not None:
        monkeypatch.setattr(
            tasks.Redis,
            "from_url",
            lambda *args, **kwargs: _Redis(lock),
        )


def test_spatial_task_caps_batch_and_uses_typed_delayed_continuation(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, lock)
    identity = SpatialFeedIdentity("lot-1", "planning", "abay-gis", "planning-1")
    claim = SpatialWorkClaim(identity, "token", "a" * 64)
    observed: dict[str, object] = {}
    runtime = SimpleNamespace(policies={"abay-gis": object()})
    monkeypatch.setattr(tasks, "parse_spatial_fetch_runtime", lambda value: runtime)
    monkeypatch.setattr(
        tasks,
        "create_redis_provider_backpressure",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        tasks,
        "seed_spatial_feed_states",
        lambda *args, **kwargs: SpatialSeedResult(0, 0, False, None, None, False),
    )

    class Store:
        def __init__(self, _session) -> None:
            pass

        def claim_due(self, **kwargs):
            observed["claim"] = kwargs
            return SpatialWorklistResult((claim,), ())

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(tasks, "SessionLocal", Session)
    monkeypatch.setattr(tasks, "SqlAlchemySpatialEvidenceStore", Store)
    monkeypatch.setattr(
        tasks,
        "process_spatial_claim",
        lambda *args, **kwargs: SpatialClaimResult(
            "retryable", "lot-1", False, 0.7
        ),
    )
    monkeypatch.setattr(tasks, "_spatial_worker_metrics", lambda **kwargs: {"queue_depth": 1})
    continuations = []
    monkeypatch.setattr(
        tasks.process_auction_spatial_feeds_task,
        "apply_async",
        lambda **kwargs: continuations.append(kwargs),
    )

    result = tasks.process_auction_spatial_feeds_task.run(batch_size=999)

    assert observed["claim"]["limit"] == 50
    assert result["retryable"] == 1
    assert result["continuation_scheduled"] is True
    assert continuations == [
        {
            "kwargs": {
                "batch_size": 50,
                "seed_after_lot_id": None,
                "seed_high_water_lot_id": None,
            },
            "countdown": 1,
        }
    ]
    assert lock.released is True


def test_spatial_lock_busy_reschedules_without_claiming(monkeypatch) -> None:
    lock = _Lock(acquired=False)
    _enable(monkeypatch, lock)
    scheduled = []
    monkeypatch.setattr(
        tasks.process_auction_spatial_feeds_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    result = tasks.process_auction_spatial_feeds_task.run(batch_size=7)
    assert result["status"] == "skipped_locked"
    assert result["continuation_scheduled"] is True
    assert scheduled[0]["kwargs"] == {"batch_size": 7}
    assert 5 <= scheduled[0]["countdown"] <= 15


def test_spatial_outbox_enqueues_exact_lots_to_w14_before_ack(monkeypatch) -> None:
    _enable(monkeypatch)
    observed: dict[str, object] = {}
    claim = SpatialOutboxClaim(
        1,
        "lot-452662",
        "a" * 64,
        7,
        1,
        tasks.datetime.now(tasks.UTC),
    )

    def fake_dispatch(_factory, enqueue, **kwargs):
        enqueue((claim,))
        observed["dispatch"] = kwargs
        return SpatialDispatchReport(1, 1, 0, False)

    monkeypatch.setattr(tasks, "dispatch_spatial_decision_outbox", fake_dispatch)
    monkeypatch.setattr(
        tasks.recompute_auction_decision_inputs_task,
        "apply_async",
        lambda **kwargs: observed.setdefault("w14", kwargs),
    )
    result = tasks.dispatch_auction_spatial_outbox_task.run(batch_size=999)
    assert observed["dispatch"]["limit"] == 100
    assert observed["w14"] == {
        "kwargs": {"batch_size": 1, "lot_ids": ["lot-452662"]},
        "countdown": 1,
    }
    assert result["dispatched"] == 1
    assert result["continuation_scheduled"] is False


def test_spatial_outbox_failure_schedules_retry_without_sleep(monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "dispatch_spatial_decision_outbox",
        lambda *args, **kwargs: SpatialDispatchReport(1, 0, 1, False, 2.2),
    )
    scheduled = []
    monkeypatch.setattr(
        tasks.dispatch_auction_spatial_outbox_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    result = tasks.dispatch_auction_spatial_outbox_task.run(batch_size=50)
    assert result["failed"] == 1
    assert scheduled == [{"kwargs": {"batch_size": 50}, "countdown": 3}]


def test_w14_exact_lot_ids_bypass_global_worklist(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch)
    monkeypatch.setattr(
        tasks.Redis,
        "from_url",
        lambda *args, **kwargs: SimpleNamespace(
            lock=lambda *args, **kwargs: lock
        ),
    )
    observed = []
    monkeypatch.setattr(
        tasks,
        "recompute_decision_inputs",
        lambda _factory, lot_id: observed.append(lot_id)
        or SimpleNamespace(status="insufficient", changed=False),
    )
    monkeypatch.setattr(
        tasks,
        "recompute_decision_input_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("global worklist must not be used")
        ),
    )
    result = tasks.recompute_auction_decision_inputs_task.run(
        lot_ids=["lot-b", "lot-a", "lot-b"], batch_size=25
    )
    assert observed == ["lot-b", "lot-a"]
    assert result["selected"] == 2
    assert result["has_more"] is False
