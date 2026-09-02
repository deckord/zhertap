from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.orm import Session

from app.auction_decision_input_producers import (
    ContractCoverageResult,
    DecisionInputProducerError,
    build_authoritative_contract_coverage,
    load_contract_coverage_inputs,
)
from app.auction_document_extractor import (
    EXTRACTOR_VERSION,
    TEXT_FILE_TYPES,
    DocumentExtractionResult,
    DocumentMetadata,
    ExtractionLimits,
    extract_auction_document,
)
from app.models import (
    AuctionDocument,
    AuctionDocumentExtractionCursor,
    AuctionDocumentExtractionState,
    AuctionEvidence,
    AuctionLot,
)

WRITER_VERSION = "auction-document-extraction-writer/2026.4"
EVIDENCE_TYPE = "document_extraction"
MAX_BATCH = 20
MAX_SCAN = 100
MAX_FILE_BYTES = 8_000_000
MAX_HASH_BYTES = 100_000_000
MAX_PAYLOAD_BYTES = 64_000
MAX_PATH_CHARS = 2_048
CLAIM_TTL = timedelta(minutes=10)
DEFAULT_REVALIDATE_AFTER = timedelta(hours=24)
IMAGE_FILE_TYPES = {"jpg", "jpeg", "png"}
MAX_RETRY_SECONDS = 6 * 60 * 60
_SQLITE_LOCK = threading.Lock()


class DocumentExtractionWriterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DocumentWorkItem:
    document_id: int
    lot_id: str
    title: str
    source_url: str
    file_type: str | None
    local_path: str
    content_sha256: str
    observed_at: datetime
    lot_context_json: str = "{}"

    @property
    def signature(self) -> str:
        material = (
            f"{self.document_id}:{self.lot_id}:{self.content_sha256}:"
            f"{self.local_path}:{self.file_type}:{self.lot_context_json}:"
            f"{WRITER_VERSION}:{EXTRACTOR_VERSION}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _ClaimedDocument:
    item: DocumentWorkItem
    claim_token: str
    validation_only: bool = False


@dataclass(frozen=True, slots=True)
class DocumentExtractionOutcome:
    document_id: int
    lot_id: str
    status: str
    evidence_id: int | None = None
    extraction_status: str | None = None
    retryable: bool = False
    retry_after_seconds: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LotCoverageReconciliation:
    lot_id: str
    coverage: ContractCoverageResult | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentExtractionBatchResult:
    selected: int
    written: int
    already_current: int
    retryable_errors: int
    terminal_results: int
    outcomes: tuple[DocumentExtractionOutcome, ...]
    coverage: tuple[LotCoverageReconciliation, ...]
    next_after_document_id: int | None
    has_more: bool


def _db_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _strict_payload(value: object) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DocumentExtractionWriterError("extraction payload is not strict JSON") from exc
    if len(rendered.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise DocumentExtractionWriterError("extraction payload exceeds evidence budget")
    return rendered


def _safe_local_path(storage_root: Path, raw_path: str) -> Path | None:
    if not isinstance(raw_path, str) or not 1 <= len(raw_path) <= MAX_PATH_CHARS:
        return None
    root = storage_root.resolve()
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _image_priority_expression():
    return case(
        (AuctionDocument.file_type.in_(tuple(sorted(IMAGE_FILE_TYPES))), 0),
        else_=1,
    )


def _legal_relevance_expression():
    title = func.lower(func.coalesce(AuctionDocument.title, ""))
    contract_or_lease = or_(
        title.like("%договор%"),
        title.like("%контракт%"),
        title.like("%аренд%"),
        title.like("%lease%"),
    )
    restriction = or_(
        title.like("%обремен%"),
        title.like("%огранич%"),
        title.like("%сервитут%"),
        title.like("%арест%"),
        title.like("%запрет%"),
    )
    return case((contract_or_lease, 0), (restriction, 1), else_=2)


def _lot_context_json(
    *,
    land_rights: object = None,
    lease_term_years: object = None,
    purpose: object = None,
    intended_use: object = None,
    area_ha: object = None,
    cadastral_number: object = None,
    guarantee_kzt: object = None,
    annual_rent_kzt: object = None,
    additional_payment_kzt: object = None,
) -> str:
    rights = str(land_rights or "").casefold()
    right_type = (
        "lease"
        if any(marker in rights for marker in ("аренд", "землепольз", "жалда"))
        else "ownership"
        if any(marker in rights for marker in ("собствен", "меншік"))
        else None
    )
    payload = {
        "right_type": right_type,
        "lease_term_years": lease_term_years,
        "target_purpose": purpose,
        "intended_use": intended_use,
        "area_hectares": area_ha,
        "cadastral_number": cadastral_number,
        "guarantee_payment_kzt": guarantee_kzt,
        "annual_payment_kzt": annual_rent_kzt,
        "one_time_payment_kzt": additional_payment_kzt,
    }
    return json.dumps(
        {key: value for key, value in payload.items() if value not in (None, "")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def read_document_extraction_worklist(
    session: Session,
    *,
    after_document_id: int = 0,
    scan_limit: int = MAX_SCAN,
    checked_at: datetime | None = None,
    revalidate_after: timedelta = DEFAULT_REVALIDATE_AFTER,
) -> tuple[DocumentWorkItem, ...]:
    if (
        not isinstance(after_document_id, int)
        or isinstance(after_document_id, bool)
        or after_document_id < 0
    ):
        raise DocumentExtractionWriterError("invalid document checkpoint")
    checked = checked_at or datetime.now(UTC)
    if _db_aware(checked) is None or not timedelta(0) <= revalidate_after <= timedelta(days=30):
        raise DocumentExtractionWriterError("invalid worklist time bounds")
    validation_cutoff = checked - revalidate_after
    bounded_scan = max(1, min(int(scan_limit), MAX_SCAN))
    state = AuctionDocumentExtractionState
    base_state = state.document_id > after_document_id
    extractable_document_join = (
        AuctionDocument.id == state.document_id,
        AuctionDocument.storage_status == "downloaded",
        AuctionDocument.local_path.is_not(None),
        AuctionDocument.content_sha256.is_not(None),
        AuctionDocument.file_type.in_(tuple(sorted(TEXT_FILE_TYPES))),
    )
    image_priority = _image_priority_expression()
    # Current auctions must be analysed before archive documents even when their
    # document IDs are newer. Historical processing continues after live work.
    live_lot_priority = case(
        (AuctionLot.active.is_(True), 0),
        else_=1,
    )
    state_queries = (
        select(state.document_id)
        .join(AuctionDocument, AuctionDocument.id == state.document_id)
        .join(AuctionLot, AuctionLot.id == AuctionDocument.lot_id)
        .where(base_state, state.status == "pending", *extractable_document_join[1:])
        .order_by(live_lot_priority.asc(), _legal_relevance_expression().asc(), image_priority.asc(), state.document_id.asc())
        .limit(bounded_scan + 1),
        select(state.document_id)
        .join(AuctionDocument, AuctionDocument.id == state.document_id)
        .join(AuctionLot, AuctionLot.id == AuctionDocument.lot_id)
        .where(
            base_state,
            state.status == "retryable",
            or_(state.next_attempt_at.is_(None), state.next_attempt_at <= checked),
            *extractable_document_join[1:],
        )
        .order_by(
            live_lot_priority.asc(),
            _legal_relevance_expression().asc(),
            image_priority.asc(),
            state.next_attempt_at.asc().nullsfirst(),
            state.document_id.asc(),
        )
        .limit(bounded_scan + 1),
        select(state.document_id)
        .join(AuctionDocument, AuctionDocument.id == state.document_id)
        .join(AuctionLot, AuctionLot.id == AuctionDocument.lot_id)
        .where(
            base_state,
            state.status == "processing",
            or_(state.claim_expires_at.is_(None), state.claim_expires_at <= checked),
            *extractable_document_join[1:],
        )
        .order_by(
            live_lot_priority.asc(),
            _legal_relevance_expression().asc(),
            image_priority.asc(),
            state.claim_expires_at.asc().nullsfirst(),
            state.document_id.asc(),
        )
        .limit(bounded_scan + 1),
        select(state.document_id)
        .join(AuctionDocument, AuctionDocument.id == state.document_id)
        .join(AuctionLot, AuctionLot.id == AuctionDocument.lot_id)
        .where(
            base_state,
            state.status == "ready",
            or_(state.last_validated_at.is_(None), state.last_validated_at <= validation_cutoff),
            *extractable_document_join[1:],
        )
        .order_by(
            live_lot_priority.asc(),
            _legal_relevance_expression().asc(),
            image_priority.asc(),
            state.last_validated_at.asc().nullsfirst(),
            state.document_id.asc(),
        )
        .limit(bounded_scan + 1),
    )
    # Preserve urgency-group ordering: pending/retryable work must drain before
    # optional revalidation of an older ready document.
    candidate_ids: list[int] = []
    seen_candidate_ids: set[int] = set()
    for query in state_queries:
        for document_id in session.scalars(query):
            value = int(document_id)
            if value in seen_candidate_ids:
                continue
            seen_candidate_ids.add(value)
            candidate_ids.append(value)
            if len(candidate_ids) >= bounded_scan + 1:
                break
        if len(candidate_ids) >= bounded_scan + 1:
            break
    if not candidate_ids:
        return ()
    rows = list(
        session.execute(
            select(
                AuctionDocument.id,
                AuctionDocument.lot_id,
                AuctionDocument.title,
                AuctionDocument.source_url,
                AuctionDocument.file_type,
                AuctionDocument.local_path,
                AuctionDocument.content_sha256,
                AuctionDocument.downloaded_at,
                AuctionDocument.created_at,
                AuctionLot.land_rights,
                AuctionLot.lease_term_years,
                AuctionLot.purpose,
                AuctionLot.use_goal,
                AuctionLot.area_ha,
                AuctionLot.cadastre_number,
                AuctionLot.guarantee_kzt,
                AuctionLot.annual_rent_kzt,
                AuctionLot.additional_payment_kzt,
            )
            .join(AuctionLot, AuctionLot.id == AuctionDocument.lot_id)
            .where(
                AuctionDocument.id.in_(candidate_ids),
                AuctionDocument.storage_status == "downloaded",
                AuctionDocument.local_path.is_not(None),
                AuctionDocument.content_sha256.is_not(None),
                AuctionDocument.file_type.in_(tuple(sorted(TEXT_FILE_TYPES))),
            )
            .order_by(_image_priority_expression().asc(), _legal_relevance_expression().asc(), AuctionDocument.id.asc())
            .limit(bounded_scan + 1)
        )
    )
    rows_by_id = {int(row.id): row for row in rows}
    return tuple(
        DocumentWorkItem(
            document_id=int(rows_by_id[document_id].id),
            lot_id=str(rows_by_id[document_id].lot_id),
            title=str(rows_by_id[document_id].title)[:320],
            source_url=str(rows_by_id[document_id].source_url)[:2_048],
            file_type=rows_by_id[document_id].file_type,
            local_path=str(rows_by_id[document_id].local_path),
            content_sha256=str(rows_by_id[document_id].content_sha256),
            observed_at=_db_aware(rows_by_id[document_id].downloaded_at)
            or _db_aware(rows_by_id[document_id].created_at)
            or checked,
            lot_context_json=_lot_context_json(
                land_rights=rows_by_id[document_id].land_rights,
                lease_term_years=rows_by_id[document_id].lease_term_years,
                purpose=rows_by_id[document_id].purpose,
                intended_use=rows_by_id[document_id].use_goal,
                area_ha=rows_by_id[document_id].area_ha,
                cadastral_number=rows_by_id[document_id].cadastre_number,
                guarantee_kzt=rows_by_id[document_id].guarantee_kzt,
                annual_rent_kzt=rows_by_id[document_id].annual_rent_kzt,
                additional_payment_kzt=rows_by_id[document_id].additional_payment_kzt,
            ),
        )
        for document_id in candidate_ids
        if document_id in rows_by_id
    )


def _work_item(document: AuctionDocument) -> DocumentWorkItem | None:
    if not (
        document.storage_status == "downloaded"
        and document.file_type in TEXT_FILE_TYPES
        and document.local_path
        and document.content_sha256
    ):
        return None
    lot = getattr(document, "lot", None)
    return DocumentWorkItem(
        document_id=int(document.id),
        lot_id=str(document.lot_id),
        title=str(document.title)[:320],
        source_url=str(document.source_url)[:2_048],
        file_type=document.file_type,
        local_path=str(document.local_path),
        content_sha256=str(document.content_sha256),
        observed_at=_db_aware(document.downloaded_at)
        or _db_aware(document.created_at)
        or datetime.now(UTC),
        lot_context_json=_lot_context_json(
            land_rights=getattr(lot, "land_rights", None),
            lease_term_years=getattr(lot, "lease_term_years", None),
            purpose=getattr(lot, "purpose", None),
            intended_use=getattr(lot, "use_goal", None),
            area_ha=getattr(lot, "area_ha", None),
            cadastral_number=getattr(lot, "cadastre_number", None),
            guarantee_kzt=getattr(lot, "guarantee_kzt", None),
            annual_rent_kzt=getattr(lot, "annual_rent_kzt", None),
            additional_payment_kzt=getattr(lot, "additional_payment_kzt", None),
        ),
    )


def _upsert_pending_state(session: Session, document: AuctionDocument) -> bool:
    item = _work_item(document)
    if item is None:
        return False
    state = session.get(AuctionDocumentExtractionState, item.document_id)
    if state is None:
        session.add(
            AuctionDocumentExtractionState(
                document_id=item.document_id,
                lot_id=item.lot_id,
                document_signature=item.signature,
                content_hash=item.content_sha256,
                document_path=item.local_path,
                extractor_version=EXTRACTOR_VERSION,
                writer_version=WRITER_VERSION,
                status="pending",
            )
        )
        return True
    if (
        state.document_signature == item.signature
        and state.content_hash == item.content_sha256
        and state.document_path == item.local_path
        and state.extractor_version == EXTRACTOR_VERSION
        and state.writer_version == WRITER_VERSION
        and state.lot_id == item.lot_id
    ):
        return False
    state.lot_id = item.lot_id
    state.document_signature = item.signature
    state.content_hash = item.content_sha256
    state.document_path = item.local_path
    state.extractor_version = EXTRACTOR_VERSION
    state.writer_version = WRITER_VERSION
    state.status = "pending"
    state.attempts = 0
    state.next_attempt_at = None
    state.claim_token = None
    state.claim_expires_at = None
    state.last_error_code = None
    state.last_error_message = None
    return True


def mark_document_extraction_pending(session: Session, document_id: int) -> bool:
    """Event-driven hook for the downloader after storage/hash/path changes."""
    if not isinstance(document_id, int) or isinstance(document_id, bool) or document_id < 1:
        raise DocumentExtractionWriterError("invalid document id")
    document = session.get(AuctionDocument, document_id)
    return _upsert_pending_state(session, document) if document is not None else False


def reconcile_document_extraction_states(
    session: Session,
    *,
    checked_at: datetime,
    limit: int = 100,
) -> int:
    """Bounded durable backfill/watermark scan; never runs an unbounded missing-state join."""
    checked = _db_aware(checked_at)
    if checked is None:
        raise DocumentExtractionWriterError("checked_at must be timezone-aware")
    bounded = max(1, min(int(limit), MAX_SCAN))
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "auction-document-extraction-cursor:default"},
        )
    cursor = session.get(
        AuctionDocumentExtractionCursor,
        "default",
        with_for_update=True,
    )
    if cursor is None:
        cursor = AuctionDocumentExtractionCursor(cursor_key="default")
        session.add(cursor)
        session.flush()
    base = (
        select(AuctionDocument)
        .where(
            AuctionDocument.storage_status == "downloaded",
            AuctionDocument.local_path.is_not(None),
            AuctionDocument.content_sha256.is_not(None),
            AuctionDocument.file_type.in_(tuple(sorted(TEXT_FILE_TYPES))),
        )
    )
    if not cursor.backfill_complete:
        query = (
            base.where(AuctionDocument.id > cursor.backfill_document_id)
            .order_by(_image_priority_expression().asc(), _legal_relevance_expression().asc(), AuctionDocument.id.asc())
            .limit(bounded + 1)
        )
    else:
        watermark = _db_aware(cursor.watermark_downloaded_at)
        query = base.where(AuctionDocument.downloaded_at.is_not(None))
        if watermark is not None:
            query = query.where(
                or_(
                    AuctionDocument.downloaded_at > watermark,
                    and_(
                        AuctionDocument.downloaded_at == watermark,
                        AuctionDocument.id > cursor.watermark_document_id,
                    ),
                )
            )
        query = query.order_by(
            _image_priority_expression().asc(),
            AuctionDocument.downloaded_at.asc(),
            AuctionDocument.id.asc(),
        ).limit(bounded + 1)
    rows = list(session.scalars(query))
    selected = rows[:bounded]
    changed = sum(_upsert_pending_state(session, document) for document in selected)
    if not cursor.backfill_complete:
        if selected:
            cursor.backfill_document_id = int(selected[-1].id)
        if len(rows) <= bounded:
            cursor.backfill_complete = True
            cursor.watermark_downloaded_at = checked
            cursor.watermark_document_id = 0
    elif selected:
        last = selected[-1]
        cursor.watermark_downloaded_at = _db_aware(last.downloaded_at)
        cursor.watermark_document_id = int(last.id)
    return changed


def _bounded_result(
    result: DocumentExtractionResult,
    *,
    item: DocumentWorkItem,
    actual_hash: str,
    extracted_at: datetime,
) -> DocumentExtractionResult:
    try:
        payload, _, _ = _immutable_extraction_payload(
            item,
            result,
            actual_hash=actual_hash,
            extracted_at=extracted_at,
        )
        _strict_payload(payload)
        return result
    except DocumentExtractionWriterError:
        return DocumentExtractionResult(
            status="oversized",
            candidates=(),
            conflicts=(),
            content_hash=actual_hash,
            pages_processed=result.pages_processed,
            text_chars_processed=result.text_chars_processed,
            detail="structured extraction exceeded immutable evidence byte budget",
        )


def _idempotency_key(
    item: DocumentWorkItem,
    *,
    actual_hash: str,
    evidence_status: str,
    extraction_status: str,
) -> str:
    material = (
        f"{WRITER_VERSION}:{EXTRACTOR_VERSION}:{item.document_id}:"
        f"{item.content_sha256}:{actual_hash}:{evidence_status}:{extraction_status}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _immutable_extraction_payload(
    item: DocumentWorkItem,
    result: DocumentExtractionResult,
    *,
    actual_hash: str,
    extracted_at: datetime,
) -> tuple[dict[str, object], str, str]:
    """Build the exact persisted envelope so byte limits include provenance."""
    evidence_status = "conflict" if actual_hash != item.content_sha256 else "found"
    key = _idempotency_key(
        item,
        actual_hash=actual_hash,
        evidence_status=evidence_status,
        extraction_status=result.status,
    )
    return (
        {
            "document_id": str(item.document_id),
            # This is the authoritative inventory hash used by coverage reconciliation.
            "content_sha256": item.content_sha256,
            "actual_content_sha256": actual_hash,
            "document_title": item.title,
            "document_source_url": item.source_url,
            "extracted_at": extracted_at.isoformat(),
            "extractor_version": EXTRACTOR_VERSION,
            "writer_version": WRITER_VERSION,
            "idempotency_key": key,
            "result": result.as_dict(),
        },
        key,
        evidence_status,
    )


def _retry_delay(attempts: int) -> int:
    return min(MAX_RETRY_SECONDS, 60 * (2 ** min(max(attempts - 1, 0), 8)))


def _claim_document(
    session: Session,
    item: DocumentWorkItem,
    *,
    checked_at: datetime,
    revalidate_after: timedelta,
) -> tuple[_ClaimedDocument | None, DocumentExtractionOutcome | None]:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"document-extraction-state:{item.document_id}"},
        )
    current = session.get(AuctionDocument, item.document_id)
    if (
        current is None
        or current.lot_id != item.lot_id
        or current.storage_status != "downloaded"
        or current.local_path != item.local_path
        or current.content_sha256 != item.content_sha256
    ):
        return None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "stale_document",
            retryable=True,
            retry_after_seconds=1,
            error_code="document_changed_before_claim",
        )
    state = session.get(AuctionDocumentExtractionState, item.document_id, with_for_update=True)
    if state is None:
        state = AuctionDocumentExtractionState(
            document_id=item.document_id,
            lot_id=item.lot_id,
            document_signature=item.signature,
            content_hash=item.content_sha256,
            document_path=item.local_path,
            extractor_version=EXTRACTOR_VERSION,
            writer_version=WRITER_VERSION,
            status="pending",
        )
        session.add(state)
        session.flush()
    same_signature = (
        state.document_signature == item.signature
        and state.content_hash == item.content_sha256
        and state.document_path == item.local_path
        and state.extractor_version == EXTRACTOR_VERSION
        and state.writer_version == WRITER_VERSION
    )
    if same_signature and state.status == "terminal":
        return None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "already_current",
            evidence_id=state.current_evidence_id,
        )
    validation_cutoff = checked_at - revalidate_after
    last_validated = _db_aware(state.last_validated_at)
    if (
        same_signature
        and state.status == "ready"
        and last_validated is not None
        and last_validated > validation_cutoff
    ):
        return None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "already_current",
            evidence_id=state.current_evidence_id,
        )
    next_attempt = _db_aware(state.next_attempt_at)
    if (
        same_signature
        and state.status == "retryable"
        and next_attempt is not None
        and next_attempt > checked_at
    ):
        return None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "retry_deferred",
            evidence_id=state.current_evidence_id,
            retryable=True,
            retry_after_seconds=max(1, int((next_attempt - checked_at).total_seconds())),
            error_code=state.last_error_code,
        )
    claim_expires = _db_aware(state.claim_expires_at)
    if (
        same_signature
        and state.status == "processing"
        and claim_expires is not None
        and claim_expires > checked_at
    ):
        return None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "claim_busy",
            retryable=True,
            retry_after_seconds=max(1, int((claim_expires - checked_at).total_seconds())),
            error_code="document_extraction_claim_busy",
        )
    if not same_signature:
        state.attempts = 0
    validation_only = same_signature and state.status == "ready"
    token = str(uuid.uuid4())
    state.lot_id = item.lot_id
    state.document_signature = item.signature
    state.content_hash = item.content_sha256
    state.document_path = item.local_path
    state.extractor_version = EXTRACTOR_VERSION
    state.writer_version = WRITER_VERSION
    state.status = "processing"
    state.attempts = min(10_000, state.attempts + 1)
    state.next_attempt_at = None
    state.claim_token = token
    state.claim_expires_at = checked_at + CLAIM_TTL
    state.last_error_code = None
    state.last_error_message = None
    return _ClaimedDocument(item, token, validation_only=validation_only), None


