"""Durable compact request-unit cursor for nonblocking provider workers."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AuctionCrawlRun,
    AuctionSource,
    ProviderRunDispatch,
    ProviderSyncRun,
    ProviderWorkflowState,
    ProviderWorkflowUnit,
)

PROVIDER_WORKFLOW_POLICY_VERSION = "provider-unit-v1"
MAX_UNIT_INPUT_BYTES = 8_000
MAX_ENQUEUE_UNITS = 1_000
MAX_WORKFLOW_TOTAL_UNITS = 20_000
MAX_UNIT_ATTEMPTS = 100
CLAIM_TTL_SECONDS = 360
FINALIZE_LEASE_SECONDS = 300
PROVIDER_PARENT_MAX_WAIT_SECONDS = 3600
EQAZYNA_HISTORY_CHECKPOINT_FIELD = "eqazyna_history_pages"
MAX_EQAZYNA_HISTORY_CHECKPOINTS = 256
MAX_PROVIDER_RUN_CONFIG_BYTES = 16_000


@dataclass(frozen=True, slots=True)
class ProviderUnitSpec:
    unit_key: str
    unit_kind: str
    input_payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class ClaimedProviderUnit:
    id: int
    workflow_key: str
    provider: str
    unit_key: str
    unit_kind: str
    input_payload: dict[str, object]
    claim_token: str
    attempts: int


@dataclass(frozen=True, slots=True)
class ProviderRunBarrier:
    run_key: str
    run_kind: str
    config_payload: dict[str, object]
    has_errors: bool


@dataclass(frozen=True, slots=True)
class ClaimedProviderDispatch:
    id: int
    run_key: str
    run_kind: str
    action: str
    payload: dict[str, object]
    claim_token: str


@dataclass(frozen=True, slots=True)
class EqazynaSourceExhaustionEntry:
    """Durable source-exhaustion evidence for one status/date-window crawl."""

    workflow_key: str
    search_status: str
    publish_date_window: tuple[str, str] | None
    page_limit: int
    pages_requested: int
    urls_seen: int
    first_empty_page: int | None
    exhausted: bool
    partial_reason: str | None


def eqazyna_history_checkpoint_key(search_status: str, publish_date_window: tuple[str, str]) -> str:
    """Return a compact stable identity for one history status/date window."""
    material = json.dumps(
        [str(search_status), list(publish_date_window)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _bounded_history_checkpoint(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    checkpoint: dict[str, int] = {}
    for key, page in sorted(value.items()):
        if (
            len(checkpoint) >= MAX_EQAZYNA_HISTORY_CHECKPOINTS
            or not isinstance(key, str)
            or len(key) != 16
            or not isinstance(page, int)
            or isinstance(page, bool)
            or not 0 <= page <= 1_000
        ):
            continue
        checkpoint[key] = page
    return checkpoint


def _checkpoint_history_run(session: Session, run: ProviderSyncRun) -> dict[str, int]:
    """Persist compact history progress while the run lock is held."""
    config = _run_config(run)
    checkpoint = _bounded_history_checkpoint(config.get(EQAZYNA_HISTORY_CHECKPOINT_FIELD))
    rows = list(
        session.scalars(
            select(ProviderWorkflowUnit)
            .join(
                ProviderWorkflowState,
                ProviderWorkflowState.workflow_key == ProviderWorkflowUnit.workflow_key,
            )
            .where(
                ProviderWorkflowState.run_key == run.run_key,
                ProviderWorkflowState.provider == "eqazyna",
                ProviderWorkflowState.workflow_kind == "auction_list_and_detail",
            )
            # List units carry the window identity and page progress, while detail
            # units prove whether that progress is safe to promote. Reading only
            # list rows could permanently exhaust a window that lost a detail.
            .order_by(ProviderWorkflowUnit.workflow_key, ProviderWorkflowUnit.unit_key)
        )
    )
    by_workflow: dict[str, list[ProviderWorkflowUnit]] = {}
    for row in rows:
        by_workflow.setdefault(str(row.workflow_key), []).append(row)
    touched: dict[str, int] = {}
    for units in by_workflow.values():
        identity: str | None = None
        empty_page: int | None = None
        completed_pages: list[int] = []
        hard_error = False
        for unit in units:
            try:
                payload = json.loads(unit.input_json)
            except (TypeError, json.JSONDecodeError):
                hard_error = True
                continue
            window = payload.get("publish_date_window")
            status = payload.get("search_status")
            if (
                identity is None
                and isinstance(status, str)
                and isinstance(window, list)
                and len(window) == 2
                and all(isinstance(item, str) for item in window)
            ):
                identity = eqazyna_history_checkpoint_key(status, (window[0], window[1]))
            page = payload.get("page")
            if unit.status == "done" and isinstance(page, int):
                completed_pages.append(page)
                if unit.result_ref == "urls:0":
                    empty_page = page if empty_page is None else min(empty_page, page)
            elif unit.status == "terminal" and unit.last_error not in {
                "pagination_complete",
                "detail_limit_reached",
            }:
                hard_error = True
        if identity is None or hard_error:
            continue
        if empty_page is not None:
            touched[identity] = 0
        elif completed_pages:
            # Deliberately replay the last page in a capped window. Listing-page
            # replay is idempotent and closes the crash gap before detail units.
            # Later list units may already be terminal/detail_limit_reached; that
            # is the normal capped-run shape and must not discard this progress.
            touched[identity] = max(completed_pages)
    checkpoint.update(touched)
    # Current-run evidence wins if a legacy config ever reaches the bound.
    ordered_keys = list(sorted(touched)) + [key for key in sorted(checkpoint) if key not in touched]
    checkpoint = {key: checkpoint[key] for key in ordered_keys[:MAX_EQAZYNA_HISTORY_CHECKPOINTS]}
    config[EQAZYNA_HISTORY_CHECKPOINT_FIELD] = checkpoint
    config_json = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(config_json.encode("utf-8")) > MAX_PROVIDER_RUN_CONFIG_BYTES:
        raise ValueError("provider run config is too large")
    run.config_json = config_json
    return checkpoint


def eqazyna_history_resume_checkpoint(
    session_factory: sessionmaker[Session],
) -> dict[str, int]:
    """Load the latest durable history checkpoint for the next bounded run."""
    with session_factory() as session:
        runs = list(
            session.scalars(
                select(ProviderSyncRun)
                .where(ProviderSyncRun.run_kind == "history")
                .order_by(ProviderSyncRun.started_at.desc(), ProviderSyncRun.run_key.desc())
                .limit(10)
            )
        )
        for run in runs:
            checkpoint = _bounded_history_checkpoint(
                _run_config(run).get(EQAZYNA_HISTORY_CHECKPOINT_FIELD)
            )
            if checkpoint:
                return checkpoint
    return {}


def _reconcile_run_children(
    session: Session, run: ProviderSyncRun, *, checked_at: datetime
) -> None:
    child_states = list(
        session.scalars(
            select(ProviderWorkflowState)
            .where(ProviderWorkflowState.run_key == run.run_key)
            .order_by(ProviderWorkflowState.workflow_key)
        )
    )
    keys = [child.workflow_key for child in child_states]
    aggregates = {
        str(workflow_key): (int(pending or 0), int(hard_errors or 0))
        for workflow_key, pending, hard_errors in session.execute(
            select(
                ProviderWorkflowUnit.workflow_key,
                func.sum(
                    func.cast(
                        ProviderWorkflowUnit.status.not_in(("done", "terminal")),
                        Integer,
                    )
                ),
                func.sum(
                    func.cast(
                        and_(
                            ProviderWorkflowUnit.status == "terminal",
                            ProviderWorkflowUnit.last_error.not_in(
                                ("pagination_complete", "detail_limit_reached")
                            ),
                        ),
                        Integer,
                    )
                ),
            )
            .where(ProviderWorkflowUnit.workflow_key.in_(keys))
            .group_by(ProviderWorkflowUnit.workflow_key)
        )
    } if keys else {}
    completed = 0
    changed = False
    for child in child_states:
        pending, hard_errors = aggregates.get(child.workflow_key, (0, 0))
        if pending:
            # A crash or an older race can leave newly appended units behind a
            # stale terminal child status. Re-open that child so the regular
            # due-workflow recovery can claim its authoritative pending rows.
            if child.status in {"complete", "error"}:
                child.status = "pending"
                child.claim_token = None
                child.claim_expires_at = None
                child.next_attempt_at = checked_at
                child.last_error = None
                child.updated_at = checked_at
                changed = True
            continue
        desired_status = "error" if hard_errors else "complete"
        if (
            child.status != desired_status
            or child.claim_token is not None
            or child.claim_expires_at is not None
        ):
            child.status = desired_status
            child.claim_token = None
            child.claim_expires_at = None
            child.updated_at = checked_at
            changed = True
        completed += 1
    completed = min(run.child_count, completed)
    if run.completed_children != completed:
        run.completed_children = completed
        changed = True
    if changed:
        run.updated_at = checked_at


def claim_ready_provider_run(
    session_factory: sessionmaker[Session], run_key: str, *, now: datetime | None = None
) -> ProviderRunBarrier | None:
    with session_factory() as session:
        run = session.scalar(
            select(ProviderSyncRun)
            .where(ProviderSyncRun.run_key == run_key)
            .with_for_update()
        )
        checked_at = _aware(now or datetime.now(UTC))
        # Child/unit rows are authoritative. Persist their repaired aggregate
        # even when the parent is not yet ready for its downstream barrier.
        if run is not None and run.status in {"active", "finalizing"}:
            _reconcile_run_children(session, run, checked_at=checked_at)
        recoverable_finalize = (
            run is not None
            and run.status == "finalizing"
            and not run.downstream_dispatched
            and _aware(run.updated_at)
            <= checked_at - timedelta(seconds=FINALIZE_LEASE_SECONDS)
        )
        if (
            run is None
            or (run.status != "active" and not recoverable_finalize)
            or run.child_count == 0
            or run.completed_children != run.child_count
            or run.downstream_dispatched
        ):
            session.commit()
            return None
        child_statuses = list(
            session.scalars(
                select(ProviderWorkflowState.status).where(
                    ProviderWorkflowState.run_key == run_key
                )
            )
        )
        try:
            config = json.loads(run.config_json)
        except json.JSONDecodeError:
            config = {}
        if not isinstance(config, dict):
            config = {}
        if run.run_kind == "history":
            config[EQAZYNA_HISTORY_CHECKPOINT_FIELD] = _checkpoint_history_run(session, run)
        run.status = "finalizing"
        # This is a recoverable lease, not an acknowledgement. A crash or broker
        # rejection leaves downstream_dispatched false and can be reclaimed.
        run.downstream_dispatched = False
        run.updated_at = checked_at
        actions = {
            "current": ("start_sources",),
            "full": ("start_sources",),
            "history": ("normalize_history",),
            "sources": ("normalize_history", "decision_input"),
        }[run.run_kind]
        for action in actions:
            existing_dispatch = session.scalar(
                select(ProviderRunDispatch.id).where(
                    ProviderRunDispatch.run_key == run.run_key,
                    ProviderRunDispatch.action == action,
                )
            )
            if existing_dispatch is None:
                payload = {
                    "parent_success": not any(
                        status == "error" for status in child_statuses
                    )
                }
                session.add(
                    ProviderRunDispatch(
                        run_key=run.run_key,
                        action=action,
                        status="pending",
                        payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                        created_at=checked_at,
                        updated_at=checked_at,
                    )
                )
        barrier = ProviderRunBarrier(
            run_key=run.run_key,
            run_kind=run.run_kind,
            config_payload=config,
            has_errors=any(status == "error" for status in child_statuses),
        )
        session.commit()
        return barrier


def claim_provider_run_dispatch(
    session_factory: sessionmaker[Session],
    *,
    run_key: str | None = None,
    now: datetime | None = None,
) -> ClaimedProviderDispatch | None:
    checked_at = _aware(now or datetime.now(UTC))
    token = secrets.token_hex(24)
    expires_at = checked_at + timedelta(seconds=CLAIM_TTL_SECONDS)
    with session_factory() as session:
        row = session.scalar(
            select(ProviderRunDispatch)
            .join(ProviderSyncRun, ProviderSyncRun.run_key == ProviderRunDispatch.run_key)
            .where(
                ProviderSyncRun.status == "finalizing",
                ProviderRunDispatch.run_key == run_key if run_key is not None else True,
                or_(
                    and_(
                        ProviderRunDispatch.status.in_(("pending", "error")),
                        or_(
                            ProviderRunDispatch.next_attempt_at.is_(None),
                            ProviderRunDispatch.next_attempt_at <= checked_at,
                        ),
                    ),
                    and_(
                        ProviderRunDispatch.status == "processing",
                        ProviderRunDispatch.claim_expires_at <= checked_at,
                    ),
                    and_(
                        ProviderRunDispatch.status == "dispatched",
                        ProviderRunDispatch.action == "start_sources",
                        ProviderRunDispatch.next_attempt_at <= checked_at,
                    ),
                ),
            )
            .order_by(ProviderRunDispatch.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        run = session.get(ProviderSyncRun, row.run_key)
        if run is None:
            return None
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        row.status = "processing"
        row.claim_token = token
        row.claim_expires_at = expires_at
        row.next_attempt_at = None
        row.attempts = min(10_000, row.attempts + 1)
        row.updated_at = checked_at
        session.commit()
        return ClaimedProviderDispatch(
            id=row.id,
            run_key=row.run_key,
            run_kind=run.run_kind,
            action=row.action,
            payload=payload,
            claim_token=token,
        )


def complete_provider_run_dispatch(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderDispatch,
    *,
    now: datetime | None = None,
) -> bool:
    checked_at = _aware(now or datetime.now(UTC))
    recurring = claimed.action == "start_sources"
    with session_factory() as session:
        changed = session.execute(
            update(ProviderRunDispatch)
            .where(
                ProviderRunDispatch.id == claimed.id,
                ProviderRunDispatch.status == "processing",
                ProviderRunDispatch.claim_token == claimed.claim_token,
            )
            .values(
                status="dispatched",
                claim_token=None,
                claim_expires_at=None,
                next_attempt_at=(
                    checked_at + timedelta(seconds=FINALIZE_LEASE_SECONDS)
                    if recurring
                    else None
                ),
                last_error=None,
                updated_at=checked_at,
            )
        )
        session.commit()
        return changed.rowcount == 1


def fail_provider_run_dispatch(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderDispatch,
    *,
    error: str,
    now: datetime | None = None,
) -> bool:
    checked_at = _aware(now or datetime.now(UTC))
    retry = min(900, 2 ** min(10, max(1, claimed.id.bit_length())))
    with session_factory() as session:
        changed = session.execute(
            update(ProviderRunDispatch)
            .where(
                ProviderRunDispatch.id == claimed.id,
                ProviderRunDispatch.status == "processing",
                ProviderRunDispatch.claim_token == claimed.claim_token,
            )
            .values(
                status="error",
                claim_token=None,
                claim_expires_at=None,
                next_attempt_at=checked_at + timedelta(seconds=retry),
                last_error=str(error)[:1000],
                updated_at=checked_at,
            )
        )
        session.commit()
        return changed.rowcount == 1


def provider_run_dispatches_complete(
    session_factory: sessionmaker[Session], run_key: str
) -> bool:
    with session_factory() as session:
        count = int(
            session.scalar(
                select(func.count(ProviderRunDispatch.id)).where(
                    ProviderRunDispatch.run_key == run_key,
                    ProviderRunDispatch.status != "dispatched",
                )
            )
            or 0
        )
        total = int(
            session.scalar(
                select(func.count(ProviderRunDispatch.id)).where(
                    ProviderRunDispatch.run_key == run_key
                )
            )
            or 0
        )
        return total > 0 and count == 0


def finalizable_provider_runs(
    session_factory: sessionmaker[Session], *, limit: int = 100
) -> list[tuple[str, str, bool]]:
    """Find downstream-complete history/source runs after a worker crash."""
    bounded = max(1, min(int(limit), 100))
    incomplete = (
        select(ProviderRunDispatch.id)
        .where(
            ProviderRunDispatch.run_key == ProviderSyncRun.run_key,
            ProviderRunDispatch.status != "dispatched",
        )
        .correlate(ProviderSyncRun)
        .exists()
    )
    with session_factory() as session:
        rows = session.execute(
            select(
                ProviderSyncRun.run_key,
                ProviderSyncRun.run_kind,
                func.min(
                    case(
                        (
                            ProviderRunDispatch.payload_json.contains(
                                '"parent_success":true'
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .join(
                ProviderRunDispatch,
                ProviderRunDispatch.run_key == ProviderSyncRun.run_key,
            )
            .where(
                ProviderSyncRun.status == "finalizing",
                ProviderSyncRun.run_kind.in_(("history", "sources")),
                ~incomplete,
                select(ProviderRunDispatch.id)
                .where(ProviderRunDispatch.run_key == ProviderSyncRun.run_key)
                .correlate(ProviderSyncRun)
                .exists(),
            )
            .group_by(ProviderSyncRun.run_key, ProviderSyncRun.run_kind)
            .order_by(ProviderSyncRun.updated_at, ProviderSyncRun.run_key)
            .limit(bounded)
        ).all()
        return [
            (str(run_key), str(run_kind), int(success or 0) == 1)
            for run_key, run_kind, success in rows
        ]


def expire_stale_provider_parents(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
    timeout_seconds: int = PROVIDER_PARENT_MAX_WAIT_SECONDS,
    limit: int = 100,
) -> list[str]:
    """Fail closed current/full parents that waited too long for shared sources.

    E-Qazyna catalogue freshness must not be blocked indefinitely by a slow
    downstream OSM/document/source run.  Only parents whose durable
    ``start_sources`` dispatch was acknowledged are eligible; the shared source
    run remains independent and is not modified.
    """
    bounded_timeout = max(FINALIZE_LEASE_SECONDS, min(int(timeout_seconds), 86_400))
    bounded_limit = max(1, min(int(limit), 100))
    checked_at = _aware(now or datetime.now(UTC))
    cutoff = checked_at - timedelta(seconds=bounded_timeout)
    dispatched_sources = (
        select(ProviderRunDispatch.id)
        .where(
            ProviderRunDispatch.run_key == ProviderSyncRun.run_key,
            ProviderRunDispatch.action == "start_sources",
            ProviderRunDispatch.status == "dispatched",
            ProviderRunDispatch.created_at <= cutoff,
        )
        .correlate(ProviderSyncRun)
        .exists()
    )
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(ProviderSyncRun)
                .where(
                    ProviderSyncRun.status == "finalizing",
                    ProviderSyncRun.run_kind.in_(("current", "full")),
                    dispatched_sources,
                )
                .order_by(ProviderSyncRun.started_at, ProviderSyncRun.run_key)
                .with_for_update(skip_locked=True)
                .limit(bounded_limit)
            )
        )
        expired: list[str] = []
        for run in rows:
            _finish_run_row(session, run, success=False, checked_at=checked_at)
            expired.append(str(run.run_key))
        session.commit()
        return expired


def finish_provider_run(
    session_factory: sessionmaker[Session],
    run_key: str,
    *,
    success: bool,
) -> bool:
    checked_at = datetime.now(UTC)
    with session_factory() as session:
        run = session.scalar(
            select(ProviderSyncRun)
            .where(ProviderSyncRun.run_key == run_key)
            .with_for_update()
        )
        if run is None or run.status != "finalizing":
            return False
        _finish_run_row(session, run, success=success, checked_at=checked_at)
        session.commit()
        return True


def _run_config(run: ProviderSyncRun) -> dict[str, object]:
    try:
        payload = json.loads(run.config_json)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _finish_run_row(
    session: Session,
    run: ProviderSyncRun,
    *,
    success: bool,
    checked_at: datetime,
) -> None:
    run.status = "complete" if success else "error"
    run.completed_at = checked_at
    run.updated_at = checked_at
    config = _run_config(run)
    crawl_run_id = config.get("crawl_run_id")
    if isinstance(crawl_run_id, int):
        crawl = session.get(AuctionCrawlRun, crawl_run_id)
        if crawl is not None and crawl.finished_at is None:
            child_keys = select(ProviderWorkflowState.workflow_key).where(
                ProviderWorkflowState.run_key == run.run_key
            )
            created = int(
                session.scalar(
                    select(func.count(ProviderWorkflowUnit.id)).where(
                        ProviderWorkflowUnit.workflow_key.in_(child_keys),
                        ProviderWorkflowUnit.result_ref.like("auction_lot:created:%"),
                    )
                )
                or 0
            )
            updated = int(
                session.scalar(
                    select(func.count(ProviderWorkflowUnit.id)).where(
                        ProviderWorkflowUnit.workflow_key.in_(child_keys),
                        ProviderWorkflowUnit.result_ref.like("auction_lot:updated:%"),
                    )
                )
                or 0
            )
            fetched = int(
                session.scalar(
                    select(func.count(ProviderWorkflowUnit.id)).where(
                        ProviderWorkflowUnit.workflow_key.in_(child_keys),
                        ProviderWorkflowUnit.unit_kind == "eqazyna_lot_detail",
                        ProviderWorkflowUnit.status == "done",
                    )
                )
                or 0
            )
            crawl.status = "success" if success else "error"
            crawl.items_seen = run.details_enqueued
            crawl.items_created = created
            crawl.items_updated = updated
            crawl.finished_at = checked_at
            crawl.error_message = None if success else "provider workflow child failed"
            crawl.raw_payload_json = json.dumps(
                {
                    "mode": "durable_provider_workflow",
                    "provider_run_key": run.run_key,
                    "children": run.child_count,
                    "completed_children": run.completed_children,
                    "details_enqueued": run.details_enqueued,
                    "fetched": fetched,
                    "url_count": run.details_enqueued,
                    "policy_version": run.policy_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            source = session.get(AuctionSource, crawl.source_id)
            if source is not None:
                source.last_checked_at = checked_at
                source.last_error = crawl.error_message
                if success:
                    source.last_success_at = checked_at


def ensure_provider_crawl_run(
    session_factory: sessionmaker[Session],
    *,
    run_key: str,
    source_code: str,
) -> int:
    """Attach exactly one legacy/UI crawl aggregate to a durable provider run."""
    checked_at = datetime.now(UTC)
    with session_factory() as session:
        run = session.scalar(
            select(ProviderSyncRun)
            .where(ProviderSyncRun.run_key == run_key)
            .with_for_update()
        )
        if run is None:
            raise ValueError("provider run is missing")
        config = _run_config(run)
        existing = config.get("crawl_run_id")
        if isinstance(existing, int):
            return existing
        source = session.scalar(select(AuctionSource).where(AuctionSource.code == source_code))
        if source is None:
            raise ValueError("auction source is missing")
        crawl = AuctionCrawlRun(
            source_id=source.id,
            status="running",
            started_at=checked_at,
            raw_payload_json=json.dumps(
                {"mode": "durable_provider_workflow", "provider_run_key": run_key},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        session.add(crawl)
        session.flush()
        config["crawl_run_id"] = crawl.id
        run.config_json = json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        run.updated_at = checked_at
        source.last_checked_at = checked_at
        session.commit()
        return crawl.id


def attach_provider_run_parent(
    session_factory: sessionmaker[Session],
    *,
    child_run_key: str,
    parent_run_key: str,
    parent_success: bool,
) -> bool:
    """Durably join a finalizing parent to a shared source run without a race."""
    checked_at = datetime.now(UTC)
    with session_factory() as session:
        child = session.scalar(
            select(ProviderSyncRun)
            .where(ProviderSyncRun.run_key == child_run_key)
            .with_for_update()
        )
        parent = session.scalar(
            select(ProviderSyncRun)
            .where(ProviderSyncRun.run_key == parent_run_key)
            .with_for_update()
        )
        if child is None or parent is None or parent.status != "finalizing":
            return False
        if child.run_kind != "sources":
            raise ValueError("provider child must be a sources run")
        if child.status in {"complete", "error"}:
            _finish_run_row(
                session,
                parent,
                success=child.status == "complete" and parent_success,
                checked_at=checked_at,
            )
            session.commit()
            return True
        config = _run_config(child)
        parents = config.get("parent_run_keys")
        parent_keys = [str(value) for value in parents] if isinstance(parents, list) else []
        if parent_run_key not in parent_keys:
            if len(parent_keys) >= 16:
                raise ValueError("provider parent bound exceeded")
            parent_keys.append(parent_run_key)
        config["parent_run_keys"] = sorted(parent_keys)
        outcomes = config.get("parent_success")
        parent_outcomes = dict(outcomes) if isinstance(outcomes, dict) else {}
        parent_outcomes[parent_run_key] = bool(parent_success)
        config["parent_success"] = parent_outcomes
        child.config_json = json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        child.updated_at = checked_at
        session.commit()
        return True


def finish_source_run_and_parents(
    session_factory: sessionmaker[Session],
    run_key: str,
    *,
    success: bool,
) -> int:
    """Atomically close a source barrier and every attached full/current parent."""
    checked_at = datetime.now(UTC)
    with session_factory() as session:
        child = session.scalar(
            select(ProviderSyncRun)
            .where(ProviderSyncRun.run_key == run_key)
            .with_for_update()
        )
        if child is None or child.run_kind != "sources" or child.status != "finalizing":
            return 0
        config = _run_config(child)
        raw_parents = config.get("parent_run_keys")
        raw_outcomes = config.get("parent_success")
        parent_outcomes = raw_outcomes if isinstance(raw_outcomes, dict) else {}
        parent_keys = sorted(
            {str(value) for value in raw_parents if isinstance(value, str)}
        ) if isinstance(raw_parents, list) else []
        parents = list(
            session.scalars(
                select(ProviderSyncRun)
                .where(ProviderSyncRun.run_key.in_(parent_keys))
                .order_by(ProviderSyncRun.run_key)
                .with_for_update()
            )
        ) if parent_keys else []
        _finish_run_row(session, child, success=success, checked_at=checked_at)
        finished = 0
        for parent in parents:
            if parent.status != "finalizing":
                continue
            _finish_run_row(
                session,
                parent,
                success=success and parent_outcomes.get(parent.run_key) is True,
                checked_at=checked_at,
            )
            finished += 1
        session.commit()
        return finished


def ensure_provider_sync_run(
    session_factory: sessionmaker[Session],
    *,
    run_kind: str,
    detail_limit: int,
    config_payload: dict[str, object],
    now: datetime | None = None,
) -> tuple[str, bool]:
    if run_kind not in {"current", "full", "history", "sources"}:
        raise ValueError("invalid provider run kind")
    if not 0 <= int(detail_limit) <= 100_000:
        raise ValueError("invalid provider detail limit")
    config_json = json.dumps(
        config_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(config_json.encode("utf-8")) > MAX_PROVIDER_RUN_CONFIG_BYTES:
        raise ValueError("provider run config is too large")
    checked_at = _aware(now or datetime.now(UTC))
    with session_factory() as session:
        active = session.scalar(
            select(ProviderSyncRun)
            .where(
                ProviderSyncRun.run_kind == run_kind,
                ProviderSyncRun.status.in_(("active", "finalizing")),
            )
            .order_by(ProviderSyncRun.started_at.asc())
            .with_for_update()
            .limit(1)
        )
        if active is not None:
            return active.run_key, False
        run_key = uuid.uuid4().hex
        session.add(
            ProviderSyncRun(
                run_key=run_key,
                run_kind=run_kind,
                status="active",
                detail_limit=int(detail_limit),
                details_enqueued=0,
                config_json=config_json,
                policy_version=PROVIDER_WORKFLOW_POLICY_VERSION,
                started_at=checked_at,
                updated_at=checked_at,
            )
        )
        try:
            session.commit()
            return run_key, True
        except IntegrityError:
            session.rollback()
            active = session.scalar(
                select(ProviderSyncRun).where(
                    ProviderSyncRun.run_kind == run_kind,
                    ProviderSyncRun.status.in_(("active", "finalizing")),
                )
            )
            if active is None:
                raise
            return active.run_key, False


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bounded_text(value: object, *, maximum: int, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"invalid {field}")
    return value


def _unit_material(spec: ProviderUnitSpec) -> tuple[str, str, str]:
    key = _bounded_text(spec.unit_key, maximum=128, field="unit key")
    kind = _bounded_text(spec.unit_kind, maximum=64, field="unit kind")
    raw = json.dumps(
        spec.input_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(raw.encode("utf-8")) > MAX_UNIT_INPUT_BYTES:
        raise ValueError("provider unit input is too large")
    return key, kind, raw


def stable_unit_key(kind: str, identity: object) -> str:
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{kind[:32]}:{hashlib.sha256(raw).hexdigest()}"


def create_provider_workflow(
    session_factory: sessionmaker[Session],
    *,
    workflow_key: str,
    provider: str,
    workflow_kind: str,
    units: list[ProviderUnitSpec],
    run_key: str | None = None,
    now: datetime | None = None,
) -> int:
    workflow_key = _bounded_text(workflow_key, maximum=128, field="workflow key")
    provider = _bounded_text(provider, maximum=32, field="provider")
    workflow_kind = _bounded_text(workflow_kind, maximum=64, field="workflow kind")
    if provider not in {
        "eqazyna",
        "egkn",
        "osm_overpass",
        "gov_kz",
        "auction_documents",
        "jerler",
    }:
        raise ValueError("unsupported provider")
    if not units or len(units) > MAX_ENQUEUE_UNITS:
        raise ValueError("provider unit batch is outside bounds")
    materials = [_unit_material(unit) for unit in units]
    if len({item[0] for item in materials}) != len(materials):
        raise ValueError("duplicate provider unit key")
    checked_at = _aware(now or datetime.now(UTC))
    with session_factory() as session:
        state = session.get(ProviderWorkflowState, workflow_key)
        if state is None:
            run = session.get(ProviderSyncRun, run_key) if run_key is not None else None
            if run_key is not None and (run is None or run.status != "active"):
                raise ValueError("provider sync run is not active")
            state = ProviderWorkflowState(
                workflow_key=workflow_key,
                run_key=run_key,
                provider=provider,
                workflow_kind=workflow_kind,
                status="pending",
                cursor_json="{}",
                policy_version=PROVIDER_WORKFLOW_POLICY_VERSION,
                created_at=checked_at,
                updated_at=checked_at,
            )
            session.add(state)
            session.flush()
            if run is not None:
                if run.child_count >= 1_000:
                    raise ValueError("provider run child bound exceeded")
                run.child_count += 1
                run.updated_at = checked_at
        elif state.provider != provider or state.workflow_kind != workflow_kind:
            raise ValueError("workflow identity conflict")
        existing = set(
            session.scalars(
                select(ProviderWorkflowUnit.unit_key).where(
                    ProviderWorkflowUnit.workflow_key == workflow_key,
                    ProviderWorkflowUnit.unit_key.in_([item[0] for item in materials]),
                )
            )
        )
        inserted = 0
        for unit_key, unit_kind, raw in materials:
            if unit_key in existing:
                continue
            session.add(
                ProviderWorkflowUnit(
                    workflow_key=workflow_key,
                    unit_key=unit_key,
                    unit_kind=unit_kind,
                    input_json=raw,
                    status="pending",
                    created_at=checked_at,
                    updated_at=checked_at,
                )
            )
            inserted += 1
        if inserted:
            state.status = "pending"
            state.updated_at = checked_at
        session.commit()
        return inserted


def claim_provider_unit(
    session_factory: sessionmaker[Session],
    *,
    workflow_key: str,
    now: datetime | None = None,
) -> ClaimedProviderUnit | None:
    checked_at = _aware(now or datetime.now(UTC))
    token = secrets.token_hex(24)
    expires_at = checked_at + timedelta(seconds=CLAIM_TTL_SECONDS)
    with session_factory() as session:
        state = session.scalar(
            select(ProviderWorkflowState)
            .where(ProviderWorkflowState.workflow_key == workflow_key)
            .with_for_update()
        )
        if state is None:
            return None
        if (
            state.status == "processing"
            and state.claim_expires_at is not None
            and _aware(state.claim_expires_at) > checked_at
        ):
            return None
        if state.status == "complete":
            return None
        row: ProviderWorkflowUnit | None = None
        while True:
            row = session.scalar(
                select(ProviderWorkflowUnit)
                .where(
                    ProviderWorkflowUnit.workflow_key == workflow_key,
                    or_(
                        and_(
                            ProviderWorkflowUnit.status.in_(("pending", "error")),
                            or_(
                                ProviderWorkflowUnit.next_attempt_at.is_(None),
                                ProviderWorkflowUnit.next_attempt_at <= checked_at,
                            ),
                        ),
                        and_(
                            ProviderWorkflowUnit.status == "processing",
                            ProviderWorkflowUnit.claim_expires_at <= checked_at,
                        ),
                    ),
                )
                .order_by(ProviderWorkflowUnit.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            if row.attempts < MAX_UNIT_ATTEMPTS:
                break
            row.status = "terminal"
            row.claim_token = None
            row.claim_expires_at = None
            row.next_attempt_at = None
            row.last_error = row.last_error or "attempt_limit_reached"
            row.updated_at = checked_at
            state.failed_units += 1
            session.flush()
            pending_count = int(
                session.scalar(
                    select(func.count(ProviderWorkflowUnit.id)).where(
                        ProviderWorkflowUnit.workflow_key == workflow_key,
                        ProviderWorkflowUnit.status.not_in(("done", "terminal")),
                    )
                )
                or 0
            )
            state.status = "error" if pending_count == 0 else "pending"
            state.claim_token = None
            state.claim_expires_at = None
            state.next_attempt_at = None
            state.last_error = row.last_error if pending_count == 0 else None
            state.updated_at = checked_at
            if state.run_key is not None:
                run = session.scalar(
                    select(ProviderSyncRun)
                    .where(ProviderSyncRun.run_key == state.run_key)
                    .with_for_update()
                )
                if run is not None:
                    _reconcile_run_children(session, run, checked_at=checked_at)
            if pending_count == 0:
                session.commit()
                return None
        claimed = session.execute(
            update(ProviderWorkflowUnit)
            .where(
                ProviderWorkflowUnit.id == row.id,
                ProviderWorkflowUnit.attempts < MAX_UNIT_ATTEMPTS,
                or_(
                    ProviderWorkflowUnit.status.in_(("pending", "error")),
                    ProviderWorkflowUnit.claim_expires_at <= checked_at,
                ),
            )
            .values(
                status="processing",
                claim_token=token,
                claim_expires_at=expires_at,
                attempts=ProviderWorkflowUnit.attempts + 1,
                updated_at=checked_at,
            )
        )
        if claimed.rowcount != 1:
            session.rollback()
            return None
        session.refresh(row)
        payload = json.loads(row.input_json)
        if not isinstance(payload, dict):
            raise ValueError("invalid persisted provider unit")
        result = ClaimedProviderUnit(
            id=row.id,
            workflow_key=workflow_key,
            provider=state.provider,
            unit_key=row.unit_key,
            unit_kind=row.unit_kind,
            input_payload=payload,
            claim_token=token,
            attempts=row.attempts,
        )
        state.status = "processing"
        state.claim_token = token
        state.claim_expires_at = expires_at
        state.updated_at = checked_at
        session.commit()
        return result


def complete_provider_unit(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    *,
    result_ref: str | None = None,
    followup_units: list[ProviderUnitSpec] | None = None,
    now: datetime | None = None,
) -> bool:
    checked_at = _aware(now or datetime.now(UTC))
    followups = followup_units or []
    if len(followups) > MAX_ENQUEUE_UNITS:
        raise ValueError("too many follow-up units")
    materials = [_unit_material(unit) for unit in followups]
    if result_ref is not None and len(result_ref) > 512:
        raise ValueError("result reference too long")
    with session_factory() as session:
        changed = session.execute(
            update(ProviderWorkflowUnit)
            .where(
                ProviderWorkflowUnit.id == claimed.id,
                ProviderWorkflowUnit.status == "processing",
                ProviderWorkflowUnit.claim_token == claimed.claim_token,
            )
            .values(
                status="done",
                claim_token=None,
                claim_expires_at=None,
                next_attempt_at=None,
                result_ref=result_ref,
                last_error=None,
                updated_at=checked_at,
            )
        )
        if changed.rowcount != 1:
            session.rollback()
            return False
        existing = set()
        state = session.scalar(
            select(ProviderWorkflowState)
            .where(ProviderWorkflowState.workflow_key == claimed.workflow_key)
            .with_for_update()
        )
        if state is None or state.claim_token != claimed.claim_token:
            session.rollback()
            return False
        run = (
            session.scalar(
                select(ProviderSyncRun)
                .where(ProviderSyncRun.run_key == state.run_key)
                .with_for_update()
            )
            if state.run_key is not None
            else None
        )
        # Count only work that will actually be inserted. Replayed list pages may
        # propose a detail URL that already exists in this workflow; charging that
        # duplicate against the run-wide cap can terminate untouched list pages.
        existing = set()
        if materials:
            unique_materials: list[tuple[str, str, str]] = []
            seen_keys: set[str] = set()
            for item in materials:
                if item[0] in seen_keys:
                    continue
                seen_keys.add(item[0])
                unique_materials.append(item)
            existing = set(
                session.scalars(
                    select(ProviderWorkflowUnit.unit_key).where(
                        ProviderWorkflowUnit.workflow_key == claimed.workflow_key,
                        ProviderWorkflowUnit.unit_key.in_([item[0] for item in unique_materials]),
                    )
                )
            )
            materials = [item for item in unique_materials if item[0] not in existing]
        detail_materials = [item for item in materials if item[1] == "eqazyna_lot_detail"]
        if run is not None and detail_materials:
            remaining = max(0, run.detail_limit - run.details_enqueued)
            allowed_keys = {item[0] for item in detail_materials[:remaining]}
            materials = [
                item
                for item in materials
                if item[1] != "eqazyna_lot_detail" or item[0] in allowed_keys
            ]
            run.details_enqueued += len(allowed_keys)
            run.updated_at = checked_at
        if materials:
            current_count = int(
                session.scalar(
                    select(func.count(ProviderWorkflowUnit.id)).where(
                        ProviderWorkflowUnit.workflow_key == claimed.workflow_key
                    )
                )
                or 0
            )
            if current_count + len(materials) > MAX_WORKFLOW_TOTAL_UNITS:
                raise ValueError("provider workflow total unit bound exceeded")
        for unit_key, unit_kind, raw in materials:
            session.add(
                ProviderWorkflowUnit(
                    workflow_key=claimed.workflow_key,
                    unit_key=unit_key,
                    unit_kind=unit_kind,
                    input_json=raw,
                    status="pending",
                    created_at=checked_at,
                    updated_at=checked_at,
                )
            )
        state.completed_units += 1
        if result_ref == "urls:0":
            session.execute(
                update(ProviderWorkflowUnit)
                .where(
                    ProviderWorkflowUnit.workflow_key == claimed.workflow_key,
                    ProviderWorkflowUnit.unit_kind == "eqazyna_list_page",
                    ProviderWorkflowUnit.status == "pending",
                )
                .values(status="terminal", last_error="pagination_complete", updated_at=checked_at)
            )
        if run is not None and run.details_enqueued >= run.detail_limit:
            child_keys = select(ProviderWorkflowState.workflow_key).where(
                ProviderWorkflowState.run_key == run.run_key
            )
            session.execute(
                update(ProviderWorkflowUnit)
                .where(
                    ProviderWorkflowUnit.workflow_key.in_(child_keys),
                    ProviderWorkflowUnit.unit_kind == "eqazyna_list_page",
                    ProviderWorkflowUnit.status == "pending",
                )
                .values(status="terminal", last_error="detail_limit_reached", updated_at=checked_at)
            )
        pending_count = int(
            session.scalar(
                select(func.count(ProviderWorkflowUnit.id)).where(
                    ProviderWorkflowUnit.workflow_key == claimed.workflow_key,
                    ProviderWorkflowUnit.status.not_in(("done", "terminal")),
                )
            )
            or 0
        )
        terminal_errors = int(
            session.scalar(
                select(func.count(ProviderWorkflowUnit.id)).where(
                    ProviderWorkflowUnit.workflow_key == claimed.workflow_key,
                    ProviderWorkflowUnit.status == "terminal",
                    ProviderWorkflowUnit.last_error.not_in(
                        ("pagination_complete", "detail_limit_reached")
                    ),
                )
            )
            or 0
        )
        state.status = "error" if pending_count == 0 and terminal_errors else (
            "complete" if pending_count == 0 else "pending"
        )
        state.claim_token = None
        state.claim_expires_at = None
        state.next_attempt_at = None
        state.attempts = 0
        state.last_error = None
        state.updated_at = checked_at
        if run is not None:
            _reconcile_run_children(session, run, checked_at=checked_at)
        session.commit()
        return True


def defer_provider_unit(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    *,
    retry_after_seconds: float,
    error: str,
    now: datetime | None = None,
) -> bool:
    checked_at = _aware(now or datetime.now(UTC))
    retry = min(max(float(retry_after_seconds), 0.1), 86_400.0)
    with session_factory() as session:
        changed = session.execute(
            update(ProviderWorkflowUnit)
            .where(
                ProviderWorkflowUnit.id == claimed.id,
                ProviderWorkflowUnit.status == "processing",
                ProviderWorkflowUnit.claim_token == claimed.claim_token,
            )
            .values(
                status="error",
                claim_token=None,
                claim_expires_at=None,
                next_attempt_at=checked_at + timedelta(seconds=retry),
                last_error=str(error)[:1000],
                updated_at=checked_at,
            )
        )
        if changed.rowcount != 1:
            session.rollback()
            return False
        state = session.get(ProviderWorkflowState, claimed.workflow_key)
        if state is None or state.claim_token != claimed.claim_token:
            session.rollback()
            return False
        state.status = "deferred"
        state.claim_token = None
        state.claim_expires_at = None
        state.next_attempt_at = checked_at + timedelta(seconds=retry)
        state.attempts = min(10_000, state.attempts + 1)
        state.last_error = str(error)[:1000]
        state.updated_at = checked_at
        session.commit()
        return True


def fail_provider_unit(
    session_factory: sessionmaker[Session],
    claimed: ClaimedProviderUnit,
    *,
    error: str,
    retry_after_seconds: int = 60,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> str:
    """Bound parser/permanent failures; terminal units are quarantined, never replayed forever."""
    checked_at = _aware(now or datetime.now(UTC))
    terminal = claimed.attempts >= max(1, min(int(max_attempts), 10))
    next_attempt = None if terminal else checked_at + timedelta(
        seconds=max(1, min(int(retry_after_seconds), 86_400))
    )
    status = "terminal" if terminal else "error"
    with session_factory() as session:
        changed = session.execute(
            update(ProviderWorkflowUnit)
            .where(
                ProviderWorkflowUnit.id == claimed.id,
                ProviderWorkflowUnit.status == "processing",
                ProviderWorkflowUnit.claim_token == claimed.claim_token,
            )
            .values(
                status=status,
                claim_token=None,
                claim_expires_at=None,
                next_attempt_at=next_attempt,
                last_error=str(error)[:1000],
                updated_at=checked_at,
            )
        )
        if changed.rowcount != 1:
            session.rollback()
            return "superseded"
        state = session.get(ProviderWorkflowState, claimed.workflow_key)
        if state is None or state.claim_token != claimed.claim_token:
            session.rollback()
            return "superseded"
        state.failed_units += int(terminal)
        pending_count = int(
            session.scalar(
                select(func.count(ProviderWorkflowUnit.id)).where(
                    ProviderWorkflowUnit.workflow_key == claimed.workflow_key,
                    ProviderWorkflowUnit.status.not_in(("done", "terminal")),
                )
            )
            or 0
        )
        state.status = (
            "deferred"
            if not terminal
            else "error"
            if pending_count == 0
            else "pending"
        )
        state.claim_token = None
        state.claim_expires_at = None
        state.next_attempt_at = next_attempt if not terminal else None
        state.last_error = str(error)[:1000] if state.status == "error" else None
        state.updated_at = checked_at
        if terminal and state.run_key is not None:
            run = session.scalar(
                select(ProviderSyncRun)
                .where(ProviderSyncRun.run_key == state.run_key)
                .with_for_update()
            )
            if run is not None:
                _reconcile_run_children(session, run, checked_at=checked_at)
        session.commit()
        return status


def due_provider_workflow_keys(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
    limit: int = 25,
) -> list[str]:
    """Return due durable workflows whose broker continuation may have been lost."""
    checked_at = _aware(now or datetime.now(UTC))
    bounded = max(1, min(int(limit), 100))
    due_state = or_(
        and_(
            ProviderWorkflowState.status.in_(("pending", "error")),
            or_(
                ProviderWorkflowState.next_attempt_at.is_(None),
                ProviderWorkflowState.next_attempt_at <= checked_at,
            ),
        ),
        and_(
            ProviderWorkflowState.status == "deferred",
            ProviderWorkflowState.next_attempt_at <= checked_at,
        ),
        and_(
            ProviderWorkflowState.status == "processing",
            ProviderWorkflowState.claim_expires_at <= checked_at,
        ),
    )
    live_run = or_(
        ProviderWorkflowState.run_key.is_(None),
        ProviderSyncRun.status.in_(("active", "finalizing")),
    )
    due_unit = (
        select(ProviderWorkflowUnit.id)
        .where(
            ProviderWorkflowUnit.workflow_key == ProviderWorkflowState.workflow_key,
            or_(
                and_(
                    ProviderWorkflowUnit.status.in_(("pending", "error")),
                    or_(
                        ProviderWorkflowUnit.next_attempt_at.is_(None),
                        ProviderWorkflowUnit.next_attempt_at <= checked_at,
                    ),
                ),
                and_(
                    ProviderWorkflowUnit.status == "processing",
                    ProviderWorkflowUnit.claim_expires_at <= checked_at,
                ),
            ),
        )
        .correlate(ProviderWorkflowState)
        .exists()
    )
    with session_factory() as session:
        return list(
            session.scalars(
                select(ProviderWorkflowState.workflow_key)
                .outerjoin(
                    ProviderSyncRun,
                    ProviderSyncRun.run_key == ProviderWorkflowState.run_key,
                )
                .where(due_state, live_run, due_unit)
                .order_by(ProviderWorkflowState.workflow_key.asc())
                .limit(bounded)
            )
        )


def provider_workflow_pending(
    session_factory: sessionmaker[Session], workflow_key: str
) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count(ProviderWorkflowUnit.id)).where(
                    ProviderWorkflowUnit.workflow_key == workflow_key,
                    ProviderWorkflowUnit.status.not_in(("done", "terminal")),
                )
            )
            or 0
        )


def provider_run_key_for_workflow(
    session_factory: sessionmaker[Session], workflow_key: str
) -> str | None:
    with session_factory() as session:
        return session.scalar(
            select(ProviderWorkflowState.run_key).where(
                ProviderWorkflowState.workflow_key == workflow_key
            )
        )


def eqazyna_source_exhaustion_ledger(
    session_factory: sessionmaker[Session], run_key: str
) -> list[EqazynaSourceExhaustionEntry]:
    """Read one run as a durable status/date/page source-exhaustion ledger.

    Completing every configured page is not exhaustion: if the last bounded
    page still contains URLs, the source may have more pages.  A successfully
    fetched empty page is the only positive exhaustion proof.
    """
    with session_factory() as session:
        workflow_rows = list(
            session.execute(
                select(ProviderWorkflowState.workflow_key, ProviderWorkflowState.status)
                .where(
                    ProviderWorkflowState.run_key == run_key,
                    ProviderWorkflowState.provider == "eqazyna",
                    ProviderWorkflowState.workflow_kind == "auction_list_and_detail",
                )
                .order_by(ProviderWorkflowState.workflow_key)
            ).all()
        )
        workflow_keys = [str(row.workflow_key) for row in workflow_rows]
        unit_rows = (
            list(
                session.scalars(
                    select(ProviderWorkflowUnit)
                    .where(
                        ProviderWorkflowUnit.workflow_key.in_(workflow_keys),
                        ProviderWorkflowUnit.unit_kind == "eqazyna_list_page",
                    )
                    .order_by(
                        ProviderWorkflowUnit.workflow_key,
                        ProviderWorkflowUnit.unit_key,
                    )
                )
            )
            if workflow_keys
            else []
        )

    units_by_workflow: dict[str, list[ProviderWorkflowUnit]] = {
        workflow_key: [] for workflow_key in workflow_keys
    }
    for unit in unit_rows:
        units_by_workflow[str(unit.workflow_key)].append(unit)

    ledger: list[EqazynaSourceExhaustionEntry] = []
    for workflow_row in workflow_rows:
        workflow_key = str(workflow_row.workflow_key)
        units = units_by_workflow[workflow_key]
        search_status = ""
        publish_date_window: tuple[str, str] | None = None
        pages_requested = 0
        urls_seen = 0
        first_empty_page: int | None = None
        has_pending = False
        has_detail_limit = False
        has_hard_error = False
        for unit in units:
            try:
                payload = json.loads(unit.input_json)
            except (TypeError, json.JSONDecodeError):
                payload = {}
                has_hard_error = True
            if not search_status:
                search_status = str(payload.get("search_status") or "")
                raw_window = payload.get("publish_date_window")
                if (
                    isinstance(raw_window, list)
                    and len(raw_window) == 2
                    and all(isinstance(value, str) for value in raw_window)
                ):
                    publish_date_window = (raw_window[0], raw_window[1])
            if unit.status == "done":
                pages_requested += 1
                result_ref = str(unit.result_ref or "")
                if result_ref.startswith("urls:"):
                    try:
                        count = max(0, int(result_ref.removeprefix("urls:")))
                    except ValueError:
                        has_hard_error = True
                    else:
                        urls_seen += count
                        page = payload.get("page")
                        if (
                            count == 0
                            and isinstance(page, int)
                            and (first_empty_page is None or page < first_empty_page)
                        ):
                            first_empty_page = page
                else:
                    has_hard_error = True
            elif unit.status == "terminal":
                if unit.last_error == "detail_limit_reached":
                    has_detail_limit = True
                elif unit.last_error != "pagination_complete":
                    has_hard_error = True
            else:
                has_pending = True

        # An empty listing page proves only that pagination ended. The window is
        # not source-exhausted until every detail discovered on earlier pages is
        # durably complete; the workflow aggregate captures pending detail rows
        # without loading their potentially large payload set into this ledger.
        workflow_status = str(workflow_row.status)
        exhausted = (
            first_empty_page is not None
            and not has_hard_error
            and not has_pending
            and workflow_status == "complete"
        )
        partial_reason = None
        if not exhausted:
            if has_hard_error or workflow_status == "error":
                partial_reason = "error"
            elif has_detail_limit:
                partial_reason = "detail_limit_reached"
            elif has_pending or workflow_status in {"pending", "processing", "deferred"}:
                partial_reason = "in_progress"
            else:
                partial_reason = "max_pages_reached"
        ledger.append(
            EqazynaSourceExhaustionEntry(
                workflow_key=workflow_key,
                search_status=search_status,
                publish_date_window=publish_date_window,
                page_limit=len(units),
                pages_requested=pages_requested,
                urls_seen=urls_seen,
                first_empty_page=first_empty_page,
                exhausted=exhausted,
                partial_reason=partial_reason,
            )
        )
    return ledger


def provider_run_crawl_completion(
    session_factory: sessionmaker[Session], run_key: str
) -> tuple[bool, set[str]]:
    """Return safe completeness and bounded seen source IDs for E-Qazyna runs."""
    from app.providers.eqazyna import extract_source_lot_id

    exhaustion_ledger = eqazyna_source_exhaustion_ledger(session_factory, run_key)
    with session_factory() as session:
        run = session.get(ProviderSyncRun, run_key)
        if run is None or run.details_enqueued >= run.detail_limit:
            return False, set()
        workflows = list(
            session.scalars(
                select(ProviderWorkflowState.workflow_key).where(
                    ProviderWorkflowState.run_key == run_key,
                    ProviderWorkflowState.provider == "eqazyna",
                    ProviderWorkflowState.workflow_kind == "auction_list_and_detail",
                )
            )
        )
        if not workflows:
            return False, set()
        pagination_complete = set(
            session.scalars(
                select(ProviderWorkflowUnit.workflow_key).where(
                    ProviderWorkflowUnit.workflow_key.in_(workflows),
                    ProviderWorkflowUnit.unit_kind == "eqazyna_list_page",
                    ProviderWorkflowUnit.status == "terminal",
                    ProviderWorkflowUnit.last_error == "pagination_complete",
                )
            )
        )
        hard_errors = int(
            session.scalar(
                select(func.count(ProviderWorkflowUnit.id)).where(
                    ProviderWorkflowUnit.workflow_key.in_(workflows),
                    ProviderWorkflowUnit.status == "terminal",
                    ProviderWorkflowUnit.last_error.not_in(
                        ("pagination_complete", "detail_limit_reached")
                    ),
                )
            )
            or 0
        )
        raw_inputs = list(
            session.scalars(
                select(ProviderWorkflowUnit.input_json).where(
                    ProviderWorkflowUnit.workflow_key.in_(workflows),
                    ProviderWorkflowUnit.unit_kind == "eqazyna_lot_detail",
                    ProviderWorkflowUnit.status == "done",
                )
            )
        )
    source_ids: set[str] = set()
    for raw in raw_inputs[:100_000]:
        try:
            payload = json.loads(raw)
            source_id = extract_source_lot_id(str(payload.get("source_url") or ""))
        except (AttributeError, json.JSONDecodeError):
            continue
        if source_id:
            source_ids.add(source_id)
    ledger_complete = bool(exhaustion_ledger) and all(
        entry.exhausted for entry in exhaustion_ledger
    )
    return (
        ledger_complete
        and len(pagination_complete) == len(workflows)
        and hard_errors == 0,
        source_ids,
    )
