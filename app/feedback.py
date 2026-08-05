from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.access import account_has_active_trial, account_has_paid_access
from app.i18n import normalize_language
from app.models import (
    Account,
    AuctionAccess,
    AuctionSubscription,
    FeedbackBroadcast,
    FeedbackBroadcastRecipient,
    FeedbackConversation,
    FeedbackMessage,
    FunnelEvent,
    PaymentStatus,
    SearchRequest,
)
from app.services import telegram_request

DEFAULT_FEEDBACK_RU = (
    "Здравствуйте. Мы развиваем Жертап и хотим понять, что нужно улучшить.\n\n"
    "Напишите, пожалуйста:\n"
    "1. Что понравилось?\n"
    "2. Что было непонятно или не сработало?\n"
    "3. Какой новый функционал нужен в первую очередь?\n\n"
    "Ответ можно отправить прямо сюда одним сообщением."
)

DEFAULT_FEEDBACK_KZ = (
    "Сәлеметсіз бе. Біз Жертап сервисін дамытып жатырмыз және нені жақсарту "
    "керек екенін білгіміз келеді.\n\n"
    "Жазыңызшы:\n"
    "1. Не ұнады?\n"
    "2. Не түсініксіз болды немесе дұрыс істемеді?\n"
    "3. Қандай жаңа функция бірінші керек?\n\n"
    "Жауапты осы чатқа бір хабарлама ретінде жібере аласыз."
)


@dataclass
class FeedbackRecipient:
    telegram_user_id: str
    telegram_chat_id: str
    language: str
    account_id: str | None = None
    phone: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class FeedbackAccessSummary:
    kind: str
    label: str
    detail: str


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_text(text: str, *, limit: int = 5000) -> str:
    return " ".join(text.strip().split())[:limit]


def _format_access_sources(
    *,
    account_paid: bool,
    paid_searches: int,
    paid_auction_accesses: int,
) -> str:
    sources: list[str] = []
    if account_paid:
        sources.append("аккаунт")
    if paid_searches:
        sources.append(f"поиск x{paid_searches}")
    if paid_auction_accesses:
        sources.append(f"аукционы x{paid_auction_accesses}")
    return ", ".join(sources)


def feedback_access_summary(
    session: Session,
    *,
    account_id: str | None = None,
    telegram_user_id: str | None = None,
) -> FeedbackAccessSummary:
    account = session.get(Account, account_id) if account_id else None
    if account and not telegram_user_id:
        telegram_user_id = account.telegram_user_id

    account_paid = bool(account and account_has_paid_access(account))
    paid_searches = 0
    paid_auction_accesses = 0
    if telegram_user_id:
        paid_searches = int(
            session.scalar(
                select(func.count(SearchRequest.id)).where(
                    SearchRequest.telegram_user_id == telegram_user_id,
                    SearchRequest.payment_status == PaymentStatus.paid.value,
                    (SearchRequest.access_expires_at.is_(None))
                    | (SearchRequest.access_expires_at > datetime.now(UTC)),
                )
            )
            or 0
        )
        paid_auction_accesses = int(
            session.scalar(
                select(func.count(AuctionAccess.id)).where(
                    AuctionAccess.telegram_user_id == telegram_user_id,
                    AuctionAccess.paid_access.is_(True),
                    (AuctionAccess.access_expires_at.is_(None))
                    | (AuctionAccess.access_expires_at > datetime.now(UTC)),
                )
            )
            or 0
        )

    paid_detail = _format_access_sources(
        account_paid=account_paid,
        paid_searches=paid_searches,
        paid_auction_accesses=paid_auction_accesses,
    )
    if paid_detail:
        return FeedbackAccessSummary(kind="paid", label="Оплатил", detail=paid_detail)

    if account and account_has_active_trial(account):
        expires_at = account.trial_expires_at
        expires_label = expires_at.strftime("%d.%m.%Y %H:%M") if expires_at else ""
        return FeedbackAccessSummary(
            kind="trial",
            label="Тестовый доступ",
            detail=f"до {expires_label}" if expires_label else "активен",
        )

    return FeedbackAccessSummary(
        kind="free",
        label="Не оплатил",
        detail="оплат не найдено",
    )