def _finalize_without_evidence(
    session: Session,
    claimed: _ClaimedDocument,
    outcome: DocumentExtractionOutcome,
    *,
    checked_at: datetime,
) -> DocumentExtractionOutcome:
    state = session.get(
        AuctionDocumentExtractionState,
        claimed.item.document_id,
        with_for_update=True,
    )
    if state is None or state.claim_token != claimed.claim_token:
        return DocumentExtractionOutcome(
            claimed.item.document_id,
            claimed.item.lot_id,
            "claim_lost",
            retryable=True,
            retry_after_seconds=1,
            error_code="document_extraction_claim_lost",
        )
    if outcome.status == "validated_current":
        state.status = "ready"
        state.next_attempt_at = None
        state.claim_token = None
        state.claim_expires_at = None
        state.last_validated_at = checked_at
        state.last_error_code = None
        state.last_error_message = None
        return DocumentExtractionOutcome(
            outcome.document_id,
            outcome.lot_id,
            "validated_current",
            evidence_id=state.current_evidence_id,
        )
    state.status = "retryable" if outcome.retryable else "terminal"
    state.next_attempt_at = (
        checked_at + timedelta(seconds=_retry_delay(state.attempts))
        if outcome.retryable
        else None
    )
    state.claim_token = None
    state.claim_expires_at = None
    state.last_validated_at = checked_at if not outcome.retryable else state.last_validated_at
    state.last_error_code = outcome.error_code
    state.last_error_message = outcome.error_code
    retry_after = (
        _retry_delay(state.attempts) if outcome.retryable else outcome.retry_after_seconds
    )
    return DocumentExtractionOutcome(
        outcome.document_id,
        outcome.lot_id,
        outcome.status,
        evidence_id=outcome.evidence_id,
        extraction_status=outcome.extraction_status,
        retryable=outcome.retryable,
        retry_after_seconds=retry_after,
        error_code=outcome.error_code,
    )


