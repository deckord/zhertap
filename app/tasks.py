import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from math import ceil
from pathlib import Path
from random import randint
from typing import Literal, TypeVar
from uuid import uuid4

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_process_init, worker_ready
from celery.utils.log import get_task_logger
from redis import Redis
from redis.exceptions import LockError, RedisError
from sqlalchemy import and_, func, or_, select

from app.access import access_expiry_is_active
from app.apipay import ApiPayError, get_invoice
from app.auction_decision_input_store import (
    recompute_decision_input_batch,
    recompute_decision_inputs,
)
from app.auction_decision_snapshot import (
    DECISION_ENGINE_VERSION,
    VERDICT_RULES_VERSION,
    recompute_decision_snapshot,
)
from app.auction_document_extraction_writer import (
    MAX_BATCH as DOCUMENT_EXTRACTION_MAX_BATCH,
)
from app.auction_document_extraction_writer import extract_downloaded_auction_documents
from app.auction_due_diligence_analysis import analyze_due_diligence_response
from app.auction_eqazyna_verified_sales import ingest_eqazyna_verified_sales_batch
from app.auction_history_worker import normalize_auction_history_step
from app.auction_land_identity import (
    backfill_canonical_land_objects_page,
    canonical_land_backfill_high_water,
)
from app.auction_llm import AuctionLlmClient, extract_auction_document_with_llm
from app.auction_market_dirty_worker import recompute_market_dirty_page
from app.auction_nsdi_worker import check_nsdi_water_batch
from app.auction_object_enrichment import (
    DEFAULT_ERROR_RETRY_MINUTES,
    JerlerEnrichmentDeferred,
    sync_auction_source_objects_detached,
)
from app.auction_service import (
    deactivate_missing_current_auction_lots,
    refresh_due_eqazyna_lot_statuses,
)
from app.auction_spatial_evidence_store import SqlAlchemySpatialEvidenceStore
from app.auction_spatial_evidence_writer import MAX_BATCH as SPATIAL_MAX_BATCH
from app.auction_spatial_fetch import SpatialFetchTerminal, parse_spatial_fetch_runtime
from app.auction_spatial_outbox import dispatch_spatial_decision_outbox
from app.auction_spatial_worker import process_spatial_claim, seed_spatial_feed_states
from app.auction_taxonomy import UNCLASSIFIED_SCENARIO, select_decision_scenario
from app.auction_territory_worker import link_territory_observation_batch
from app.auction_v2 import (
    AuctionV2DocumentSyncResult,
    _refresh_auction_v2_infrastructure_batch,
    eqazyna_history_publish_date_windows,
    prepare_auction_v2_worklist,
    seed_auction_v2_sources,
    sync_auction_v2_documents,
)
from app.config import settings
from app.db import SessionLocal, engine, init_db
from app.models import (
    AccountPayment,
    AuctionAccess,
    AuctionDecisionSnapshot,
    AuctionDocumentExtractionCursor,
    AuctionDocumentExtractionState,
    AuctionDueDiligenceAttachment,
    AuctionEvidence,
    AuctionLandIdentityBackfillCursor,
    AuctionLot,
    AuctionLotGeoCheck,
    AuctionSpatialDecisionSignal,
    AuctionSpatialFeedState,
    FreePreviewStatus,
    PaymentStatus,
    SearchRequest,
    SearchStatus,
)
from app.provider_backpressure import create_redis_provider_backpressure
from app.provider_guard import ProviderCallDeferred
from app.provider_workflow_store import (
    attach_provider_run_parent,
    claim_provider_run_dispatch,
    claim_ready_provider_run,
    complete_provider_run_dispatch,
    due_provider_workflow_keys,
    ensure_provider_crawl_run,
    ensure_provider_sync_run,
    eqazyna_history_checkpoint_key,
    eqazyna_history_resume_checkpoint,
    expire_stale_provider_parents,
    fail_provider_run_dispatch,
    finalizable_provider_runs,
    finish_provider_run,
    finish_source_run_and_parents,
    provider_run_crawl_completion,
    provider_run_dispatches_complete,
    provider_run_key_for_workflow,
)
from app.provider_workflow_worker import (
    process_provider_workflow_step,
    seed_eqazyna_page_workflow,
    seed_gov_kz_workflow,
    seed_jerler_provider_workflow,
    seed_provider_barrier_noop,
    seed_spatial_provider_workflows,
)
from app.providers.egkn import EgknProviderError
from app.providers.eqazyna import configured_search_statuses
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
DEADLOCK_SQLSTATE = "40P01"
MAX_JERLER_DEFERRED_CONTINUATIONS = 3
_TaskResult = TypeVar("_TaskResult")


def _ensure_task_database_ready() -> None:
    """Keep compatibility DDL out of the production task hot path."""
    if settings.app_env.strip().lower() in {"production", "prod"}:
        return
    return init_db()


@worker_process_init.connect
def _dispose_inherited_database_pool(**_: object) -> None:
    """Celery children must not reuse connections opened before fork."""
    engine.dispose(close=False)


def _auction_pipeline_singleton(
    *,
    timeout_seconds: int = 4200,
) -> Callable[[Callable[..., _TaskResult]], Callable[..., _TaskResult | dict[str, int]]]:
    """Serialize auction ingestion stages even when the queue gains consumers."""

    def decorate(
        function: Callable[..., _TaskResult],
    ) -> Callable[..., _TaskResult | dict[str, int]]:
        @wraps(function)
        def wrapped(*args: object, **kwargs: object) -> _TaskResult | dict[str, int]:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-pipeline",
                timeout=timeout_seconds,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
            if not acquired:
                return {"skipped_locked": 1}
            try:
                return function(*args, **kwargs)
            finally:
                try:
                    lock.release()
                except (LockError, RedisError, OSError):
                    logger.warning("Auction pipeline singleton lock expired before release")

        return wrapped

    return decorate


def _auction_history_singleton(
    function: Callable[..., object],
) -> Callable[..., object]:
    """Prevent duplicate checkpoint workers; DB constraints remain authoritative."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        lock = None
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-history-normalization",
                timeout=300,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
        except (RedisError, OSError):
            # The partial unique building index and optimistic checkpoint are the
            # correctness boundary; Redis only reduces duplicate work.
            logger.warning("Auction history Redis lock unavailable; using DB guard")
            return function(*args, **kwargs)
        if not acquired:
            task = args[0] if args else None
            apply_async = getattr(task, "apply_async", None)
            if callable(apply_async):
                apply_async(kwargs=kwargs, countdown=randint(5, 15))
            return {
                "status": "skipped_locked",
                "has_more": False,
                "continuation_scheduled": callable(apply_async),
            }
        try:
            return function(*args, **kwargs)
        finally:
            try:
                if lock is not None:
                    lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Auction history lock expired before release")

    return wrapped


def _provider_sources_singleton(
    function: Callable[..., object],
) -> Callable[..., object]:
    """Serialize source seeding; a busy lock must preserve parent-barrier delivery."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        lock = None
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-provider-sources",
                timeout=1080,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
        except (RedisError, OSError):
            logger.warning("Provider sources Redis lock unavailable; using DB guards")
            return function(*args, **kwargs)
        if not acquired:
            task = args[0] if args else None
            apply_async = getattr(task, "apply_async", None)
            if callable(apply_async):
                apply_async(kwargs=kwargs, countdown=randint(5, 15))
            return {
                "status": "skipped_locked",
                "continuation_scheduled": callable(apply_async),
            }
        try:
            return function(*args, **kwargs)
        finally:
            try:
                if lock is not None:
                    lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Provider sources singleton lock expired before release")

    return wrapped


def _auction_decision_singleton(function: Callable[..., object]) -> Callable[..., object]:
    """Serialize bounded decision batches; DB fingerprints remain authoritative."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        lock = None
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-decision-snapshots",
                timeout=300,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
        except (RedisError, OSError):
            logger.warning("Auction decision Redis lock unavailable; using DB guards")
            return function(*args, **kwargs)
        if not acquired:
            task = args[0] if args else None
            apply_async = getattr(task, "apply_async", None)
            if callable(apply_async):
                apply_async(kwargs=kwargs, countdown=randint(5, 15))
            return {
                "status": "skipped_locked",
                "processed": 0,
                "has_more": False,
                "continuation_scheduled": callable(apply_async),
            }
        try:
            return function(*args, **kwargs)
        finally:
            try:
                if lock is not None:
                    lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Auction decision lock expired before release")

    return wrapped


def _auction_decision_input_singleton(
    function: Callable[..., object],
) -> Callable[..., object]:
    """Nonblocking singleton for bounded input assembly continuations."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        lock = None
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-decision-inputs",
                timeout=300,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
        except (RedisError, OSError):
            logger.warning("Auction decision-input Redis lock unavailable; using DB claims")
            return function(*args, **kwargs)
        if not acquired:
            task = args[0] if args else None
            apply_async = getattr(task, "apply_async", None)
            if callable(apply_async):
                apply_async(kwargs=kwargs, countdown=randint(5, 15))
            return {
                "status": "skipped_locked",
                "processed": 0,
                "has_more": False,
                "continuation_scheduled": callable(apply_async),
            }
        try:
            return function(*args, **kwargs)
        finally:
            try:
                if lock is not None:
                    lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Auction decision-input lock expired before release")

    return wrapped


DOCUMENT_EXTRACTION_CONTINUATION_TTL_SECONDS = 900
DOCUMENT_EXTRACTION_CONTINUATION_KEY = "land-scout:document-extraction-continuation"


def _consume_document_extraction_continuation(
    redis_client: Redis,
    *,
    task_id: str | None,
) -> None:
    """Release the gate only when this worker consumed the reserved message."""
    if not task_id:
        return
    current = redis_client.get(DOCUMENT_EXTRACTION_CONTINUATION_KEY)
    if isinstance(current, bytes):
        current = current.decode("utf-8", errors="replace")
    if current == task_id:
        redis_client.delete(DOCUMENT_EXTRACTION_CONTINUATION_KEY)


