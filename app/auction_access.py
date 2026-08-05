from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import (
    access_expiry_is_active,
    find_pending_platform_invoice,
    has_platform_access,
    has_platform_paid_access,
    next_platform_access_expiry,
)
from app.apipay import cancel_invoice, create_qr_invoice, get_invoice
from app.config import settings
from app.i18n import normalize_language
from app.models import AuctionAccess, AuctionLot, PaymentStatus

AUCTION_ORDER_PREFIX = "auction-"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuctionPaymentResult:
    access_id: str
    status: str
    activated: bool = False
    notify_retry: bool = False


def _auction_invoice_idempotency_key(access: AuctionAccess) -> str:
    status = access.payment_provider_status or ""
    if status.startswith("refresh:"):
        return f"land-scout:auction:{access.id}:{status}"
    return f"land-scout:auction:{access.id}"


def _prepare_auction_invoice_refresh(access: AuctionAccess) -> None:
    access.payment_status = PaymentStatus.rejected.value
    access.payment_provider = "apipay"
    access.payment_provider_invoice_id = None
    access.payment_provider_url = None
    access.payment_provider_status = f"refresh:{uuid.uuid4().hex[:12]}"
    access.payment_provider_updated_at = datetime.now(UTC)


def _admin_user_ids() -> set[str]:
    return {
        value.strip()
        for value in settings.telegram_admin_user_ids.split(",")
        if value.strip()
    }


def is_auction_admin(telegram_user_id: str) -> bool:
    return telegram_user_id in _admin_user_ids()


def get_auction_access(
    session: Session,
    telegram_user_id: str,
) -> AuctionAccess | None:
    return session.scalar(
        select(AuctionAccess).where(
            AuctionAccess.telegram_user_id == telegram_user_id
        )
    )


def get_or_create_auction_access(
    session: Session,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    language: str,
) -> AuctionAccess:
    access = get_auction_access(session, telegram_user_id)
    if access is None:
        access = AuctionAccess(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            language=normalize_language(language),
        )
        session.add(access)
        session.flush()
    else:
        access.telegram_chat_id = telegram_chat_id
        access.language = normalize_language(language)
    session.commit()
    session.refresh(access)
    return access


def has_auction_paid_access(session: Session, telegram_user_id: str) -> bool:
    if is_auction_admin(telegram_user_id):
        return True
    return has_platform_access(session, telegram_user_id)


def claim_free_auction_lot(
    session: Session,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    language: str,
    lot_id: str,
) -> AuctionAccess:
    access = get_or_create_auction_access(
        session,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=language,
    )
    if (
        not has_platform_access(session, telegram_user_id)
        and access.free_lot_id is None
        and settings.auction_free_preview_lots > 0
    ):
        if session.get(AuctionLot, lot_id) is None:
            raise LookupError("Auction lot not found")
        access.free_lot_id = lot_id
        session.commit()
        session.refresh(access)
    return access


def can_view_auction_lot(
    session: Session,
    telegram_user_id: str,
    lot_id: str,
) -> bool:
    if has_auction_paid_access(session, telegram_user_id):
        return True
    return session.get(AuctionLot, lot_id) is not None


def start_auction_payment(
    session: Session,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    language: str,
) -> AuctionAccess:
    if not settings.apipay_enabled:
        raise ValueError("Автоматическая оплата временно недоступна")
    access = get_or_create_auction_access(
        session,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=language,
    )
    if has_platform_paid_access(session, telegram_user_id):
        return access
    platform_pending = find_pending_platform_invoice(
        session,
        telegram_user_id,
        exclude_auction_access_id=access.id,
    )
    if (
        platform_pending is not None
        and platform_pending.payment_status == PaymentStatus.awaiting_transfer.value
        and platform_pending.payment_provider == "apipay"
        and platform_pending.payment_provider_url
    ):
        access.payment_status = PaymentStatus.awaiting_transfer.value
        access.payment_amount_kzt = (
            platform_pending.payment_amount_kzt
            or settings.platform_access_price_kzt
        )
        access.payment_provider = "apipay"
        access.payment_provider_invoice_id = None
        access.payment_provider_status = platform_pending.payment_provider_status
        access.payment_provider_url = platform_pending.payment_provider_url
        access.payment_requested_at = datetime.now(UTC)
        access.payment_provider_updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(access)
        return access
    if (
        access.payment_status == PaymentStatus.awaiting_transfer.value
        and access.payment_provider == "apipay"
        and access.payment_provider_invoice_id
        and access.payment_provider_url
    ):
        return access

    order_id = f"{AUCTION_ORDER_PREFIX}{access.id}"
    invoice = create_qr_invoice(
        request_id=order_id,
        amount_kzt=settings.platform_access_price_kzt,
        description="Жертап: полный доступ на 1 месяц",
        idempotency_key=_auction_invoice_idempotency_key(access),
    )
    access.payment_status = PaymentStatus.awaiting_transfer.value
    access.payment_amount_kzt = settings.platform_access_price_kzt
    access.payment_provider = "apipay"
    access.payment_provider_invoice_id = invoice.invoice_id
    access.payment_provider_status = invoice.status
    access.payment_provider_url = invoice.payment_url
    access.payment_requested_at = datetime.now(UTC)
    access.payment_provider_updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(access)
    dispatch_auction_payment_reconciliation(access.id)
    return access


def apply_auction_apipay_invoice(
    session: Session,
    invoice: dict,
) -> AuctionPaymentResult:
    external_order_id = str(invoice.get("external_order_id") or "")
    if not external_order_id.startswith(AUCTION_ORDER_PREFIX):
        raise ValueError("Счет не относится к доступу к аукционам")
    access_id = external_order_id.removeprefix(AUCTION_ORDER_PREFIX)
    invoice_id = str(invoice.get("id") or "")
    provider_status = str(invoice.get("status") or "")
    access = session.get(AuctionAccess, access_id)
    if access is None:
        raise LookupError("Доступ к аукционам не найден")
    if (
        access.payment_provider != "apipay"
    ):
        raise ValueError("ID счета ApiPay не совпадает с доступом к аукционам")
    if access.payment_provider_invoice_id != invoice_id:
        if provider_status == "paid":
            logger.info(
                "Accepting paid stale ApiPay invoice %s for auction access %s; "
                "current invoice is %s",
                invoice_id,
                access.id,
                access.payment_provider_invoice_id,
            )
        elif provider_status in {"cancelled", "expired", "error"}:
            logger.info(
                "Ignoring stale terminal ApiPay invoice %s for auction access %s; "
                "current invoice is %s",
                invoice_id,
                access.id,
                access.payment_provider_invoice_id,
            )
            return AuctionPaymentResult(
                access_id=access.id,
                status=provider_status,
                notify_retry=False,
            )
        else:
            raise ValueError("ID счета ApiPay не совпадает с доступом к аукционам")

    access.payment_provider_status = provider_status
    access.payment_provider_updated_at = datetime.now(UTC)
    activated = False
    notify_retry = False
    if provider_status == "paid":
        try:
            paid_amount = Decimal(str(invoice.get("amount")))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("ApiPay передал некорректную сумму") from exc
        expected = Decimal(
            access.payment_amount_kzt or settings.platform_access_price_kzt
        )
        if paid_amount != expected:
            raise ValueError(
                f"Сумма ApiPay {paid_amount} не совпадает с ожидаемой {expected}"
            )
        was_active = bool(
            access.paid_access and access_expiry_is_active(access.access_expires_at)
        )
        access.paid_access = True
        access.payment_status = PaymentStatus.paid.value
        access.payment_confirmed_at = datetime.now(UTC)
        if not was_active:
            activated = True
        access.access_expires_at = next_platform_access_expiry(
            access.access_expires_at,
            now=access.payment_confirmed_at,
        )
    elif provider_status in {"cancelled", "expired", "error"}:
        if not (
            access.paid_access and access_expiry_is_active(access.access_expires_at)
        ):
            notify_retry = access.payment_status != PaymentStatus.rejected.value
            access.payment_status = PaymentStatus.rejected.value
    session.commit()
    return AuctionPaymentResult(
        access_id=access.id,
        status=provider_status,
        activated=activated,
        notify_retry=notify_retry,
    )


def refresh_auction_payment(
    session: Session,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    language: str,
) -> AuctionAccess:
    access = get_or_create_auction_access(
        session,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=language,
    )
    if access.paid_access and access_expiry_is_active(access.access_expires_at):
        return access
    if not access.payment_provider_invoice_id:
        return start_auction_payment(
            session,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            language=language,
        )
    invoice = get_invoice(access.payment_provider_invoice_id)
    invoice["external_order_id"] = f"{AUCTION_ORDER_PREFIX}{access.id}"
    provider_status = str(invoice.get("status") or "")
    if provider_status == "paid":
        apply_auction_apipay_invoice(session, invoice)
        session.refresh(access)
        return access
    if provider_status in {"cancelled", "expired", "error"}:
        apply_auction_apipay_invoice(session, invoice)
    elif provider_status in {"pending", "processing"}:
        try:
            cancellation = cancel_invoice(access.payment_provider_invoice_id)
            access.payment_provider_status = cancellation.status
        except Exception:
            logger.exception(
                "Could not cancel stale ApiPay invoice %s",
                access.payment_provider_invoice_id,
            )
            access.payment_provider_status = "cancel_failed"
        access.payment_provider_updated_at = datetime.now(UTC)
    else:
        raise ValueError("ApiPay еще обрабатывает предыдущий счет")

    _prepare_auction_invoice_refresh(access)
    session.commit()
    return start_auction_payment(
        session,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        language=language,
    )


def notify_auction_payment_confirmed(access_id: str) -> None:
    from app.analytics import track_funnel_event
    from app.db import SessionLocal
    from app.services import telegram_request

    with SessionLocal() as session:
        access = session.get(AuctionAccess, access_id)
        if (
            access is None
            or not (access.paid_access and access_expiry_is_active(access.access_expires_at))
            or access.payment_confirmation_notified_at is not None
        ):
            return
        language = normalize_language(access.language)
        text = (
            "✅ <b>Төлем расталды</b>\n\n"
            "Барлық сервиске 1 айлық қолжетімділік қосылды.\n"
            "Енді жер орындарын іздеуге, аукцион лоттарын көруге, "
            "сақтауға, салыстыруға және жаңа лоттар туралы хабарлама алуға болады."
            if language == "kz"
            else "✅ <b>Оплата подтверждена</b>\n\n"
            "Доступ ко всему сервису активирован на 1 месяц.\n"
            "Теперь можно без ограничений искать места для участков, просматривать, "
            "сохранять и сравнивать аукционные лоты и получать уведомления."
        )
        telegram_request(
            "sendMessage",
            {
                "chat_id": access.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": (
                                    "🏷 Аукциондарды ашу"
                                    if language == "kz"
                                    else "🏷 Открыть аукционы"
                                ),
                                "callback_data": "auction:menu",
                            }
                        ]
                    ]
                },
            },
        )
        access.payment_confirmation_notified_at = datetime.now(UTC)
        session.commit()
        track_funnel_event(
            session,
            "auction_payment_paid",
            telegram_user_id=access.telegram_user_id,
            telegram_chat_id=access.telegram_chat_id,
            language=language,
            metadata={"amount_kzt": access.payment_amount_kzt},
        )


def notify_auction_payment_retry(access_id: str) -> None:
    from app.db import SessionLocal
    from app.services import telegram_request

    with SessionLocal() as session:
        access = session.get(AuctionAccess, access_id)
        if access is None or (
            access.paid_access and access_expiry_is_active(access.access_expires_at)
        ):
            return
        language = normalize_language(access.language)
        telegram_request(
            "sendMessage",
            {
                "chat_id": access.telegram_chat_id,
                "text": (
                    "Төлем сілтемесінің мерзімі аяқталды немесе төлем тоқтатылды. "
                    "Жаңа қауіпсіз сілтеме алыңыз."
                    if language == "kz"
                    else "Срок ссылки оплаты истек или платеж был отменен. "
                    "Получите новую безопасную ссылку."
                ),
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": (
                                    "🔄 Жаңа төлем сілтемесі"
                                    if language == "kz"
                                    else "🔄 Новая ссылка оплаты"
                                ),
                                "callback_data": "auction:pay",
                            }
                        ]
                    ]
                },
            },
        )


def dispatch_auction_payment_reconciliation(access_id: str) -> None:
    if not settings.apipay_polling_enabled:
        return
    from app.tasks import reconcile_auction_apipay_invoice_task

    reconcile_auction_apipay_invoice_task.apply_async(
        args=[access_id],
        countdown=settings.apipay_poll_interval_seconds,
    )