def _persist_immutable_extraction(
    session: Session,
    claimed: _ClaimedDocument,
    result: DocumentExtractionResult,
    *,
    actual_hash: str,
    extracted_at: datetime,
) -> DocumentExtractionOutcome:
    item = claimed.item
    payload, key, evidence_status = _immutable_extraction_payload(
        item,
        result,
        actual_hash=actual_hash,
        extracted_at=extracted_at,
    )
    current = session.get(AuctionDocument, item.document_id)
    state = session.get(AuctionDocumentExtractionState, item.document_id, with_for_update=True)
    if (
        current is None
        or current.lot_id != item.lot_id
        or current.storage_status != "downloaded"
        or current.local_path != item.local_path
        or current.content_sha256 != item.content_sha256
        or state is None
        or state.claim_token != claimed.claim_token
    ):
        if state is not None and state.claim_token == claimed.claim_token:
            state.status = "retryable"
            state.next_attempt_at = extracted_at + timedelta(seconds=1)
            state.claim_token = None
            state.claim_expires_at = None
            state.last_error_code = "document_changed_during_extraction"
        return DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "stale_document",
            retryable=True,
            retry_after_seconds=1,
            error_code="document_changed_during_extraction",
        )
    marker = f"idempotency:{key}"
    if state.current_evidence_hash == key and state.current_evidence_id is not None:
        if evidence_status == "conflict":
            state.status = "retryable"
            state.next_attempt_at = extracted_at + timedelta(
                seconds=_retry_delay(state.attempts)
            )
            state.last_error_code = "local_content_hash_mismatch"
            state.last_error_message = result.detail
        elif result.status == "ok":
            state.status = "ready"
            state.next_attempt_at = None
            state.last_error_code = None
            state.last_error_message = None
        else:
            state.status = "terminal"
            state.next_attempt_at = None
            state.last_error_code = f"extraction_{result.status}"
            state.last_error_message = result.detail
        state.claim_token = None
        state.claim_expires_at = None
        state.last_validated_at = extracted_at
        return DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "already_current",
            evidence_id=int(state.current_evidence_id),
            extraction_status=result.status,
        )
    rendered = _strict_payload(payload)
    evidence = AuctionEvidence(
        lot_id=item.lot_id,
        evidence_type=EVIDENCE_TYPE,
        status=evidence_status,
        title=f"Извлечение документа #{item.document_id}: {result.status}"[:320],
        value_text=marker,
        source_url=item.source_url,
        confidence=1.0 if evidence_status == "found" and result.status == "ok" else 0.0,
        raw_payload_json=rendered,
        observed_at=extracted_at,
    )
    session.add(evidence)
    session.flush()
    state.current_evidence_id = int(evidence.id)
    state.current_evidence_hash = key
    state.claim_token = None
    state.claim_expires_at = None
    state.last_validated_at = extracted_at
    if evidence_status == "conflict":
        state.status = "retryable"
        state.next_attempt_at = extracted_at + timedelta(seconds=_retry_delay(state.attempts))
        state.last_error_code = "local_content_hash_mismatch"
        state.last_error_message = result.detail
    elif result.status == "ok":
        state.status = "ready"
        state.next_attempt_at = None
        state.last_error_code = None
        state.last_error_message = None
    else:
        state.status = "terminal"
        state.next_attempt_at = None
        state.last_error_code = f"extraction_{result.status}"
        state.last_error_message = result.detail
    return DocumentExtractionOutcome(
        item.document_id,
        item.lot_id,
        "written",
        evidence_id=int(evidence.id),
        extraction_status=result.status,
    )