def _schedule_document_extraction_continuation(
    kwargs: dict[str, object],
    *,
    countdown: int,
) -> bool:
    bounded_countdown = max(1, min(int(countdown), 86_400))
    redis_client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    key = DOCUMENT_EXTRACTION_CONTINUATION_KEY
    reservation = uuid4().hex
    ttl = max(DOCUMENT_EXTRACTION_CONTINUATION_TTL_SECONDS, bounded_countdown + 300)
    if not redis_client.set(key, reservation, nx=True, ex=ttl):
        return False
    try:
        result = extract_auction_documents_task.apply_async(
            kwargs=kwargs,
            countdown=bounded_countdown,
        )
        redis_client.set(key, str(getattr(result, "id", reservation)), xx=True, ex=ttl)
        return True
    except Exception:
        redis_client.delete(key)
        raise


def _auction_document_extraction_singleton(
    function: Callable[..., object],
) -> Callable[..., object]:
    """Reduce duplicate document CPU; durable claims remain authoritative."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        lock = None
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            # A reserved continuation can arrive while the previous extraction
            # still owns the singleton. Consume its gate before attempting the
            # lock; otherwise the busy path cannot reserve a replacement until
            # the 15-minute TTL expires and a large document backlog advances
            # only on the periodic beat.
            task = args[0] if args else None
            request = getattr(task, "request", None)
            _consume_document_extraction_continuation(
                redis_client,
                task_id=str(request.id) if request is not None and request.id else None,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-document-extraction",
                # Outlive the task's 300-second hard limit so another worker
                # cannot enter while Celery is still terminating this one.
                timeout=360,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
        except (RedisError, OSError):
            logger.warning("Document extraction Redis lock unavailable; using DB claims")
            return function(*args, **kwargs)
        if not acquired:
            scheduled = _schedule_document_extraction_continuation(
                dict(kwargs),
                countdown=randint(5, 15),
            )
            return {
                "status": "skipped_locked",
                "selected": 0,
                "has_more": False,
                "continuation_scheduled": scheduled,
            }
        try:
            return function(*args, **kwargs)
        finally:
            try:
                if lock is not None:
                    lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Document extraction lock expired before release")

    return wrapped


def _auction_verified_market_singleton(
    function: Callable[..., object],
) -> Callable[..., object]:
    """Serialize bounded global sale ingest/W9 sweeps across auction workers."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        lock = None
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-verified-market",
                # Must outlive the Celery hard limit so a terminating worker cannot
                # overlap its replacement before database identity locks take over.
                timeout=360,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
        except (RedisError, OSError):
            logger.warning("Verified market Redis lock unavailable; using DB identity locks")
            return function(*args, **kwargs)
        if not acquired:
            # The lock owner is the authoritative ingest/market chain and schedules
            # its own continuation whenever more work remains. Re-enqueuing every
            # overlapping beat/downstream trigger here preserves all duplicates and
            # can amplify one scan into several full-corpus scans.
            return {
                "status": "skipped_locked",
                "processed": 0,
                "has_more": False,
                "continuation_scheduled": False,
            }
        try:
            return function(*args, **kwargs)
        finally:
            try:
                if lock is not None:
                    lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Verified market singleton lock expired before release")

    return wrapped


def _auction_spatial_singleton(function: Callable[..., object]) -> Callable[..., object]:
    """Avoid duplicate sweeps while durable feed claims remain authoritative."""

    @wraps(function)
    def wrapped(*args: object, **kwargs: object) -> object:
        lock = None
        try:
            redis_client = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                health_check_interval=30,
            )
            lock = redis_client.lock(
                "land-scout:lock:auction-spatial-feeds",
                timeout=360,
                blocking_timeout=1,
            )
            acquired = bool(lock.acquire(blocking=False))
        except (RedisError, OSError):
            logger.warning("Spatial singleton Redis unavailable; using durable DB claims")
            return function(*args, **kwargs)
        if not acquired:
            task = args[0] if args else None
            apply_async = getattr(task, "apply_async", None)
            if callable(apply_async):
                apply_async(kwargs=kwargs, countdown=randint(5, 15))
            return {
                "status": "skipped_locked",
                "selected": 0,
                "has_more": False,
                "continuation_scheduled": callable(apply_async),
            }
        try:
            return function(*args, **kwargs)
        finally:
            try:
                if lock is not None:
                    lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Spatial singleton lock expired before release")

    return wrapped


def _schedule_decision_snapshot_recompute(*, force: bool = False, countdown: int = 5) -> None:
    recompute_auction_decision_snapshots_task.apply_async(
        kwargs={"force": force},
        countdown=max(0, min(int(countdown), 300)),
    )


def _schedule_decision_input_recompute(*, countdown: int = 5) -> None:
    recompute_auction_decision_inputs_task.apply_async(
        kwargs={"batch_size": 25},
        countdown=max(0, min(int(countdown), 300)),
    )


def _schedule_auction_v2_sources_refresh(*, countdown: int = 5) -> None:
    refresh_auction_v2_infrastructure_task.apply_async(
        kwargs={"limit": 25},
        countdown=max(0, min(int(countdown), 300)),
    )


def _schedule_verified_market_sync(*, countdown: int = 5) -> None:
    sync_auction_verified_market_task.apply_async(
        kwargs={"phase": "ingest", "batch_size": 100},
        countdown=max(0, min(int(countdown), 300)),
    )


PROVIDER_WORKFLOW_CONTINUATION_TTL_SECONDS = 900
PROVIDER_WORKFLOW_PAUSE_TTL_SECONDS = 30 * 24 * 60 * 60


def _provider_workflow_continuation_key(workflow_key: str) -> str:
    return f"land-scout:provider-workflow-continuation:{workflow_key}"


def _provider_workflow_pause_key(run_key: str) -> str:
    return f"land-scout:provider-workflow-paused:{run_key}"


def _schedule_provider_workflow_continuation(
    workflow_key: str,
    *,
    countdown: int,
) -> bool:
    """Enqueue at most one broker continuation for a workflow."""
    bounded_countdown = max(1, min(int(countdown), 86_400))
    redis_client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    key = _provider_workflow_continuation_key(workflow_key)
    reservation = uuid4().hex
    ttl = max(PROVIDER_WORKFLOW_CONTINUATION_TTL_SECONDS, bounded_countdown + 300)
    if not redis_client.set(key, reservation, nx=True, ex=ttl):
        return False
    try:
        result = sync_provider_workflow_task.apply_async(
            kwargs={"workflow_key": workflow_key},
            countdown=bounded_countdown,
        )
        redis_client.set(key, str(getattr(result, "id", reservation)), xx=True, ex=ttl)
        return True
    except Exception:
        redis_client.delete(key)
        raise


def _schedule_workflows(workflow_keys: list[str], *, countdown: int = 1) -> None:
    for offset, workflow_key in enumerate(workflow_keys):
        _schedule_provider_workflow_continuation(
            workflow_key,
            countdown=countdown + offset,
        )


def _seed_current_eqazyna_workflows(run_key: str) -> list[str]:
    keys: list[str] = []
    for index, status in enumerate(configured_search_statuses()):
        key = f"{run_key}:eq:{index}:{status}"[:128]
        try:
            seed_eqazyna_page_workflow(
                SessionLocal,
                workflow_key=key,
                search_status=status,
                max_pages=settings.eqazyna_sync_max_pages,
                run_key=run_key,
            )
        except ValueError as exc:
            if str(exc) != "provider sync run is not active":
                raise
            logger.info(
                "Current E-Qazyna run %s entered finalizing while workflows were seeded; "
                "stopping without attaching late children",
                run_key,
            )
            break
        keys.append(key)
    return keys


def _csv_task_setting(value: str | None) -> list[str]:
    return [
        item.strip()
        for item in str(value or "").replace(";", ",").replace("\n", ",").split(",")
        if item.strip()
    ]


def _finalize_provider_barrier(workflow_key: str) -> bool:
    run_key = provider_run_key_for_workflow(SessionLocal, workflow_key)
    if run_key is None:
        return False
    barrier = claim_ready_provider_run(SessionLocal, run_key)
    if barrier is None:
        return False
    _dispatch_provider_run_outbox(run_key=run_key, limit=4)
    return True


def _dispatch_provider_run_outbox(*, run_key: str | None = None, limit: int = 25) -> int:
    """Publish durable barrier actions; repeats are safe and broker failures stay due."""
    dispatched = 0
    touched: set[tuple[str, str, bool]] = set()
    for _ in range(max(1, min(int(limit), 100))):
        claimed = claim_provider_run_dispatch(SessionLocal, run_key=run_key)
        if claimed is None:
            break
        success = claimed.payload.get("parent_success") is True
        try:
            if claimed.action == "start_sources":
                crawl_complete, source_ids = provider_run_crawl_completion(
                    SessionLocal, claimed.run_key
                )
                if success and crawl_complete:
                    with SessionLocal() as session:
                        deactivate_missing_current_auction_lots(session, source_ids)
                        session.commit()
                with SessionLocal() as session:
                    prepare_auction_v2_worklist(session, send_notifications=True)
                    session.commit()
                sync_auction_v2_sources_task.apply_async(
                    kwargs={
                        "parent_run_key": claimed.run_key,
                        "parent_success": success,
                    },
                    countdown=1,
                )
            elif claimed.action == "normalize_history":
                normalize_auction_history_task.apply_async(countdown=1)
            elif claimed.action == "decision_input":
                _schedule_decision_input_recompute(countdown=2)
            else:
                raise ValueError("unsupported provider dispatch action")
            complete_provider_run_dispatch(SessionLocal, claimed)
            dispatched += 1
            touched.add((claimed.run_key, claimed.run_kind, success))
        except Exception as exc:
            fail_provider_run_dispatch(SessionLocal, claimed, error=str(exc))
            logger.warning("Provider run outbox %s failed: %s", claimed.action, exc)
            break
    for touched_run, run_kind, success in touched:
        if not provider_run_dispatches_complete(SessionLocal, touched_run):
            continue
        if run_kind == "history":
            finish_provider_run(SessionLocal, touched_run, success=success)
        elif run_kind == "sources":
            finish_source_run_and_parents(SessionLocal, touched_run, success=success)
        elif run_kind in {"current", "full"} and not success:
            # The catalogue parent is already irreversibly failed.  Its source
            # enrichment child remains durable and independent, but waiting for
            # that child (for example an open OSM circuit) only monopolizes the
            # unique current-run slot and prevents the next bounded refresh.
            finish_provider_run(SessionLocal, touched_run, success=False)
    return dispatched


