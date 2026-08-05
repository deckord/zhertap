from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks as tasks
from app.db import Base
from app.models import FreePreviewStatus, SearchRequest, SearchStatus


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _request(*, status: str, preview_status: str, updated_at: datetime) -> SearchRequest:
    return SearchRequest(
        region="Region",
        district="District",
        locality="Locality",
        telegram_user_id="1001",
        telegram_chat_id="1001",
        status=status,
        free_preview_status=preview_status,
        updated_at=updated_at,
    )


def test_recovery_does_not_redeliver_old_or_already_notified_requests(monkeypatch) -> None:
    factory = _session_factory()
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    now = datetime.now(UTC)

    with factory() as session:
        old_ready = _request(
            status=SearchStatus.ready.value,
            preview_status=FreePreviewStatus.not_requested.value,
            updated_at=now - timedelta(days=1),
        )
        already_notified = _request(
            status=SearchStatus.ready.value,
            preview_status=FreePreviewStatus.not_requested.value,
            updated_at=now,
        )
        already_notified.search_completed_notified_at = now
        old_pending = _request(
            status=SearchStatus.ready.value,
            preview_status=FreePreviewStatus.pending.value,
            updated_at=now - timedelta(days=1),
        )
        fresh_pending = _request(
            status=SearchStatus.ready.value,
            preview_status=FreePreviewStatus.pending.value,
            updated_at=now,
        )
        fresh_ready = _request(
            status=SearchStatus.ready.value,
            preview_status=FreePreviewStatus.not_requested.value,
            updated_at=now,
        )
        session.add_all(
            [old_ready, already_notified, old_pending, fresh_pending, fresh_ready]
        )
        session.commit()
        fresh_pending_id = fresh_pending.id
        fresh_ready_id = fresh_ready.id

    stale_ids, pending_free_ids, ready_delivery_ids = tasks._recover_stale_searches()

    assert stale_ids == []
    assert pending_free_ids == [fresh_pending_id]
    assert ready_delivery_ids == [fresh_ready_id]