def _extract_one(
    item: DocumentWorkItem,
    *,
    storage_root: Path,
    extracted_at: datetime,
    extractor: Callable[..., DocumentExtractionResult],
    validation_only: bool = False,
) -> tuple[DocumentExtractionResult | None, str | None, DocumentExtractionOutcome | None]:
    path = _safe_local_path(storage_root, item.local_path)
    if path is None:
        return None, None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "terminal_error",
            error_code="unsafe_local_path",
        )
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            if size > MAX_HASH_BYTES:
                return None, None, DocumentExtractionOutcome(
                    item.document_id,
                    item.lot_id,
                    "terminal_error",
                    error_code="local_file_exceeds_hash_bound",
                )
            digest = hashlib.sha256()
            consumed = 0
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    consumed += len(chunk)
                    if consumed > MAX_HASH_BYTES:
                        return None, None, DocumentExtractionOutcome(
                            item.document_id,
                            item.lot_id,
                            "terminal_error",
                            error_code="local_file_exceeds_hash_bound",
                        )
                    digest.update(chunk)
            if consumed != size:
                return None, None, DocumentExtractionOutcome(
                    item.document_id,
                    item.lot_id,
                    "retryable_error",
                    retryable=True,
                    retry_after_seconds=60,
                    error_code="local_file_changed_while_hashing",
                )
            actual_hash = digest.hexdigest()
            result = DocumentExtractionResult(
                "oversized", (), (), actual_hash, 0, 0, "file exceeds writer byte limit"
            )
            return result, actual_hash, None
        data = path.read_bytes()
    except OSError:
        return None, None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "retryable_error",
            retryable=True,
            retry_after_seconds=60,
            error_code="local_file_unavailable",
        )
    if len(data) > MAX_FILE_BYTES:
        return None, None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "retryable_error",
            retryable=True,
            retry_after_seconds=60,
            error_code="local_file_changed_while_reading",
        )
    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != item.content_sha256:
        result = DocumentExtractionResult(
            "corrupt",
            (),
            (),
            actual_hash,
            0,
            0,
            "local content hash differs from authoritative downloaded-document hash",
        )
        return result, actual_hash, None
    if validation_only:
        return None, None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "validated_current",
        )
    try:
        lot_context = json.loads(item.lot_context_json)
    except json.JSONDecodeError:
        lot_context = {}
    metadata = DocumentMetadata(
        document_id=item.document_id,
        title=item.title,
        source_url=item.source_url,
        file_type=item.file_type,
        observed_at=item.observed_at,
        lot_context=lot_context if isinstance(lot_context, dict) else {},
    )
    limits = ExtractionLimits(
        max_file_bytes=MAX_FILE_BYTES,
        max_candidates=40,
        max_excerpt_chars=160,
    )
    try:
        result = extractor(data, metadata, limits=limits, extracted_at=extracted_at)
    except Exception:
        return None, None, DocumentExtractionOutcome(
            item.document_id,
            item.lot_id,
            "retryable_error",
            retryable=True,
            retry_after_seconds=60,
            error_code="extractor_runtime_error",
        )
    return (
        _bounded_result(
            result,
            item=item,
            actual_hash=actual_hash,
            extracted_at=extracted_at,
        ),
        actual_hash,
        None,
    )