def _extract_db_error_code(exc: BaseException) -> str | None:
    visited: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        code = getattr(current, "pgcode", None)
        if code is None and hasattr(current, "orig"):
            code = getattr(current.orig, "pgcode", None)
        if isinstance(code, str):
            return code

        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if cause is not None:
            current = cause
            continue
        current = context
    return None


def _is_deadlock_error(exc: BaseException) -> bool:
    return (
        _extract_db_error_code(exc) == DEADLOCK_SQLSTATE or "deadlock detected" in str(exc).lower()
    )


def _raise_deadlock_retry(exc: BaseException, self: object, delay_seconds: int) -> None:
    if not _is_deadlock_error(exc):
        return
    if not hasattr(self, "request") or not hasattr(self, "max_retries"):
        return
    retries = getattr(self.request, "retries", 0)
    max_retries = getattr(self, "max_retries", 0)
    if retries >= max_retries:
        return
    countdown = delay_seconds * (retries + 1)
    task_name = getattr(self, "name", "land_scout.sync_task")
    logger.warning(
        "Task %s hit deadlock, retrying in %ss (attempt %s/%s)",
        task_name,
        countdown,
        retries + 1,
        max_retries,
    )
    raise self.retry(exc=exc, countdown=countdown) from exc  # type: ignore[attr-defined]


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
    beat_schedule["refresh-due-eqazyna-lot-statuses"] = {
        "task": "land_scout.refresh_due_eqazyna_lot_statuses",
        "schedule": 300,
    }
    beat_schedule["backfill-canonical-land-identities"] = {
        "task": "land_scout.backfill_canonical_land_identities",
        "schedule": 3600,
    }
    beat_schedule["check-nsdi-water-protection"] = {
        "task": "land_scout.check_nsdi_water_protection",
        "schedule": 900,
    }
    beat_schedule["sync-eqazyna-auction-history"] = {
        "task": "land_scout.sync_auction_v2_eqazyna_history_backfill",
        "schedule": 3600,
    }
    beat_schedule["normalize-auction-history"] = {
        "task": "land_scout.normalize_auction_history",
        "schedule": 3600,
    }
    beat_schedule["recompute-auction-decision-inputs"] = {
        "task": "land_scout.recompute_auction_decision_inputs",
        "schedule": 900,
    }
    if settings.auction_v2_document_extraction_enabled:
        beat_schedule["extract-auction-documents"] = {
            "task": "land_scout.extract_auction_documents",
            # A retryable document that becomes due just after a sweep waits at
            # most five minutes; task continuations themselves use two seconds.
            "schedule": 300,
        }
    beat_schedule["sync-auction-verified-market"] = {
        "task": "land_scout.sync_auction_verified_market",
        "schedule": 3600,
    }
    beat_schedule["recover-provider-run-outbox"] = {
        "task": "land_scout.recover_provider_run_outbox",
        "schedule": 60,
    }
    beat_schedule["recover-provider-workflows"] = {
        "task": "land_scout.recover_provider_workflows",
        "schedule": 60,
    }
    if settings.auction_spatial_feed_enabled:
        beat_schedule["process-auction-spatial-feeds"] = {
            "task": "land_scout.process_auction_spatial_feeds",
            "schedule": 300,
        }
        beat_schedule["dispatch-auction-spatial-outbox"] = {
            "task": "land_scout.dispatch_auction_spatial_outbox",
            "schedule": 60,
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
        "land_scout.check_nsdi_water_protection": {"queue": "auctions"},
        "land_scout.link_territory_observation": {"queue": "auctions"},
        "land_scout.refresh_due_eqazyna_lot_statuses": {"queue": "auctions"},
        "land_scout.backfill_canonical_land_identities": {"queue": "auctions"},
        "land_scout.sync_auction_v2_eqazyna_history_backfill": {"queue": "auctions"},
        "land_scout.sync_auction_v2_full_cycle": {"queue": "auctions"},
        "land_scout.sync_auction_v2_sources": {"queue": "auctions"},
        "land_scout.refresh_auction_v2_infrastructure": {"queue": "auctions"},
        "land_scout.sync_auction_source_objects": {"queue": "auctions"},
        "land_scout.extract_auction_documents": {"queue": "auctions"},
        "land_scout.normalize_auction_history": {"queue": "auctions"},
        "land_scout.recompute_auction_decision_inputs": {"queue": "auctions"},
        "land_scout.recompute_auction_decision_snapshots": {"queue": "auctions"},
        "land_scout.sync_auction_verified_market": {"queue": "auctions"},
        "land_scout.sync_provider_workflow": {"queue": "auctions"},
        "land_scout.recover_provider_workflows": {"queue": "auctions"},
        "land_scout.recover_provider_run_outbox": {"queue": "auctions"},
        "land_scout.process_auction_spatial_feeds": {"queue": "auctions"},
        "land_scout.dispatch_auction_spatial_outbox": {"queue": "auctions"},
    },
    broker_transport_options={"visibility_timeout": 3600},
    beat_schedule=beat_schedule,
)


def _recover_stale_searches() -> tuple[list[str], list[str], list[str], list[str]]:
    cutoff = datetime.now(UTC) - timedelta(minutes=15)
    queued_cutoff = datetime.now(UTC) - timedelta(minutes=5)
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
        lost_queued_ids = session.scalars(
            select(SearchRequest.id).where(
                SearchRequest.status == SearchStatus.queued.value,
                SearchRequest.updated_at < queued_cutoff,
                or_(
                    SearchRequest.error_message.is_(None),
                    ~SearchRequest.error_message.like("Публичный сервис %"),
                ),
            )
        ).all()
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
    return (
        list(stale_ids),
        list(lost_queued_ids),
        list(pending_free_ids),
        list(ready_delivery_ids),
    )


def _dispatch_recovered_searches(
    stale_ids: list[str],
    lost_queued_ids: list[str],
    pending_free_ids: list[str],
    ready_delivery_ids: list[str],
) -> None:
    for request_id in stale_ids:
        logger.warning("Recovering stale search request %s", request_id)
        process_search_task.delay(request_id)
    for request_id in lost_queued_ids:
        logger.warning("Redispatching queued search request without broker task %s", request_id)
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
    _ensure_task_database_ready()
    _dispatch_recovered_searches(*_recover_stale_searches())