def conversation_access_summary(
    session: Session, conversation: FeedbackConversation
) -> FeedbackAccessSummary:
    return feedback_access_summary(
        session,
        account_id=conversation.account_id,
        telegram_user_id=conversation.telegram_user_id,
    )


def recipient_access_summary(
    session: Session, recipient: FeedbackBroadcastRecipient
) -> FeedbackAccessSummary:
    return feedback_access_summary(
        session,
        account_id=recipient.account_id,
        telegram_user_id=recipient.telegram_user_id,
    )


def feedback_start_text(language: str | None) -> str:
    if normalize_language(language) == "kz":
        return (
            "Кері байланыс\n\n"
            "Не ұнады, не ыңғайсыз болды, қандай қате кездестірдіңіз немесе қандай "
            "функция керек екенін жазыңыз. Хабарламаңызды толық мәтінмен жіберіңіз."
        )
    return (
        "Обратная связь\n\n"
        "Напишите, что понравилось, что было неудобно, какие ошибки встретились "
        "или какой функционал нужен. Отправьте текст одним сообщением."
    )


def feedback_thanks_text(language: str | None) -> str:
    if normalize_language(language) == "kz":
        return "Рақмет. Пікіріңіз сақталды, админ оны панельден көреді."
    return "Спасибо. Отзыв сохранён, админ увидит его в панели."


def has_pending_feedback_request(session: Session, telegram_user_id: str) -> bool:
    return (
        session.scalar(
            select(FeedbackBroadcastRecipient.id).where(
                FeedbackBroadcastRecipient.telegram_user_id == telegram_user_id,
                FeedbackBroadcastRecipient.status == "sent",
                FeedbackBroadcastRecipient.responded_at.is_(None),
            )
        )
        is not None
    )


def admin_reply_prefix(language: str | None) -> str:
    if normalize_language(language) == "kz":
        return "Жертап әкімшісінен жауап:"
    return "Ответ администратора Жертап:"


def get_latest_telegram_language(session: Session, telegram_user_id: str) -> str:
    candidates: list[tuple[datetime, str]] = []
    for created_at, language in session.execute(
        select(FunnelEvent.created_at, FunnelEvent.language).where(
            FunnelEvent.telegram_user_id == telegram_user_id,
            FunnelEvent.language.is_not(None),
        )
    ):
        candidates.append((created_at, normalize_language(language)))
    for created_at, language in session.execute(
        select(SearchRequest.created_at, SearchRequest.language).where(
            SearchRequest.telegram_user_id == telegram_user_id,
            SearchRequest.language.is_not(None),
        )
    ):
        candidates.append((created_at, normalize_language(language)))
    for updated_at, language in session.execute(
        select(AuctionAccess.updated_at, AuctionAccess.language).where(
            AuctionAccess.telegram_user_id == telegram_user_id,
            AuctionAccess.language.is_not(None),
        )
    ):
        candidates.append((updated_at, normalize_language(language)))
    for updated_at, language in session.execute(
        select(AuctionSubscription.updated_at, AuctionSubscription.language).where(
            AuctionSubscription.telegram_user_id == telegram_user_id,
            AuctionSubscription.language.is_not(None),
        )
    ):
        candidates.append((updated_at, normalize_language(language)))
    if not candidates:
        return "ru"
    return max(candidates, key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC))[1]


