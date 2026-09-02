from __future__ import annotations

from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError

from app import tasks


class _FakeTask:
    def __init__(self) -> None:
        self.scheduled: list[dict[str, Any]] = []

    def apply_async(self, **kwargs: object) -> None:
        self.scheduled.append(dict(kwargs))


class _BusyLock:
    def acquire(self, *, blocking: bool) -> bool:
        assert blocking is False
        return False


class _BusyRedis:
    def lock(self, *_args: object, **_kwargs: object) -> _BusyLock:
        return _BusyLock()


def test_history_lock_busy_schedules_one_bounded_continuation(monkeypatch) -> None:
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: _BusyRedis())
    called = False

    @tasks._auction_history_singleton
    def guarded(_task: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"status": "unexpected"}

    fake_task = _FakeTask()
    result = guarded(fake_task, generation=7, batch_size=200)

    assert called is False
    assert result == {
        "status": "skipped_locked",
        "has_more": False,
        "continuation_scheduled": True,
    }
    assert len(fake_task.scheduled) == 1
    assert fake_task.scheduled[0]["kwargs"] == {"generation": 7, "batch_size": 200}
    assert 5 <= int(fake_task.scheduled[0]["countdown"]) <= 15


def test_history_lock_redis_failure_uses_database_guard(monkeypatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise RedisConnectionError("offline")

    monkeypatch.setattr(tasks.Redis, "from_url", unavailable)

    @tasks._auction_history_singleton
    def guarded(_task: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "db_guarded"}

    assert guarded(_FakeTask(), generation=None) == {"status": "db_guarded"}


def test_history_task_is_routed_to_auction_queue() -> None:
    routes = tasks.celery_app.conf.task_routes
    assert routes["land_scout.normalize_auction_history"] == {"queue": "auctions"}