@celery_app.task(
    bind=True,
    name="land_scout.process_search",
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_search_task(self, request_id: str) -> str:
    _ensure_task_database_ready()
    try:
        with SessionLocal() as session:
            process_search(session, request_id)
        return request_id
    except Exception as exc:
        if isinstance(exc, ProviderCallDeferred):
            retry_after = max(30, min(86_400, ceil(exc.retry_after_seconds)))
            if self.request.retries >= self.max_retries:
                with SessionLocal() as session:
                    request = session.get(SearchRequest, request_id)
                    if request is not None:
                        request.status = SearchStatus.failed.value
                        request.progress = 100
                        request.error_message = (
                            f"Публичный сервис {exc.provider} не восстановил доступ "
                            "после нескольких попыток. Запустите анализ повторно позже."
                        )
                        request.search_finished_at = datetime.now(UTC)
                        request.search_outcome = "provider_unavailable"
                        session.commit()
                        notify_terminal_search_failure(request)
                return request_id
            with SessionLocal() as session:
                request = session.get(SearchRequest, request_id)
                if request is not None:
                    request.status = SearchStatus.queued.value
                    request.progress = max(request.progress or 0, 10)
                    request.error_message = (
                        f"Публичный сервис {exc.provider} временно ограничил запросы; "
                        f"повтор через {retry_after} сек."
                    )
                    request.search_outcome = None
                    request.search_finished_at = None
                    session.commit()
            raise self.retry(
                exc=exc,
                countdown=retry_after,
                max_retries=self.max_retries,
            ) from exc
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
    _ensure_task_database_ready()
    stale_ids, lost_queued_ids, pending_free_ids, ready_delivery_ids = _recover_stale_searches()
    _dispatch_recovered_searches(stale_ids, lost_queued_ids, pending_free_ids, ready_delivery_ids)
    return {
        "stale_searches": len(stale_ids),
        "lost_queued_searches": len(lost_queued_ids),
        "pending_free_previews": len(pending_free_ids),
        "ready_deliveries": len(ready_delivery_ids),
    }


@celery_app.task(
    bind=True,
    name="land_scout.reconcile_apipay_invoice",
    max_retries=30,
)
def reconcile_apipay_invoice_task(self, request_id: str) -> str:
    _ensure_task_database_ready()
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
            if request.payment_provider != "apipay" or not request.payment_provider_invoice_id:
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

    _ensure_task_database_ready()
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
            if access.payment_provider != "apipay" or not access.payment_provider_invoice_id:
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

    _ensure_task_database_ready()
    try:
        with SessionLocal() as session:
            payment = session.get(AccountPayment, payment_id)
            if payment is None:
                return "payment_not_found"
            if payment.payment_status == PaymentStatus.paid.value:
                return "paid"
            if payment.payment_status != PaymentStatus.awaiting_transfer.value:
                return payment.payment_status
            if payment.payment_provider != "apipay" or not payment.payment_provider_invoice_id:
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
    name="land_scout.sync_provider_workflow",
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
def sync_provider_workflow_task(self, *, workflow_key: str) -> dict[str, object]:
    """Run one durable request unit; continuation never restarts completed units."""
    _ensure_task_database_ready()
    if not isinstance(workflow_key, str) or not workflow_key or len(workflow_key) > 128:
        return {"status": "invalid", "pending": 0}
    redis_client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    lock = redis_client.lock(
        f"land-scout:lock:provider-workflow:{workflow_key}",
        timeout=360,
        blocking_timeout=1,
    )
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=False))
        if not acquired:
            return {"status": "locked", "pending": 0}
        run_key = provider_run_key_for_workflow(SessionLocal, workflow_key)
        if run_key and redis_client.exists(_provider_workflow_pause_key(run_key)):
            return {"status": "paused", "pending": 0, "run_key": run_key}
        continuation_key = _provider_workflow_continuation_key(workflow_key)
        scheduled_task_id = redis_client.get(continuation_key)
        current_task_id = str(getattr(self.request, "id", ""))
        if scheduled_task_id is not None:
            scheduled_task_id = scheduled_task_id.decode("utf-8", "replace")
            if scheduled_task_id != current_task_id:
                return {"status": "duplicate_suppressed", "pending": 0}
            redis_client.delete(continuation_key)
        result = process_provider_workflow_step(SessionLocal, workflow_key=workflow_key)
        continuation = result.pending > 0 and result.status in {
            "progress",
            "waiting",
            "deferred",
            "error",
            "terminal",
        }
        countdown = (
            max(1, min(86_400, ceil(result.retry_after_seconds or 1)))
            if result.status in {"deferred", "error"}
            else 30
            if result.status == "waiting"
            else 1
        )
        continuation_scheduled = (
            _schedule_provider_workflow_continuation(
                workflow_key,
                countdown=countdown,
            )
            if continuation
            and not (
                run_key
                and redis_client.exists(_provider_workflow_pause_key(run_key))
            )
            else False
        )
        barrier_finalized = False
        if result.status in {"complete", "terminal"}:
            barrier_finalized = _finalize_provider_barrier(workflow_key)
        return {
            "status": result.status,
            "unit_kind": result.unit_kind,
            "pending": result.pending,
            "continuation_scheduled": continuation_scheduled,
            "barrier_finalized": barrier_finalized,
            "retry_after_seconds": countdown if continuation else None,
        }
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        logger.warning("Provider workflow %s failed: %s", workflow_key, exc)
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc
    finally:
        if acquired:
            try:
                lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Provider workflow singleton lock expired before release")


@celery_app.task(
    name="land_scout.recover_provider_workflows",
    soft_time_limit=120,
    time_limit=180,
)
def recover_provider_workflows_task(*, limit: int = 25) -> dict[str, int]:
    """Wake due durable provider cursors after a broker task or worker was lost."""
    _ensure_task_database_ready()
    bounded = max(1, min(int(limit), 100))
    keys = due_provider_workflow_keys(SessionLocal, limit=bounded)
    scheduled = 0
    for offset, workflow_key in enumerate(keys, start=1):
        scheduled += int(
            _schedule_provider_workflow_continuation(
                workflow_key,
                countdown=min(offset, 60),
            )
        )
    return {
        "due": len(keys),
        "scheduled": scheduled,
        "suppressed": len(keys) - scheduled,
    }


@celery_app.task(
    name="land_scout.recover_provider_run_outbox",
    soft_time_limit=120,
    time_limit=180,
)
def recover_provider_run_outbox_task(*, limit: int = 25) -> dict[str, int]:
    """Periodic durable wake-up for crash/broker recovery of barrier actions."""
    _ensure_task_database_ready()
    bounded = max(1, min(int(limit), 100))
    dispatched = _dispatch_provider_run_outbox(limit=bounded)
    expired_parents = expire_stale_provider_parents(SessionLocal, limit=bounded)
    finalized = 0
    for run_key, run_kind, success in finalizable_provider_runs(
        SessionLocal, limit=bounded
    ):
        if run_kind == "history":
            finalized += int(finish_provider_run(SessionLocal, run_key, success=success))
        else:
            finish_source_run_and_parents(SessionLocal, run_key, success=success)
            finalized += 1
    if dispatched >= bounded:
        recover_provider_run_outbox_task.apply_async(kwargs={"limit": bounded}, countdown=1)
    return {
        "dispatched": dispatched,
        "finalized": finalized,
        "expired_parents": len(expired_parents),
        "continuation": int(dispatched >= bounded),
    }


@celery_app.task(
    bind=True,
    name="land_scout.check_nsdi_water_protection",
    max_retries=2,
    soft_time_limit=180,
    time_limit=240,
)
def check_nsdi_water_protection_task(self) -> dict[str, int]:
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {"selected": 0, "checked": 0, "warnings": 0, "unavailable": 0}
    try:
        return check_nsdi_water_batch(SessionLocal, limit=5)
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        raise self.retry(
            exc=exc, countdown=60 * (self.request.retries + 1)
        ) from exc