def get_or_create_feedback_conversation(
    session: Session,
    *,
    account: Account | None = None,
    telegram_user_id: str | None = None,
    telegram_chat_id: str | None = None,
    language: str | None = None,
    phone: str | None = None,
) -> FeedbackConversation:
    conversation: FeedbackConversation | None = None
    if account is not None:
        conversation = session.scalar(
            select(FeedbackConversation).where(FeedbackConversation.account_id == account.id)
        )
        telegram_user_id = telegram_user_id or account.telegram_user_id
        telegram_chat_id = telegram_chat_id or account.telegram_chat_id
        phone = phone or account.phone
    if conversation is None and telegram_user_id:
        conversation = session.scalar(
            select(FeedbackConversation).where(
                FeedbackConversation.telegram_user_id == telegram_user_id
            )
        )
    if conversation is None:
        conversation = FeedbackConversation(
            account_id=account.id if account else None,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            phone=phone,
            language=normalize_language(language),
        )
        session.add(conversation)
        session.flush()

    if account is not None and not conversation.account_id:
        conversation.account_id = account.id
    if telegram_user_id:
        conversation.telegram_user_id = telegram_user_id
    if telegram_chat_id:
        conversation.telegram_chat_id = telegram_chat_id
    if phone:
        conversation.phone = phone
    if language:
        conversation.language = normalize_language(language)
    conversation.status = "open"
    return conversation


def _mark_broadcast_responded(session: Session, conversation: FeedbackConversation) -> None:
    if not conversation.telegram_user_id:
        return
    recipients = session.scalars(
        select(FeedbackBroadcastRecipient).where(
            FeedbackBroadcastRecipient.telegram_user_id == conversation.telegram_user_id,
            FeedbackBroadcastRecipient.responded_at.is_(None),
            FeedbackBroadcastRecipient.status == "sent",
        )
    ).all()
    responded_at = _now()
    broadcast_ids: set[str] = set()
    for recipient in recipients:
        recipient.responded_at = responded_at
        recipient.status = "responded"
        broadcast_ids.add(recipient.broadcast_id)
    for broadcast_id in broadcast_ids:
        broadcast = session.get(FeedbackBroadcast, broadcast_id)
        if broadcast is not None:
            broadcast.responded_count = int(
                session.scalar(
                    select(func.count(FeedbackBroadcastRecipient.id)).where(
                        FeedbackBroadcastRecipient.broadcast_id == broadcast_id,
                        FeedbackBroadcastRecipient.responded_at.is_not(None),
                    )
                )
                or 0
            )


def record_client_feedback(
    session: Session,
    *,
    text: str,
    channel: str,
    account: Account | None = None,
    telegram_user_id: str | None = None,
    telegram_chat_id: str | None = None,
    language: str | None = None,
) -> FeedbackMessage:
    cleaned = _clean_text(text)
    if not cleaned:
        raise ValueError("Пустое сообщение обратной связи")
    conversation = get_or_create_feedback_conversation(
        session,
        account=account,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=language,
    )
    created_at = _now()
    message = FeedbackMessage(
        conversation_id=conversation.id,
        sender_type="client",
        channel=channel,
        text=cleaned,
        delivery_status="stored",
        created_at=created_at,
    )
    session.add(message)
    conversation.last_message_at = created_at
    conversation.last_client_message_at = created_at
    _mark_broadcast_responded(session, conversation)
    session.commit()
    session.refresh(message)
    return message


