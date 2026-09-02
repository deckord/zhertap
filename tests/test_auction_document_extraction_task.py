from __future__ import annotations

from app import tasks
from app.auction_document_extraction_writer import (
    DocumentExtractionBatchResult,
    DocumentExtractionOutcome,
)
from app.config import settings
from app.provider_guard import ProviderCallDeferred


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
        self.values: dict[str, str] = {}

    def lock(self, name: str, *, timeout: int, blocking_timeout: int) -> _Lock:
        assert name == "land-scout:lock:auction-document-extraction"
        assert timeout == 360
        assert blocking_timeout == 1
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


def _enable(monkeypatch, lock: _Lock) -> None:
    monkeypatch.setattr(settings, "auctions_enabled", True)
    monkeypatch.setattr(settings, "auction_v2_document_download_enabled", False)
    monkeypatch.setattr(settings, "auction_v2_document_extraction_enabled", True)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(
        tasks.Redis,
        "from_url",
        lambda *args, **kwargs: _Redis(lock),
    )


def _batch(*, has_more: bool = True) -> DocumentExtractionBatchResult:
    return DocumentExtractionBatchResult(
        selected=2,
        written=1,
        already_current=0,
        retryable_errors=1,
        terminal_results=0,
        outcomes=(
            DocumentExtractionOutcome(7, "lot-452662", "written", evidence_id=10),
            DocumentExtractionOutcome(
                8,
                "lot-retry",
                "retryable_error",
                retryable=True,
                retry_after_seconds=60,
            ),
        ),
        coverage=(),
        next_after_document_id=8,
        has_more=has_more,
    )


def test_document_extraction_task_is_bounded_continues_and_enqueues_w14(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, lock)
    observed: dict[str, object] = {}
    events: list[str] = []

    class DownloadResult:
        checked = 4
        downloaded = 3
        errors = 1

    class Session:
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def commit(self):
            self.committed = True
            events.append("download_commit")

    monkeypatch.setattr(tasks, "SessionLocal", Session)
    monkeypatch.setattr(
        tasks,
        "sync_auction_v2_documents",
        lambda *args, **kwargs: events.append("download") or DownloadResult(),
    )

    def fake_extract(_factory, **kwargs):
        events.append("extract")
        observed["extract"] = kwargs
        return _batch()

    monkeypatch.setattr(tasks, "extract_downloaded_auction_documents", fake_extract)
    monkeypatch.setattr(
        tasks,
        "_document_extraction_metrics",
        lambda **kwargs: {"queue_depth": 3, "terminal": 1},
    )
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_input_recompute",
        lambda **kwargs: observed.setdefault("w14", kwargs),
    )
    monkeypatch.setattr(
        tasks.extract_auction_documents_task,
        "apply_async",
        lambda **kwargs: observed.setdefault("continuation", kwargs),
    )

    result = tasks.extract_auction_documents_task.run(
        batch_size=999,
        after_document_id=4,
    )

    extract_args = observed["extract"]
    assert extract_args["limit"] == 20
    assert extract_args["after_document_id"] == 4
    assert extract_args["storage_root"] == settings.auction_v2_document_storage_dir
    assert observed["w14"] == {"countdown": 1}
    assert observed["continuation"] == {
        "kwargs": {"batch_size": 20, "after_document_id": 8},
        "countdown": 2,
    }
    assert result["status"] == "partial"
    assert result["download_checked"] == 4
    assert result["downloaded"] == 3
    assert result["download_errors"] == 1
    assert result["changed_lots"] == 1
    assert result["metrics"] == {"queue_depth": 3, "terminal": 1}
    assert events[:3] == ["download", "download_commit", "extract"]
    assert lock.released is True


def test_document_extraction_without_new_evidence_does_not_enqueue_w14(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, lock)
    result_batch = DocumentExtractionBatchResult(
        selected=1,
        written=0,
        already_current=1,
        retryable_errors=0,
        terminal_results=0,
        outcomes=(DocumentExtractionOutcome(7, "lot-452662", "already_current"),),
        coverage=(),
        next_after_document_id=7,
        has_more=False,
    )
    monkeypatch.setattr(
        tasks,
        "extract_downloaded_auction_documents",
        lambda *args, **kwargs: result_batch,
    )
    monkeypatch.setattr(tasks, "_document_extraction_metrics", lambda **kwargs: {})
    w14_calls = []
    continuation_calls = []
    monkeypatch.setattr(
        tasks,
        "_schedule_decision_input_recompute",
        lambda **kwargs: w14_calls.append(kwargs),
    )
    monkeypatch.setattr(
        tasks.extract_auction_documents_task,
        "apply_async",
        lambda **kwargs: continuation_calls.append(kwargs),
    )

    result = tasks.extract_auction_documents_task.run()

    assert result["status"] == "ok"
    assert result["changed_lots"] == 0
    assert result["continuation_scheduled"] is False
    assert w14_calls == []
    assert continuation_calls == []