@celery_app.task(
    bind=True,
    name="land_scout.link_territory_observation",
    max_retries=2,
    soft_time_limit=180,
    time_limit=240,
)
def link_territory_observation_task(
    self, *, observation_id: int, limit: int = 25
) -> dict[str, int]:
    """Link only a trusted, already-persisted structured official observation."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {
            "selected": 0,
            "assessed": 0,
            "applicable": 0,
            "manual_required": 0,
            "not_applicable": 0,
        }
    try:
        return link_territory_observation_batch(
            SessionLocal, observation_id=int(observation_id), limit=limit
        )
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1)) from exc


@celery_app.task(
    bind=True,
    name="land_scout.refresh_due_eqazyna_lot_statuses",
    max_retries=20,
    soft_time_limit=300,
    time_limit=360,
)
def refresh_due_eqazyna_lot_statuses_task(self) -> dict[str, int]:
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {"selected": 0, "changed": 0, "terminal": 0, "errors": 0}
    try:
        with SessionLocal() as session:
            return refresh_due_eqazyna_lot_statuses(session, limit=5)
    except ProviderCallDeferred as exc:
        retry_after = max(30, min(86_400, ceil(exc.retry_after_seconds)))
        raise self.retry(exc=exc, countdown=retry_after) from exc


@celery_app.task(
    name="land_scout.backfill_canonical_land_identities",
    soft_time_limit=240,
    time_limit=300,
)
def backfill_canonical_land_identities_task(*, batch_size: int = 100) -> dict[str, object]:
    """Resume one durable, bounded canonical-land identity reconciliation page."""
    _ensure_task_database_ready()
    bounded = max(1, min(int(batch_size), 250))
    with SessionLocal() as session:
        cursor = session.scalar(
            select(AuctionLandIdentityBackfillCursor)
            .where(AuctionLandIdentityBackfillCursor.cursor_key == "default")
            .with_for_update()
        )
        if cursor is None:
            cursor = AuctionLandIdentityBackfillCursor(cursor_key="default")
            session.add(cursor)
            session.flush()
        if cursor.high_water_lot_id is None:
            cursor.high_water_lot_id = canonical_land_backfill_high_water(session)

        page = backfill_canonical_land_objects_page(
            session,
            limit=bounded,
            after_lot_id=cursor.after_lot_id,
            high_water_lot_id=cursor.high_water_lot_id,
        )
        cursor.scanned_count += page.scanned
        cursor.linked_count += page.linked
        cursor.conflict_count += page.unlinked
        cursor.after_lot_id = page.last_scanned_lot_id
        if not page.has_more:
            cursor.cycle_count += 1
            cursor.after_lot_id = None
            cursor.high_water_lot_id = None
        session.commit()

        result: dict[str, object] = {
            "scanned": page.scanned,
            "selected": page.selected,
            "linked": page.linked,
            "unlinked": page.unlinked,
            "has_more": page.has_more,
            "cycle_count": cursor.cycle_count,
            "total_scanned": cursor.scanned_count,
            "total_linked": cursor.linked_count,
            "total_unlinked": cursor.conflict_count,
        }
    if page.has_more:
        backfill_canonical_land_identities_task.apply_async(
            kwargs={"batch_size": bounded}, countdown=2
        )
    return result


@celery_app.task(
    bind=True,
    name="land_scout.sync_auctions",
    max_retries=3,
    soft_time_limit=1200,
    time_limit=1320,
)
@_auction_pipeline_singleton()
def sync_current_auctions_task(self) -> dict[str, int]:
    _ensure_task_database_ready()
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
    run_key, _created = ensure_provider_sync_run(
        SessionLocal,
        run_kind="current",
        detail_limit=settings.eqazyna_sync_max_lots,
        config_payload={"deactivate_missing": True},
    )
    with SessionLocal() as session:
        seed_auction_v2_sources(session)
        session.commit()
    ensure_provider_crawl_run(
        SessionLocal, run_key=run_key, source_code="eqazyna_current_lots"
    )
    workflow_keys = _seed_current_eqazyna_workflows(run_key)
    _schedule_workflows(workflow_keys)
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
        "provider_workflows_scheduled": len(workflow_keys),
    }


@celery_app.task(
    bind=True,
    name="land_scout.refresh_auction_v2_infrastructure",
    max_retries=2,
    soft_time_limit=420,
    time_limit=480,
)
def refresh_auction_v2_infrastructure_task(
    self,
    *,
    limit: int = 25,
    force: bool = False,
) -> dict[str, int]:
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {"selected": 0, "checked": 0, "errors": 0}
    bounded_limit = max(1, min(int(limit), 100))
    with SessionLocal() as session:
        lots = list(
            session.scalars(
                select(AuctionLot)
                .join(AuctionLotGeoCheck, AuctionLotGeoCheck.lot_id == AuctionLot.id)
                .where(
                    AuctionLot.active.is_(True),
                    AuctionLot.object_type == "land",
                    AuctionLotGeoCheck.coordinate_status == "found",
                    AuctionLotGeoCheck.latitude.is_not(None),
                    AuctionLotGeoCheck.longitude.is_not(None),
                    or_(
                        bool(force),
                        AuctionLotGeoCheck.osm_status.is_(None),
                        AuctionLotGeoCheck.osm_status.in_(
                            ["", "not_checked", "stale", "unavailable", "missing_coordinates"]
                        ),
                    ),
                )
                .order_by(AuctionLotGeoCheck.updated_at.desc(), AuctionLot.id.asc())
                .limit(bounded_limit)
            ).all()
        )
        try:
            checked, errors = _refresh_auction_v2_infrastructure_batch(
                session,
                lots,
                force=force,
            )
            session.commit()
        except ProviderCallDeferred as exc:
            session.rollback()
            retry_after = max(1, min(86_400, ceil(exc.retry_after_seconds)))
            refresh_auction_v2_infrastructure_task.apply_async(
                kwargs={"limit": bounded_limit, "force": force},
                countdown=retry_after,
            )
            logger.info(
                "Auction infrastructure refresh deferred (%s); continuation in %ss",
                exc.reason,
                retry_after,
            )
            return {
                "selected": len(lots),
                "checked": 0,
                "errors": 0,
                "deferred": 1,
                "retry_after_seconds": retry_after,
            }
        except Exception as exc:
            _raise_deadlock_retry(exc, self, 30)
            logger.warning("Auction infrastructure refresh batch failed: %s", exc)
            session.rollback()
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1)) from exc
    if checked:
        _schedule_decision_input_recompute(countdown=2)
    return {"selected": len(lots), "checked": checked, "errors": errors}


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_source_objects",
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
)
def sync_auction_source_objects_task(
    self,
    *,
    limit: int = 20,
    ttl_minutes: int = 1440,
    error_retry_minutes: int = DEFAULT_ERROR_RETRY_MINUTES,
    deferred_attempt: int = 0,
) -> dict[str, int]:
    """Refresh Jerler cards in a worker; web handlers only enqueue this task."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {
            "selected": 0,
            "fetched": 0,
            "updated": 0,
            "skipped_fresh": 0,
            "errors": 0,
        }
    bounded_error_retry_minutes = max(1, min(int(error_retry_minutes), 120))
    redis_client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    lock = redis_client.lock(
        "land-scout:lock:auction-source-objects",
        timeout=720,
        blocking_timeout=1,
    )
    acquired = False
    try:
        acquired = bool(lock.acquire(blocking=False))
        if not acquired:
            return {
                "selected": 0,
                "fetched": 0,
                "updated": 0,
                "skipped_fresh": 0,
                "errors": 0,
            }
        result = sync_auction_source_objects_detached(
            SessionLocal,
            limit=max(1, min(int(limit), 100)),
            ttl_minutes=max(5, int(ttl_minutes)),
            error_retry_minutes=bounded_error_retry_minutes,
        )
        if result.selected and result.errors == result.selected and not result.fetched:
            raise RuntimeError("Jerler source-object batch is fully unavailable")
        if result.updated:
            _schedule_auction_v2_sources_refresh(countdown=3)
            _schedule_decision_input_recompute(countdown=2)
        elif result.fetched:
            _schedule_decision_input_recompute(countdown=2)
        return result.as_dict()
    except JerlerEnrichmentDeferred as exc:
        retry_after = max(1, min(86_400, ceil(exc.retry_after_seconds)))
        bounded_deferred_attempt = max(0, int(deferred_attempt))
        partial_result = exc.partial_result
        payload = (
            partial_result.as_dict()
            if partial_result is not None
            else {
                "selected": 0,
                "fetched": 0,
                "updated": 0,
                "skipped_fresh": 0,
                "errors": 0,
            }
        )
        continuation_exhausted = (
            bounded_deferred_attempt >= MAX_JERLER_DEFERRED_CONTINUATIONS
        )
        if continuation_exhausted:
            logger.warning(
                "Jerler enrichment deferred (%s); bounded continuation exhausted after %s attempts",
                exc.reason,
                bounded_deferred_attempt,
            )
        else:
            sync_auction_source_objects_task.apply_async(
                kwargs={
                    "limit": max(1, min(int(limit), 100)),
                    "ttl_minutes": max(5, int(ttl_minutes)),
                    "error_retry_minutes": bounded_error_retry_minutes,
                    "deferred_attempt": bounded_deferred_attempt + 1,
                },
                countdown=retry_after,
            )
            logger.info(
                "Jerler enrichment deferred (%s); continuation %s/%s in %ss",
                exc.reason,
                bounded_deferred_attempt + 1,
                MAX_JERLER_DEFERRED_CONTINUATIONS,
                retry_after,
            )
        if partial_result is not None:
            if partial_result.updated:
                _schedule_auction_v2_sources_refresh(countdown=3)
                _schedule_decision_input_recompute(countdown=2)
            elif partial_result.fetched:
                _schedule_decision_input_recompute(countdown=2)
        return {
            **payload,
            "deferred": 1,
            "retry_after_seconds": retry_after,
            "continuation_exhausted": int(continuation_exhausted),
        }
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 60)
        logger.warning("Auction source-object sync failed: %s", exc)
        retry_delay = max(60, bounded_error_retry_minutes * 60)
        raise self.retry(exc=exc, countdown=retry_delay * (self.request.retries + 1)) from exc
    finally:
        if acquired:
            try:
                lock.release()
            except (LockError, RedisError, OSError):
                logger.warning("Auction source-object singleton lock expired before release")


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_v2_full_cycle",
    max_retries=3,
    soft_time_limit=1200,
    time_limit=1320,
)
@_auction_pipeline_singleton()
def sync_auction_v2_full_cycle_task(self) -> dict[str, int]:
    _ensure_task_database_ready()
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
    run_key, _created = ensure_provider_sync_run(
        SessionLocal,
        run_kind="full",
        detail_limit=settings.eqazyna_sync_max_lots,
        config_payload={"deactivate_missing": True, "chain_sources": True},
    )
    with SessionLocal() as session:
        seed_auction_v2_sources(session)
        session.commit()
    ensure_provider_crawl_run(
        SessionLocal, run_key=run_key, source_code="eqazyna_current_lots"
    )
    workflow_keys = _seed_current_eqazyna_workflows(run_key)
    _schedule_workflows(workflow_keys)
    return {
        "lots_fetched": 0,
        "lots_created": 0,
        "lots_updated": 0,
        "lots_deactivated": 0,
        "crawl_errors": 0,
        "provider_workflows_scheduled": len(workflow_keys),
    }


def _seed_history_eqazyna_workflows(
    run_key: str,
    *,
    statuses: list[str],
    windows: list[tuple[str, str]],
    checkpoint: dict[str, int],
) -> list[str]:
    """Seed only unfinished history windows from their durable absolute page."""
    workflow_keys: list[str] = []
    for status_index, status in enumerate(statuses):
        for window_index, window in enumerate(windows):
            resume_page = checkpoint.get(eqazyna_history_checkpoint_key(status, window), 1)
            if resume_page == 0:
                continue
            key = f"{run_key}:eq:{status_index}:{window_index}:{status}"[:128]
            seed_eqazyna_page_workflow(
                SessionLocal,
                workflow_key=key,
                search_status=status,
                max_pages=settings.eqazyna_history_sync_max_pages,
                start_page=resume_page,
                publish_date_window=window,
                run_key=run_key,
                skip_existing_details=True,
            )
            workflow_keys.append(key)
    if not workflow_keys:
        noop_key = f"{run_key}:history-complete"[:128]
        seed_provider_barrier_noop(SessionLocal, workflow_key=noop_key, run_key=run_key)
        workflow_keys.append(noop_key)
    return workflow_keys


