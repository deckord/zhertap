from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account, AuctionAccess, PaymentStatus, SearchRequest

PENDING_PLATFORM_PAYMENT_STATUSES = (
    PaymentStatus.awaiting_transfer.value,
    PaymentStatus.pending_confirmation.value,
)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_lengths = (
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    return value.replace(year=year, month=month, day=min(value.day, month_lengths[month - 1]))


def access_expiry_is_active(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    if expires_at is None:
        return True
    current = _as_aware(now or datetime.now(UTC)) or datetime.now(UTC)
    expires_at = _as_aware(expires_at)
    return bool(expires_at and expires_at > current)


def next_platform_access_expiry(
    current_expires_at: datetime | None = None,
    *,
    now: datetime | None = None,
    months: int | None = None,
) -> datetime:
    current = _as_aware(now or datetime.now(UTC)) or datetime.now(UTC)
    current_expires_at = _as_aware(current_expires_at)
    base = current
    if current_expires_at and current_expires_at > current:
        base = current_expires_at
    return add_months(base, months or settings.platform_access_months)


def account_has_paid_access(account: Account, *, now: datetime | None = None) -> bool:
    return bool(
        account.paid_access and access_expiry_is_active(account.access_expires_at, now=now)
    )


def grant_account_paid_access(
    account: Account,
    *,
    now: datetime | None = None,
    months: int | None = None,
) -> datetime:
    current = now or datetime.now(UTC)
    account.paid_access = True
    account.access_granted_at = account.access_granted_at or current
    account.access_expires_at = next_platform_access_expiry(
        account.access_expires_at,
        now=current,
        months=months,
    )
    return account.access_expires_at


@dataclass(frozen=True, slots=True)
class PendingPlatformInvoice:
    source: str
    object_id: str
    telegram_user_id: str
    telegram_chat_id: str | None
    language: str | None
    payment_status: str
    payment_amount_kzt: int | None
    payment_provider: str | None
    payment_provider_invoice_id: str | None
    payment_provider_status: str | None
    payment_provider_url: str | None


def has_platform_paid_access(
    session: Session,
    telegram_user_id: str | None,
    *,
    exclude_search_request_id: str | None = None,
) -> bool:
    """Return whether a Telegram user has active paid platform access."""
    if not telegram_user_id:
        return False
    web_account_paid = session.scalar(
        select(Account.id)
        .where(
            Account.telegram_user_id == telegram_user_id,
            Account.paid_access.is_(True),
            or_(Account.access_expires_at.is_(None), Account.access_expires_at > datetime.now(UTC)),
        )
        .limit(1)
    )
    if web_account_paid is not None:
        return True

    auction_access = session.scalar(
        select(AuctionAccess.id)
        .where(
            AuctionAccess.telegram_user_id == telegram_user_id,
            AuctionAccess.paid_access.is_(True),
            or_(
                AuctionAccess.access_expires_at.is_(None),
                AuctionAccess.access_expires_at > datetime.now(UTC),
            ),
        )
        .limit(1)
    )
    if auction_access is not None:
        return True

    search_query = select(SearchRequest.id).where(
        SearchRequest.telegram_user_id == telegram_user_id,
        SearchRequest.payment_status == PaymentStatus.paid.value,
        or_(
            SearchRequest.access_expires_at.is_(None),
            SearchRequest.access_expires_at > datetime.now(UTC),
        ),
    )
    if exclude_search_request_id:
        search_query = search_query.where(
            SearchRequest.id != exclude_search_request_id
        )
    return session.scalar(search_query.limit(1)) is not None


def account_has_active_trial(account: Account, *, now: datetime | None = None) -> bool:
    if account_has_paid_access(account, now=now):
        return False
    if account.trial_expires_at is None:
        return False
    current = now or datetime.now(UTC)
    expires_at = account.trial_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > current


def ensure_account_trial(account: Account, *, now: datetime | None = None) -> bool:
    """Start a one-time trial for a newly verified web account."""
    if not settings.trial_access_enabled or settings.trial_access_days <= 0:
        return False
    if account_has_paid_access(account, now=now) or account.trial_started_at is not None:
        return False
    current = now or datetime.now(UTC)
    account.trial_started_at = current
    account.trial_expires_at = current + timedelta(days=settings.trial_access_days)
    return True


def account_has_permanent_access(session: Session, account: Account) -> bool:
    if account_has_paid_access(account):
        return True
    if account.telegram_user_id and has_platform_paid_access(session, account.telegram_user_id):
        account.paid_access = True
        account.access_granted_at = account.access_granted_at or datetime.now(UTC)
        search_expiries = session.scalars(
            select(SearchRequest.access_expires_at)
            .where(
                SearchRequest.telegram_user_id == account.telegram_user_id,
                SearchRequest.payment_status == PaymentStatus.paid.value,
                or_(
                    SearchRequest.access_expires_at.is_(None),
                    SearchRequest.access_expires_at > datetime.now(UTC),
                ),
            )
        ).all()
        auction_expiries = session.scalars(
            select(AuctionAccess.access_expires_at)
            .where(
                AuctionAccess.telegram_user_id == account.telegram_user_id,
                AuctionAccess.paid_access.is_(True),
                or_(
                    AuctionAccess.access_expires_at.is_(None),
                    AuctionAccess.access_expires_at > datetime.now(UTC),
                ),
            )
        ).all()
        expiries = list(search_expiries) + list(auction_expiries)
        if any(expiry is None for expiry in expiries):
            account.access_expires_at = None
        elif expiries:
            account.access_expires_at = max(
                _as_aware(expiry) for expiry in expiries if expiry is not None
            )
        session.flush()
        return True
    return False


def account_has_platform_access(session: Session, account: Account) -> bool:
    return account_has_permanent_access(session, account) or account_has_active_trial(account)


def account_access_kind(session: Session, account: Account) -> str:
    if account_has_permanent_access(session, account):
        return "paid"
    if account_has_active_trial(account):
        return "trial"
    return "free"


def has_platform_access(
    session: Session,
    telegram_user_id: str | None,
    *,
    exclude_search_request_id: str | None = None,
) -> bool:
    if has_platform_paid_access(
        session,
        telegram_user_id,
        exclude_search_request_id=exclude_search_request_id,
    ):
        return True
    if not telegram_user_id:
        return False
    account = session.scalar(
        select(Account).where(Account.telegram_user_id == telegram_user_id).limit(1)
    )
    return bool(account and account_has_active_trial(account))


def find_pending_platform_invoice(
    session: Session,
    telegram_user_id: str | None,
    *,
    exclude_search_request_id: str | None = None,
    exclude_auction_access_id: str | None = None,
) -> PendingPlatformInvoice | None:
    if not telegram_user_id:
        return None

    search_query = select(SearchRequest).where(
        SearchRequest.telegram_user_id == telegram_user_id,
        SearchRequest.payment_status.in_(PENDING_PLATFORM_PAYMENT_STATUSES),
    )
    if exclude_search_request_id:
        search_query = search_query.where(
            SearchRequest.id != exclude_search_request_id
        )
    search = session.scalar(
        search_query.order_by(SearchRequest.payment_requested_at.desc()).limit(1)
    )
    if search is not None:
        return PendingPlatformInvoice(
            source="search",
            object_id=search.id,
            telegram_user_id=search.telegram_user_id or "",
            telegram_chat_id=search.telegram_chat_id,
            language=search.language,
            payment_status=search.payment_status,
            payment_amount_kzt=search.payment_amount_kzt,
            payment_provider=search.payment_provider,
            payment_provider_invoice_id=search.payment_provider_invoice_id,
            payment_provider_status=search.payment_provider_status,
            payment_provider_url=search.payment_provider_url,
        )

    auction_query = select(AuctionAccess).where(
        AuctionAccess.telegram_user_id == telegram_user_id,
        AuctionAccess.payment_status.in_(PENDING_PLATFORM_PAYMENT_STATUSES),
        AuctionAccess.paid_access.is_(False),
    )
    if exclude_auction_access_id:
        auction_query = auction_query.where(
            AuctionAccess.id != exclude_auction_access_id
        )
    auction = session.scalar(
        auction_query.order_by(AuctionAccess.payment_requested_at.desc()).limit(1)
    )
    if auction is None:
        return None
    return PendingPlatformInvoice(
        source="auction",
        object_id=auction.id,
        telegram_user_id=auction.telegram_user_id,
        telegram_chat_id=auction.telegram_chat_id,
        language=auction.language,
        payment_status=auction.payment_status,
        payment_amount_kzt=auction.payment_amount_kzt,
        payment_provider=auction.payment_provider,
        payment_provider_invoice_id=auction.payment_provider_invoice_id,
        payment_provider_status=auction.payment_provider_status,
        payment_provider_url=auction.payment_provider_url,
    )