def test_document_extraction_continues_when_download_is_deferred(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, lock)
    observed: dict[str, object] = {}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def commit(self):
            raise AssertionError("deferred download should not commit")

    def defer_download(*_args, **_kwargs):
        raise ProviderCallDeferred("auction_documents", "rate_limited", 60)

    def fake_extract(_factory, **kwargs):
        observed["extract"] = kwargs
        return _batch(has_more=False)

    monkeypatch.setattr(tasks, "SessionLocal", Session)
    monkeypatch.setattr(tasks, "sync_auction_v2_documents", defer_download)
    monkeypatch.setattr(tasks, "extract_downloaded_auction_documents", fake_extract)
    monkeypatch.setattr(tasks, "_document_extraction_metrics", lambda **kwargs: {})
    monkeypatch.setattr(tasks, "_schedule_decision_input_recompute", lambda **kwargs: None)

    result = tasks.extract_auction_documents_task.run(batch_size=5)

    assert observed["extract"]["limit"] == 5
    assert result["download_deferred"] is True
    assert result["download_checked"] == 0
    assert result["changed_lots"] == 1


def test_document_extraction_task_passes_llm_extractor_when_enabled(monkeypatch) -> None:
    lock = _Lock()
    _enable(monkeypatch, lock)
    sentinel = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(settings, "auction_v2_llm_enabled", True)
    monkeypatch.setattr(tasks, "_auction_document_extractor_for_runtime", lambda: sentinel)

    def fake_extract(_factory, **kwargs):
        observed["extract"] = kwargs
        return _batch(has_more=False)

    monkeypatch.setattr(tasks, "extract_downloaded_auction_documents", fake_extract)
    monkeypatch.setattr(tasks, "_document_extraction_metrics", lambda **kwargs: {})
    monkeypatch.setattr(tasks, "_schedule_decision_input_recompute", lambda **kwargs: None)

    result = tasks.extract_auction_documents_task.run()

    assert observed["extract"]["extractor"] is sentinel
    assert observed["extract"]["limit"] == 1
    assert result["status"] == "partial"


def test_document_extraction_lock_busy_schedules_single_jittered_continuation(
    monkeypatch,
) -> None:
    lock = _Lock(acquired=False)
    _enable(monkeypatch, lock)
    called = False

    def fail_extract(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("writer must not run while singleton is busy")

    scheduled = []
    monkeypatch.setattr(tasks, "extract_downloaded_auction_documents", fail_extract)
    monkeypatch.setattr(
        tasks.extract_auction_documents_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )

    result = tasks.extract_auction_documents_task.run(
        batch_size=20,
        after_document_id=11,
    )

    assert called is False
    assert result["status"] == "skipped_locked"
    assert scheduled[0]["kwargs"] == {"batch_size": 20, "after_document_id": 11}
    assert 5 <= scheduled[0]["countdown"] <= 15


def test_consumed_document_extraction_continuation_reopens_gate() -> None:
    redis_client = _Redis(_Lock())
    key = tasks.DOCUMENT_EXTRACTION_CONTINUATION_KEY
    redis_client.values[key] = "current-task"

    tasks._consume_document_extraction_continuation(
        redis_client,
        task_id="different-task",
    )
    assert redis_client.get(key) == b"current-task"

    tasks._consume_document_extraction_continuation(
        redis_client,
        task_id="current-task",
    )
    assert redis_client.get(key) is None
    assert redis_client.set(key, "next-task", nx=True, ex=900) is True


def test_busy_reserved_continuation_consumes_gate_before_rescheduling(monkeypatch) -> None:
    lock = _Lock(acquired=False)
    redis_client = _Redis(lock)
    key = tasks.DOCUMENT_EXTRACTION_CONTINUATION_KEY
    redis_client.values[key] = "reserved-task"
    monkeypatch.setattr(
        tasks.Redis,
        "from_url",
        lambda *args, **kwargs: redis_client,
    )
    scheduled = []

    class AsyncResult:
        id = "replacement-task"

    monkeypatch.setattr(
        tasks.extract_auction_documents_task,
        "apply_async",
        lambda **kwargs: scheduled.append(kwargs) or AsyncResult(),
    )

    class Request:
        id = "reserved-task"

    class Task:
        request = Request()

    def fail_writer(*_args, **_kwargs):
        raise AssertionError("writer must not run while singleton is busy")

    wrapped = tasks._auction_document_extraction_singleton(fail_writer)
    result = wrapped(Task(), batch_size=1, after_document_id=808611)

    assert result["status"] == "skipped_locked"
    assert result["continuation_scheduled"] is True
    assert scheduled[0]["kwargs"] == {"batch_size": 1, "after_document_id": 808611}
    assert redis_client.get(key) == b"replacement-task"


def test_document_extraction_task_route_and_limits() -> None:
    assert tasks.celery_app.conf.task_routes["land_scout.extract_auction_documents"] == {
        "queue": "auctions"
    }
    assert tasks.extract_auction_documents_task.soft_time_limit == 240
    assert tasks.extract_auction_documents_task.time_limit == 300


def test_document_extraction_backlog_is_not_processed_while_paused(monkeypatch) -> None:
    monkeypatch.setattr(settings, "auction_v2_document_extraction_enabled", False)
    monkeypatch.setattr(tasks, "_ensure_task_database_ready", lambda: None)
    monkeypatch.setattr(
        tasks,
        "extract_downloaded_auction_documents",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paused extraction must not process backlog")
        ),
    )

    assert tasks.extract_auction_documents_task.run() == {
        "status": "disabled",
        "selected": 0,
        "has_more": False,
    }
    assert "extract-auction-documents" not in tasks.beat_schedule
