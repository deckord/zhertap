from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from app.access import account_has_paid_access, grant_account_paid_access
from app.db import SessionLocal
from app.models import Account, AccountPayment, PaymentStatus
from app.services import telegram_request


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not digits.startswith("7") or len(digits) != 11:
        raise ValueError("Phone must be a Kazakhstan number")
    return "+" + digits


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grant a free platform access month.")
    parser.add_argument("phone", help="Client phone number")
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send Telegram notification when account has linked chat id",
    )
    parser.add_argument(
        "--site-url",
        default="https://zhertap.kz",
        help="Site URL included in Telegram notification",
    )
    parser.add_argument(
        "--reason",
        default="admin:free-month",
        help="Audit marker stored in payment_confirmed_by",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    phone = normalize_phone(args.phone)
    now = datetime.now(UTC)
    message_sent = False
    message_error = None
    created = False

    with SessionLocal() as session:
        account = session.scalar(select(Account).where(Account.phone == phone))
        if account is None:
            account = Account(phone=phone)
            session.add(account)
            session.flush()
            created = True

        old_expires_at = account.access_expires_at
        expires_at = grant_account_paid_access(account, now=now)
        session.add(
            AccountPayment(
                account_id=account.id,
                payment_status=PaymentStatus.paid.value,
                payment_amount_kzt=0,
                payment_requested_at=now,
                payment_confirmed_at=now,
                payment_confirmed_by=args.reason,
                payment_provider="manual_free_month",
                payment_provider_status="paid",
                payment_provider_updated_at=now,
            )
        )
        session.commit()
        session.refresh(account)

        if args.notify and account.telegram_chat_id:
            text = (
                "Здравствуйте! Вам предоставлен бесплатный полный доступ к Жертап "
                "на 1 месяц.\n\n"
                f"Доступ активен до: {expires_at.strftime('%d.%m.%Y')}.\n"
                f"Сайт: {args.site_url}"
            )
            try:
                telegram_request("sendMessage", {"chat_id": account.telegram_chat_id, "text": text})
                message_sent = True
            except Exception as exc:
                message_error = str(exc)

        result = {
            "phone": phone,
            "account_id": account.id,
            "created": created,
            "old_expires_at": old_expires_at.isoformat() if old_expires_at else None,
            "access_expires_at": account.access_expires_at.isoformat()
            if account.access_expires_at
            else None,
            "paid_access": account.paid_access,
            "active": account_has_paid_access(account, now=now),
            "telegram_user_id": account.telegram_user_id,
            "telegram_chat_id": account.telegram_chat_id,
            "message_sent": message_sent,
            "message_error": message_error,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
