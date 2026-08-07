from datetime import UTC, datetime, timedelta

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_ready
from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.access import access_expiry_is_active
from app.apipay import ApiPayError, get_invoice
from app.auction_service import sync_current_auctions
from app.auction_v2 import (
    prepare_auction_v2_worklist,
    sync_auction_v2_eqazyna_history_backfill,
    sync_auction_v2_full_cycle,
    sync_auction_v2_sources,
)
from app.config import settings
from app.db import SessionLocal, init_db
from app.models import (
    AccountPayment,
    AuctionAccess,
    FreePreviewStatus,
    PaymentStatus,
    SearchRequest,
    SearchStatus,
)
from app.providers.egkn import EgknProviderError
from app.services import (
    apply_apipay_webhook,
    approve_free_preview,
    deliver_request,
    elapsed_seconds,
    ensure_ready_delivery,
    notify_apipay_payment_retry,
    notify_terminal_search_failure,
    process_search,
)

logger = get_task_logger(__name__)

beat_schedule = {
    "recover-stale-searches": {
        "task": "land_scout.recover_stale_searches",
        "schedule": 300,
    }
}
if settings.auctions_enabled:
    beat_schedule["sync-eqazyna-auctions"] = {
        "task": "land_scout.sync_auctions",
        "schedule": min(
            settings.eqazyna_sync_interval_minutes,
            settings.auction_v2_full_cycle_interval_minutes,
        )
        * 60,
    }

celery_app = Celery("land_scout", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_queue="critical",
    task_routes={
        "land_scout.process_search": {"queue": "critical"},
        "land_scout.recover_stale_searches": {"queue": "critical"},
        "land_scout.reconcile_apipay_invoice": {"queue": "critical"},
        "land_scout.reconcile_auction_apipay_invoice": {"queue": "critical"},
        "land_scout.reconcile_account_apipay_invoice": {"queue": "critical"},
        "land_scout.sync_auctions": {"queue": "auctions"},
        "land_scout.sync_auction_v2_eqazyna_history_backfill": {"queue": "auctions"},
        "land_scout.sync_auction_v2_full_cycle": {"queue": "auctions"},
        "land_scout.sync_auction_v2_sources": {"queue": "auctions"},
    },
    broker_transport_options={"visibility_timeout": 3600},
    beat_schedule=beat_schedule,
)


def _recover_stale_searches() -> tuple[list[str], list[str], list[str]]:
    cutoff = datetime.now(UTC) - timedelta(minutes=15)
    delivery_cutoff = datetime.now(UTC) - timedelta(hours=6)
    with SessionLocal() as session:
        stale_ids = session.scalars(
            select(SearchRequest.id).where(
                SearchRequest.status == SearchStatus.processing.value,
                SearchRequest.updated_at < cutoff,
            )
        ).all()
        for request_id in stale_ids:
            request = session.get(SearchRequest, request_id)
            if request is not None:
                request.status = SearchStatus.queued.value
                request.progress = 10
                request.error_message = "Заявка восстановлена после прерывания worker"
        session.commit()
        pending_free_ids = session.scalars(
            select(SearchRequest.id).where(
                SearchRequest.free_preview_status == FreePreviewStatus.pending.value,
                SearchRequest.telegram_chat_id.is_not(None),
                SearchRequest.free_preview_delivered_at.is_(None),
                SearchRequest.search_completed_notified_at.is_(None),
                SearchRequest.updated_at >= delivery_cutoff,
            )
        ).all()
        ready_delivery_ids = session.scalars(
            select(SearchRequest.id).where(
                SearchRequest.status == SearchStatus.ready.value,
                SearchRequest.telegram_chat_id.is_not(None),
                SearchRequest.updated_at >= delivery_cutoff,
                SearchRequest.free_preview_status != FreePreviewStatus.pending.value,
                (
                    (SearchRequest.search_completed_notified_at.is_(None))
                    | (
                        (SearchRequest.free_preview_status == FreePreviewStatus.delivered.value)
                        & (SearchRequest.updated_at >= delivery_cutoff)
                    )
                ),
            )
        ).all()
    return list(stale_ids), list(pending_free_ids), list(ready_delivery_ids)


