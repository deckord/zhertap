from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import services
from app.db import Base
from app.models import SearchRequest, SearchStatus


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_excluded_telegram_user_is_not_counted_in_active_queue() -> None:
    with build_session() as session:
        session.add_all(
            [
                SearchRequest(
                    telegram_user_id="70557953",
                    telegram_chat_id="70557953",
                    region="Акмолинская область",
                    district="Бурабайский район",
                    purpose="ЛПХ",
                    status=SearchStatus.queued.value,
                ),
                SearchRequest(
                    telegram_user_id="real-user",
                    telegram_chat_id="real-user",
                    region="Акмолинская область",
                    district="Бурабайский район",
                    purpose="ЛПХ",
                    status=SearchStatus.processing.value,
                ),
            ]
        )
        session.commit()

        visible_ids = session.scalars(
            select(SearchRequest.telegram_user_id).where(services.search_queue_visible_condition())
        ).all()

        assert services.active_search_queue_count(session) == 1
        assert "real-user" in visible_ids
        assert "70557953" not in visible_ids
