from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.feedback as feedback
from app.db import Base
from app.models import (
    Account,
    AuctionAccess,
    FeedbackBroadcastRecipient,
    FeedbackConversation,
    FunnelEvent,
    PaymentStatus,
    SearchRequest,
)


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_feedback_broadcast_uses_latest_language_and_tracks_response(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        feedback,
        "telegram_request",
        lambda _method, payload: sent.append(payload) or {"ok": True, "result": {"message_id": 7}},
    )
    now = datetime.now(UTC)
    with build_session() as session:
        session.add(
            FunnelEvent(
                event_name="start_opened",
                telegram_user_id="1001",
                telegram_chat_id="1001",
                language="ru",
                created_at=now - timedelta(days=2),
            )
        )
        session.add(
            FunnelEvent(
                event_name="language_selected",
                telegram_user_id="1001",
                telegram_chat_id="1001",
                language="kz",
                created_at=now,
            )
        )
        session.commit()

        broadcast = feedback.send_feedback_broadcast(session, title="Test", created_by="admin")
        recipient = session.query(FeedbackBroadcastRecipient).one()

        assert broadcast.sent_count == 1
        assert recipient.language == "kz"
        assert recipient.status == "sent"
        assert "Сәлеметсіз бе" in sent[0]["text"]

        feedback.record_client_feedback(
            session,
            text="Жақсы, бірақ карта керек",
            channel="telegram",
            telegram_user_id="1001",
            telegram_chat_id="1001",
            language="kz",
        )
        session.refresh(recipient)
        session.refresh(broadcast)

        assert recipient.status == "responded"
        assert recipient.responded_at is not None
        assert broadcast.responded_count == 1


def test_admin_reply_is_saved_and_sent(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        feedback,
        "telegram_request",
        lambda _method, payload: sent.append(payload) or {"ok": True, "result": {"message_id": 9}},
    )
    with build_session() as session:
        client_message = feedback.record_client_feedback(
            session,
            text="Не понял оплату",
            channel="telegram",
            telegram_user_id="2002",
            telegram_chat_id="2002",
            language="ru",
        )
        reply = feedback.send_admin_feedback_reply(
            session,
            conversation_id=client_message.conversation_id,
            text="Проверим и поправим текст оплаты.",
            admin_user="admin",
        )

        assert reply.delivery_status == "sent"
        assert sent[-1]["chat_id"] == "2002"
        assert "Ответ администратора" in sent[-1]["text"]


def test_feedback_access_summary_detects_paid_sources() -> None:
    with build_session() as session:
        session.add(
            SearchRequest(
                telegram_user_id="3003",
                telegram_chat_id="3003",
                region="Акмолинская область",
                district="Бурабайский район",
                purpose="ЛПХ",
                payment_status=PaymentStatus.paid.value,
            )
        )
        session.add(
            AuctionAccess(
                telegram_user_id="3003",
                telegram_chat_id="3003",
                paid_access=True,
                payment_status=PaymentStatus.paid.value,
            )
        )
        session.commit()

        summary = feedback.feedback_access_summary(
            session,
            telegram_user_id="3003",
        )

        assert summary.kind == "paid"
        assert summary.label == "Оплатил"
        assert "поиск x1" in summary.detail
        assert "аукционы x1" in summary.detail


def test_feedback_access_summary_detects_trial_account() -> None:
    now = datetime.now(UTC)
    with build_session() as session:
        account = Account(
            phone="+77020000001",
            trial_started_at=now - timedelta(hours=1),
            trial_expires_at=now + timedelta(hours=23),
        )
        session.add(account)
        session.commit()

        summary = feedback.feedback_access_summary(session, account_id=account.id)

        assert summary.kind == "trial"
        assert summary.label == "Тестовый доступ"


def test_feedback_conversations_put_unread_client_replies_first() -> None:
    now = datetime.now(UTC)
    with build_session() as session:
        answered = FeedbackConversation(
            telegram_user_id="answered",
            telegram_chat_id="answered",
            language="ru",
            last_message_at=now,
            last_client_message_at=now - timedelta(minutes=3),
            last_admin_message_at=now,
        )
        unread = FeedbackConversation(
            telegram_user_id="unread",
            telegram_chat_id="unread",
            language="ru",
            last_message_at=now - timedelta(hours=1),
            last_client_message_at=now - timedelta(hours=1),
            last_admin_message_at=None,
        )
        session.add_all([answered, unread])
        session.commit()

        conversations = feedback.list_feedback_conversations(session)

        assert conversations[0].telegram_user_id == "unread"
        assert feedback.feedback_conversation_is_unread(conversations[0])
        assert not feedback.feedback_conversation_is_unread(conversations[1])


def test_mark_feedback_conversation_read_clears_unread_state() -> None:
    now = datetime.now(UTC)
    with build_session() as session:
        conversation = FeedbackConversation(
            telegram_user_id="client",
            telegram_chat_id="client",
            language="ru",
            last_message_at=now,
            last_client_message_at=now,
            last_admin_message_at=None,
        )
        session.add(conversation)
        session.commit()

        assert feedback.feedback_conversation_is_unread(conversation)

        marked = feedback.mark_feedback_conversation_read(session, conversation)

        assert marked.last_admin_message_at is not None
        assert not feedback.feedback_conversation_is_unread(marked)