def extract_downloaded_auction_documents(
    session_factory: Callable[[], Session],
    *,
    storage_root: Path | str,
    limit: int = 10,
    after_document_id: int = 0,
    now: datetime | None = None,
    revalidate_after: timedelta = DEFAULT_REVALIDATE_AFTER,
    extractor: Callable[..., DocumentExtractionResult] = extract_auction_document,
) -> DocumentExtractionBatchResult:
    """Worker-only keyset batch; no database transaction is open during file parsing."""
    bounded_limit = max(1, min(int(limit), MAX_BATCH))
    extracted_at = now or datetime.now(UTC)
    if extracted_at.tzinfo is None or extracted_at.utcoffset() is None:
        raise DocumentExtractionWriterError("now must be timezone-aware")
    root = Path(storage_root).resolve()
    with _SQLITE_LOCK:
        with session_factory() as session:
            reconcile_document_extraction_states(
                session,
                checked_at=extracted_at,
                limit=max(bounded_limit, min(MAX_SCAN, bounded_limit * 5)),
            )
            session.flush()
            worklist = read_document_extraction_worklist(
                session,
                after_document_id=after_document_id,
                scan_limit=max(bounded_limit, min(MAX_SCAN, bounded_limit * 5)),
                checked_at=extracted_at,
                revalidate_after=revalidate_after,
            )
            session.commit()
    has_more = len(worklist) > bounded_limit
    selected = worklist[:bounded_limit]
    outcomes: list[DocumentExtractionOutcome] = []
    touched_lots: set[str] = set()
    for item in selected:
        with _SQLITE_LOCK:
            with session_factory() as session:
                claimed, claim_outcome = _claim_document(
                    session,
                    item,
                    checked_at=extracted_at,
                    revalidate_after=revalidate_after,
                )
                session.commit()
        if claim_outcome is not None:
            outcomes.append(claim_outcome)
            touched_lots.add(item.lot_id)
            continue
        if claimed is None:
            raise DocumentExtractionWriterError("document claim returned invalid state")
        result, actual_hash, immediate = _extract_one(
            item,
            storage_root=root,
            extracted_at=extracted_at,
            extractor=extractor,
            validation_only=claimed.validation_only,
        )
        if immediate is not None:
            with _SQLITE_LOCK:
                with session_factory() as session:
                    finalized = _finalize_without_evidence(
                        session,
                        claimed,
                        immediate,
                        checked_at=extracted_at,
                    )
                    session.commit()
            outcomes.append(finalized)
            touched_lots.add(item.lot_id)
            continue
        if result is None or actual_hash is None:
            raise DocumentExtractionWriterError("extractor returned an invalid writer state")
        with _SQLITE_LOCK:
            with session_factory() as session:
                outcome = _persist_immutable_extraction(
                    session,
                    claimed,
                    result,
                    actual_hash=actual_hash,
                    extracted_at=extracted_at,
                )
                session.commit()
        outcomes.append(outcome)
        touched_lots.add(item.lot_id)

    coverage_results: list[LotCoverageReconciliation] = []
    for lot_id in sorted(touched_lots):
        try:
            with session_factory() as session:
                coverage_inputs = load_contract_coverage_inputs(session, lot_id)
                session.commit()
            coverage = build_authoritative_contract_coverage(
                coverage_inputs,
                assembled_at=extracted_at,
            )
            coverage_results.append(LotCoverageReconciliation(lot_id, coverage))
        except DecisionInputProducerError as exc:
            coverage_results.append(
                LotCoverageReconciliation(lot_id, None, str(exc)[:240])
            )
    written = sum(item.status == "written" for item in outcomes)
    already = sum(item.status == "already_current" for item in outcomes)
    retryable = sum(item.retryable for item in outcomes)
    terminal = sum(
        item.status == "terminal_error"
        or (
            item.status == "written"
            and item.extraction_status
            in {"unsupported", "oversized", "encrypted", "corrupt"}
        )
        for item in outcomes
    )
    next_after = selected[-1].document_id if selected else None
    return DocumentExtractionBatchResult(
        selected=len(selected),
        written=written,
        already_current=already,
        retryable_errors=retryable,
        terminal_results=terminal,
        outcomes=tuple(outcomes),
        coverage=tuple(coverage_results),
        next_after_document_id=next_after,
        has_more=has_more,
    )