def _dispatch_recovered_searches(
    stale_ids: list[str],
    pending_free_ids: list[str],
    ready_delivery_ids: list[str],
) -> None:
    for request_id in stale_ids:
        logger.warning("Recovering stale search request %s", request_id)
        process_search_task.delay(request_id)
    for request_id in pending_free_ids:
        try:
            with SessionLocal() as session:
                approve_free_preview(session, request_id, approved_by="automatic-recovery")
        except Exception:
            logger.exception("Failed to recover pending free preview %s", request_id)
    for request_id in ready_delivery_ids:
        try:
            with SessionLocal() as session:
                if ensure_ready_delivery(session, request_id):
                    logger.warning("Recovered ready delivery for search request %s", request_id)
        except Exception:
            logger.exception("Failed to recover ready delivery %s", request_id)


@worker_ready.connect
def recover_stale_searches(**_: object) -> None:
    init_db()
    _dispatch_recovered_searches(*_recover_stale_searches())


@celery_app.task(
    bind=True,
    name="land_scout.process_search",
    max_retries=3,
    soft_time_limit=240,
    time_limit=270,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_search_task(self, request_id: str) -> str:
    init_db()
    try:
        with SessionLocal() as session:
            process_search(session, request_id)
        return request_id
    except Exception as exc:
        retry_limit = self.max_retries
        if self.request.retries < retry_limit:
            with SessionLocal() as session:
                request = session.get(SearchRequest, request_id)
                if request is not None:
                    request.status = SearchStatus.queued.value
                    request.progress = 10
                    request.error_message = (
                        f"Повторная попытка {self.request.retries + 2} из "
                        f"{self.max_retries + 1}: {exc}"
                    )
                    session.commit()
            raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1)) from exc
        with SessionLocal() as session:
            request = session.get(SearchRequest, request_id)
            if request is not None:
                request.status = SearchStatus.failed.value
                request.progress = 100
                request.error_message = str(exc)
                request.search_finished_at = datetime.now(UTC)
                request.search_outcome = (
                    "egkn_unavailable"
                    if isinstance(exc, EgknProviderError)
                    else "timeout"
                    if isinstance(exc, SoftTimeLimitExceeded)
                    else "technical_error"
                )
                session.commit()
                from app.analytics import track_funnel_event

                track_funnel_event(
                    session,
                    "search_failed",
                    telegram_user_id=request.telegram_user_id,
                    telegram_chat_id=request.telegram_chat_id,
                    request_id=request.id,
                    funnel_session_id=request.funnel_session_id,
                    language=request.language,
                    metadata={
                        "outcome": request.search_outcome,
                        "duration_seconds": elapsed_seconds(
                            request.search_started_at, request.search_finished_at
                        ),
                    },
                )
                notify_terminal_search_failure(request)
        raise


@celery_app.task(name="land_scout.recover_stale_searches")
def recover_stale_searches_task() -> dict[str, int]:
    init_db()
    stale_ids, pending_free_ids, ready_delivery_ids = _recover_stale_searches()
    _dispatch_recovered_searches(stale_ids, pending_free_ids, ready_delivery_ids)
    return {
        "stale_searches": len(stale_ids),
        "pending_free_previews": len(pending_free_ids),
        "ready_deliveries": len(ready_delivery_ids),
    }


@celery_app.task(
    bind=True,
    name="land_scout.reconcile_apipay_invoice",
    max_retries=30,
)
def reconcile_apipay_invoice_task(self, request_id: str) -> str:
    init_db()
    try:
        with SessionLocal() as session:
            request = session.get(SearchRequest, request_id)
            if request is None:
                return "request_not_found"
            if (
                request.payment_status == PaymentStatus.paid.value
                and request.status != SearchStatus.delivered.value
            ):
                deliver_request(session, request_id)
                return "paid"
            if request.payment_status != PaymentStatus.awaiting_transfer.value:
                return request.payment_status
            if (
                request.payment_provider != "apipay"
                or not request.payment_provider_invoice_id
            ):
                return "provider_invoice_missing"

            invoice = get_invoice(request.payment_provider_invoice_id)
            invoice["external_order_id"] = request.id
            result = apply_apipay_webhook(
                session,
                {
                    "event": "invoice.status_changed",
                    "invoice": invoice,
                    "source": "polling",
                },
            )
            if result.deliver_report:
                deliver_request(session, request_id)
            if result.notify_payment_retry:
                notify_apipay_payment_retry(request_id)
            if result.status in {"paid", "cancelled", "expired", "error"}:
                return result.status or "terminal"
    except ApiPayError as exc:
        logger.warning("ApiPay reconciliation failed for %s: %s", request_id, exc)

    raise self.retry(
        countdown=settings.apipay_poll_interval_seconds,
        max_retries=settings.apipay_poll_attempts,
    )


@celery_app.task(
    bind=True,
    name="land_scout.reconcile_auction_apipay_invoice",
    max_retries=30,
)
def reconcile_auction_apipay_invoice_task(self, access_id: str) -> str:
    from app.auction_access import (
        AUCTION_ORDER_PREFIX,
        notify_auction_payment_confirmed,
        notify_auction_payment_retry,
    )

    init_db()
    try:
        with SessionLocal() as session:
            access = session.get(AuctionAccess, access_id)
            if access is None:
                return "access_not_found"
            if access.paid_access and access_expiry_is_active(access.access_expires_at):
                notify_auction_payment_confirmed(access_id)
                return "paid"
            if access.payment_status != PaymentStatus.awaiting_transfer.value:
                return access.payment_status
            if (
                access.payment_provider != "apipay"
                or not access.payment_provider_invoice_id
            ):
                return "provider_invoice_missing"

            invoice = get_invoice(access.payment_provider_invoice_id)
            invoice["external_order_id"] = f"{AUCTION_ORDER_PREFIX}{access.id}"
            result = apply_apipay_webhook(
                session,
                {
                    "event": "invoice.status_changed",
                    "invoice": invoice,
                    "source": "polling",
                },
            )
            if result.activate_auction_access:
                notify_auction_payment_confirmed(access_id)
            if result.notify_auction_payment_retry:
                notify_auction_payment_retry(access_id)
            if result.status in {"paid", "cancelled", "expired", "error"}:
                return result.status or "terminal"
    except ApiPayError as exc:
        logger.warning(
            "ApiPay auction reconciliation failed for %s: %s",
            access_id,
            exc,
        )

    raise self.retry(
        countdown=settings.apipay_poll_interval_seconds,
        max_retries=settings.apipay_poll_attempts,
    )


@celery_app.task(
    bind=True,
    name="land_scout.reconcile_account_apipay_invoice",
    max_retries=30,
)
def reconcile_account_apipay_invoice_task(self, payment_id: str) -> str:
    from app.account_payments import ACCOUNT_ORDER_PREFIX

    init_db()
    try:
        with SessionLocal() as session:
            payment = session.get(AccountPayment, payment_id)
            if payment is None:
                return "payment_not_found"
            if payment.payment_status == PaymentStatus.paid.value:
                return "paid"
            if payment.payment_status != PaymentStatus.awaiting_transfer.value:
                return payment.payment_status
            if (
                payment.payment_provider != "apipay"
                or not payment.payment_provider_invoice_id
            ):
                return "provider_invoice_missing"

            invoice = get_invoice(payment.payment_provider_invoice_id)
            invoice["external_order_id"] = f"{ACCOUNT_ORDER_PREFIX}{payment.id}"
            result = apply_apipay_webhook(
                session,
                {
                    "event": "invoice.status_changed",
                    "invoice": invoice,
                    "source": "polling",
                },
            )
            if result.status in {"paid", "cancelled", "expired", "error"}:
                return result.status or "terminal"
    except ApiPayError as exc:
        logger.warning(
            "ApiPay account reconciliation failed for %s: %s",
            payment_id,
            exc,
        )

    raise self.retry(
        countdown=settings.apipay_poll_interval_seconds,
        max_retries=settings.apipay_poll_attempts,
    )


@celery_app.task(
    bind=True,
    name="land_scout.sync_auctions",
    max_retries=3,
    soft_time_limit=1200,
    time_limit=1320,
)
def sync_current_auctions_task(self) -> dict[str, int]:
    init_db()
    if not settings.auctions_enabled:
        return {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "notifications_sent": 0,
            "errors": 0,
            "detail_errors": 0,
            "deactivated": 0,
            "crawl_complete": 0,
            "url_count": 0,
            "pages_scanned": 0,
            "lots_checked": 0,
            "analyses_updated": 0,
            "sources_checked": 0,
            "documents_checked": 0,
            "documents_downloaded": 0,
            "document_errors": 0,
            "evidence_created": 0,
            "crawl_runs_created": 0,
            "watchlists_checked": 0,
            "watchlist_matches_seen": 0,
            "web_notifications_created": 0,
            "telegram_notifications_sent": 0,
            "notification_errors": 0,
        }
    try:
        with SessionLocal() as session:
            result = sync_current_auctions(session)
            prepare_auction_v2_worklist(session, send_notifications=False)
            session.commit()
            v2_result = sync_auction_v2_sources(session)
            session.commit()
        return {
            "fetched": result.fetched,
            "created": result.created,
            "updated": result.updated,
            "notifications_sent": result.notifications_sent,
            "errors": result.errors,
            "detail_errors": result.detail_errors,
            "deactivated": result.deactivated,
            "crawl_complete": int(result.crawl_complete),
            "url_count": result.url_count,
            "pages_scanned": result.pages_scanned,
            **v2_result.as_dict(),
        }
    except Exception as exc:
        logger.warning("E-Qazyna auction sync failed: %s", exc)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1)) from exc


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_v2_full_cycle",
    max_retries=3,
    soft_time_limit=1200,
    time_limit=1320,
)
def sync_auction_v2_full_cycle_task(self) -> dict[str, int]:
    init_db()
    if not settings.auctions_enabled:
        return {
            "lots_fetched": 0,
            "lots_created": 0,
            "lots_updated": 0,
            "lots_deactivated": 0,
            "crawl_errors": 0,
            "lots_checked": 0,
            "analyses_updated": 0,
            "sources_checked": 0,
            "documents_checked": 0,
            "documents_downloaded": 0,
            "document_errors": 0,
            "evidence_created": 0,
            "crawl_runs_created": 0,
            "watchlists_checked": 0,
            "watchlist_matches_seen": 0,
            "web_notifications_created": 0,
            "telegram_notifications_sent": 0,
            "notification_errors": 0,
        }
    try:
        with SessionLocal() as session:
            result = sync_auction_v2_full_cycle(session)
            session.commit()
        return result.as_dict()
    except Exception as exc:
        logger.warning("Auction v2 full-cycle sync failed: %s", exc)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1)) from exc


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_v2_eqazyna_history_backfill",
    max_retries=2,
    soft_time_limit=3600,
    time_limit=3900,
)
def sync_auction_v2_eqazyna_history_backfill_task(self) -> dict[str, int]:
    init_db()
    if not settings.auctions_enabled:
        return {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "notifications_sent": 0,
            "errors": 0,
            "detail_errors": 0,
            "deactivated": 0,
            "crawl_complete": 0,
            "url_count": 0,
            "pages_scanned": 0,
        }
    try:
        with SessionLocal() as session:
            result = sync_auction_v2_eqazyna_history_backfill(session)
            session.commit()
        return {
            "fetched": result.fetched,
            "created": result.created,
            "updated": result.updated,
            "notifications_sent": result.notifications_sent,
            "errors": result.errors,
            "detail_errors": result.detail_errors,
            "deactivated": result.deactivated,
            "crawl_complete": int(result.crawl_complete),
            "url_count": result.url_count,
            "pages_scanned": result.pages_scanned,
        }
    except Exception as exc:
        logger.warning("Auction v2 E-Qazyna history backfill failed: %s", exc)
        raise self.retry(exc=exc, countdown=120 * (self.request.retries + 1)) from exc


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_v2_sources",
    max_retries=3,
    soft_time_limit=900,
    time_limit=990,
)
def sync_auction_v2_sources_task(self) -> dict[str, int]:
    init_db()
    if not settings.auctions_enabled:
        return {
            "lots_checked": 0,
            "analyses_updated": 0,
            "sources_checked": 0,
            "documents_checked": 0,
            "documents_downloaded": 0,
            "document_errors": 0,
            "evidence_created": 0,
            "crawl_runs_created": 0,
            "watchlists_checked": 0,
            "watchlist_matches_seen": 0,
            "web_notifications_created": 0,
            "telegram_notifications_sent": 0,
            "notification_errors": 0,
        }
    try:
        with SessionLocal() as session:
            result = sync_auction_v2_sources(session)
            session.commit()
        return result.as_dict()
    except Exception as exc:
        logger.warning("Auction v2 source sync failed: %s", exc)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1)) from exc
