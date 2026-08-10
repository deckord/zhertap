from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import account_has_paid_access, grant_account_paid_access
from app.apipay import cancel_invoice, create_qr_invoice, get_invoice
from app.config import settings
from app.models import Account, AccountPayment, PaymentStatus

ACCOUNT_ORDER_PREFIX = "account-"
TERMINAL_PROVIDER_STATUSES = {"cancelled", "expired", "error"}
ACTIVE_PROVIDER_STATUSES = {"pending", "processing", "cancelling"}
QR_REFRESH_AFTER_SECONDS = 270
logger = logging.getLogger(__name__)
SUPPORTED_ACCOUNT_PLANS = {"investor", "team"}


def account_plan_price_kzt(plan: str) -> int:
    if plan == "team":
        return settings.auction_team_price_kzt
    return settings.platform_access_price_kzt


@dataclass(frozen=True, slots=True)
class AccountPaymentResult:
    payment_id: str
    account_id: str
    status: str
    activated: bool = False
    notify_retry: bool = False


def latest_account_payment(session: Session, account: Account) -> AccountPayment | None:
    return session.scalar(
        select(AccountPayment)
        .where(AccountPayment.account_id == account.id)
        .order_by(AccountPayment.created_at.desc())
        .limit(1)
    )


def _account_invoice_idempotency_key(payment: AccountPayment) -> str:
    status = payment.payment_provider_status or ""
    if status.startswith("refresh:"):
        return f"land-scout:account:{payment.id}:{status}"
    return f"land-scout:account:{payment.id}"


def _prepare_account_invoice_refresh(payment: AccountPayment) -> None:
    payment.payment_status = PaymentStatus.rejected.value
    payment.payment_provider = "apipay"
    payment.payment_provider_invoice_id = None
    payment.payment_provider_url = None
    payment.payment_provider_qr_image_url = None
    payment.payment_provider_status = f"refresh:{uuid.uuid4().hex[:12]}"
    payment.payment_provider_updated_at = datetime.now(UTC)


def _qr_invoice_is_stale(payment: AccountPayment) -> bool:
    if (payment.payment_provider_status or "") not in {"pending", "processing"}:
        return False
    updated_at = payment.payment_provider_updated_at or payment.payment_requested_at
    if updated_at is None:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return updated_at <= datetime.now(UTC) - timedelta(seconds=QR_REFRESH_AFTER_SECONDS)


def start_account_payment(
    session: Session,
    account: Account,
    *,
    target_plan: str = "investor",
) -> AccountPayment:
    if target_plan not in SUPPORTED_ACCOUNT_PLANS:
        raise ValueError("Неизвестный тариф")
    if account_has_paid_access(account) and (
        account.auction_plan == target_plan
        or (target_plan == "investor" and account.auction_plan != "team")
    ):
        payment = latest_account_payment(session, account)
        if payment is not None:
            return payment
        raise ValueError("Доступ уже активен")
    if not settings.apipay_enabled:
        raise ValueError("Автоматическая оплата временно недоступна")

    payment = session.scalar(
        select(AccountPayment)
        .where(
            AccountPayment.account_id == account.id,
            AccountPayment.target_plan == target_plan,
            AccountPayment.payment_status == PaymentStatus.awaiting_transfer.value,
            AccountPayment.payment_provider == "apipay",
            AccountPayment.payment_provider_url.is_not(None),
        )
        .order_by(AccountPayment.payment_requested_at.desc())
        .limit(1)
    )
    if payment is not None:
        if (payment.payment_provider_status or "") not in TERMINAL_PROVIDER_STATUSES:
            return payment
        payment.payment_status = PaymentStatus.rejected.value
        session.commit()

    amount_kzt = account_plan_price_kzt(target_plan)
    payment = AccountPayment(
        account_id=account.id,
        payment_status=PaymentStatus.awaiting_transfer.value,
        payment_amount_kzt=amount_kzt,
        target_plan=target_plan,
        payment_provider="apipay",
        payment_requested_at=datetime.now(UTC),
    )
    session.add(payment)
    session.flush()
    invoice = create_qr_invoice(
        request_id=f"{ACCOUNT_ORDER_PREFIX}{payment.id}",
        amount_kzt=amount_kzt,
        description=(
            "Жертап: тариф Команда на 1 месяц"
            if target_plan == "team"
            else "Жертап: тариф Инвестор Pro на 1 месяц"
        ),
        idempotency_key=_account_invoice_idempotency_key(payment),
    )
    payment.payment_provider_invoice_id = invoice.invoice_id
    payment.payment_provider_status = invoice.status
    payment.payment_provider_url = invoice.payment_url
    payment.payment_provider_qr_image_url = invoice.qr_image_url
    payment.payment_provider_updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(payment)
    dispatch_account_payment_reconciliation(payment.id)
    return payment


