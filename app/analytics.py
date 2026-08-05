import json
import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.models import FunnelEvent

logger = logging.getLogger(__name__)


def excluded_analytics_user_ids() -> set[str]:
    return {
        value.strip()
        for value in settings.analytics_excluded_telegram_user_ids.split(",")
        if value.strip()
    }


def analytics_user_excluded(telegram_user_id: str | None) -> bool:
    return bool(telegram_user_id and telegram_user_id in excluded_analytics_user_ids())


def track_funnel_event(
    session: Session,
    event_name: str,
    *,
    telegram_user_id: str | None = None,
    telegram_chat_id: str | None = None,
    request_id: str | None = None,
    funnel_session_id: str | None = None,
    language: str = "ru",
    metadata: dict | None = None,
) -> None:
    if analytics_user_excluded(telegram_user_id):
        return
    try:
        session.add(
            FunnelEvent(
                event_name=event_name,
                funnel_version=settings.client_funnel_version,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                request_id=request_id,
                funnel_session_id=funnel_session_id,
                language=language,
                metadata_json=(
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True)
                    if metadata
                    else None
                ),
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not record funnel event %s", event_name)
