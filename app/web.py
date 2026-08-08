# ruff: noqa: E501

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from re import sub
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import qrcode
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from qrcode.image.svg import SvgPathImage
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.access import (
    account_access_kind,
    account_has_paid_access,
    account_has_permanent_access,
    ensure_account_trial,
)
from app.account_payments import (
    TERMINAL_PROVIDER_STATUSES,
    latest_account_payment,
    refresh_account_payment,
    renew_account_payment,
    start_account_payment,
)
from app.auction_documents import unique_auction_documents
from app.auction_service import (
    AuctionFilters,
    auction_lot_changes,
    auction_lot_history,
    auction_lot_metrics,
    auction_market_snapshot,
    get_auction_lot,
    list_auction_lots,
)
from app.auction_v2 import (
    ACTION_LABELS,
    ARCHIVED_EQAZYNA_SEARCH_STATUSES,
    AUCTION_V2_SORT_LABELS,
    CONFIDENCE_LABELS,
    DEADLINE_STATUS_LABELS,
    EQAZYNA_STATUS_FILTER_LABELS,
    EVIDENCE_STATUS_LABELS,
    EVIDENCE_TYPE_LABELS,
    GEO_STATUS_LABELS,
    LOT_SCOPE_LABELS,
    RISK_LABELS,
    AuctionV2Filters,
    auction_v2_analytics_payload,
    auction_v2_dashboard,
    auction_v2_search_diagnostics,
    auction_v2_source_admin_payload,
    auction_v2_watchlist_matches,
    build_auction_v2_dossier_text,
    create_auction_v2_market_comparable,
    create_auction_v2_watchlist,
    ensure_default_auction_v2_watchlist,
    get_auction_v2_payload,
    list_auction_v2_lots,
    list_auction_v2_map_markers,
    list_auction_v2_market_comparables,
    list_auction_v2_watchlists,
    list_auction_v2_web_notifications,
    mark_auction_v2_web_notifications_seen,
    pipeline_stage_options,
    seed_auction_v2_sources,
    set_auction_v2_watchlist_active,
    sync_auction_v2_eqazyna_history_backfill,
    sync_auction_v2_full_cycle,
    update_auction_v2_pipeline,
)
from app.config import settings
from app.db import get_db
from app.feedback import (
    get_or_create_feedback_conversation,
    record_client_feedback,
)
from app.genplan_references import genplan_reference_payload
from app.manual_genplans import manual_genplan_records
from app.models import (
    Account,
    AccountPayment,
    AuctionEvidence,
    AuctionFavorite,
    AuctionLot,
    AuctionSubscription,
    Candidate,
    PaymentStatus,
    SearchRequest,
    TelegramLinkToken,
    WebLoginCode,
    WebSession,
)
from app.providers.egkn import EgknProvider, EgknProviderError, normalize_name
from app.purposes import LPH_NEW
from app.rate_limit import consume_rate_limit
from app.request_context import client_ip
from app.schemas import SearchCreate
from app.search_explanations import explain_search_result
from app.services import (
    approved_candidates,
    create_next_batch,
    create_search,
    dispatch_search,
    get_request_with_candidates,
    telegram_request,
)
from app.sms import send_login_code
from app.urban_plan_labels import urban_plan_badge_payload

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
catalog_provider = EgknProvider()
ALMATY_TZ = timezone(timedelta(hours=5))

KAZAKHSTAN_REGION_FALLBACKS = (
    "Область Абай",
    "Акмолинская область",
    "Актюбинская область",
    "Алматинская область",
    "Атырауская область",
    "Восточно-Казахстанская область",
    "Жамбылская область",
    "Область Жетісу",
    "Западно-Казахстанская область",
    "Карагандинская область",
    "Костанайская область",
    "Кызылординская область",
    "Мангистауская область",
    "Павлодарская область",
    "Северо-Казахстанская область",
    "Туркестанская область",
    "Область Ұлытау",
    "г. Астана",
    "г. Алматы",
    "г. Шымкент",
)

SESSION_COOKIE = "zhertap_session"
CODE_TTL_MINUTES = 10
SESSION_DAYS = 30
LOCK_MINUTES = 5
MAX_FAILED_ATTEMPTS = 3
OFFER_VERSION = "2026-07-28-v1"
PASSWORD_MIN_LENGTH = 8

LOGIN_RATE_LIMIT_IP_PER_MINUTE = 15
LOGIN_RATE_LIMIT_IP_WINDOW_SECONDS = 60
LOGIN_RATE_LIMIT_PHONE_PER_HOUR = 8
LOGIN_RATE_LIMIT_PHONE_WINDOW_SECONDS = 3600

SMS_REQUEST_RATE_LIMIT_PER_PHONE_PER_HOUR = 3
SMS_REQUEST_RATE_LIMIT_PER_IP_PER_HOUR = 10
SMS_REQUEST_RATE_LIMIT_WINDOW_SECONDS = 3600

CABINET_SEARCH_RATE_LIMIT_PER_ACCOUNT_PER_MINUTE = 30
CABINET_SEARCH_RATE_LIMIT_PER_IP_PER_MINUTE = 120
CABINET_SEARCH_RATE_LIMIT_WINDOW_SECONDS = 60

CABINET_SEARCH_STATUS_RATE_LIMIT_PER_ACCOUNT_PER_MINUTE = 120
CABINET_SEARCH_STATUS_RATE_LIMIT_PER_IP_PER_MINUTE = 240
CABINET_SEARCH_STATUS_WINDOW_SECONDS = 60


def _now() -> datetime:
    return datetime.now(UTC)


def _to_almaty(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ALMATY_TZ)


def _format_almaty(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local_value = _to_almaty(value)
    return local_value.strftime(fmt) if local_value else ""


def _notify_admin_web_registration(account: Account, request: Request) -> None:
    if not settings.telegram_admin_chat_id:
        return
    trial_text = (
        f"Тест до {_format_almaty(account.trial_expires_at)}"
        if account.trial_expires_at
        else "Тест не активирован"
    )
    telegram_id = account.telegram_user_id or "не привязан"
    text = (
        "🆕 Новая регистрация на сайте\n\n"
        f"Телефон: {account.phone}\n"
        f"Web account: {account.id}\n"
        f"Telegram: {telegram_id}\n"
        f"Доступ: {trial_text}\n"
        f"IP: {_client_ip(request)}"
    )
    try:
        telegram_request(
            "sendMessage",
            {
                "chat_id": settings.telegram_admin_chat_id,
                "text": text,
            },
        )
    except (RuntimeError, ValueError):
        logger.exception("Could not notify admin about web registration %s", account.id)


def _hash(value: str) -> str:
    return hmac.new(settings.session_secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def csrf_token_value(session_token: str, client_ip: str) -> str:
    return hmac.new(
        settings.session_secret.encode(),
        f"{client_ip}:{session_token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def csrf_token(request: Request) -> str:
    return csrf_token_value(request.cookies.get(SESSION_COOKIE, ""), _client_ip(request))


templates.env.globals["csrf_token"] = csrf_token


def _legacy_hash(value: str) -> str:
    legacy_key = settings.internal_api_key or settings.admin_password or "local-dev-secret"
    return hashlib.sha256(f"{legacy_key}:{value}".encode()).hexdigest()


def _hash_match(stored: str, value: str) -> bool:
    new_hash = _hash(value)
    if hmac.compare_digest(stored, new_hash):
        return True
    legacy_hash = _legacy_hash(value)
    return hmac.compare_digest(stored, legacy_hash) and not hmac.compare_digest(legacy_hash, new_hash)


def _migrate_hash(record: WebLoginCode | TelegramLinkToken | WebSession, field: str, value: str) -> bool:
    current = getattr(record, field)
    new_hash = _hash(value)
    if hmac.compare_digest(current, new_hash):
        return False
    legacy_hash = _legacy_hash(value)
    if hmac.compare_digest(current, legacy_hash):
        setattr(record, field, new_hash)
        return True
    return False


def _redirect_with_query(url: str, **params: str) -> RedirectResponse:
    parts = urlsplit(url or "/cabinet")
    if parts.netloc and parts.netloc != urlsplit(settings.app_base_url).netloc:
        parts = urlsplit("/cabinet")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value})
    target = urlunsplit(("", "", parts.path or "/cabinet", urlencode(query), parts.fragment))
    return RedirectResponse(target, status_code=303)


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000)
    return "pbkdf2_sha256$210000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def _verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, binascii.Error):
        return False


def _password_error(password: str, password_confirm: str | None = None) -> str | None:
    if len(password) < PASSWORD_MIN_LENGTH:
        return "Пароль должен быть не короче 8 символов."
    if password_confirm is not None and password != password_confirm:
        return "Пароли не совпадают."
    return None


def consume_telegram_link_token(
    session: Session,
    raw_token: str,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
) -> Account | None:
    token_hash = _hash(raw_token)
    legacy_token_hash = _legacy_hash(raw_token)
    link_token = session.scalar(
        select(TelegramLinkToken).where(
            TelegramLinkToken.token_hash == token_hash,
            TelegramLinkToken.consumed_at.is_(None),
            TelegramLinkToken.expires_at > _now(),
        )
    )
    if link_token is None and not hmac.compare_digest(token_hash, legacy_token_hash):
        link_token = session.scalar(
            select(TelegramLinkToken).where(
                TelegramLinkToken.token_hash == legacy_token_hash,
                TelegramLinkToken.consumed_at.is_(None),
                TelegramLinkToken.expires_at > _now(),
            )
        )
        if link_token:
            _migrate_hash(link_token, "token_hash", raw_token)
            session.commit()
    if link_token is None:
        return None
    account = session.get(Account, link_token.account_id)
    if account is None:
        return None
    account.telegram_user_id = telegram_user_id
    account.telegram_chat_id = telegram_chat_id
    account.telegram_linked_at = _now()
    link_token.telegram_user_id = telegram_user_id
    link_token.consumed_at = _now()
    account_has_permanent_access(session, account)
    session.commit()
    return account


def _get_session_by_token(session: Session, token: str) -> WebSession | None:
    token_hash = _hash(token)
    legacy_token_hash = _legacy_hash(token)
    web_session = session.scalar(
        select(WebSession).where(
            WebSession.token_hash == token_hash,
            WebSession.revoked_at.is_(None),
            WebSession.expires_at > _now(),
        )
    )
    if web_session is None and not hmac.compare_digest(token_hash, legacy_token_hash):
        web_session = session.scalar(
            select(WebSession).where(
                WebSession.token_hash == legacy_token_hash,
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > _now(),
            )
        )
        if web_session:
            if _migrate_hash(web_session, "token_hash", token):
                session.commit()
    return web_session


def normalize_phone(phone: str) -> str:
    digits = sub(r"\D+", "", phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if not digits.startswith("7") or len(digits) != 11:
        raise ValueError("Введите номер Казахстана в формате +7 7XX XXX XX XX")
    return "+" + digits


def _admin_web_phones() -> set[str]:
    phones: set[str] = set()
    for raw_phone in settings.admin_web_phones.split(","):
        raw_phone = raw_phone.strip()
        if not raw_phone:
            continue
        try:
            phones.add(normalize_phone(raw_phone))
        except ValueError:
            logger.warning("Ignoring invalid admin web phone %r", raw_phone)
    return phones


def is_web_admin_account(account: Account) -> bool:
    try:
        phone = normalize_phone(account.phone)
    except ValueError:
        return False
    return phone in _admin_web_phones()


def _require_web_admin_account(account: Account) -> None:
    if not is_web_admin_account(account):
        raise HTTPException(status_code=404, detail="Not found")


def _client_ip(request: Request) -> str:
    return client_ip(request)


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:1000]


def _login_context(request: Request, **extra: object) -> dict:
    context = {
        "app_name": settings.app_name,
        "price_kzt": settings.platform_access_price_kzt,
        "dev_code": None,
    }
    context.update(extra)
    return context


def _trial_access_label(days: int) -> str:
    if days == 1:
        return "1 день"
    if 2 <= days <= 4:
        return f"{days} дня"
    return f"{days} дней"


def _access_context(session: Session, account: Account) -> dict[str, object]:
    access_kind = account_access_kind(session, account)
    has_full_access = access_kind in {"paid", "trial"}
    if access_kind == "paid":
        access_label = (
            f"Оплачен до {_format_almaty(account.access_expires_at, '%d.%m.%Y')}"
            if account.access_expires_at
            else "Доступ активен"
        )
    elif access_kind == "trial":
        access_label = "Тестовый доступ"
    else:
        access_label = "Бесплатный режим"
    return {
        "access_kind": access_kind,
        "access_label": access_label,
        "has_full_access": has_full_access,
        "has_paid_unlock": has_full_access,
        "trial_active": access_kind == "trial",
        "trial_expires_at": account.trial_expires_at,
        "trial_expires_at_local": _to_almaty(account.trial_expires_at),
        "access_expires_at": account.access_expires_at,
        "access_expires_at_local": _to_almaty(account.access_expires_at),
    }


def _cabinet_context(
    session: Session,
    account: Account,
    **extra: object,
) -> dict[str, object]:
    show_onboarding_tour = (
        account.phone_verified_at is not None
        and account.onboarding_tour_available_at is not None
        and account.onboarding_tour_dismissed_at is None
    )
    context = {
        "app_name": settings.app_name,
        "account": account,
        "is_web_admin": is_web_admin_account(account),
        "show_onboarding_tour": show_onboarding_tour,
        "urban_plan_badge": urban_plan_badge_payload,
    }
    context.update(_access_context(session, account))
    context.update(extra)
    return context


def _qr_data_uri(value: str | None) -> str | None:
    if not value:
        return None
    image = qrcode.make(
        value,
        image_factory=SvgPathImage,
        box_size=12,
        border=2,
    )
    buffer = BytesIO()
    image.save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _payment_context(payment: AccountPayment | None) -> dict[str, object]:
    is_payable = bool(
        payment
        and payment.payment_status == PaymentStatus.awaiting_transfer.value
        and (payment.payment_provider_status or "") not in TERMINAL_PROVIDER_STATUSES
        and payment.payment_provider_url
    )
    payment_url = payment.payment_provider_url if payment and is_payable else None
    payment_qr_image_url = payment.payment_provider_qr_image_url if payment and is_payable else None
    return {
        "payment": payment,
        "payment_id": payment.id if payment else None,
        "payment_url": payment_url,
        "payment_qr_image_url": payment_qr_image_url,
        "payment_qr": None if payment_qr_image_url else _qr_data_uri(payment_url),
        "payment_status": payment.payment_status if payment else PaymentStatus.not_requested.value,
        "payment_amount": (
            payment.payment_amount_kzt if payment and payment.payment_amount_kzt else settings.platform_access_price_kzt
        ),
    }


def _get_session_account(request: Request, session: Session) -> Account | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    web_session = _get_session_by_token(session, token)
    if web_session is None:
        return None
    account = session.get(Account, web_session.account_id)
    if account is not None and account_has_permanent_access(session, account):
        session.commit()
    return account


def _account_search_filter(account: Account):
    conditions = [SearchRequest.web_account_id == account.id]
    if account.telegram_user_id:
        conditions.append(SearchRequest.telegram_user_id == account.telegram_user_id)
    return or_(*conditions)


def _can_access_search(account: Account, search: SearchRequest) -> bool:
    if search.web_account_id == account.id:
        return True
    if account.telegram_user_id and search.telegram_user_id == account.telegram_user_id:
        return True
    return (
        search.web_account_id is None
        and search.telegram_user_id is None
        and (search.raw_query or "").startswith("web-cabinet")
    )


def _search_status_label(status: str) -> str:
    return {
        "queued": "Заявка в очереди",
        "processing": "Идет обработка",
        "review": "Проверка результата",
        "ready": "Результат готов",
        "delivered": "Результат выдан",
        "failed": "Ошибка обработки",
    }.get(status, status)


def _search_status_message(search: SearchRequest) -> str:
    if (
        search.error_message
        or search.search_outcome in {"no_candidates", "filtered_out"}
        or search.urban_plan_status in {"unavailable", "blocked"}
    ):
        return explain_search_result(search).text
    if search.status == "queued":
        return "Заявка принята. Скоро начнется проверка ЕГКН, OSM и градостроительных слоев."
    if search.status == "processing":
        return "Идет поиск расчетных мест. Страница обновит результат сама, закрывать ее не нужно."
    if search.status == "review":
        return "Кандидаты найдены и проходят финальную проверку."
    if search.status in {"ready", "delivered"}:
        visible_count = len(_visible_search_candidates(search))
        if visible_count:
            return f"Готово: найдено подходящих вариантов {visible_count}."
        return "По выбранным условиям подходящие варианты не найдены."
    if search.status == "failed":
        return "Обработка не завершилась. Можно запустить новый поиск или повторить позже."
    return "Статус обновляется."


def _search_explanation_payload(search: SearchRequest) -> dict[str, str] | None:
    if not (
        search.error_message
        or search.search_outcome in {"no_candidates", "filtered_out"}
        or search.urban_plan_status in {"unavailable", "blocked"}
    ):
        return None
    explanation = explain_search_result(search)
    return {
        "title": explanation.title,
        "body": explanation.body,
        "next_step": explanation.next_step,
        "next_step_title": explanation.next_step_title,
    }


def _candidate_payload(candidate: Candidate, *, unlocked: bool) -> dict[str, object]:
    return {
        "rank": candidate.rank,
        "locality": candidate.locality,
        "region_chain": candidate.region_chain,
        "latitude": candidate.latitude if unlocked else None,
        "longitude": candidate.longitude if unlocked else None,
        "score": candidate.score,
        "nearby_cadastre": candidate.nearby_cadastre if unlocked else None,
        "nearby_distance_m": candidate.nearby_distance_m,
        "nearby_land_use": candidate.nearby_land_use,
        "road_distance_m": candidate.road_distance_m,
        "cemetery_distance_m": candidate.cemetery_distance_m,
        "review_status": candidate.review_status,
        "urban_plan_status": candidate.urban_plan_status,
        "urban_plan_badge": urban_plan_badge_payload(candidate.urban_plan_status),
        "urban_plan_zone": candidate.urban_plan_zone,
        "google_maps_url": candidate.google_maps_url if unlocked else None,
        "egkn_url": candidate.egkn_url if unlocked else None,
        "risk_notes": candidate.risk_notes,
        "locked": not unlocked,
    }


def _visible_search_candidates(search: SearchRequest) -> list[Candidate]:
    return sorted(approved_candidates(search), key=lambda item: item.rank)


def _auction_user_key(account: Account) -> str:
    if account.telegram_user_id:
        return account.telegram_user_id
    return hashlib.sha256(f"web:{account.id}".encode()).hexdigest()[:32]


def _auction_user_keys(account: Account) -> list[str]:
    keys = [_auction_user_key(account)]
    web_key = f"web:{account.id}"
    if web_key not in keys:
        keys.append(web_key)
    return keys


def _favorite_lot_ids(session: Session, account: Account) -> set[str]:
    return {
        row[0]
        for row in session.execute(
            select(AuctionFavorite.lot_id).where(
                or_(
                    AuctionFavorite.account_id == account.id,
                    AuctionFavorite.telegram_user_id.in_(_auction_user_keys(account)),
                )
            )
        ).all()
    }


def _filters_from_values(
    *,
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    min_price_kzt: float | None = None,
    max_price_kzt: float | None = None,
    min_area_ha: float | None = None,
    max_area_ha: float | None = None,
) -> AuctionFilters:
    return AuctionFilters(
        region=region or None,
        district=district or None,
        locality=locality or None,
        purpose_query=purpose or None,
        min_price_kzt=min_price_kzt,
        max_price_kzt=max_price_kzt,
        min_area_ha=min_area_ha,
        max_area_ha=max_area_ha,
    )


def _optional_float(value: str | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        if not cleaned:
            return None
        return float(cleaned)
    return float(value)


def _optional_score(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        score = int(cleaned)
    else:
        score = int(value)
    if not 0 <= score <= 100:
        raise ValueError
    return score


def _auction_v2_filter_values(
    *,
    q: str = "",
    lot_scope: str = "active",
    sort_by: str = "best",
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    min_price_kzt: str = "",
    max_price_kzt: str = "",
    min_area_ha: str = "",
    max_area_ha: str = "",
    min_score: str = "",
    eqazyna_status: str = "",
    risk_level: str = "",
    confidence_level: str = "",
    recommended_action: str = "",
    stage: str = "",
    deadline_status: str = "",
    geo_status: str = "",
) -> dict[str, str]:
    lot_scope_value = lot_scope.strip() if lot_scope else "active"
    if lot_scope_value not in LOT_SCOPE_LABELS:
        lot_scope_value = "active"
    sort_value = sort_by.strip() if sort_by else "best"
    if sort_value not in AUCTION_V2_SORT_LABELS:
        sort_value = "best"
    eqazyna_status_value = eqazyna_status.strip() if eqazyna_status else ""
    if eqazyna_status_value not in EQAZYNA_STATUS_FILTER_LABELS:
        eqazyna_status_value = ""
    return {
        "q": q.strip() if q else "",
        "lot_scope": lot_scope_value if lot_scope_value != "active" else "",
        "sort_by": sort_value if sort_value != "best" else "",
        "region": region or "",
        "district": district or "",
        "locality": locality or "",
        "purpose": purpose or "",
        "min_price_kzt": min_price_kzt.strip() if min_price_kzt else "",
        "max_price_kzt": max_price_kzt.strip() if max_price_kzt else "",
        "min_area_ha": min_area_ha.strip() if min_area_ha else "",
        "max_area_ha": max_area_ha.strip() if max_area_ha else "",
        "min_score": min_score.strip() if min_score else "",
        "eqazyna_status": eqazyna_status_value,
        "risk_level": risk_level.strip() if risk_level else "",
        "confidence_level": confidence_level.strip() if confidence_level else "",
        "recommended_action": recommended_action.strip() if recommended_action else "",
        "stage": stage.strip() if stage else "",
        "deadline_status": deadline_status.strip() if deadline_status else "",
        "geo_status": geo_status.strip() if geo_status else "",
    }


def _auction_v2_filter_query(filter_values: dict[str, str]) -> str:
    return urlencode({key: value for key, value in filter_values.items() if value})


def _auction_v2_query_with(
    filter_values: dict[str, str],
    **overrides: str,
) -> str:
    next_values = dict(filter_values)
    next_values.update(overrides)
    return _auction_v2_filter_query(next_values)


def _auction_v2_filters_from_values(filter_values: dict[str, str]) -> AuctionV2Filters:
    parsed_min_score = (
        int(filter_values["min_score"]) if filter_values["min_score"] else None
    )
    if parsed_min_score is not None and not 0 <= parsed_min_score <= 100:
        raise ValueError
    lot_scope_value = filter_values["lot_scope"] or "active"
    base_filters = _filters_from_values(
        region=filter_values["region"],
        district=filter_values["district"],
        locality=filter_values["locality"],
        purpose=filter_values["purpose"],
        min_price_kzt=_optional_float(filter_values["min_price_kzt"]),
        max_price_kzt=_optional_float(filter_values["max_price_kzt"]),
        min_area_ha=_optional_float(filter_values["min_area_ha"]),
        max_area_ha=_optional_float(filter_values["max_area_ha"]),
    )
    if lot_scope_value in {"archive", "all"}:
        base_filters.active_only = False
    return AuctionV2Filters(
        base=base_filters,
        search_query=filter_values["q"] or None,
        lot_scope=lot_scope_value,
        sort_by=filter_values["sort_by"] or "best",
        eqazyna_status=filter_values["eqazyna_status"] or None,
        min_score=parsed_min_score,
        risk_level=filter_values["risk_level"] or None,
        confidence_level=filter_values["confidence_level"] or None,
        recommended_action=filter_values["recommended_action"] or None,
        stage=filter_values["stage"] or None,
        deadline_status=filter_values["deadline_status"] or None,
        geo_status=filter_values["geo_status"] or None,
    )


def _search_payload(
    search: SearchRequest,
    *,
    session: Session,
    account: Account,
) -> dict[str, object]:
    candidates = _visible_search_candidates(search)
    genplan_reference = genplan_reference_payload(
        search,
        language=search.language,
        manual_files_root=settings.manual_genplan_files_root,
    )
    unlocked = account_access_kind(session, account) in {"paid", "trial"}
    return {
        "id": search.id,
        "status": search.status,
        "status_label": _search_status_label(search.status),
        "progress": search.progress or 0,
        "message": _search_status_message(search),
        "explanation": _search_explanation_payload(search),
        "candidate_count": len(candidates),
        "is_running": search.status in {"queued", "processing", "review"},
        "is_failed": search.status == "failed",
        "updated_at": search.updated_at.isoformat() if search.updated_at else None,
        "urban_plan_status": search.urban_plan_status,
        "urban_plan_badge": urban_plan_badge_payload(
            search.urban_plan_status,
            language=search.language,
            reference_source_kind=genplan_reference.get("source_kind"),
        ),
        "urban_plan_message": search.urban_plan_message,
        "urban_plan_reference": genplan_reference,
        "payment_status": search.payment_status,
        "can_request_next_batch": _can_request_next_batch(session, account, search),
        "report_unlocked": unlocked,
        "candidates": [_candidate_payload(candidate, unlocked=unlocked) for candidate in candidates],
    }


def _can_request_next_batch(session: Session, account: Account, search: SearchRequest) -> bool:
    if search.status not in {"ready", "delivered"}:
        return False
    return len(search.candidates) >= search.result_limit


def require_web_account(request: Request, session: Session = Depends(get_db)) -> Account:
    account = _get_session_account(request, session)
    if account is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return account


def _auction_catalog_rows(
    session: Session,
    column,
    *conditions,
) -> list[dict[str, str]]:
    return _auction_catalog_rows_from_pairs(
        _auction_catalog_count_pairs(session, column, *conditions)
    )


def _auction_catalog_count_pairs(
    session: Session,
    column,
    *conditions,
    lot_scope: str | None = None,
) -> list[tuple[str, int]]:
    scope_conditions = _auction_catalog_scope_conditions(lot_scope)
    rows = session.execute(
        select(column, func.count(AuctionLot.id))
        .where(column.is_not(None), *conditions, *scope_conditions)
        .group_by(column)
        .order_by(column)
    ).all()
    return [(str(value), int(count)) for value, count in rows if value]


def _auction_catalog_count_pairs_for_scope(
    session: Session,
    column,
    *,
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    lot_scope: str | None = None,
) -> list[tuple[str, int]]:
    scope_conditions = _auction_catalog_scope_conditions(lot_scope)
    rows = session.execute(
        select(
            column,
            AuctionLot.region,
            AuctionLot.district,
            AuctionLot.locality,
            func.count(AuctionLot.id),
        )
        .where(column.is_not(None), *scope_conditions)
        .group_by(column, AuctionLot.region, AuctionLot.district, AuctionLot.locality)
        .order_by(column)
    ).all()
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for value, lot_region, lot_district, lot_locality, count in rows:
        if not value:
            continue
        if region and not _catalog_values_match(lot_region, region):
            continue
        if district and not _catalog_values_match(lot_district, district):
            continue
        if locality and not _catalog_values_match(lot_locality, locality):
            continue
        key = _auction_catalog_key(value)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + int(count)
        labels.setdefault(key, str(value))
    return sorted(
        [(labels[key], count) for key, count in counts.items()],
        key=lambda item: _auction_catalog_key(item[0]),
    )


def _auction_catalog_scope_conditions(lot_scope: str | None) -> list[object]:
    scope = lot_scope if lot_scope in LOT_SCOPE_LABELS else "active"
    if scope == "all":
        return []
    now = datetime.now(UTC)
    if scope == "archive":
        return [
            or_(
                AuctionLot.active.is_(False),
                AuctionLot.auction_starts_at < now,
                AuctionLot.source_search_status.in_(ARCHIVED_EQAZYNA_SEARCH_STATUSES),
            )
        ]
    if scope == "future":
        return [
            AuctionLot.active.is_(True),
            AuctionLot.auction_starts_at >= now,
        ]
    return [AuctionLot.active.is_(True)]


def _auction_catalog_rows_from_pairs(
    rows: list[tuple[str, int]],
) -> list[dict[str, str]]:
    return [
        {"value": value, "label": f"{value} ({count})"}
        for value, count in rows
    ]


def _egkn_region_rows() -> list[dict[str, str]]:
    return [
        {
            "value": row.get("name") or row.get("nameRu") or "",
            "label": row.get("nameRu") or row.get("name") or "",
        }
        for row in catalog_provider.regions()
        if row.get("name") or row.get("nameRu")
    ]


def _egkn_district_rows(region: str) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    district_prefix = normalize_name("район")
    for row in catalog_provider.districts(region):
        display_key = normalize_name(row.display_name)
        is_district_label = row.display_name.lower().startswith("р-н") or (
            bool(district_prefix) and display_key.startswith(district_prefix)
        )
        rows.append(
            {
                "id": row.id,
                "value": f"{row.name} район" if is_district_label else row.name,
                "label": row.display_name,
            }
        )
    return rows


def _egkn_settlement_rows(district_id: int) -> list[dict[str, str]]:
    rows = catalog_provider.settlement_options(district_id)
    rows.sort(key=lambda row: normalize_name(row.name))
    if not rows:
        return [
            {
                "value": "",
                "label": "Искать по территории выбранного района",
            }
        ]
    return [
        {"value": row.name, "label": f"{row.name} · КАТО {row.kato}"}
        for row in rows
    ]


def _auction_catalog_key(value: object) -> str:
    key = normalize_name(str(value or ""))
    key = sub(r"\([^)]*\)", " ", key)
    key = key.replace("р-н.", " ")
    key = key.replace("р-н", " ")
    key = key.replace("г.", " ")
    key = sub(r"\b(область|облысы|район|ауданы|город|қаласы)\b", " ", key)
    return sub(r"\s+", " ", key).strip(" .,-")


def _catalog_keys_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return len(left) >= 4 and len(right) >= 4 and (left in right or right in left)


def _catalog_values_match(left: object, right: object) -> bool:
    return _catalog_keys_match(_auction_catalog_key(left), _auction_catalog_key(right))


def _dedupe_catalog_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        value = str(row.get("value") or "")
        label = str(row.get("label") or value)
        key = _auction_catalog_key(value) or _auction_catalog_key(label)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        result.append({"value": value, "label": label})
    return result


def _fallback_region_rows() -> list[dict[str, object]]:
    rows = [{"value": name, "label": name} for name in KAZAKHSTAN_REGION_FALLBACKS]
    for record in manual_genplan_records():
        if record.region:
            rows.append({"value": record.region, "label": record.region})
    return _dedupe_catalog_rows(rows)


def _manual_genplan_district_rows(region: str) -> list[dict[str, object]]:
    rows = [
        {"value": record.district, "label": record.district}
        for record in manual_genplan_records()
        if record.district and _catalog_values_match(record.region, region)
    ]
    return _dedupe_catalog_rows(rows)


def _manual_genplan_locality_rows(
    *,
    region: str | None = None,
    district: str | None = None,
) -> list[dict[str, object]]:
    rows = []
    for record in manual_genplan_records():
        if not record.locality:
            continue
        if region and not _catalog_values_match(record.region, region):
            continue
        if district and not _catalog_values_match(record.district, district):
            continue
        rows.append({"value": record.locality, "label": record.locality})
    return _dedupe_catalog_rows(rows)


def _merge_official_catalog_with_auction_counts(
    official_rows: list[dict[str, object]],
    auction_rows: list[tuple[str, int]],
) -> list[dict[str, object]]:
    counts_by_key: dict[str, int] = {}
    for value, count in auction_rows:
        key = _auction_catalog_key(value)
        if key:
            counts_by_key[key] = counts_by_key.get(key, 0) + count

    result: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for row in official_rows:
        value = str(row.get("value") or "")
        if not value:
            continue
        label = str(row.get("label") or value)
        key = _auction_catalog_key(value) or _auction_catalog_key(label)
        matched_keys = {
            row_key for row_key in counts_by_key if _catalog_keys_match(row_key, key)
        }
        count = sum(counts_by_key[row_key] for row_key in matched_keys)
        merged = dict(row)
        merged["value"] = value
        merged["label"] = f"{label} ({count})" if count else label
        result.append(merged)
        if key:
            seen_keys.add(key)
        seen_keys.update(matched_keys)

    for value, count in auction_rows:
        key = _auction_catalog_key(value)
        if key and any(_catalog_keys_match(key, seen_key) for seen_key in seen_keys):
            continue
        result.append({"value": value, "label": f"{value} ({count})"})
        if key:
            seen_keys.add(key)

    return result


def _start_web_session(request: Request, session: Session, account: Account) -> RedirectResponse:
    token = secrets.token_urlsafe(48)
    web_session = WebSession(
        account_id=account.id,
        token_hash=_hash(token),
        user_agent=_user_agent(request),
        ip_address=_client_ip(request),
        expires_at=_now() + timedelta(days=SESSION_DAYS),
    )
    session.add(web_session)
    session.commit()
    response = RedirectResponse("/cabinet", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.app_base_url.startswith("https://"),
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 60 * 60,
    )
    return response


def _create_sms_code(session: Session, *, phone: str, purpose: str) -> tuple[WebLoginCode, str]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    login_code = WebLoginCode(
        phone=phone,
        code_hash=_hash(code),
        purpose=purpose,
        expires_at=_now() + timedelta(minutes=CODE_TTL_MINUTES),
    )
    session.add(login_code)
    return login_code, code


def _latest_login_code(
    session: Session,
    *,
    phone: str,
    purpose: str,
) -> WebLoginCode | None:
    return session.scalar(
        select(WebLoginCode)
        .where(
            WebLoginCode.phone == phone,
            WebLoginCode.purpose == purpose,
            WebLoginCode.consumed_at.is_(None),
            WebLoginCode.expires_at > _now(),
        )
        .order_by(WebLoginCode.created_at.desc())
    )


def _register_failed_code_attempt(
    session: Session,
    *,
    account: Account,
    login_code: WebLoginCode | None,
) -> None:
    account.failed_login_attempts += 1
    if account.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
        account.locked_until = _now() + timedelta(minutes=LOCK_MINUTES)
        account.failed_login_attempts = 0
    if login_code:
        login_code.attempts += 1
    session.commit()


def _revoke_web_sessions(session: Session, account: Account) -> None:
    now = _now()
    for web_session in session.scalars(
        select(WebSession).where(
            WebSession.account_id == account.id,
            WebSession.revoked_at.is_(None),
        )
    ):
        web_session.revoked_at = now


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, session: Session = Depends(get_db)):
    account = _get_session_account(request, session)
    market = auction_market_snapshot(session)
    active_lots = session.scalar(
        select(func.count(AuctionLot.id)).where(AuctionLot.active.is_(True))
    )
    analysis_count = session.scalar(select(func.count(SearchRequest.id)))
    completed_analysis_count = session.scalar(
        select(func.count(SearchRequest.id)).where(
            SearchRequest.status.in_(("ready", "delivered", "completed"))
        )
    )
    return templates.TemplateResponse(
        request=request,
        name="site_landing.html",
        context={
            "app_name": settings.app_name,
            "account": account,
            "price_kzt": settings.platform_access_price_kzt,
            "trial_days": settings.trial_access_days,
            "trial_label": _trial_access_label(settings.trial_access_days),
            "active_lots": active_lots or 0,
            "analysis_count": analysis_count or 0,
            "completed_analysis_count": completed_analysis_count or 0,
            "market": market,
        },
    )


@router.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request, session: Session = Depends(get_db)):
    return _legal_page(
        request,
        session,
        title="Публичная оферта",
        eyebrow="Юридические условия",
        lead="Документ фиксирует правила доступа к сервису Жертап и ограничения предварительного земельного анализа.",
        sections=[
            {
                "title": "Предмет сервиса",
                "paragraphs": [
                    "Жертап предоставляет информационный сервис для предварительного анализа открытых земельных данных, публичных карт, слоев ЕГКН, OSM, опубликованных градостроительных слоев и аукционных данных E-Qazyna.",
                    "Сервис не является государственным органом, юридическим заключением, кадастровой экспертизой или гарантией получения земельного участка.",
                ],
            },
            {
                "title": "Ограничения ответственности",
                "paragraphs": [
                    "Клиент понимает, что результаты являются ориентировочными и должны быть дополнительно проверены через акимат, земельные органы, E-Qazyna, кадастровые и иные официальные источники.",
                    "Пустое место на карте, найденный лот или расчетная пригодность не подтверждают юридическую свободу участка и не создают права требования к Жертап.",
                ],
            },
            {
                "title": "Согласие клиента",
                "paragraphs": [
                    "Запрашивая SMS-код и входя в личный кабинет, клиент подтверждает принятие оферты, условий сервиса и отсутствие претензий к предварительному характеру анализа.",
                    "Факт согласия сохраняется в системе: версия оферты, дата, IP-адрес и технические данные браузера.",
                ],
            },
        ],
    )


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request, session: Session = Depends(get_db)):
    return _legal_page(
        request,
        session,
        title="Политика конфиденциальности",
        eyebrow="Персональные данные",
        lead="Мы храним только данные, необходимые для входа, связи аккаунта с Telegram, оплаты и работы заявок.",
        sections=[
            {
                "title": "Какие данные используются",
                "paragraphs": [
                    "Сервис может хранить номер телефона, идентификатор Telegram, историю заявок, статусы оплат, технические данные входа и результаты расчетов.",
                    "SMS-коды хранятся только в виде хеша и действуют ограниченное время.",
                ],
            },
            {
                "title": "Безопасность",
                "paragraphs": [
                    "Сессии веб-кабинета хранятся в httpOnly cookie, коды одноразовые, после нескольких неверных попыток вход временно блокируется.",
                    "Доступ к серверу и базе должен использоваться только администраторами проекта.",
                ],
            },
        ],
    )


@router.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request, session: Session = Depends(get_db)):
    return _legal_page(
        request,
        session,
        title="Условия сервиса",
        eyebrow="Правила использования",
        lead="Короткие правила, которые клиент принимает перед входом в личный кабинет.",
        sections=[
            {
                "title": "Проверка результатов",
                "paragraphs": [
                    "Перед покупкой, участием в аукционе, подачей заявления или выездом клиент самостоятельно проверяет данные в официальных источниках.",
                    "Жертап помогает сократить время поиска, но не заменяет юридическую, кадастровую и градостроительную проверку.",
                ],
            },
            {
                "title": "Доступ и оплата",
                "paragraphs": [
                    "Доступ к платным функциям активируется после подтверждения оплаты через подключенный платежный сценарий.",
                    "Единый аккаунт может использоваться в веб-кабинете и Telegram после привязки Telegram.",
                ],
            },
        ],
    )


def _legal_page(
    request: Request,
    session: Session,
    *,
    title: str,
    eyebrow: str,
    lead: str,
    sections: list[dict],
):
    return templates.TemplateResponse(
        request=request,
        name="site_legal.html",
        context={
            "app_name": settings.app_name,
            "account": _get_session_account(request, session),
            "title": title,
            "eyebrow": eyebrow,
            "lead": lead,
            "sections": sections,
        },
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_db)):
    if _get_session_account(request, session):
        return RedirectResponse("/cabinet", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="site_login.html",
        context=_login_context(request),
    )


@router.post("/login")
def password_login(
    request: Request,
    phone: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_db),
):
    try:
        normalized = normalize_phone(phone)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(request, error=str(exc), phone=phone),
            status_code=400,
        )
    ip = _client_ip(request)
    login_ip_state = consume_rate_limit(
        f"web:login:ip:{ip}",
        limit=LOGIN_RATE_LIMIT_IP_PER_MINUTE,
        window_seconds=LOGIN_RATE_LIMIT_IP_WINDOW_SECONDS,
    )
    if not login_ip_state.allowed:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Слишком много попыток входа с данного IP. Попробуйте позже.",
                login_phone=normalized,
            ),
            status_code=429,
        )
    login_phone_state = consume_rate_limit(
        f"web:login:phone:{normalized}",
        limit=LOGIN_RATE_LIMIT_PHONE_PER_HOUR,
        window_seconds=LOGIN_RATE_LIMIT_PHONE_WINDOW_SECONDS,
    )
    if not login_phone_state.allowed:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Слишком много попыток входа для этого номера. Попробуйте позже.",
                login_phone=normalized,
            ),
            status_code=429,
        )
    account = session.scalar(select(Account).where(Account.phone == normalized))
    if account is None or not account.password_hash:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Неверные учетные данные. Проверьте телефон и пароль.",
                phone=normalized,
            ),
            status_code=400,
        )
    if account.locked_until and account.locked_until > _now():
        return RedirectResponse("/login?locked=1", status_code=303)
    if not _verify_password(password, account.password_hash):
        account.failed_login_attempts += 1
        if account.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            account.locked_until = _now() + timedelta(minutes=LOCK_MINUTES)
            account.failed_login_attempts = 0
        session.commit()
        return RedirectResponse("/login?invalid_password=1", status_code=303)
    account.failed_login_attempts = 0
    account.locked_until = None
    session.commit()
    return _start_web_session(request, session, account)


@router.post("/register/request-code", response_class=HTMLResponse)
def request_registration_code(
    request: Request,
    phone: str = Form(...),
    offer_accepted: str | None = Form(None),
    session: Session = Depends(get_db),
):
    try:
        normalized = normalize_phone(phone)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(request, register_error=str(exc), register_phone=phone),
            status_code=400,
        )
    ip = _client_ip(request)
    sms_ip_state = consume_rate_limit(
        f"web:register:ip:{ip}",
        limit=SMS_REQUEST_RATE_LIMIT_PER_IP_PER_HOUR,
        window_seconds=SMS_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not sms_ip_state.allowed:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Слишком много неуспешных запросов с вашего IP. Попробуйте позже.",
                register_phone=normalized,
            ),
            status_code=429,
        )
    sms_phone_state = consume_rate_limit(
        f"web:register:phone:{normalized}",
        limit=SMS_REQUEST_RATE_LIMIT_PER_PHONE_PER_HOUR,
        window_seconds=SMS_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not sms_phone_state.allowed:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Слишком много SMS-кодов на этот номер. Попробуйте позже.",
                register_phone=normalized,
            ),
            status_code=429,
        )
    if offer_accepted != "yes":
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Для входа нужно принять оферту и условия сервиса.",
                phone=phone,
            ),
            status_code=400,
        )
    account = session.scalar(select(Account).where(Account.phone == normalized))
    if account and account.locked_until and account.locked_until > _now():
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Слишком много неверных попыток. Повторите вход через 5 минут.",
                phone=normalized,
            ),
            status_code=429,
        )
    if account and (account.password_hash or account.phone_verified_at):
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Не удалось отправить SMS-код. Проверьте номер и повторите позже.",
                register_phone=normalized,
            ),
            status_code=200,
        )
    if account is None:
        account = Account(phone=normalized)
        session.add(account)
        session.flush()
    account.offer_version = OFFER_VERSION
    account.offer_accepted_at = _now()
    account.offer_accepted_ip = _client_ip(request)
    account.offer_accepted_user_agent = _user_agent(request)
    _, code = _create_sms_code(session, phone=normalized, purpose="register")
    try:
        send_login_code(normalized, code)
    except Exception as exc:
        logger.warning("Could not send web login SMS to %s: %s", normalized, exc)
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                error="Не удалось отправить SMS-код. Попробуйте еще раз чуть позже.",
                phone=normalized,
            ),
            status_code=502,
        )
    session.commit()
    dev_code = code if settings.app_env.lower() in {"development", "dev", "local", "test"} else None
    return templates.TemplateResponse(
        request=request,
        name="site_login.html",
        context=_login_context(
            request,
            register_phone=normalized,
            register_code_requested=True,
            dev_code=dev_code,
        ),
    )


@router.post("/register/verify")
def verify_registration_code(
    request: Request,
    phone: str = Form(...),
    code: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    session: Session = Depends(get_db),
):
    try:
        normalized = normalize_phone(phone)
    except ValueError:
        return RedirectResponse("/login", status_code=303)
    account = session.scalar(select(Account).where(Account.phone == normalized))
    if account and account.locked_until and account.locked_until > _now():
        return RedirectResponse("/login?locked=1", status_code=303)
    password_error = _password_error(password, password_confirm)
    if password_error:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                register_error=password_error,
                register_phone=normalized,
                register_code_requested=True,
            ),
            status_code=400,
        )
    if account and (account.password_hash or account.phone_verified_at):
        return RedirectResponse("/login?invalid=1", status_code=303)
    login_code = _latest_login_code(session, phone=normalized, purpose="register")
    normalized_code = code.strip()
    if login_code is None or not _hash_match(login_code.code_hash, normalized_code):
        if account is None:
            account = Account(phone=normalized)
            session.add(account)
        _register_failed_code_attempt(session, account=account, login_code=login_code)
        return RedirectResponse("/login?invalid=1", status_code=303)
    if _migrate_hash(login_code, "code_hash", normalized_code):
        session.flush()
    if account is None:
        account = Account(phone=normalized)
        session.add(account)
        session.flush()
    account.phone_verified_at = _now()
    account.password_hash = _hash_password(password)
    account.password_set_at = _now()
    account.failed_login_attempts = 0
    account.locked_until = None
    ensure_account_trial(account)
    account.onboarding_tour_available_at = _now()
    login_code.consumed_at = _now()
    token = secrets.token_urlsafe(48)
    web_session = WebSession(
        account_id=account.id,
        token_hash=_hash(token),
        user_agent=request.headers.get("user-agent", "")[:1000],
        ip_address=_client_ip(request),
        expires_at=_now() + timedelta(days=SESSION_DAYS),
    )
    session.add(web_session)
    session.commit()
    _notify_admin_web_registration(account, request)
    response = RedirectResponse("/cabinet", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.app_base_url.startswith("https://"),
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 60 * 60,
    )
    return response


@router.post("/password/reset/request-code", response_class=HTMLResponse)
def request_password_reset_code(
    request: Request,
    phone: str = Form(...),
    session: Session = Depends(get_db),
):
    try:
        normalized = normalize_phone(phone)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(request, reset_error=str(exc), reset_phone=phone),
            status_code=400,
        )
    ip = _client_ip(request)
    sms_ip_state = consume_rate_limit(
        f"web:password_reset:ip:{ip}",
        limit=SMS_REQUEST_RATE_LIMIT_PER_IP_PER_HOUR,
        window_seconds=SMS_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not sms_ip_state.allowed:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                reset_error="Слишком много неуспешных запросов с вашего IP. Попробуйте позже.",
                reset_phone=normalized,
            ),
            status_code=429,
        )
    sms_phone_state = consume_rate_limit(
        f"web:password_reset:phone:{normalized}",
        limit=SMS_REQUEST_RATE_LIMIT_PER_PHONE_PER_HOUR,
        window_seconds=SMS_REQUEST_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not sms_phone_state.allowed:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                reset_error="Слишком много SMS-кодов для этого номера. Попробуйте позже.",
                reset_phone=normalized,
            ),
            status_code=429,
        )
    account = session.scalar(select(Account).where(Account.phone == normalized))
    if account is None or not account.password_hash:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                reset_error="Если этот номер зарегистрирован, проверьте его и повторите запрос.",
                reset_phone=normalized,
            ),
            status_code=200,
        )
    if account.locked_until and account.locked_until > _now():
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                reset_error="Слишком много неверных попыток. Повторите через 5 минут.",
                reset_phone=normalized,
            ),
            status_code=429,
        )
    _, code = _create_sms_code(session, phone=normalized, purpose="password_reset")
    try:
        send_login_code(normalized, code)
    except Exception as exc:
        logger.warning("Could not send password reset SMS to %s: %s", normalized, exc)
        session.rollback()
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                reset_error="Не удалось отправить SMS-код. Попробуйте еще раз чуть позже.",
                reset_phone=normalized,
            ),
            status_code=502,
        )
    session.commit()
    dev_code = code if settings.app_env.lower() in {"development", "dev", "local", "test"} else None
    return templates.TemplateResponse(
        request=request,
        name="site_login.html",
        context=_login_context(
            request,
            reset_phone=normalized,
            reset_code_requested=True,
            dev_code=dev_code,
        ),
    )


@router.post("/password/reset/verify")
def verify_password_reset_code(
    request: Request,
    phone: str = Form(...),
    code: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    session: Session = Depends(get_db),
):
    try:
        normalized = normalize_phone(phone)
    except ValueError:
        return RedirectResponse("/login", status_code=303)
    account = session.scalar(select(Account).where(Account.phone == normalized))
    if account is None or not account.password_hash:
        return RedirectResponse("/login?reset_invalid=1", status_code=303)
    if account.locked_until and account.locked_until > _now():
        return RedirectResponse("/login?locked=1", status_code=303)
    password_error = _password_error(password, password_confirm)
    if password_error:
        return templates.TemplateResponse(
            request=request,
            name="site_login.html",
            context=_login_context(
                request,
                reset_error=password_error,
                reset_phone=normalized,
                reset_code_requested=True,
            ),
            status_code=400,
        )
    login_code = _latest_login_code(session, phone=normalized, purpose="password_reset")
    normalized_code = code.strip()
    if login_code is None or not _hash_match(login_code.code_hash, normalized_code):
        _register_failed_code_attempt(session, account=account, login_code=login_code)
        return RedirectResponse("/login?reset_invalid=1", status_code=303)
    if _migrate_hash(login_code, "code_hash", normalized_code):
        session.flush()
    account.password_hash = _hash_password(password)
    account.password_set_at = _now()
    account.phone_verified_at = account.phone_verified_at or _now()
    account.failed_login_attempts = 0
    account.locked_until = None
    login_code.consumed_at = _now()
    _revoke_web_sessions(session, account)
    session.commit()
    return _start_web_session(request, session, account)


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        web_session = _get_session_by_token(session, token)
        if web_session:
            web_session.revoked_at = _now()
            session.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/cabinet", response_class=HTMLResponse)
def cabinet(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    searches = session.scalars(
        select(SearchRequest)
        .where(_account_search_filter(account))
        .order_by(SearchRequest.created_at.desc())
        .limit(8)
    ).all()
    lots, total_lots = list_auction_lots(session, AuctionFilters(), offset=0, limit=6)
    return templates.TemplateResponse(
        request=request,
        name="site_cabinet.html",
        context=_cabinet_context(
            session,
            account,
            searches=searches,
            lots=lots,
            total_lots=total_lots,
            price_kzt=settings.platform_access_price_kzt,
            telegram_bot_username=settings.telegram_bot_username,
        ),
    )


@router.get("/cabinet/help", response_class=HTMLResponse)
def cabinet_help(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request=request,
        name="site_help.html",
        context=_cabinet_context(session, account),
    )


@router.post("/cabinet/onboarding/dismiss")
def dismiss_cabinet_onboarding(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    account.onboarding_tour_dismissed_at = _now()
    session.commit()
    return {"ok": True}


@router.get("/cabinet/payment", response_class=HTMLResponse)
def cabinet_payment(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    account_has_permanent_access(session, account)
    payment = latest_account_payment(session, account)
    if not account_has_paid_access(account):
        try:
            if payment is None or payment.payment_status != PaymentStatus.awaiting_transfer.value or (
                payment.payment_provider_status or ""
            ) in TERMINAL_PROVIDER_STATUSES:
                payment = refresh_account_payment(session, account)
        except Exception:
            session.rollback()
            logger.exception("Could not prepare web account payment for account %s", account.id)
            payment = latest_account_payment(session, account)
    return templates.TemplateResponse(
        request=request,
        name="site_payment.html",
        context=_cabinet_context(
            session,
            account,
            price_kzt=settings.platform_access_price_kzt,
            apipay_enabled=settings.apipay_enabled,
            started=request.query_params.get("started") == "1",
            refreshed=request.query_params.get("refreshed") == "1",
            error=request.query_params.get("error"),
            **_payment_context(payment),
        ),
    )


@router.post("/cabinet/payment/start")
def start_cabinet_payment(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    if account_has_permanent_access(session, account):
        session.commit()
        return RedirectResponse("/cabinet/payment?paid=1", status_code=303)
    try:
        start_account_payment(session, account)
    except ValueError:
        return RedirectResponse("/cabinet/payment?error=unavailable", status_code=303)
    return RedirectResponse("/cabinet/payment?started=1", status_code=303)


@router.post("/cabinet/payment/refresh")
def refresh_cabinet_payment(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    try:
        renew_account_payment(session, account)
    except Exception:
        session.rollback()
        logger.exception("Could not renew web account payment for account %s", account.id)
        return RedirectResponse("/cabinet/payment?error=refresh", status_code=303)
    return RedirectResponse("/cabinet/payment?refreshed=1", status_code=303)


@router.get("/cabinet/payment/status")
def cabinet_payment_status(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        payment = latest_account_payment(session, account)
        if not account_has_paid_access(account):
            payment = refresh_account_payment(session, account)
    except Exception:
        session.rollback()
        logger.exception("Could not refresh web account payment status for account %s", account.id)
        payment = latest_account_payment(session, account)
    account_has_permanent_access(session, account)
    session.commit()
    return {
        "paid": account_has_paid_access(account),
        "access_label": _access_context(session, account)["access_label"],
        "payment_id": payment.id if payment else None,
        "payment_status": payment.payment_status if payment else PaymentStatus.not_requested.value,
        "provider_status": payment.payment_provider_status if payment else None,
        "payment_url": _payment_context(payment)["payment_url"],
        "payment_qr_image_url": _payment_context(payment)["payment_qr_image_url"],
    }


@router.get("/cabinet/settings", response_class=HTMLResponse)
def cabinet_settings(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request=request,
        name="site_settings.html",
        context=_cabinet_context(
            session,
            account,
            password_changed=request.query_params.get("password_changed") == "1",
        ),
    )


@router.post("/cabinet/settings/password", response_class=HTMLResponse)
def change_web_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    password_confirm: str = Form(...),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    if account.locked_until and account.locked_until > _now():
        return RedirectResponse("/login?locked=1", status_code=303)
    if not _verify_password(current_password, account.password_hash):
        account.failed_login_attempts += 1
        if account.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            account.locked_until = _now() + timedelta(minutes=LOCK_MINUTES)
            account.failed_login_attempts = 0
        session.commit()
        return templates.TemplateResponse(
            request=request,
            name="site_settings.html",
            context=_cabinet_context(
                session,
                account,
                password_error="Текущий пароль указан неверно.",
            ),
            status_code=400,
        )
    password_error = _password_error(new_password, password_confirm)
    if password_error:
        return templates.TemplateResponse(
            request=request,
            name="site_settings.html",
            context=_cabinet_context(
                session,
                account,
                password_error=password_error,
            ),
            status_code=400,
        )
    account.password_hash = _hash_password(new_password)
    account.password_set_at = _now()
    account.failed_login_attempts = 0
    account.locked_until = None
    session.commit()
    return RedirectResponse("/cabinet/settings?password_changed=1", status_code=303)


@router.get("/cabinet/feedback", response_class=HTMLResponse)
def cabinet_feedback(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    conversation = get_or_create_feedback_conversation(session, account=account, language="ru")
    session.commit()
    session.refresh(conversation)
    return templates.TemplateResponse(
        request=request,
        name="site_feedback.html",
        context=_cabinet_context(
            session,
            account,
            conversation=conversation,
            sent=request.query_params.get("sent") == "1",
            feedback_error=request.query_params.get("error") == "1",
        ),
    )


@router.post("/cabinet/feedback", response_class=HTMLResponse)
def submit_cabinet_feedback(
    message: str = Form(...),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    try:
        record_client_feedback(
            session,
            text=message,
            channel="web",
            account=account,
            language="ru",
        )
    except ValueError:
        return RedirectResponse("/cabinet/feedback?error=1", status_code=303)
    return RedirectResponse("/cabinet/feedback?sent=1", status_code=303)


@router.get("/cabinet/catalog/regions")
def web_catalog_regions(_: Account = Depends(require_web_account)) -> list[dict[str, str]]:
    try:
        return _egkn_region_rows()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Справочник областей ЕГКН недоступен",
        ) from exc


@router.get("/cabinet/catalog/districts")
def web_catalog_districts(
    region: str,
    _: Account = Depends(require_web_account),
) -> list[dict[str, str | int]]:
    try:
        return _egkn_district_rows(region)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Справочник районов ЕГКН недоступен",
        ) from exc


@router.get("/cabinet/catalog/settlements")
def web_catalog_settlements(
    district_id: int,
    _: Account = Depends(require_web_account),
) -> list[dict[str, str]]:
    try:
        return _egkn_settlement_rows(district_id)
    except EgknProviderError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Справочник населенных пунктов ЕГКН недоступен",
        ) from exc


@router.get("/cabinet/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    search_error = {
        "location": "Выберите область и район перед запуском анализа.",
    }.get(request.query_params.get("error"))
    return templates.TemplateResponse(
        request=request,
        name="site_search.html",
        context=_cabinet_context(session, account, search_error=search_error),
    )


@router.post("/cabinet/search")
def submit_search(
    request: Request,
    region: str = Form(""),
    district: str = Form(""),
    locality: str = Form(""),
    purpose: str = Form(LPH_NEW),
    irrigation_type: str = Form(""),
    area_ha: float = Form(0.12),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    region = region.strip()
    district = district.strip()
    if not region or not district:
        return RedirectResponse("/cabinet/search?error=location", status_code=303)
    ip = _client_ip(request)
    search_account_state = consume_rate_limit(
        f"web:cabinet:search:account:{account.id}",
        limit=CABINET_SEARCH_RATE_LIMIT_PER_ACCOUNT_PER_MINUTE,
        window_seconds=CABINET_SEARCH_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not search_account_state.allowed:
        raise HTTPException(status_code=429, detail="Too many search requests for this account.")
    search_ip_state = consume_rate_limit(
        f"web:cabinet:search:ip:{ip}",
        limit=CABINET_SEARCH_RATE_LIMIT_PER_IP_PER_MINUTE,
        window_seconds=CABINET_SEARCH_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not search_ip_state.allowed:
        raise HTTPException(status_code=429, detail="Too many search requests from this IP.")
    payload = SearchCreate(
        language="ru",
        region=region,
        district=district,
        locality=locality or None,
        purpose=purpose,
        irrigation_type=irrigation_type or None,
        area_ha=area_ha,
        telegram_user_id=account.telegram_user_id,
        telegram_chat_id=account.telegram_chat_id,
        raw_query=f"web-cabinet:{account.id}",
    )
    search, _ = create_search(session, payload)
    search.web_account_id = account.id
    session.commit()
    dispatch_search(search.id)
    return RedirectResponse(f"/cabinet/searches/{search.id}", status_code=303)


@router.get("/cabinet/searches/{search_id}", response_class=HTMLResponse)
def search_detail(
    request: Request,
    search_id: str,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    search = session.get(SearchRequest, search_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    if not _can_access_search(account, search):
        raise HTTPException(status_code=403, detail="Нет доступа к заявке")
    genplan_reference = genplan_reference_payload(
        search,
        language=search.language,
        manual_files_root=settings.manual_genplan_files_root,
    )
    return templates.TemplateResponse(
        request=request,
        name="site_search_detail.html",
        context=_cabinet_context(
            session,
            account,
            search=search,
            displayed_candidates=_visible_search_candidates(search),
            search_explanation=_search_explanation_payload(search),
            genplan_reference=genplan_reference,
            search_urban_plan_badge=urban_plan_badge_payload(
                search.urban_plan_status,
                language=search.language,
                reference_source_kind=genplan_reference.get("source_kind"),
            ),
            urban_plan_badge=urban_plan_badge_payload,
            can_request_next_batch=_can_request_next_batch(session, account, search),
            search_unlocked=account_access_kind(session, account) in {"paid", "trial"},
            next_error=request.query_params.get("next_error"),
        ),
    )


@router.post("/cabinet/searches/{search_id}/next")
def search_next_batch(
    search_id: str,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    search = get_request_with_candidates(session, search_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    if not _can_access_search(account, search):
        raise HTTPException(status_code=403, detail="Нет доступа к заявке")
    try:
        next_request, _position, created = create_next_batch(
            session,
            search.id,
            telegram_user_id=account.telegram_user_id,
            telegram_chat_id=account.telegram_chat_id,
            web_account_id=account.id,
            require_paid_access=False,
        )
    except ValueError:
        return RedirectResponse(
            f"/cabinet/searches/{search.id}?next_error=no_more",
            status_code=303,
        )
    except PermissionError:
        return RedirectResponse(
            f"/cabinet/searches/{search.id}?next_error=access",
            status_code=303,
        )
    if created:
        dispatch_search(next_request.id)
    return RedirectResponse(f"/cabinet/searches/{next_request.id}", status_code=303)


@router.get("/cabinet/searches/{search_id}/status")
def search_status(
    search_id: str,
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    ip = _client_ip(request)
    status_account_state = consume_rate_limit(
        f"web:cabinet:search_status:account:{account.id}",
        limit=CABINET_SEARCH_STATUS_RATE_LIMIT_PER_ACCOUNT_PER_MINUTE,
        window_seconds=CABINET_SEARCH_STATUS_WINDOW_SECONDS,
    )
    if not status_account_state.allowed:
        raise HTTPException(status_code=429, detail="Too many status requests for this account.")
    status_ip_state = consume_rate_limit(
        f"web:cabinet:search_status:ip:{ip}",
        limit=CABINET_SEARCH_STATUS_RATE_LIMIT_PER_IP_PER_MINUTE,
        window_seconds=CABINET_SEARCH_STATUS_WINDOW_SECONDS,
    )
    if not status_ip_state.allowed:
        raise HTTPException(status_code=429, detail="Too many status requests from this IP.")
    search = get_request_with_candidates(session, search_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    if not _can_access_search(account, search):
        raise HTTPException(status_code=403, detail="Нет доступа к заявке")
    return _search_payload(search, session=session, account=account)


@router.get("/cabinet/auctions/catalog/regions")
def web_auction_regions(
    lot_scope: str = "active",
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    auction_rows = _auction_catalog_count_pairs(
        session,
        AuctionLot.region,
        lot_scope=lot_scope,
    )
    try:
        official_rows = _egkn_region_rows()
    except Exception as exc:
        fallback_rows = _fallback_region_rows()
        if fallback_rows or auction_rows:
            return _merge_official_catalog_with_auction_counts(fallback_rows, auction_rows)
        raise HTTPException(
            status_code=502,
            detail="Справочник областей ЕГКН недоступен, а локальных аукционных данных пока нет",
        ) from exc
    if not official_rows:
        official_rows = _fallback_region_rows()
    return _merge_official_catalog_with_auction_counts(official_rows, auction_rows)


@router.get("/cabinet/auctions/catalog/districts")
def web_auction_districts(
    region: str,
    lot_scope: str = "active",
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    auction_rows = _auction_catalog_count_pairs_for_scope(
        session,
        AuctionLot.district,
        region=region,
        lot_scope=lot_scope,
    )
    try:
        official_rows = _egkn_district_rows(region)
    except Exception as exc:
        fallback_rows = _manual_genplan_district_rows(region)
        if fallback_rows or auction_rows:
            return _merge_official_catalog_with_auction_counts(fallback_rows, auction_rows)
        raise HTTPException(
            status_code=502,
            detail="Справочник районов ЕГКН недоступен, а локальных аукционных данных по региону пока нет",
        ) from exc
    if not official_rows:
        official_rows = _manual_genplan_district_rows(region)
    return _merge_official_catalog_with_auction_counts(official_rows, auction_rows)


@router.get("/cabinet/auctions/catalog/localities")
def web_auction_localities(
    region: str | None = None,
    district: str | None = None,
    district_id: int | None = None,
    lot_scope: str = "active",
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    auction_rows = _auction_catalog_count_pairs_for_scope(
        session,
        AuctionLot.locality,
        region=region,
        district=district,
        lot_scope=lot_scope,
    )
    fallback_rows = _manual_genplan_locality_rows(region=region, district=district)
    if district_id:
        try:
            official_rows = _egkn_settlement_rows(district_id)
        except EgknProviderError:
            return _merge_official_catalog_with_auction_counts(fallback_rows, auction_rows)
        except Exception as exc:
            if fallback_rows or auction_rows:
                return _merge_official_catalog_with_auction_counts(fallback_rows, auction_rows)
            raise HTTPException(
                status_code=502,
                detail="Справочник населенных пунктов ЕГКН недоступен, а локальных аукционных данных по району пока нет",
            ) from exc
        if not official_rows:
            official_rows = fallback_rows
        merged = _merge_official_catalog_with_auction_counts(official_rows, auction_rows)
        if merged:
            return merged
    if fallback_rows:
        return _merge_official_catalog_with_auction_counts(fallback_rows, auction_rows)
    return _auction_catalog_rows_from_pairs(auction_rows)


@router.get("/cabinet/auctions/catalog/purposes")
def web_auction_purposes(
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    lot_scope: str = "active",
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
) -> list[dict[str, str]]:
    return _auction_catalog_rows_from_pairs(
        _auction_catalog_count_pairs_for_scope(
            session,
            AuctionLot.functional_purpose_level2,
            region=region,
            district=district,
            locality=locality,
            lot_scope=lot_scope,
        )
    )


@router.get("/cabinet/auctions/favorites", response_class=HTMLResponse)
def web_auction_favorites(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    lots = list(
        session.scalars(
            select(AuctionLot)
            .join(AuctionFavorite, AuctionFavorite.lot_id == AuctionLot.id)
            .where(
                or_(
                    AuctionFavorite.account_id == account.id,
                    AuctionFavorite.telegram_user_id.in_(_auction_user_keys(account)),
                )
            )
            .order_by(AuctionFavorite.created_at.desc())
        ).all()
    )
    return templates.TemplateResponse(
        request=request,
        name="site_auction_favorites.html",
        context=_cabinet_context(
            session,
            account,
            lots=lots,
            favorite_ids={lot.id for lot in lots},
        ),
    )


@router.get("/cabinet/auctions/compare", response_class=HTMLResponse)
def web_auction_compare(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    lots = list(
        session.scalars(
            select(AuctionLot)
            .join(AuctionFavorite, AuctionFavorite.lot_id == AuctionLot.id)
            .where(
                or_(
                    AuctionFavorite.account_id == account.id,
                    AuctionFavorite.telegram_user_id.in_(_auction_user_keys(account)),
                )
            )
            .order_by(AuctionFavorite.created_at.desc())
            .limit(10)
        ).all()
    )
    metrics_by_lot = {lot.id: auction_lot_metrics(session, lot) for lot in lots}
    return templates.TemplateResponse(
        request=request,
        name="site_auction_compare.html",
        context=_cabinet_context(
            session,
            account,
            lots=lots,
            metrics_by_lot=metrics_by_lot,
        ),
    )


@router.get("/cabinet/auctions/subscriptions", response_class=HTMLResponse)
def web_auction_subscriptions(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    subscriptions = session.scalars(
        select(AuctionSubscription)
        .where(
            or_(
                AuctionSubscription.account_id == account.id,
                AuctionSubscription.telegram_user_id.in_(_auction_user_keys(account)),
            )
        )
        .order_by(AuctionSubscription.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="site_auction_subscriptions.html",
        context=_cabinet_context(session, account, subscriptions=subscriptions),
    )


@router.post("/cabinet/auctions/subscriptions")
def web_create_auction_subscription(
    region: str = Form(""),
    district: str = Form(""),
    locality: str = Form(""),
    purpose: str = Form(""),
    min_price_kzt: str = Form(""),
    max_price_kzt: str = Form(""),
    min_area_ha: str = Form(""),
    max_area_ha: str = Form(""),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    if account_access_kind(session, account) not in {"paid", "trial"}:
        return _redirect_with_query("/cabinet/auctions", locked="auction_subscription")
    filters = _filters_from_values(
        region=region,
        district=district,
        locality=locality,
        purpose=purpose,
        min_price_kzt=_optional_float(min_price_kzt),
        max_price_kzt=_optional_float(max_price_kzt),
        min_area_ha=_optional_float(min_area_ha),
        max_area_ha=_optional_float(max_area_ha),
    )
    user_key = _auction_user_key(account)
    subscription = session.scalar(
        select(AuctionSubscription).where(
            AuctionSubscription.account_id == account.id,
            AuctionSubscription.region == filters.region,
            AuctionSubscription.district == filters.district,
            AuctionSubscription.locality == filters.locality,
            AuctionSubscription.purpose_query == filters.purpose_query,
            AuctionSubscription.min_price_kzt == filters.min_price_kzt,
            AuctionSubscription.max_price_kzt == filters.max_price_kzt,
            AuctionSubscription.min_area_ha == filters.min_area_ha,
            AuctionSubscription.max_area_ha == filters.max_area_ha,
        )
    )
    if subscription is None:
        subscription = AuctionSubscription(
            account_id=account.id,
            telegram_user_id=user_key,
            telegram_chat_id=account.telegram_chat_id or "",
            language="ru",
            region=filters.region,
            district=filters.district,
            locality=filters.locality,
            purpose_query=filters.purpose_query,
            min_price_kzt=filters.min_price_kzt,
            max_price_kzt=filters.max_price_kzt,
            min_area_ha=filters.min_area_ha,
            max_area_ha=filters.max_area_ha,
        )
        session.add(subscription)
    else:
        subscription.telegram_user_id = user_key
        subscription.telegram_chat_id = account.telegram_chat_id or ""
        subscription.active = True
    session.commit()
    return _redirect_with_query("/cabinet/auctions/subscriptions", subscription="saved")


@router.post("/cabinet/auctions/subscriptions/{subscription_id}/disable")
def web_disable_auction_subscription(
    subscription_id: int,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    if account_access_kind(session, account) not in {"paid", "trial"}:
        return _redirect_with_query("/cabinet/auctions/subscriptions", locked="auction_subscription")
    subscription = session.scalar(
        select(AuctionSubscription).where(
            AuctionSubscription.id == subscription_id,
            or_(
                AuctionSubscription.account_id == account.id,
                AuctionSubscription.telegram_user_id.in_(_auction_user_keys(account)),
            ),
        )
    )
    if subscription:
        subscription.active = False
        session.commit()
    return _redirect_with_query("/cabinet/auctions/subscriptions", subscription="disabled")


@router.post("/cabinet/auctions/subscriptions/{subscription_id}/enable")
def web_enable_auction_subscription(
    subscription_id: int,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    if account_access_kind(session, account) not in {"paid", "trial"}:
        return _redirect_with_query("/cabinet/auctions/subscriptions", locked="auction_subscription")
    subscription = session.scalar(
        select(AuctionSubscription).where(
            AuctionSubscription.id == subscription_id,
            or_(
                AuctionSubscription.account_id == account.id,
                AuctionSubscription.telegram_user_id.in_(_auction_user_keys(account)),
            ),
        )
    )
    if subscription:
        subscription.active = True
        subscription.telegram_user_id = _auction_user_key(account)
        subscription.telegram_chat_id = account.telegram_chat_id or ""
        session.commit()
    return _redirect_with_query("/cabinet/auctions/subscriptions", subscription="enabled")


@router.post("/cabinet/auctions/subscriptions/{subscription_id}/delete")
def web_delete_auction_subscription(
    subscription_id: int,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    if account_access_kind(session, account) not in {"paid", "trial"}:
        return _redirect_with_query("/cabinet/auctions/subscriptions", locked="auction_subscription")
    subscription = session.scalar(
        select(AuctionSubscription).where(
            AuctionSubscription.id == subscription_id,
            or_(
                AuctionSubscription.account_id == account.id,
                AuctionSubscription.telegram_user_id.in_(_auction_user_keys(account)),
            ),
        )
    )
    if subscription:
        subscription.active = False
        subscription.account_id = None
        subscription.telegram_user_id = f"deleted:{subscription.id}:{subscription.telegram_user_id}"[:32]
        subscription.telegram_chat_id = ""
        subscription.updated_at = _now()
        session.commit()
    return _redirect_with_query("/cabinet/auctions/subscriptions", subscription="deleted")


@router.get("/cabinet/auctions", response_class=HTMLResponse)
def web_auctions(
    request: Request,
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    min_price_kzt: str = "",
    max_price_kzt: str = "",
    min_area_ha: str = "",
    max_area_ha: str = "",
    page: int = 1,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    page = max(page, 1)
    page_size = 40
    filter_values = {
        "region": region or "",
        "district": district or "",
        "locality": locality or "",
        "purpose": purpose or "",
        "min_price_kzt": min_price_kzt.strip() if min_price_kzt else "",
        "max_price_kzt": max_price_kzt.strip() if max_price_kzt else "",
        "min_area_ha": min_area_ha.strip() if min_area_ha else "",
        "max_area_ha": max_area_ha.strip() if max_area_ha else "",
    }
    filter_query = urlencode(
        {key: value for key, value in filter_values.items() if value}
    )
    try:
        min_price = _optional_float(min_price_kzt)
        max_price = _optional_float(max_price_kzt)
        min_area = _optional_float(min_area_ha)
        max_area = _optional_float(max_area_ha)
    except ValueError:
        return templates.TemplateResponse(
            request=request,
            name="site_auctions.html",
            context=_cabinet_context(
                session,
                account,
                lots=[],
                total=0,
                favorite_ids=set(),
                page=page,
                page_size=page_size,
                has_next_page=False,
                has_previous_page=page > 1,
                filters=filter_values,
                filter_query=filter_query,
                filter_error="Введите цену и площадь числами. Например: 500000 или 0,25.",
            ),
            status_code=400,
        )
    filters = _filters_from_values(
        region=region,
        district=district,
        locality=locality,
        purpose=purpose,
        min_price_kzt=min_price,
        max_price_kzt=max_price,
        min_area_ha=min_area,
        max_area_ha=max_area,
    )
    lots, total = list_auction_lots(
        session,
        filters,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return templates.TemplateResponse(
        request=request,
        name="site_auctions.html",
        context=_cabinet_context(
            session,
            account,
            lots=lots,
            total=total,
            favorite_ids=_favorite_lot_ids(session, account),
            page=page,
            page_size=page_size,
            has_next_page=page * page_size < total,
            has_previous_page=page > 1,
            filters=filter_values,
            filter_query=filter_query,
        ),
    )


@router.get("/cabinet/auctions-v2", response_class=HTMLResponse)
def web_auctions_v2(
    request: Request,
    q: str = "",
    lot_scope: str = "active",
    sort_by: str = "best",
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    min_price_kzt: str = "",
    max_price_kzt: str = "",
    min_area_ha: str = "",
    max_area_ha: str = "",
    min_score: str = "",
    eqazyna_status: str = "",
    risk_level: str = "",
    confidence_level: str = "",
    recommended_action: str = "",
    stage: str = "",
    deadline_status: str = "",
    geo_status: str = "",
    page: int = 1,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    page = max(page, 1)
    page_size = 30
    filter_values = _auction_v2_filter_values(
        q=q,
        lot_scope=lot_scope,
        sort_by=sort_by,
        region=region,
        district=district,
        locality=locality,
        purpose=purpose,
        min_price_kzt=min_price_kzt,
        max_price_kzt=max_price_kzt,
        min_area_ha=min_area_ha,
        max_area_ha=max_area_ha,
        min_score=min_score,
        eqazyna_status=eqazyna_status,
        risk_level=risk_level,
        confidence_level=confidence_level,
        recommended_action=recommended_action,
        stage=stage,
        deadline_status=deadline_status,
        geo_status=geo_status,
    )
    filter_query = _auction_v2_filter_query(filter_values)
    try:
        filters = _auction_v2_filters_from_values(filter_values)
    except ValueError:
        seed_auction_v2_sources(session)
        return templates.TemplateResponse(
            request=request,
            name="site_auctions_v2.html",
            context=_cabinet_context(
                session,
                account,
                dashboard=auction_v2_dashboard(session),
                lots=[],
                total=0,
                page=page,
                page_size=page_size,
                has_next_page=False,
                has_previous_page=page > 1,
                filters=filter_values,
                filter_query=filter_query,
                risk_labels=RISK_LABELS,
                confidence_labels=CONFIDENCE_LABELS,
                action_labels=ACTION_LABELS,
                deadline_labels=DEADLINE_STATUS_LABELS,
                geo_status_labels=GEO_STATUS_LABELS,
                eqazyna_status_labels=EQAZYNA_STATUS_FILTER_LABELS,
                lot_scope_labels=LOT_SCOPE_LABELS,
                sort_labels=AUCTION_V2_SORT_LABELS,
                stage_options=pipeline_stage_options(),
                refresh_stats={"checked": 0},
                watchlists=[],
                watchlist_notifications=[],
                watchlist_matches=[],
                all_lots_filter_query=_auction_v2_query_with(
                    filter_values,
                    lot_scope="all",
                ),
                active_lots_filter_query=_auction_v2_query_with(
                    filter_values,
                    lot_scope="",
                ),
                archive_filter_query=_auction_v2_query_with(
                    filter_values,
                    lot_scope="archive",
                ),
                clear_search_filter_query=_auction_v2_query_with(
                    filter_values,
                    q="",
                ),
                search_diagnostics=None,
                filter_error="Введите цену, площадь и индекс числами. Индекс преимущества должен быть от 0 до 100.",
            ),
            status_code=400,
        )

    refresh_stats = {"checked": 0}
    ensure_default_auction_v2_watchlist(session, account.id)
    lots, total = list_auction_v2_lots(
        session,
        filters,
        account_id=account.id,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    search_diagnostics = auction_v2_search_diagnostics(
        session,
        filters,
        current_total=total,
    )
    dashboard = auction_v2_dashboard(session)
    watchlists = list_auction_v2_watchlists(session, account.id)
    watchlist_notifications = list_auction_v2_web_notifications(
        session,
        account_id=account.id,
    )
    watchlist_matches = auction_v2_watchlist_matches(session, account_id=account.id)
    session.commit()
    return templates.TemplateResponse(
        request=request,
        name="site_auctions_v2.html",
        context=_cabinet_context(
            session,
            account,
            dashboard=dashboard,
            lots=lots,
            total=total,
            page=page,
            page_size=page_size,
            has_next_page=page * page_size < total,
            has_previous_page=page > 1,
            filters=filter_values,
            filter_query=filter_query,
            risk_labels=RISK_LABELS,
            confidence_labels=CONFIDENCE_LABELS,
            action_labels=ACTION_LABELS,
            deadline_labels=DEADLINE_STATUS_LABELS,
            geo_status_labels=GEO_STATUS_LABELS,
            eqazyna_status_labels=EQAZYNA_STATUS_FILTER_LABELS,
            lot_scope_labels=LOT_SCOPE_LABELS,
            sort_labels=AUCTION_V2_SORT_LABELS,
            stage_options=pipeline_stage_options(),
            refresh_stats=refresh_stats,
            watchlists=watchlists,
            watchlist_notifications=watchlist_notifications,
            watchlist_matches=watchlist_matches,
            all_lots_filter_query=_auction_v2_query_with(
                filter_values,
                lot_scope="all",
            ),
            active_lots_filter_query=_auction_v2_query_with(
                filter_values,
                lot_scope="",
            ),
            archive_filter_query=_auction_v2_query_with(
                filter_values,
                lot_scope="archive",
            ),
            clear_search_filter_query=_auction_v2_query_with(
                filter_values,
                q="",
            ),
            search_diagnostics=search_diagnostics,
            filter_error=None,
        ),
    )


@router.get("/cabinet/auctions-v2/map", response_class=HTMLResponse)
def web_auctions_v2_map(
    request: Request,
    q: str = "",
    lot_scope: str = "active",
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    purpose: str | None = None,
    min_price_kzt: str = "",
    max_price_kzt: str = "",
    min_area_ha: str = "",
    max_area_ha: str = "",
    min_score: str = "",
    eqazyna_status: str = "",
    risk_level: str = "",
    confidence_level: str = "",
    recommended_action: str = "",
    stage: str = "",
    deadline_status: str = "",
    geo_status: str = "",
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    filter_values = _auction_v2_filter_values(
        q=q,
        lot_scope=lot_scope,
        region=region,
        district=district,
        locality=locality,
        purpose=purpose,
        min_price_kzt=min_price_kzt,
        max_price_kzt=max_price_kzt,
        min_area_ha=min_area_ha,
        max_area_ha=max_area_ha,
        min_score=min_score,
        eqazyna_status=eqazyna_status,
        risk_level=risk_level,
        confidence_level=confidence_level,
        recommended_action=recommended_action,
        stage=stage,
        deadline_status=deadline_status,
        geo_status=geo_status,
    )
    filter_query = _auction_v2_filter_query(filter_values)
    try:
        filters = _auction_v2_filters_from_values(filter_values)
    except ValueError:
        seed_auction_v2_sources(session)
        return templates.TemplateResponse(
            request=request,
            name="site_auctions_v2_map.html",
            context=_cabinet_context(
                session,
                account,
                dashboard=auction_v2_dashboard(session),
                map_data={
                    "markers": [],
                    "total": 0,
                    "loaded": 0,
                    "mapped": 0,
                    "without_coordinates": 0,
                    "with_boundaries": 0,
                    "egkn_layers": [],
                    "egkn_layer_counts": {
                        "free_lands": 0,
                        "pdp": 0,
                        "functional_zones": 0,
                        "engineering": 0,
                    },
                    "egkn_layer_total": 0,
                    "limit": settings.auction_v2_map_limit,
                    "risk_counts": {"low": 0, "medium": 0, "high": 0, "unknown": 0},
                    "scope_counts": {"active": 0, "future": 0, "archive": 0},
                },
                filters=filter_values,
                filter_query=filter_query,
                risk_labels=RISK_LABELS,
                confidence_labels=CONFIDENCE_LABELS,
                action_labels=ACTION_LABELS,
                deadline_labels=DEADLINE_STATUS_LABELS,
                geo_status_labels=GEO_STATUS_LABELS,
                eqazyna_status_labels=EQAZYNA_STATUS_FILTER_LABELS,
                lot_scope_labels=LOT_SCOPE_LABELS,
                stage_options=pipeline_stage_options(),
                refresh_stats={"checked": 0},
                filter_error="Введите цену, площадь и индекс числами. Индекс преимущества должен быть от 0 до 100.",
            ),
            status_code=400,
        )

    refresh_stats = {"checked": 0}
    ensure_default_auction_v2_watchlist(session, account.id)
    map_data = list_auction_v2_map_markers(
        session,
        filters,
        account_id=account.id,
        limit=settings.auction_v2_map_limit,
    )
    dashboard = auction_v2_dashboard(session)
    session.commit()
    return templates.TemplateResponse(
        request=request,
        name="site_auctions_v2_map.html",
        context=_cabinet_context(
            session,
            account,
            dashboard=dashboard,
            map_data=map_data,
            filters=filter_values,
            filter_query=filter_query,
            risk_labels=RISK_LABELS,
            confidence_labels=CONFIDENCE_LABELS,
            action_labels=ACTION_LABELS,
            deadline_labels=DEADLINE_STATUS_LABELS,
            geo_status_labels=GEO_STATUS_LABELS,
            eqazyna_status_labels=EQAZYNA_STATUS_FILTER_LABELS,
            lot_scope_labels=LOT_SCOPE_LABELS,
            stage_options=pipeline_stage_options(),
            refresh_stats=refresh_stats,
            filter_error=None,
        ),
    )


@router.get("/cabinet/auctions-v2/analytics", response_class=HTMLResponse)
def web_auctions_v2_analytics(
    request: Request,
    region: str | None = None,
    district: str | None = None,
    locality: str | None = None,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    filter_values = _auction_v2_filter_values(
        region=region,
        district=district,
        locality=locality,
    )
    refresh_stats = {"checked": 0}
    ensure_default_auction_v2_watchlist(session, account.id)
    analytics = auction_v2_analytics_payload(
        session,
        region=filter_values["region"],
        district=filter_values["district"],
        locality=filter_values["locality"],
    )
    dashboard = auction_v2_dashboard(session)
    session.commit()
    return templates.TemplateResponse(
        request=request,
        name="site_auction_v2_analytics.html",
        context=_cabinet_context(
            session,
            account,
            dashboard=dashboard,
            analytics=analytics,
            filters=filter_values,
            filter_query=_auction_v2_filter_query(filter_values),
            refresh_stats=refresh_stats,
        ),
    )


@router.post("/cabinet/auctions-v2/watchlists")
def web_auction_v2_create_watchlist(
    name: str = Form(""),
    lot_scope: str = Form("active"),
    region: str = Form(""),
    district: str = Form(""),
    locality: str = Form(""),
    purpose: str = Form(""),
    min_price_kzt: str = Form(""),
    max_price_kzt: str = Form(""),
    min_area_ha: str = Form(""),
    max_area_ha: str = Form(""),
    min_score: str = Form(""),
    eqazyna_status: str = Form(""),
    risk_level: str = Form(""),
    confidence_level: str = Form(""),
    stage: str = Form(""),
    deadline_status: str = Form(""),
    geo_status: str = Form(""),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    try:
        filters = AuctionV2Filters(
            base=_filters_from_values(
                region=region,
                district=district,
                locality=locality,
                purpose=purpose,
                min_price_kzt=_optional_float(min_price_kzt),
                max_price_kzt=_optional_float(max_price_kzt),
                min_area_ha=_optional_float(min_area_ha),
                max_area_ha=_optional_float(max_area_ha),
            ),
            lot_scope=lot_scope or "active",
            eqazyna_status=eqazyna_status or None,
            min_score=_optional_score(min_score) or 70,
            risk_level=risk_level or None,
            confidence_level=confidence_level or None,
            stage=stage or None,
            deadline_status=deadline_status or None,
            geo_status=geo_status or None,
        )
    except ValueError:
        return _redirect_with_query("/cabinet/auctions-v2", watchlist="invalid")
    create_auction_v2_watchlist(
        session,
        account_id=account.id,
        name=name,
        filters=filters,
    )
    session.commit()
    return _redirect_with_query("/cabinet/auctions-v2", watchlist="saved")


@router.post("/cabinet/auctions-v2/watchlists/{watchlist_id}/active")
def web_auction_v2_set_watchlist_active(
    watchlist_id: int,
    active: bool = Form(False),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    watchlist = set_auction_v2_watchlist_active(
        session,
        account_id=account.id,
        watchlist_id=watchlist_id,
        active=active,
    )
    if watchlist is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    session.commit()
    return _redirect_with_query(
        "/cabinet/auctions-v2",
        watchlist="enabled" if active else "disabled",
    )


@router.post("/cabinet/auctions-v2/sync")
def web_auctions_v2_sync(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    if settings.run_tasks_inline:
        result = sync_auction_v2_full_cycle(session)
        session.commit()
        return _redirect_with_query(
            "/cabinet/auctions-v2",
            sync="done",
            lots_fetched=str(result.lots_fetched),
            lots_created=str(result.lots_created),
            lots_updated=str(result.lots_updated),
            lots_checked=str(result.v2.lots_checked),
            sources_checked=str(result.v2.sources_checked),
            documents_checked=str(result.v2.documents_checked),
            documents_downloaded=str(result.v2.documents_downloaded),
            web_notifications=str(result.v2.web_notifications_created),
            telegram_notifications=str(result.v2.telegram_notifications_sent),
        )
    from app.tasks import sync_auction_v2_full_cycle_task

    sync_auction_v2_full_cycle_task.delay()
    return _redirect_with_query("/cabinet/auctions-v2", sync="queued")


@router.post("/cabinet/auctions-v2/history-backfill")
def web_auctions_v2_history_backfill(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    if settings.run_tasks_inline:
        result = sync_auction_v2_eqazyna_history_backfill(session)
        session.commit()
        return _redirect_with_query(
            "/cabinet/auctions-v2/sources",
            backfill="done",
            lots_fetched=str(result.fetched),
            lots_created=str(result.created),
            lots_updated=str(result.updated),
            url_count=str(result.url_count),
            pages_scanned=str(result.pages_scanned),
        )
    from app.tasks import sync_auction_v2_eqazyna_history_backfill_task

    sync_auction_v2_eqazyna_history_backfill_task.delay()
    return _redirect_with_query("/cabinet/auctions-v2/sources", backfill="queued")


@router.post("/cabinet/auctions-v2/notifications/seen")
def web_auction_v2_notifications_seen(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    count = mark_auction_v2_web_notifications_seen(session, account_id=account.id)
    session.commit()
    return _redirect_with_query(
        "/cabinet/auctions-v2",
        notifications="seen",
        count=str(count),
    )


@router.get("/cabinet/auctions-v2/sources", response_class=HTMLResponse)
def web_auction_v2_sources(
    request: Request,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    payload = auction_v2_source_admin_payload(session)
    return templates.TemplateResponse(
        request=request,
        name="site_auction_v2_sources.html",
        context=_cabinet_context(
            session,
            account,
            payload=payload,
        ),
    )


@router.get("/cabinet/auctions-v2/{lot_id}/dossier.txt")
def web_auction_v2_dossier(
    lot_id: str,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    text = build_auction_v2_dossier_text(
        session,
        lot_id,
        account_id=account.id,
    )
    if text is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    mark_auction_v2_web_notifications_seen(
        session,
        account_id=account.id,
        lot_id=lot_id,
    )
    session.commit()
    filename_id = sub(r"[^A-Za-z0-9_.-]+", "-", lot_id).strip("-") or "lot"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="auction-v2-{filename_id}.txt"'
        },
    )


@router.get("/cabinet/auctions-v2/{lot_id}", response_class=HTMLResponse)
def web_auction_v2_detail(
    request: Request,
    lot_id: str,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    payload = get_auction_v2_payload(session, lot_id, account_id=account.id, force=True)
    if payload is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    lot_context = auction_v2_analytics_payload(
        session,
        region=payload.lot.region,
        district=payload.lot.district,
        locality=payload.lot.locality,
        limit=12,
    )
    history = auction_lot_history(session, lot_id)[:10]
    changes = auction_lot_changes(session, lot_id)[:12]
    market_comparables = list_auction_v2_market_comparables(session, lot_id)
    evidence = session.scalars(
        select(AuctionEvidence)
        .where(AuctionEvidence.lot_id == lot_id)
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        .limit(40)
    ).all()
    mark_auction_v2_web_notifications_seen(
        session,
        account_id=account.id,
        lot_id=lot_id,
    )
    session.commit()
    return templates.TemplateResponse(
        request=request,
        name="site_auction_v2_detail.html",
        context=_cabinet_context(
            session,
            account,
            item=payload,
            lot=payload.lot,
            lot_documents=unique_auction_documents(payload.lot.documents),
            analysis=payload.analysis,
            geo_check=payload.geo_check,
            metrics=payload.metrics,
            lot_context=lot_context,
            history=history,
            changes=changes,
            market_comparables=market_comparables,
            evidence=evidence,
            evidence_type_labels=EVIDENCE_TYPE_LABELS,
            evidence_status_labels=EVIDENCE_STATUS_LABELS,
            stage_options=pipeline_stage_options(),
        ),
    )


@router.post("/cabinet/auctions-v2/{lot_id}/pipeline")
def web_auction_v2_pipeline(
    lot_id: str,
    stage: str = Form("watching"),
    max_bid_kzt: str = Form(""),
    notes: str = Form(""),
    pinned: bool = Form(False),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    lot = get_auction_lot(session, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    try:
        parsed_max_bid = _optional_float(max_bid_kzt)
        update_auction_v2_pipeline(
            session,
            account_id=account.id,
            lot_id=lot.id,
            stage=stage,
            max_bid_kzt=parsed_max_bid,
            notes=notes,
            pinned=pinned,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return _redirect_with_query(f"/cabinet/auctions-v2/{lot.id}", pipeline="saved")


@router.post("/cabinet/auctions-v2/{lot_id}/market-comparables")
def web_auction_v2_create_market_comparable(
    lot_id: str,
    source_name: str = Form(""),
    source_url: str = Form(""),
    title: str = Form(""),
    area_ha: str = Form(""),
    price_kzt: str = Form(""),
    listing_status: str = Form("active"),
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    _require_web_admin_account(account)
    lot = get_auction_lot(session, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    try:
        parsed_area = _optional_float(area_ha)
        parsed_price = _optional_float(price_kzt)
        if parsed_area is None or parsed_price is None:
            raise ValueError("Укажите цену и площадь аналога")
        create_auction_v2_market_comparable(
            session,
            lot_id=lot.id,
            source_name=source_name,
            source_url=source_url,
            title=title,
            area_ha=parsed_area,
            price_kzt=parsed_price,
            listing_status=listing_status,
        )
    except ValueError:
        session.rollback()
        return _redirect_with_query(f"/cabinet/auctions-v2/{lot.id}", market="invalid")
    session.commit()
    return _redirect_with_query(f"/cabinet/auctions-v2/{lot.id}", market="saved")


@router.post("/cabinet/auctions/{lot_id}/favorite")
def web_toggle_auction_favorite(
    request: Request,
    lot_id: str,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    referer = request.headers.get("referer") or "/cabinet/auctions"
    if account_access_kind(session, account) not in {"paid", "trial"}:
        return _redirect_with_query(referer, locked="auction_favorite")
    lot = session.get(AuctionLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    favorite = session.scalar(
        select(AuctionFavorite).where(
            AuctionFavorite.lot_id == lot_id,
            or_(
                AuctionFavorite.account_id == account.id,
                AuctionFavorite.telegram_user_id.in_(_auction_user_keys(account)),
            ),
        )
    )
    if favorite:
        session.delete(favorite)
        favorite_status = "removed"
    else:
        session.add(
            AuctionFavorite(
                account_id=account.id,
                telegram_user_id=_auction_user_key(account),
                lot_id=lot_id,
            )
        )
        favorite_status = "added"
    session.commit()
    return _redirect_with_query(referer, favorite=favorite_status)


@router.get("/cabinet/auctions/{lot_id}", response_class=HTMLResponse)
def web_auction_detail(
    request: Request,
    lot_id: str,
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    lot = get_auction_lot(session, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="Лот не найден")
    metrics = auction_lot_metrics(session, lot)
    history = auction_lot_history(session, lot.id)[:10]
    changes = auction_lot_changes(session, lot.id)[:12]
    return templates.TemplateResponse(
        request=request,
        name="site_auction_detail.html",
        context=_cabinet_context(
            session,
            account,
            lot=lot,
            metrics=metrics,
            history=history,
            changes=changes,
            is_favorite=lot.id in _favorite_lot_ids(session, account),
        ),
    )


@router.post("/cabinet/telegram/link")
def start_telegram_link(
    account: Account = Depends(require_web_account),
    session: Session = Depends(get_db),
):
    raw_token = secrets.token_urlsafe(24)
    session.add(
        TelegramLinkToken(
            account_id=account.id,
            token_hash=_hash(raw_token),
            expires_at=_now() + timedelta(minutes=30),
        )
    )
    session.commit()
    return RedirectResponse(f"/cabinet?telegram_link_token={raw_token}", status_code=303)
