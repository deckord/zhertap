from __future__ import annotations

from app import tasks
from app.provider_workflow_worker import ProviderWorkflowStepResult


class _Lock:
    released = False

    def acquire(self, *, blocking: bool):
        assert blocking is False
        return True

    def release(self):
        self.released = True


class _Redis:
    def __init__(self, lock: _Lock) -> None:
        self._lock = lock
        self.values: dict[str, str] = {}

    def lock(self, *_args, **_kwargs):
        return self._lock

    def set(self, key, value, *, nx=False, xx=False, ex=None):
        if nx and key in self.values:
            return False
        if xx and key not in self.values:
            return False
        self.values[key] = str(value)
        return True

    def get(self, key):
        value = self.values.get(key)
        return value.encode() if value is not None else None

    def delete(self, key):
        self.values.pop(key, None)


class _BusyLock(_Lock):
    def acquire(self, *, blocking: bool):
        assert blocking is False
        return False


def test_history_backfill_is_periodically_reseeded_for_incremental_coverage() -> None:
    schedule = tasks.beat_schedule["sync-eqazyna-auction-history"]
    assert schedule["task"] == "land_scout.sync_auction_v2_eqazyna_history_backfill"
    assert schedule["schedule"] == 3600


def test_provider_unit_deferral_schedules_exact_cursor_continuation_and_releases_lock(
    monkeypatch,
) -> None:
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    lock = _Lock()
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: _Redis(lock))
    monkeypatch.setattr(
        tasks,
        "process_provider_workflow_step",
        lambda *_args, **_kwargs: ProviderWorkflowStepResult(
            "eq:run", "deferred", "eqazyna_list_page", 299, 29.2
        ),
    )
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        tasks.sync_provider_workflow_task,
        "apply_async",
        lambda **kwargs: (
            scheduled.append(kwargs),
            type("Result", (), {"id": "task-1"})(),
        )[1],
    )
    monkeypatch.setattr(
        tasks.sync_provider_workflow_task,
        "retry",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("self.retry consumed")),
    )

    result = tasks.sync_provider_workflow_task.run(workflow_key="eq:run")

    assert result["status"] == "deferred"
    assert result["pending"] == 299
    assert scheduled == [{"kwargs": {"workflow_key": "eq:run"}, "countdown": 30}]
    assert lock.released is True


def test_provider_unit_progress_continues_same_workflow(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    lock = _Lock()
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: _Redis(lock))
    monkeypatch.setattr(
        tasks,
        "process_provider_workflow_step",
        lambda *_args, **_kwargs: ProviderWorkflowStepResult(
            "osm:run", "progress", "osm_batch", 8
        ),
    )
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        tasks.sync_provider_workflow_task,
        "apply_async",
        lambda **kwargs: (
            scheduled.append(kwargs),
            type("Result", (), {"id": "task-2"})(),
        )[1],
    )

    result = tasks.sync_provider_workflow_task.run(workflow_key="osm:run")

    assert result["status"] == "progress"
    assert scheduled == [{"kwargs": {"workflow_key": "osm:run"}, "countdown": 1}]
    assert lock.released is True


def test_provider_workflow_duplicate_continuation_is_suppressed(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    redis = _Redis(_Lock())
    redis.values[tasks._provider_workflow_continuation_key("eq:run")] = "other-task"
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(
        tasks,
        "process_provider_workflow_step",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate ran")),
    )

    result = tasks.sync_provider_workflow_task.run(workflow_key="eq:run")

    assert result == {"status": "duplicate_suppressed", "pending": 0}


def test_sources_busy_lock_preserves_parent_barrier_continuation(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(
        tasks.Redis, "from_url", lambda *_args, **_kwargs: _Redis(_BusyLock())
    )
    scheduled: list[dict[str, object]] = []
    monkeypatch.setattr(
        tasks.sync_auction_v2_sources_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )

    result = tasks.sync_auction_v2_sources_task.run(
        parent_run_key="parent-1", parent_success=False
    )

    assert result["status"] == "skipped_locked"
    assert result["continuation_scheduled"] is True
    assert len(scheduled) == 1
    assert scheduled[0]["kwargs"] == {
        "parent_run_key": "parent-1",
        "parent_success": False,
    }
    assert 5 <= int(scheduled[0]["countdown"]) <= 15


def test_provider_workflow_recovery_schedules_each_due_cursor_once(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(
        tasks,
        "due_provider_workflow_keys",
        lambda *_args, **_kwargs: ["eq:one", "eq:two", "eq:three"],
    )
    scheduled: list[tuple[str, int]] = []
    monkeypatch.setattr(
        tasks,
        "_schedule_provider_workflow_continuation",
        lambda key, *, countdown: scheduled.append((key, countdown)) or key != "eq:two",
    )

    result = tasks.recover_provider_workflows_task.run(limit=25)

    assert result == {"due": 3, "scheduled": 2, "suppressed": 1}
    assert scheduled == [("eq:one", 1), ("eq:two", 2), ("eq:three", 3)]