def collect_feedback_recipients(session: Session) -> list[FeedbackRecipient]:
    recipients: dict[str, FeedbackRecipient] = {}

    def remember(
        telegram_user_id: str | None,
        telegram_chat_id: str | None,
        language: str | None,
        observed_at: datetime | None,
        *,
        account_id: str | None = None,
        phone: str | None = None,
    ) -> None:
        if not telegram_user_id or not telegram_chat_id:
            return
        current = recipients.get(telegram_user_id)
        selected_language = normalize_language(language)
        if current is None:
            recipients[telegram_user_id] = FeedbackRecipient(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                language=selected_language,
                account_id=account_id,
                phone=phone,
                observed_at=observed_at,
            )
            return
        if observed_at and (current.observed_at is None or observed_at >= current.observed_at):
            current.telegram_chat_id = telegram_chat_id
            current.language = selected_language
            current.observed_at = observed_at
        current.account_id = current.account_id or account_id
        current.phone = current.phone or phone

    for event in session.scalars(
        select(FunnelEvent).where(
            FunnelEvent.telegram_user_id.is_not(None),
            FunnelEvent.telegram_chat_id.is_not(None),
        )
    ):
        remember(event.telegram_user_id, event.telegram_chat_id, event.language, event.created_at)

    for search in session.scalars(
        select(SearchRequest).where(
            SearchRequest.telegram_user_id.is_not(None),
            SearchRequest.telegram_chat_id.is_not(None),
        )
    ):
        remember(
            search.telegram_user_id,
            search.telegram_chat_id,
            search.language,
            search.created_at,
        )

    for access in session.scalars(
        select(AuctionAccess).where(
            AuctionAccess.telegram_user_id.is_not(None),
            AuctionAccess.telegram_chat_id.is_not(None),
        )
    ):
        remember(
            access.telegram_user_id,
            access.telegram_chat_id,
            access.language,
            access.updated_at,
        )

    for subscription in session.scalars(
        select(AuctionSubscription).where(
            AuctionSubscription.telegram_user_id.is_not(None),
            AuctionSubscription.telegram_chat_id.is_not(None),
        )
    ):
        remember(
            subscription.telegram_user_id,
            subscription.telegram_chat_id,
            subscription.language,
            subscription.updated_at,
            account_id=subscription.account_id,
        )

    for account in session.scalars(
        select(Account).where(
            Account.telegram_user_id.is_not(None),
            Account.telegram_chat_id.is_not(None),
        )
    ):
        remember(
            account.telegram_user_id,
            account.telegram_chat_id,
            get_latest_telegram_language(session, account.telegram_user_id or ""),
            account.updated_at,
            account_id=account.id,
            phone=account.phone,
        )

    return sorted(
        recipients.values(),
        key=lambda item: item.observed_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def _broadcast_text(language: str, broadcast: FeedbackBroadcast) -> str:
    return broadcast.kz_text if normalize_language(language) == "kz" else broadcast.ru_text


def send_feedback_broadcast(
    session: Session,
    *,
    title: str,
    ru_text: str = DEFAULT_FEEDBACK_RU,
    kz_text: str = DEFAULT_FEEDBACK_KZ,
    created_by: str | None = None,
) -> FeedbackBroadcast:
    broadcast = FeedbackBroadcast(
        title=title.strip() or "Запрос обратной связи",
        ru_text=ru_text.strip(),
        kz_text=kz_text.strip(),
        created_by=created_by,
    )
    session.add(broadcast)
    session.commit()
    session.refresh(broadcast)

    sent_count = 0
    failed_count = 0
    for item in collect_feedback_recipients(session):
        conversation = get_or_create_feedback_conversation(
            session,
            telegram_user_id=item.telegram_user_id,
            telegram_chat_id=item.telegram_chat_id,
            language=item.language,
            phone=item.phone,
        )
        if item.account_id and not conversation.account_id:
            conversation.account_id = item.account_id
        recipient = FeedbackBroadcastRecipient(
            broadcast_id=broadcast.id,
            conversation_id=conversation.id,
            account_id=conversation.account_id,
            telegram_user_id=item.telegram_user_id,
            telegram_chat_id=item.telegram_chat_id,
            language=item.language,
        )
        message = FeedbackMessage(
            conversation_id=conversation.id,
            broadcast_id=broadcast.id,
            sender_type="system",
            channel="telegram",
            text=_broadcast_text(item.language, broadcast),
            delivery_status="pending",
        )
        session.add_all([recipient, message])
        session.commit()
        try:
            response = telegram_request(
                "sendMessage",
                {
                    "chat_id": item.telegram_chat_id,
                    "text": message.text,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": (
                                        "Жауап жазу"
                                        if normalize_language(item.language) == "kz"
                                        else "Написать ответ"
                                    ),
                                    "callback_data": "feedback:start",
                                }
                            ]
                        ]
                    },
                },
            )
            telegram_message_id = response.get("result", {}).get("message_id")
            now = _now()
            recipient.status = "sent"
            recipient.sent_at = now
            recipient.telegram_message_id = telegram_message_id
            message.delivery_status = "sent"
            message.telegram_message_id = telegram_message_id
            conversation.last_message_at = now
            sent_count += 1
        except Exception as exc:
            recipient.status = "failed"
            recipient.error_message = str(exc)[:1000]
            message.delivery_status = "failed"
            message.error_message = str(exc)[:1000]
            failed_count += 1
        session.commit()

    broadcast.sent_count = sent_count
    broadcast.failed_count = failed_count
    broadcast.responded_count = 0
    broadcast.completed_at = _now()
    session.commit()
    session.refresh(broadcast)
    return broadcast