def _history_detail_limit(requested: int | None) -> int:
    """Resolve an explicit one-run archive budget without removing the hard cap."""
    value = settings.eqazyna_history_sync_max_lots if requested is None else int(requested)
    return max(1, min(value, 20_000))


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_v2_eqazyna_history_backfill",
    max_retries=2,
    soft_time_limit=3600,
    time_limit=3900,
)
@_auction_pipeline_singleton()
def sync_auction_v2_eqazyna_history_backfill_task(
    self, *, max_lots: int | None = None
) -> dict[str, int]:
    _ensure_task_database_ready()
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
    checkpoint = eqazyna_history_resume_checkpoint(SessionLocal)
    detail_limit = _history_detail_limit(max_lots)
    config_payload: dict[str, object] = {
        "normalize_history": True,
        "detail_limit": detail_limit,
    }
    if checkpoint:
        config_payload["eqazyna_history_pages"] = checkpoint
    run_key, _created = ensure_provider_sync_run(
        SessionLocal,
        run_kind="history",
        detail_limit=detail_limit,
        config_payload=config_payload,
    )
    with SessionLocal() as session:
        seed_auction_v2_sources(session)
        session.commit()
    ensure_provider_crawl_run(
        SessionLocal, run_key=run_key, source_code="eqazyna_history_backfill"
    )
    statuses = [
        item.strip()
        for item in settings.eqazyna_history_sync_statuses.split(",")
        if item.strip()
    ] or ["SuccessProtocolSigned", "FailureProtocolSigned"]
    windows = eqazyna_history_publish_date_windows()
    workflow_keys = _seed_history_eqazyna_workflows(
        run_key,
        statuses=statuses,
        windows=windows,
        checkpoint=checkpoint,
    )
    _schedule_workflows(workflow_keys)
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
        "provider_workflows_scheduled": len(workflow_keys),
    }


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_v2_sources",
    max_retries=3,
    soft_time_limit=900,
    time_limit=990,
)
@_provider_sources_singleton
def sync_auction_v2_sources_task(
    self, *, parent_run_key: str | None = None, parent_success: bool = True
) -> dict[str, int]:
    _ensure_task_database_ready()
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
    run_key, _created = ensure_provider_sync_run(
        SessionLocal,
        run_kind="sources",
        detail_limit=0,
        config_payload={"decision_input": True},
    )
    if parent_run_key is not None:
        attach_provider_run_parent(
            SessionLocal,
            child_run_key=run_key,
            parent_run_key=parent_run_key,
            parent_success=bool(parent_success),
        )
    workflow_keys = seed_spatial_provider_workflows(
        SessionLocal, workflow_prefix=run_key, run_key=run_key
    )
    jerler_key = f"{run_key}:jerler"[:128]
    seed_jerler_provider_workflow(
        SessionLocal, workflow_key=jerler_key, run_key=run_key
    )
    workflow_keys.append(jerler_key)
    projects = _csv_task_setting(settings.auction_v2_gov_kz_projects)
    detail_urls = _csv_task_setting(settings.auction_v2_gov_kz_detail_urls)
    if projects or detail_urls:
        gov_key = f"{run_key}:gov"[:128]
        seed_gov_kz_workflow(
            SessionLocal,
            workflow_key=gov_key,
            projects=projects,
            detail_urls=detail_urls,
            max_pages=settings.auction_v2_gov_kz_max_pages,
            run_key=run_key,
        )
        workflow_keys.append(gov_key)
    if not workflow_keys:
        noop_key = f"{run_key}:noop"
        seed_provider_barrier_noop(SessionLocal, workflow_key=noop_key, run_key=run_key)
        workflow_keys.append(noop_key)
    _schedule_workflows(workflow_keys)
    return {
        "lots_checked": 0,
        "analyses_updated": 0,
        "sources_checked": len(workflow_keys),
        "provider_workflows_scheduled": len(workflow_keys),
    }


def _document_extraction_metrics(*, checked_at: datetime) -> dict[str, object]:
    """Return index-backed operational counters without inspecting document files."""
    with SessionLocal() as session:
        status_rows = session.execute(
            select(
                AuctionDocumentExtractionState.status,
                func.count(AuctionDocumentExtractionState.document_id),
            ).group_by(AuctionDocumentExtractionState.status)
        ).all()
        status_counts = {str(status): int(count) for status, count in status_rows}
        retry_due = int(
            session.scalar(
                select(func.count(AuctionDocumentExtractionState.document_id)).where(
                    AuctionDocumentExtractionState.status == "retryable",
                    or_(
                        AuctionDocumentExtractionState.next_attempt_at.is_(None),
                        AuctionDocumentExtractionState.next_attempt_at <= checked_at,
                    ),
                )
            )
            or 0
        )
        claims_expired = int(
            session.scalar(
                select(func.count(AuctionDocumentExtractionState.document_id)).where(
                    AuctionDocumentExtractionState.status == "processing",
                    AuctionDocumentExtractionState.claim_expires_at <= checked_at,
                )
            )
            or 0
        )
        cursor = session.get(AuctionDocumentExtractionCursor, "default")
    queue_depth = status_counts.get("pending", 0) + retry_due + claims_expired
    return {
        "queue_depth": queue_depth,
        "queue_depth_alarm": queue_depth >= 500,
        "retry_due": retry_due,
        "terminal": status_counts.get("terminal", 0),
        "processing": status_counts.get("processing", 0),
        "ready": status_counts.get("ready", 0),
        "claims_expired": claims_expired,
        "backfill_complete": bool(cursor and cursor.backfill_complete),
        "backfill_document_id": int(cursor.backfill_document_id) if cursor else 0,
        "watermark_document_id": int(cursor.watermark_document_id) if cursor else 0,
        "watermark_downloaded_at": (
            cursor.watermark_downloaded_at.isoformat()
            if cursor and cursor.watermark_downloaded_at
            else None
        ),
    }


def _spatial_worker_metrics(*, checked_at: datetime) -> dict[str, object]:
    """Return bounded index-backed queue counters; never parse GIS payloads."""
    with SessionLocal() as session:
        rows = session.execute(
            select(
                AuctionSpatialFeedState.status,
                func.count(AuctionSpatialFeedState.id),
            ).group_by(AuctionSpatialFeedState.status)
        ).all()
        counts = {str(status): int(count) for status, count in rows}
        outbox_due = int(
            session.scalar(
                select(func.count(AuctionSpatialDecisionSignal.id)).where(
                    or_(
                        AuctionSpatialDecisionSignal.status == "pending",
                        and_(
                            AuctionSpatialDecisionSignal.status == "failed",
                            or_(
                                AuctionSpatialDecisionSignal.next_attempt_at.is_(None),
                                AuctionSpatialDecisionSignal.next_attempt_at <= checked_at,
                            ),
                        ),
                    )
                )
            )
            or 0
        )
    due = counts.get("pending", 0) + counts.get("retryable", 0)
    return {
        "queue_depth": due,
        "queue_depth_alarm": due >= 500,
        "processing": counts.get("processing", 0),
        "ready": counts.get("ready", 0),
        "conflict": counts.get("conflict", 0),
        "quarantined": counts.get("quarantined", 0),
        "expired": counts.get("expired", 0),
        "outbox_due": outbox_due,
    }


def _auction_document_extractor_for_runtime() -> Callable[..., object] | None:
    if not settings.auction_v2_llm_enabled:
        return None
    client = AuctionLlmClient(
        base_url=settings.auction_v2_llm_base_url,
        model=settings.auction_v2_llm_model,
        timeout_seconds=settings.auction_v2_llm_timeout_seconds,
        max_text_chars=settings.auction_v2_llm_max_text_chars,
    )

    def extractor(*args, **kwargs):
        return extract_auction_document_with_llm(*args, client=client, **kwargs)

    return extractor


def _auction_document_runtime_batch_size(
    *,
    requested_batch_size: int,
    extractor: Callable[..., object] | None,
) -> int:
    bounded = max(1, min(int(requested_batch_size), DOCUMENT_EXTRACTION_MAX_BATCH))
    if extractor is not None:
        return 1
    return bounded