def apply_account_apipay_invoice(
    session: Session,
    invoice: dict,
) -> AccountPaymentResult:
    external_order_id = str(invoice.get("external_order_id") or "")
    if not external_order_id.startswith(ACCOUNT_ORDER_PREFIX):
        raise ValueError("Счет не относится к веб-аккаунту")
    payment_id = external_order_id.removeprefix(ACCOUNT_ORDER_PREFIX)
    invoice_id = str(invoice.get("id") or "")
    provider_status = str(invoice.get("status") or "")
    payment = session.get(AccountPayment, payment_id)
    if payment is None:
        raise LookupError("Оплата аккаунта не найдена")
    if payment.payment_provider != "apipay":
        raise ValueError("ID счета ApiPay не совпадает с оплатой аккаунта")
    if payment.payment_provider_invoice_id != invoice_id:
        if provider_status == "paid":
            logger.info(
                "Accepting paid stale ApiPay invoice %s for account payment %s; "
                "current invoice is %s",
                invoice_id,
                payment.id,
                payment.payment_provider_invoice_id,
            )
        elif provider_status in {"cancelled", "expired", "error"}:
            logger.info(
                "Ignoring stale terminal ApiPay invoice %s for account payment %s; "
                "current invoice is %s",
                invoice_id,
                payment.id,
                payment.payment_provider_invoice_id,
            )
            return AccountPaymentResult(
                payment_id=payment.id,
                account_id=payment.account_id,
                status=provider_status,
                notify_retry=False,
            )
        else:
            raise ValueError("ID счета ApiPay не совпадает с оплатой аккаунта")

    payment.payment_provider_status = provider_status
    payment.payment_provider_updated_at = datetime.now(UTC)
    activated = False
    notify_retry = False
    if provider_status == "paid":
        try:
            paid_amount = Decimal(str(invoice.get("amount")))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("ApiPay передал некорректную сумму") from exc
        expected = Decimal(payment.payment_amount_kzt or settings.platform_access_price_kzt)
        if paid_amount != expected:
            raise ValueError(f"Сумма ApiPay {paid_amount} не совпадает с ожидаемой {expected}")
        account = session.get(Account, payment.account_id)
        if account is None:
            raise LookupError("Веб-аккаунт не найден")
        if payment.payment_status != PaymentStatus.paid.value:
            payment.payment_status = PaymentStatus.paid.value
            payment.payment_confirmed_at = datetime.now(UTC)
            payment.payment_confirmed_by = f"apipay:{invoice_id}"
        was_active = account_has_paid_access(account)
        paid_plan = (
            payment.target_plan
            if payment.target_plan in SUPPORTED_ACCOUNT_PLANS
            else "investor"
        )
        if paid_plan == "team" or account.auction_plan != "team":
            account.auction_plan = paid_plan
        grant_account_paid_access(account)
        activated = not was_active
    elif provider_status in {"cancelled", "expired", "error"}:
        if payment.payment_status != PaymentStatus.paid.value:
            notify_retry = payment.payment_status != PaymentStatus.rejected.value
            payment.payment_status = PaymentStatus.rejected.value

    session.commit()
    return AccountPaymentResult(
        payment_id=payment.id,
        account_id=payment.account_id,
        status=provider_status,
        activated=activated,
        notify_retry=notify_retry,
    )