def list_feedback_conversations(session: Session) -> list[FeedbackConversation]:
    unread_first = case(
        (
            FeedbackConversation.last_client_message_at.is_not(None)
            & (
                FeedbackConversation.last_admin_message_at.is_(None)
                | (
                    FeedbackConversation.last_client_message_at
                    > FeedbackConversation.last_admin_message_at
                )
            ),
            0,
        ),
        else_=1,
    )
    return session.scalars(
        select(FeedbackConversation)
        .options(selectinload(FeedbackConversation.messages))
        .order_by(
            unread_first.asc(),
            FeedbackConversation.last_client_message_at.desc().nullslast(),
            FeedbackConversation.last_message_at.desc().nullslast(),
        )
    ).all()


def feedback_conversation_is_unread(conversation: FeedbackConversation) -> bool:
    return bool(
        conversation.last_client_message_at
        and (
            conversation.last_admin_message_at is None
            or conversation.last_client_message_at > conversation.last_admin_message_at
        )
    )


def mark_feedback_conversation_read(
    session: Session, conversation: FeedbackConversation
) -> FeedbackConversation:
    if not feedback_conversation_is_unread(conversation):
        return conversation
    conversation.last_admin_message_at = _now()
    session.commit()
    session.refresh(conversation)
    return conversation


def get_feedback_conversation(
    session: Session, conversation_id: str
) -> FeedbackConversation | None:
    return session.scalar(
        select(FeedbackConversation)
        .options(selectinload(FeedbackConversation.messages))
        .where(FeedbackConversation.id == conversation_id)
    )


def send_admin_feedback_reply(
    session: Session,
    *,
    conversation_id: str,
    text: str,
    admin_user: str,
) -> FeedbackMessage:
    cleaned = _clean_text(text)
    if not cleaned:
        raise ValueError("Пустой ответ")
    conversation = get_feedback_conversation(session, conversation_id)
    if conversation is None:
        raise LookupError("Диалог не найден")
    message = FeedbackMessage(
        conversation_id=conversation.id,
        sender_type="admin",
        channel="admin",
        text=cleaned,
        delivery_status="stored",
    )
    session.add(message)
    now = _now()
    conversation.last_message_at = now
    conversation.last_admin_message_at = now
    if conversation.telegram_chat_id:
        try:
            response = telegram_request(
                "sendMessage",
                {
                    "chat_id": conversation.telegram_chat_id,
                    "text": f"{admin_reply_prefix(conversation.language)}\n\n{cleaned}",
                },
            )
            message.delivery_status = "sent"
            message.telegram_message_id = response.get("result", {}).get("message_id")
        except Exception as exc:
            message.delivery_status = "failed"
            message.error_message = str(exc)[:1000]
    else:
        message.delivery_status = "stored"
        message.error_message = f"Ответ сохранен в админке пользователем {admin_user}"
    session.commit()
    session.refresh(message)
    return message