@celery_app.task(
    bind=True,
    name="land_scout.process_auction_spatial_feeds",
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
@_auction_spatial_singleton
def process_auction_spatial_feeds_task(
    self,
    *,
    batch_size: int | None = None,
    seed_after_lot_id: str | None = None,
    seed_high_water_lot_id: str | None = None,
) -> dict[str, object]:
    """Process one bounded due-feed batch; HTTP/GIS work runs outside DB transactions."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled or not settings.auction_spatial_feed_enabled:
        return {"status": "disabled", "selected": 0, "has_more": False}
    bounded = max(
        1,
        min(
            int(batch_size or settings.auction_spatial_batch_size),
            SPATIAL_MAX_BATCH,
        ),
    )
    checked = datetime.now(UTC)
    try:
        runtime = parse_spatial_fetch_runtime(settings.auction_spatial_providers_json)
    except SpatialFetchTerminal as exc:
        logger.error("Spatial provider configuration rejected: %s", exc)
        return {
            "status": "config_error",
            "selected": 0,
            "has_more": False,
            "error_code": exc.code,
        }
    pressure = create_redis_provider_backpressure(
        settings.redis_url,
        app_env=settings.app_env,
        policies=runtime.policies,
    )
    owner = f"spatial:{str(self.request.id or 'manual')[:48]}"
    try:
        seed = seed_spatial_feed_states(
            SessionLocal,
            runtime=runtime,
            after_lot_id=seed_after_lot_id,
            high_water_lot_id=seed_high_water_lot_id,
            limit=5,
            checked_at=checked,
        )
        if seed.enqueue_w14:
            dispatch_auction_spatial_outbox_task.apply_async(countdown=0)
        with SessionLocal() as session:
            work = SqlAlchemySpatialEvidenceStore(session).claim_due(
                checked_at=checked,
                limit=bounded,
                owner_token=owner,
            )
        if work.invalidated_manifests:
            dispatch_auction_spatial_outbox_task.apply_async(countdown=0)
        written = 0
        superseded = 0
        retryable = 0
        quarantined = 0
        errors = 0
        enqueue = False
        retry_delays: list[float] = []
        for index, claim in enumerate(work.claims):
            try:
                outcome = process_spatial_claim(
                    SessionLocal,
                    claim,
                    runtime=runtime,
                    backpressure=pressure,
                    owner_token=f"{owner}:{index}",
                    checked_at=checked,
                )
                written += int(outcome.status in {"written", "already_current"})
                superseded += int(outcome.status == "superseded")
                retryable += int(outcome.status == "retryable")
                quarantined += int(outcome.status == "quarantined")
                enqueue = enqueue or outcome.enqueue_w14
                if outcome.retry_after_seconds is not None:
                    retry_delays.append(outcome.retry_after_seconds)
            except Exception:
                errors += 1
                logger.exception("Spatial feed claim failed for %s", claim.identity.key)
        if enqueue:
            dispatch_auction_spatial_outbox_task.apply_async(countdown=0)
        has_more = (
            seed.has_more
            or len(work.claims) == bounded
            or bool(retry_delays)
            or errors > 0
        )
        continuation = False
        if has_more:
            countdown = max(1, min(300, ceil(min(retry_delays or [5]))))
            process_auction_spatial_feeds_task.apply_async(
                kwargs={
                    "batch_size": bounded,
                    "seed_after_lot_id": seed.next_after_lot_id,
                    "seed_high_water_lot_id": seed.high_water_lot_id,
                },
                countdown=countdown,
            )
            continuation = True
        return {
            "status": "partial" if errors or retryable else "ok",
            "selected": len(work.claims),
            "written": written,
            "superseded": superseded,
            "retryable": retryable,
            "quarantined": quarantined,
            "errors": errors,
            "invalidated": len(work.invalidated_manifests),
            "seed_lots": seed.lots_scanned,
            "seed_feeds_changed": seed.feeds_created_or_changed,
            "has_more": has_more,
            "continuation_scheduled": continuation,
            "metrics": _spatial_worker_metrics(checked_at=checked),
        }
    except SoftTimeLimitExceeded:
        process_auction_spatial_feeds_task.apply_async(
            kwargs={
                "batch_size": bounded,
                "seed_after_lot_id": seed_after_lot_id,
                "seed_high_water_lot_id": seed_high_water_lot_id,
            },
            countdown=5,
        )
        return {"status": "soft_timeout", "selected": 0, "has_more": True}
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        logger.warning("Spatial feed task failed: %s", exc)
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc


@celery_app.task(
    bind=True,
    name="land_scout.dispatch_auction_spatial_outbox",
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
def dispatch_auction_spatial_outbox_task(
    self,
    *,
    batch_size: int = 50,
) -> dict[str, object]:
    """Deliver durable invalidations to exact-lot W14 recompute after commit."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled or not settings.auction_spatial_feed_enabled:
        return {"status": "disabled", "claimed": 0, "has_more": False}
    bounded = max(1, min(int(batch_size), 100))
    checked = datetime.now(UTC)

    def enqueue(claims) -> None:
        lot_ids = sorted({claim.lot_id for claim in claims})
        recompute_auction_decision_inputs_task.apply_async(
            kwargs={"batch_size": min(100, len(lot_ids)), "lot_ids": lot_ids},
            countdown=1,
        )

    report = dispatch_spatial_decision_outbox(
        SessionLocal,
        enqueue,
        checked_at=checked,
        limit=bounded,
    )
    continuation = report.has_more or report.failed > 0
    if continuation:
        countdown = max(1, min(300, ceil(report.retry_after_seconds or 2)))
        dispatch_auction_spatial_outbox_task.apply_async(
            kwargs={"batch_size": bounded}, countdown=countdown
        )
    return {
        "status": "partial" if report.failed else "ok",
        "claimed": report.claimed,
        "dispatched": report.dispatched,
        "failed": report.failed,
        "has_more": report.has_more,
        "continuation_scheduled": continuation,
        "retry_after_seconds": report.retry_after_seconds,
    }


@celery_app.task(
    bind=True,
    name="land_scout.analyze_due_diligence_attachment",
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
def analyze_due_diligence_attachment_task(self, attachment_id: str) -> dict[str, object]:
    _ensure_task_database_ready()
    with SessionLocal() as session:
        attachment = session.get(AuctionDueDiligenceAttachment, attachment_id)
        if attachment is None:
            return {"status": "missing", "attachment_id": attachment_id}
        storage_root = Path(settings.auction_v2_document_storage_dir).resolve()
        file_path = (storage_root / attachment.local_path).resolve()
        if storage_root not in file_path.parents or not file_path.exists():
            attachment.extraction_status = "corrupt"
            attachment.extraction_json = json.dumps(
                {"status": "corrupt", "detail": "attachment file is missing"},
                ensure_ascii=False,
            )
            attachment.extracted_at = datetime.now(UTC)
            session.commit()
            return {"status": "corrupt", "attachment_id": attachment_id}
        try:
            payload = analyze_due_diligence_response(
                file_path.read_bytes(),
                attachment_id=attachment.id,
                title=attachment.title,
                source_url=f"due-diligence://{attachment.id}",
                observed_at=attachment.created_at,
            )
            attachment.extraction_status = str(payload.get("status") or "unknown")
            attachment.extraction_json = json.dumps(payload, ensure_ascii=False)
            attachment.extracted_at = datetime.now(UTC)
            session.commit()
            return {
                "status": attachment.extraction_status,
                "attachment_id": attachment_id,
                "candidate_count": len(payload.get("candidates") or []),
            }
        except Exception as exc:
            session.rollback()
            if self.request.retries < self.max_retries:
                raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc
            with SessionLocal() as failed_session:
                failed = failed_session.get(AuctionDueDiligenceAttachment, attachment_id)
                if failed is not None:
                    failed.extraction_status = "corrupt"
                    failed.extraction_json = json.dumps(
                        {"status": "corrupt", "detail": type(exc).__name__},
                        ensure_ascii=False,
                    )
                    failed.extracted_at = datetime.now(UTC)
                    failed_session.commit()
            return {"status": "corrupt", "attachment_id": attachment_id}


@celery_app.task(
    bind=True,
    name="land_scout.extract_auction_documents",
    max_retries=3,
    soft_time_limit=240,
    time_limit=300,
)
@_auction_document_extraction_singleton
def extract_auction_documents_task(
    self,
    *,
    batch_size: int = 10,
    after_document_id: int = 0,
) -> dict[str, object]:
    """Extract one bounded local-file batch; request handlers never parse documents."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled or not settings.auction_v2_document_extraction_enabled:
        return {"status": "disabled", "selected": 0, "has_more": False}
    runtime_extractor = _auction_document_extractor_for_runtime()
    bounded_batch = _auction_document_runtime_batch_size(
        requested_batch_size=batch_size,
        extractor=runtime_extractor,
    )
    bounded_after = max(0, int(after_document_id))
    checked_at = datetime.now(UTC)
    try:
        download_deferred = False
        try:
            with SessionLocal() as session:
                download_result = sync_auction_v2_documents(
                    session,
                    limit=settings.auction_v2_document_download_limit,
                    # A failed signed URL must not consume every continuation
                    # before already-downloaded legal documents are extracted.
                    # Failed rows remain available to explicit/manual retries.
                    retry_failed=False,
                )
                session.commit()
        except ProviderCallDeferred as exc:
            logger.info("Auction document download deferred before extraction: %s", exc)
            download_deferred = True
            download_result = AuctionV2DocumentSyncResult()
        extraction_kwargs: dict[str, object] = {
            "storage_root": settings.auction_v2_document_storage_dir,
            "limit": bounded_batch,
            "after_document_id": bounded_after,
            "now": checked_at,
        }
        if runtime_extractor is not None:
            extraction_kwargs["extractor"] = runtime_extractor
        result = extract_downloaded_auction_documents(
            SessionLocal,
            **extraction_kwargs,
        )
        changed_lot_ids = sorted(
            {outcome.lot_id for outcome in result.outcomes if outcome.status == "written"}
        )
        if changed_lot_ids:
            _schedule_decision_input_recompute(countdown=1)
        continuation_scheduled = False
        if result.has_more and result.next_after_document_id is not None:
            continuation_scheduled = _schedule_document_extraction_continuation(
                {
                    "batch_size": bounded_batch,
                    "after_document_id": result.next_after_document_id,
                },
                countdown=2,
            )
        metrics = _document_extraction_metrics(checked_at=checked_at)
        return {
            "status": "partial" if result.retryable_errors else "ok",
            "selected": result.selected,
            "written": result.written,
            "already_current": result.already_current,
            "retryable_errors": result.retryable_errors,
            "terminal_results": result.terminal_results,
            "download_checked": download_result.checked,
            "downloaded": download_result.downloaded,
            "download_errors": download_result.errors,
            "download_deferred": download_deferred,
            "changed_lots": len(changed_lot_ids),
            "coverage_reconciled": sum(item.coverage is not None for item in result.coverage),
            "coverage_errors": sum(item.error_code is not None for item in result.coverage),
            "has_more": result.has_more,
            "continuation_scheduled": continuation_scheduled,
            "next_after_document_id": result.next_after_document_id,
            "metrics": metrics,
        }
    except SoftTimeLimitExceeded:
        extract_auction_documents_task.apply_async(
            kwargs={
                "batch_size": bounded_batch,
                "after_document_id": bounded_after,
            },
            countdown=30,
        )
        return {
            "status": "soft_timeout",
            "selected": 0,
            "has_more": True,
            "continuation_scheduled": True,
        }
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        logger.warning("Auction document extraction failed: %s", exc)
        delay = min(900, 60 * (self.request.retries + 1))
        raise self.retry(exc=exc, countdown=delay) from exc


@celery_app.task(
    bind=True,
    name="land_scout.normalize_auction_history",
    max_retries=3,
    soft_time_limit=240,
    time_limit=300,
)
@_auction_history_singleton
def normalize_auction_history_task(
    self,
    *,
    generation: int | None = None,
    batch_size: int = 200,
) -> dict[str, object]:
    """Build one immutable history generation in bounded, resumable steps."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {"status": "disabled", "has_more": False}
    try:
        result = normalize_auction_history_step(
            SessionLocal,
            generation=generation,
            batch_size=max(1, min(int(batch_size), 500)),
        )
        if result.get("has_more"):
            next_generation = result.get("generation")
            normalize_auction_history_task.apply_async(
                kwargs={
                    "generation": (int(next_generation) if next_generation is not None else None),
                    "batch_size": max(1, min(int(batch_size), 500)),
                },
                countdown=2,
            )
        elif result.get("status") == "active":
            _schedule_verified_market_sync(countdown=1)
            _schedule_decision_input_recompute(countdown=2)
        return result
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        logger.warning("Auction history normalization failed: %s", exc)
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc


@celery_app.task(
    bind=True,
    name="land_scout.sync_auction_verified_market",
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
@_auction_verified_market_singleton
def sync_auction_verified_market_task(
    self,
    *,
    phase: Literal["ingest", "market"] = "ingest",
    after_lot_id: str | None = None,
    high_water_lot_id: str | None = None,
    batch_size: int = 100,
    inventory_changed: bool = False,
) -> dict[str, object]:
    """Ingest pages, then reconcile only generation/target-dirty W9 actions."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {"status": "disabled", "processed": 0, "has_more": False}
    if phase not in {"ingest", "market"}:
        return {"status": "invalid_phase", "processed": 0, "has_more": False}
    bounded = max(1, min(int(batch_size), 100))
    try:
        if phase == "ingest":
            result = ingest_eqazyna_verified_sales_batch(
                SessionLocal,
                after_lot_id=after_lot_id,
                high_water_lot_id=high_water_lot_id,
                limit=bounded,
            )
            if result.has_more:
                sync_auction_verified_market_task.apply_async(
                    kwargs={
                        "phase": "ingest",
                        "after_lot_id": result.last_lot_id,
                        "high_water_lot_id": result.high_water_lot_id,
                        "batch_size": bounded,
                        "inventory_changed": bool(
                            inventory_changed or result.inventory_generation is not None
                        ),
                    },
                    countdown=2,
                )
            elif inventory_changed or result.inventory_generation is not None:
                sync_auction_verified_market_task.apply_async(
                    kwargs={"phase": "market", "batch_size": min(bounded, 25)},
                    countdown=2,
                )
            return {
                "status": result.status,
                "phase": "ingest",
                "processed": result.selected,
                "ingested": result.ingested,
                "unchanged": result.unchanged,
                "rejected": result.rejected,
                "rejection_reasons": result.rejection_reasons,
                "duration_ms": result.duration_ms,
                "max_source_lag_seconds": result.max_source_lag_seconds,
                "inventory_generation": result.inventory_generation,
                "has_more": result.has_more,
                "high_water_lot_id": result.high_water_lot_id,
            }

        dirty = recompute_market_dirty_page(SessionLocal, limit=min(bounded, 25))
        if dirty.changed:
            # strict_market_estimate is an allowlisted W14 source; schedule only post-commit.
            _schedule_decision_input_recompute(countdown=1)
        if dirty.has_more:
            sync_auction_verified_market_task.apply_async(
                kwargs={
                    "phase": "market",
                    "batch_size": bounded,
                },
                countdown=30 if dirty.errors else 2,
            )
        return {
            "status": dirty.status,
            "phase": "market",
            "processed": dirty.recomputed + dirty.advanced,
            "scanned": dirty.scanned,
            "recomputed": dirty.recomputed,
            "advanced": dirty.advanced,
            "changed": dirty.changed,
            "errors": dirty.errors,
            "has_more": dirty.has_more,
            "latest_generation": dirty.latest_generation,
        }
    except SoftTimeLimitExceeded:
        sync_auction_verified_market_task.apply_async(
            kwargs={
                "phase": phase,
                "after_lot_id": after_lot_id,
                "high_water_lot_id": high_water_lot_id,
                "batch_size": bounded,
                "inventory_changed": inventory_changed,
            },
            countdown=10,
        )
        return {"status": "soft_timeout", "processed": 0, "has_more": True}
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        logger.warning("Verified market task failed: %s", exc)
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc


@celery_app.task(
    bind=True,
    name="land_scout.recompute_auction_decision_inputs",
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
@_auction_decision_input_singleton
def recompute_auction_decision_inputs_task(
    self,
    *,
    batch_size: int = 25,
    lot_ids: list[str] | None = None,
) -> dict[str, object]:
    """Assemble one bounded dirty batch; W13 is enqueued only after changed commits."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {"status": "disabled", "processed": 0, "has_more": False}
    bounded_batch = max(1, min(int(batch_size), 100))
    try:
        if lot_ids is not None:
            if (
                not isinstance(lot_ids, list)
                or len(lot_ids) > 100
                or any(
                    not isinstance(lot_id, str) or not 1 <= len(lot_id) <= 64
                    for lot_id in lot_ids
                )
            ):
                raise ValueError("invalid bounded decision-input lot IDs")
            selected_ids = list(dict.fromkeys(lot_ids))
            results = [
                recompute_decision_inputs(SessionLocal, lot_id)
                for lot_id in selected_ids
            ]
        else:
            results = recompute_decision_input_batch(
                SessionLocal,
                limit=bounded_batch,
            )
        changed = sum(result.changed for result in results)
        errors = sum(result.status == "error" for result in results)
        busy = sum(result.status in {"busy", "superseded"} for result in results)
        if changed:
            _schedule_decision_snapshot_recompute(countdown=1)
        has_more = lot_ids is None and len(results) == bounded_batch
        if has_more:
            recompute_auction_decision_inputs_task.apply_async(
                kwargs={"batch_size": bounded_batch},
                countdown=2,
            )
        return {
            "status": "ok" if not errors else "partial",
            "selected": len(results),
            "processed": len(results) - busy,
            "changed": changed,
            "errors": errors,
            "busy": busy,
            "has_more": has_more,
        }
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        logger.warning("Auction decision-input batch failed: %s", exc)
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc


def _default_decision_scenario(lot: AuctionLot) -> str:
    """Compatibility hint; W13 authoritatively consumes persisted W14 selection."""
    purpose = lot.purpose or lot.use_goal or lot.functional_purpose_level4
    return select_decision_scenario(purpose).scenario_key or UNCLASSIFIED_SCENARIO


def _decision_snapshot_worklist(
    *,
    after_lot_id: str | None,
    high_water_lot_id: str | None,
    limit: int,
    force: bool,
) -> tuple[list[tuple[str, str]], str | None]:
    bounded_limit = max(1, min(int(limit), 100))
    with SessionLocal() as session:
        if high_water_lot_id is None:
            high_water_lot_id = session.scalar(
                select(func.max(AuctionLot.id)).where(
                    AuctionLot.object_type == "land",
                    AuctionLot.active.is_(True),
                )
            )
        if high_water_lot_id is None:
            return [], None
        current_join = and_(
            AuctionDecisionSnapshot.lot_id == AuctionLot.id,
            AuctionDecisionSnapshot.engine_version == DECISION_ENGINE_VERSION,
            AuctionDecisionSnapshot.rules_version == VERDICT_RULES_VERSION,
            AuctionDecisionSnapshot.is_current.is_(True),
        )
        conditions = [
            AuctionLot.object_type == "land",
            AuctionLot.active.is_(True),
            AuctionLot.id <= high_water_lot_id,
        ]
        if after_lot_id is not None:
            conditions.append(AuctionLot.id > after_lot_id)
        if not force:
            newer_input_exists = (
                select(AuctionEvidence.id)
                .where(
                    AuctionEvidence.lot_id == AuctionLot.id,
                    AuctionEvidence.evidence_type.like("decision_input:%"),
                    AuctionEvidence.status.in_(("found", "conflict")),
                    AuctionEvidence.id
                    > func.coalesce(AuctionDecisionSnapshot.validated_evidence_id, 0),
                )
                .exists()
            )
            conditions.append(
                or_(
                    AuctionDecisionSnapshot.id.is_(None),
                    AuctionLot.updated_at > AuctionDecisionSnapshot.last_validated_at,
                    newer_input_exists,
                )
            )
        lots = list(
            session.scalars(
                select(AuctionLot)
                .outerjoin(AuctionDecisionSnapshot, current_join)
                .where(*conditions)
                .order_by(AuctionLot.id.asc())
                .limit(bounded_limit)
            )
        )
        return [(lot.id, _default_decision_scenario(lot)) for lot in lots], high_water_lot_id


@celery_app.task(
    bind=True,
    name="land_scout.recompute_auction_decision_snapshots",
    max_retries=2,
    soft_time_limit=240,
    time_limit=300,
)
@_auction_decision_singleton
def recompute_auction_decision_snapshots_task(
    self,
    *,
    after_lot_id: str | None = None,
    high_water_lot_id: str | None = None,
    batch_size: int = 25,
    force: bool = False,
) -> dict[str, object]:
    """Materialize one bounded worker-only batch; request handlers only read snapshots."""
    _ensure_task_database_ready()
    if not settings.auctions_enabled:
        return {"status": "disabled", "processed": 0, "has_more": False}
    bounded_batch = max(1, min(int(batch_size), 100))
    try:
        worklist, high_water = _decision_snapshot_worklist(
            after_lot_id=after_lot_id,
            high_water_lot_id=high_water_lot_id,
            limit=bounded_batch,
            force=bool(force),
        )
        processed = 0
        errors = 0
        last_lot_id = after_lot_id
        for lot_id, scenario_key in worklist:
            last_lot_id = lot_id
            try:
                recompute_decision_snapshot(
                    SessionLocal,
                    lot_id,
                    scenario_key=scenario_key,
                )
                processed += 1
            except Exception:
                errors += 1
                logger.exception("Decision snapshot recompute failed for lot %s", lot_id)
        has_more = len(worklist) == bounded_batch and last_lot_id is not None
        if has_more:
            recompute_auction_decision_snapshots_task.apply_async(
                kwargs={
                    "after_lot_id": last_lot_id,
                    "high_water_lot_id": high_water,
                    "batch_size": bounded_batch,
                    "force": bool(force),
                },
                countdown=2,
            )
        return {
            "status": "ok" if not errors else "partial",
            "processed": processed,
            "errors": errors,
            "selected": len(worklist),
            "has_more": has_more,
            "high_water_lot_id": high_water,
        }
    except Exception as exc:
        _raise_deadlock_retry(exc, self, 30)
        logger.warning("Decision snapshot batch failed: %s", exc)
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc
