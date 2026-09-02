from __future__ import annotations

from types import SimpleNamespace

from app import tasks
from app.config import settings


class _FakeLock:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.released = False

    def acquire(self, *, blocking: bool) -> bool:
        assert blocking is False
        return self.acquired

    def release(self) -> None:
        self.released = True


class _FakeRedis:
    def __init__(self, lock: _FakeLock) -> None:
        self._lock = lock

    def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> _FakeLock:
        assert name == "land-scout:lock:auction-pipeline"
        assert timeout == 4200
        assert blocking_timeout == 1
        return self._lock


def test_production_task_startup_does_not_run_compatibility_ddl(monkeypatch) -> None:
    called = False

    def fake_init_db() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(tasks, "init_db", fake_init_db)

    tasks._ensure_task_database_ready()

    assert called is False


def test_auction_pipeline_singleton_skips_overlapping_task(monkeypatch) -> None:
    lock = _FakeLock(acquired=False)
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *args, **kwargs: _FakeRedis(lock))
    called = False

    @tasks._auction_pipeline_singleton()
    def work() -> dict[str, int]:
        nonlocal called
        called = True
        return {"done": 1}

    assert work() == {"skipped_locked": 1}
    assert called is False
    assert lock.released is False


def test_auction_pipeline_singleton_releases_lock(monkeypatch) -> None:
    lock = _FakeLock(acquired=True)
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *args, **kwargs: _FakeRedis(lock))

    @tasks._auction_pipeline_singleton()
    def work() -> dict[str, int]:
        return {"done": 1}

    assert work() == {"done": 1}
    assert lock.released is True


def test_verified_market_singleton_drops_overlapping_duplicate(monkeypatch) -> None:
    """A busy singleton owner schedules its own continuation; duplicates must die here."""
    lock = _FakeLock(acquired=False)

    class MarketRedis:
        def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> _FakeLock:
            assert name == "land-scout:lock:auction-verified-market"
            assert timeout == 360
            assert blocking_timeout == 1
            return lock

    monkeypatch.setattr(tasks.Redis, "from_url", lambda *args, **kwargs: MarketRedis())
    scheduled: list[dict[str, object]] = []
    task = SimpleNamespace(
        apply_async=lambda **kwargs: scheduled.append(kwargs),
    )

    @tasks._auction_verified_market_singleton
    def work(_task, **_kwargs) -> dict[str, int]:
        raise AssertionError("overlapping work must not execute")

    result = work(task, phase="ingest", batch_size=100)

    assert result == {
        "status": "skipped_locked",
        "processed": 0,
        "has_more": False,
        "continuation_scheduled": False,
    }
    assert scheduled == []
    assert lock.released is False


def test_current_workflow_seed_stops_cleanly_when_run_is_finalizing(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        tasks,
        "configured_search_statuses",
        lambda: ["ApplicationsAccept", "Pending", "Running"],
    )

    def seed(_session_factory, *, search_status: str, **_kwargs) -> None:
        calls.append(search_status)
        if search_status == "Pending":
            raise ValueError("provider sync run is not active")

    monkeypatch.setattr(tasks, "seed_eqazyna_page_workflow", seed)

    assert tasks._seed_current_eqazyna_workflows("current-run") == [
        "current-run:eq:0:ApplicationsAccept"
    ]
    assert calls == ["ApplicationsAccept", "Pending"]


def test_current_workflow_seed_does_not_hide_unrelated_value_error(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "configured_search_statuses", lambda: ["ApplicationsAccept"])

    def seed(*_args, **_kwargs) -> None:
        raise ValueError("invalid provider unit")

    monkeypatch.setattr(tasks, "seed_eqazyna_page_workflow", seed)

    try:
        tasks._seed_current_eqazyna_workflows("current-run")
    except ValueError as exc:
        assert str(exc) == "invalid provider unit"
    else:
        raise AssertionError("unrelated provider workflow error was suppressed")


def test_history_seed_resumes_caps_and_preserves_exhausted_windows(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        tasks,
        "seed_eqazyna_page_workflow",
        lambda _factory, **kwargs: calls.append(kwargs),
    )
    windows = [("01.01.2024", "31.12.2024"), ("01.01.2023", "31.12.2023")]
    status = "SuccessProtocolSigned"
    checkpoint = {
        tasks.eqazyna_history_checkpoint_key(status, windows[0]): 0,
        tasks.eqazyna_history_checkpoint_key(status, windows[1]): 12,
    }

    keys = tasks._seed_history_eqazyna_workflows(
        "history-run",
        statuses=[status],
        windows=windows,
        checkpoint=checkpoint,
    )

    assert keys == [f"history-run:eq:0:1:{status}"]
    assert len(calls) == 1
    assert calls[0]["publish_date_window"] == windows[1]
    assert calls[0]["start_page"] == 12
    assert calls[0]["skip_existing_details"] is True


def test_history_detail_limit_override_is_explicit_and_bounded() -> None:
    configured = tasks.settings.eqazyna_history_sync_max_lots

    assert tasks._history_detail_limit(None) == configured
    assert tasks._history_detail_limit(8_000) == 8_000
    assert tasks._history_detail_limit(0) == 1
    assert tasks._history_detail_limit(50_000) == 20_000


def test_failed_current_provider_parent_finishes_after_source_dispatch(monkeypatch) -> None:
    """A known-failed catalogue run must not wait on unrelated slow source enrichment."""

    claimed = SimpleNamespace(
        run_key="current-failed",
        run_kind="current",
        action="start_sources",
        payload={"parent_success": False},
    )
    claims = iter([claimed, None])
    finished: list[tuple[str, bool]] = []

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def commit(self) -> None:
            return None

    monkeypatch.setattr(tasks, "SessionLocal", _Session)
    monkeypatch.setattr(tasks, "claim_provider_run_dispatch", lambda *_a, **_k: next(claims))
    monkeypatch.setattr(tasks, "provider_run_crawl_completion", lambda *_a, **_k: (False, set()))
    monkeypatch.setattr(tasks, "prepare_auction_v2_worklist", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tasks.sync_auction_v2_sources_task,
        "apply_async",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(tasks, "complete_provider_run_dispatch", lambda *_a, **_k: True)
    monkeypatch.setattr(tasks, "provider_run_dispatches_complete", lambda *_a, **_k: True)
    monkeypatch.setattr(
        tasks,
        "finish_provider_run",
        lambda _factory, run_key, *, success: finished.append((run_key, success)) or True,
    )

    assert tasks._dispatch_provider_run_outbox(run_key="current-failed", limit=1) == 1
    assert finished == [("current-failed", False)]