def refresh_account_payment(
    session: Session,
    account: Account,
    *,
    target_plan: str | None = None,
) -> AccountPayment:
    payment = latest_account_payment(session, account)
    desired_plan = target_plan or (payment.target_plan if payment else "investor")
    if payment is None or not payment.payment_provider_invoice_id:
        return start_account_payment(session, account, target_plan=desired_plan)
    if target_plan and payment.target_plan != target_plan:
        return start_account_payment(session, account, target_plan=target_plan)
    if (
        account_has_paid_access(account)
        and account.auction_plan == desired_plan
    ) or payment.payment_status == PaymentStatus.paid.value:
        return payment
    if payment.payment_provider != "apipay":
        return start_account_payment(session, account, target_plan=desired_plan)
    if payment.payment_status == PaymentStatus.rejected.value or (
        payment.payment_provider_status or ""
    ) in TERMINAL_PROVIDER_STATUSES:
        return start_account_payment(session, account, target_plan=desired_plan)

    invoice = get_invoice(payment.payment_provider_invoice_id)
    invoice["external_order_id"] = f"{ACCOUNT_ORDER_PREFIX}{payment.id}"
    provider_status = str(invoice.get("status") or "")
    if provider_status == "paid":
        apply_account_apipay_invoice(session, invoice)
        session.refresh(payment)
        return payment
    if provider_status in TERMINAL_PROVIDER_STATUSES:
        apply_account_apipay_invoice(session, invoice)
    elif provider_status in ACTIVE_PROVIDER_STATUSES:
        if not _qr_invoice_is_stale(payment):
            return payment
    else:
        raise ValueError("ApiPay еще обрабатывает предыдущий счет")

    try:
        if payment.payment_provider_invoice_id:
            cancellation = cancel_invoice(payment.payment_provider_invoice_id)
            payment.payment_provider_status = cancellation.status
    except Exception:
        logger.exception("Could not cancel stale ApiPay invoice %s", payment.id)
        payment.payment_provider_status = "cancel_failed"
    payment.payment_provider_updated_at = datetime.now(UTC)
    _prepare_account_invoice_refresh(payment)
    session.commit()
    return start_account_payment(session, account, target_plan=desired_plan)


def renew_account_payment(
    session: Session,
    account: Account,
    *,
    target_plan: str | None = None,
) -> AccountPayment:
    payment = latest_account_payment(session, account)
    desired_plan = target_plan or (payment.target_plan if payment else "investor")
    if payment is None or not payment.payment_provider_invoice_id:
        return start_account_payment(session, account, target_plan=desired_plan)
    if target_plan and payment.target_plan != target_plan:
        return start_account_payment(session, account, target_plan=target_plan)
    if (
        account_has_paid_access(account)
        and account.auction_plan == desired_plan
    ) or payment.payment_status == PaymentStatus.paid.value:
        return payment
    if payment.payment_provider != "apipay":
        return start_account_payment(session, account, target_plan=desired_plan)

    if payment.payment_status != PaymentStatus.rejected.value and (
        payment.payment_provider_status or ""
    ) not in TERMINAL_PROVIDER_STATUSES:
        invoice = get_invoice(payment.payment_provider_invoice_id)
        invoice["external_order_id"] = f"{ACCOUNT_ORDER_PREFIX}{payment.id}"
        provider_status = str(invoice.get("status") or "")
        if provider_status == "paid":
            apply_account_apipay_invoice(session, invoice)
            session.refresh(payment)
            return payment
        if provider_status in TERMINAL_PROVIDER_STATUSES:
            apply_account_apipay_invoice(session, invoice)
        elif provider_status in ACTIVE_PROVIDER_STATUSES:
            try:
                cancellation = cancel_invoice(payment.payment_provider_invoice_id)
                payment.payment_provider_status = cancellation.status
            except Exception:
                logger.exception("Could not cancel active ApiPay invoice %s", payment.id)
                payment.payment_provider_status = "cancel_failed"
        else:
            raise ValueError("ApiPay еще обрабатывает предыдущий счет")

    payment.payment_provider_updated_at = datetime.now(UTC)
    _prepare_account_invoice_refresh(payment)
    session.commit()
    return start_account_payment(session, account, target_plan=desired_plan)


def dispatch_account_payment_reconciliation(payment_id: str) -> None:
    if not settings.apipay_polling_enabled:
        return
    from app.tasks import reconcile_account_apipay_invoice_task

    reconcile_account_apipay_invoice_task.apply_async(
        args=[payment_id],
        countdown=settings.apipay_poll_interval_seconds,
    )
