from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.access import (
    account_access_kind,
    account_has_active_trial,
    account_has_paid_access,
    account_has_permanent_access,
    ensure_account_trial,
    grant_account_paid_access,
    has_platform_access,
)
from app.auction_access import has_auction_paid_access
from app.config import settings
from app.db import Base
from app.models import Account, PaymentStatus, SearchRequest


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_trial_starts_once_and_expires(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trial_access_enabled", True)
    monkeypatch.setattr(settings, "trial_access_days", 1)
    account = Account(phone="+77020000001")
    now = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)

    assert ensure_account_trial(account, now=now) is True
    assert account.trial_started_at == now
    assert account.trial_expires_at == now + timedelta(days=1)
    assert account_has_active_trial(account, now=now + timedelta(hours=23)) is True
    assert account_has_active_trial(account, now=now + timedelta(days=1, seconds=1)) is False

    original_expiry = account.trial_expires_at
    assert ensure_account_trial(account, now=now + timedelta(days=3)) is False
    assert account.trial_expires_at == original_expiry


def test_legacy_paid_access_without_expiry_remains_active(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trial_access_enabled", True)
    account = Account(
        phone="+77020000002",
        paid_access=True,
        trial_started_at=datetime(2026, 7, 1, tzinfo=UTC),
        trial_expires_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    with build_session() as session:
        session.add(account)
        session.commit()

        assert account_access_kind(session, account) == "paid"
        assert account_has_active_trial(account) is False


def test_paid_access_expires_after_paid_period(monkeypatch) -> None:
    monkeypatch.setattr(settings, "trial_access_enabled", True)
    account = Account(
        phone="+77020000022",
        paid_access=True,
        access_granted_at=datetime(2026, 7, 1, tzinfo=UTC),
        access_expires_at=datetime(2026, 7, 20, tzinfo=UTC),
        trial_started_at=datetime(2026, 7, 1, tzinfo=UTC),
        trial_expires_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    with build_session() as session:
        session.add(account)
        session.commit()

        assert account_has_paid_access(account, now=datetime(2026, 7, 19, tzinfo=UTC)) is True
        assert account_has_paid_access(account, now=datetime(2026, 7, 21, tzinfo=UTC)) is False
        assert account_access_kind(session, account) == "free"


def test_grant_account_paid_access_adds_and_extends_month(monkeypatch) -> None:
    monkeypatch.setattr(settings, "platform_access_months", 1)
    account = Account(phone="+77020000023")
    first = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

    assert grant_account_paid_access(account, now=first) == datetime(
        2026, 8, 29, 12, 0, tzinfo=UTC
    )
    assert grant_account_paid_access(
        account,
        now=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    ) == datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def test_linked_telegram_user_gets_active_trial_access(monkeypatch) -> None:
    monkeypatch.setattr(settings, "telegram_admin_user_ids", "")
    account = Account(
        phone="+77020000003",
        telegram_user_id="trial-user",
        trial_started_at=datetime.now(UTC) - timedelta(hours=1),
        trial_expires_at=datetime.now(UTC) + timedelta(hours=23),
    )
    with build_session() as session:
        session.add(account)
        session.commit()

        assert has_platform_access(session, "trial-user") is True
        assert has_auction_paid_access(session, "trial-user") is True


def test_linked_paid_telegram_user_syncs_legacy_permanent_access() -> None:
    account = Account(phone="+77020000004", telegram_user_id="paid-linked")
    request = SearchRequest(
        region="region",
        district="district",
        telegram_user_id="paid-linked",
        telegram_chat_id="paid-linked",
        payment_status=PaymentStatus.paid.value,
    )
    with build_session() as session:
        session.add_all([account, request])
        session.commit()

        assert account.paid_access is False
        assert account_has_permanent_access(session, account) is True
        session.commit()
        assert account.paid_access is True
        assert account_access_kind(session, account) == "paid"


def test_linked_paid_telegram_user_syncs_monthly_expiry() -> None:
    expiry = datetime.now(UTC) + timedelta(days=30)
    account = Account(phone="+77020000005", telegram_user_id="paid-monthly")
    request = SearchRequest(
        region="region",
        district="district",
        telegram_user_id="paid-monthly",
        telegram_chat_id="paid-monthly",
        payment_status=PaymentStatus.paid.value,
        access_expires_at=expiry,
    )
    with build_session() as session:
        session.add_all([account, request])
        session.commit()

        assert account_has_permanent_access(session, account) is True
        session.commit()
        assert account.paid_access is True
        assert account.access_expires_at == expiry.replace(tzinfo=None)
        assert account_access_kind(session, account) == "paid"


def test_expired_linked_paid_telegram_user_does_not_sync_access() -> None:
    account = Account(phone="+77020000006", telegram_user_id="paid-expired")
    request = SearchRequest(
        region="region",
        district="district",
        telegram_user_id="paid-expired",
        telegram_chat_id="paid-expired",
        payment_status=PaymentStatus.paid.value,
        access_expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with build_session() as session:
        session.add_all([account, request])
        session.commit()

        assert account_has_permanent_access(session, account) is False
        assert account.paid_access is False
        assert account_access_kind(session, account) == "free"
