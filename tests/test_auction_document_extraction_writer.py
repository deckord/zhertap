from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.auction_decision_input_producers import (
    build_authoritative_contract_coverage,
    load_contract_coverage_inputs,
)
from app.auction_document_extraction_writer import (
    EVIDENCE_TYPE,
    EXTRACTOR_VERSION,
    WRITER_VERSION,
    extract_downloaded_auction_documents,
    mark_document_extraction_pending,
    read_document_extraction_worklist,
)
from app.auction_document_extractor import DocumentExtractionResult, extract_auction_document
from app.db import Base
from app.models import (
    AuctionDocument,
    AuctionDocumentExtractionCursor,
    AuctionDocumentExtractionState,
    AuctionEvidence,
    AuctionLot,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _docx(paragraphs: list[str]) -> bytes:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml.encode("utf-8"))
    return output.getvalue()


def _factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'writer.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_document(
    factory,
    storage_root: Path,
    *,
    content: bytes,
    document_id: int = 7,
    lot_id: str = "lot-452662",
    file_type: str = "docx",
) -> Path:
    path = storage_root / lot_id / f"{document_id}.{file_type}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    with factory() as session, session.begin():
        if session.get(AuctionLot, lot_id) is None:
            session.add(
                AuctionLot(
                    id=lot_id,
                    source="e-qazyna",
                    source_lot_id=lot_id,
                    title="Кемпинг",
                    source_url="https://sauda.e-qazyna.kz/452662",
                    guarantee_kzt=216_250,
                    additional_payment_kzt=16_200,
                    annual_rent_kzt=17_970,
                    last_seen_at=NOW,
                )
            )
        session.add(
            AuctionDocument(
                id=document_id,
                lot_id=lot_id,
                title="Проект договора аренды",
                source_url=f"https://sauda.e-qazyna.kz/documents/{document_id}",
                file_type=file_type,
                storage_status="downloaded",
                local_path=str(path),
                content_sha256=digest,
                downloaded_at=NOW,
            )
        )
    return path


def test_writer_extracts_452662_idempotently_and_reconciles_trusted_coverage(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    content = _docx(
        [
            "Срок аренды: 3 года.",
            "Дополнительный платеж: 16 200 тенге.",
            "Ежегодная арендная плата: 17 970 тенге.",
        ]
    )
    _seed_document(factory, storage_root, content=content)

    calls = 0

    def counting_extractor(*args, **kwargs):
        nonlocal calls
        calls += 1
        return extract_auction_document(*args, **kwargs)

    first = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW,
        extractor=counting_extractor,
    )
    second = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW + timedelta(minutes=1),
        extractor=counting_extractor,
    )

    assert first.written == 1
    assert first.outcomes[0].extraction_status == "ok"
    assert first.coverage[0].coverage is not None
    assert first.coverage[0].coverage.status == "complete"
    assert second.written == 0
    assert second.selected == 0
    assert calls == 1
    validation = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW + timedelta(hours=25),
        extractor=counting_extractor,
    )
    assert validation.outcomes[0].status == "validated_current"
    assert calls == 1
    with factory() as session:
        rows = list(
            session.scalars(
                select(AuctionEvidence).where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
            )
        )
    assert len(rows) == 1
    payload = json.loads(rows[0].raw_payload_json or "{}")
    assert payload["document_id"] == "7"
    assert payload["content_sha256"] == hashlib.sha256(content).hexdigest()
    fields = {item["field"] for item in payload["result"]["candidates"]}
    assert "lease_term_years" in fields
    assert "annual_payment_kzt" in fields
    assert rows[0].value_text and rows[0].value_text.startswith("idempotency:")


def test_writer_holds_no_database_transaction_during_file_extraction(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    content = _docx(["Срок аренды: 3 года."])
    _seed_document(factory, storage_root, content=content)

    def inspecting_extractor(*args, **kwargs):
        with factory() as probe:
            assert probe.scalar(select(func.count(AuctionDocument.id))) == 1
        return extract_auction_document(*args, **kwargs)

    result = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW,
        extractor=inspecting_extractor,
    )
    assert result.written == 1


def test_writer_bounds_the_complete_evidence_envelope_not_only_extractor_result(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    content = _docx(["Срок аренды: 3 года."])
    _seed_document(factory, storage_root, content=content)

    def envelope_overflow_extractor(*args, **kwargs):
        return DocumentExtractionResult(
            status="ok",
            candidates=(),
            conflicts=(),
            content_hash=hashlib.sha256(content).hexdigest(),
            pages_processed=1,
            text_chars_processed=1,
            summary="x" * 63_200,
        )

    result = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW,
        extractor=envelope_overflow_extractor,
    )

    assert result.written == 1
    assert result.terminal_results == 1
    assert result.outcomes[0].extraction_status == "oversized"
    with factory() as session:
        evidence = session.scalar(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
        )
        state = session.get(AuctionDocumentExtractionState, 7)
    assert evidence is not None
    assert len((evidence.raw_payload_json or "").encode("utf-8")) <= 64_000
    assert state is not None and state.status == "terminal"


def test_a_to_b_to_a_appends_reactivation_then_skips_current(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    original = _docx(["Срок аренды: 3 года."])
    path = _seed_document(factory, storage_root, content=original)
    first = extract_downloaded_auction_documents(
        factory, storage_root=storage_root, now=NOW
    )
    assert first.written == 1

    path.write_bytes(_docx(["Срок аренды: 5 лет."]))
    conflict = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW + timedelta(minutes=1),
        revalidate_after=timedelta(0),
    )
    path.write_bytes(original)
    restored = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW + timedelta(minutes=3),
        revalidate_after=timedelta(0),
    )
    no_op = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW + timedelta(minutes=3, seconds=1),
    )
    assert conflict.written == 1
    assert conflict.outcomes[0].extraction_status == "corrupt"
    assert conflict.coverage[0].coverage is not None
    assert conflict.coverage[0].coverage.status == "incomplete"
    assert conflict.coverage[0].coverage.coverage is not None
    assert conflict.coverage[0].coverage.coverage.processed_document_ids == ()
    assert restored.written == 1
    assert restored.coverage[0].coverage is not None
    assert restored.coverage[0].coverage.status == "complete"
    assert no_op.selected == 0
    with factory() as session:
        rows = list(
            session.scalars(
                select(AuctionEvidence)
                .where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
                .order_by(AuctionEvidence.id.asc())
            )
        )
    assert [row.status for row in rows] == ["found", "conflict", "found"]
    with factory() as session:
        state = session.get(AuctionDocumentExtractionState, 7)
        assert state is not None and state.status == "ready"
        assert state.current_evidence_id == rows[-1].id


def test_missing_local_file_is_retryable_and_writes_no_false_evidence(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    path = _seed_document(
        factory,
        storage_root,
        content=_docx(["Срок аренды: 3 года."]),
    )
    path.unlink()

    result = extract_downloaded_auction_documents(
        factory, storage_root=storage_root, now=NOW
    )
    assert result.retryable_errors == 1
    assert result.outcomes[0].error_code == "local_file_unavailable"
    assert result.outcomes[0].retry_after_seconds == 60
    deferred = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW + timedelta(seconds=30),
    )
    assert deferred.selected == 0
    with factory() as session:
        assert session.scalar(select(func.count(AuctionEvidence.id))) == 0


def test_terminal_path_failure_and_expired_claim_have_durable_worklist_semantics(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    _seed_document(
        factory,
        storage_root,
        content=_docx(["Срок аренды: 3 года."]),
    )
    with factory() as session, session.begin():
        document = session.get(AuctionDocument, 7)
        assert document is not None
        document.local_path = str(tmp_path / "outside.docx")
    first = extract_downloaded_auction_documents(
        factory, storage_root=storage_root, now=NOW
    )
    second = extract_downloaded_auction_documents(
        factory, storage_root=storage_root, now=NOW + timedelta(minutes=1)
    )
    assert first.terminal_results == 1
    assert first.outcomes[0].error_code == "unsafe_local_path"
    assert second.selected == 0

    with factory() as session, session.begin():
        document = session.get(AuctionDocument, 7)
        assert document is not None
        document.local_path = str(storage_root / "lot-452662" / "7.docx")
        session.flush()
        assert mark_document_extraction_pending(session, 7) is True
        item = read_document_extraction_worklist(
            session,
            checked_at=NOW + timedelta(minutes=2),
        )[0]
        state = session.get(AuctionDocumentExtractionState, 7)
        assert state is not None
        state.document_signature = item.signature
        state.content_hash = item.content_sha256
        state.document_path = item.local_path
        state.extractor_version = EXTRACTOR_VERSION
        state.writer_version = WRITER_VERSION
        state.status = "processing"
        state.claim_token = "expired-claim"
        state.claim_expires_at = NOW + timedelta(minutes=1)
    recovered = extract_downloaded_auction_documents(
        factory,
        storage_root=storage_root,
        now=NOW + timedelta(minutes=2),
    )
    assert recovered.written == 1
    with factory() as session:
        state = session.get(AuctionDocumentExtractionState, 7)
        assert state is not None and state.status == "ready"


def test_writer_worklist_includes_downloaded_jpeg(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    _seed_document(
        factory,
        storage_root,
        content=b"\xff\xd8\xff\xd9",
        document_id=9,
        file_type="jpg",
    )
    with factory() as session:
        assert mark_document_extraction_pending(session, 9) is True
        item = read_document_extraction_worklist(
            session,
            checked_at=NOW,
        )[0]

    assert item.document_id == 9
    assert item.file_type == "jpg"


def test_writer_worklist_prioritizes_image_scans_over_pdf_backlog(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    _seed_document(
        factory,
        storage_root,
        content=_docx(["PDF backlog"]),
        document_id=10,
        file_type="pdf",
    )
    _seed_document(
        factory,
        storage_root,
        content=b"\x89PNG\r\n\x1a\n",
        document_id=100,
        file_type="png",
    )
    with factory() as session, session.begin():
        assert mark_document_extraction_pending(session, 10) is True
        assert mark_document_extraction_pending(session, 100) is True
    with factory() as session:
        worklist = read_document_extraction_worklist(
            session,
            checked_at=NOW,
            scan_limit=10,
        )

    assert [item.document_id for item in worklist[:2]] == [100, 10]


def test_worklist_prioritizes_live_legal_documents_before_archive_and_generic_scans(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    content = _docx(["Условия документа"])
    _seed_document(
        factory,
        storage_root,
        content=content,
        document_id=1,
        lot_id="archive-lot",
        file_type="pdf",
    )
    _seed_document(
        factory,
        storage_root,
        content=b"\x89PNG\r\n\x1a\n",
        document_id=2,
        lot_id="live-lot",
        file_type="png",
    )
    _seed_document(
        factory,
        storage_root,
        content=content,
        document_id=3,
        lot_id="live-lot",
        file_type="pdf",
    )
    with factory() as session, session.begin():
        session.get(AuctionLot, "archive-lot").active = False
        session.get(AuctionLot, "live-lot").active = True
        session.get(AuctionDocument, 1).title = "проект договора аренды"
        session.get(AuctionDocument, 2).title = "схема участка"
        session.get(AuctionDocument, 3).title = "договор с обязательствами победителя"
        assert mark_document_extraction_pending(session, 1) is True
        assert mark_document_extraction_pending(session, 2) is True
        assert mark_document_extraction_pending(session, 3) is True

    with factory() as session:
        worklist = read_document_extraction_worklist(session, checked_at=NOW, scan_limit=10)

    assert [item.document_id for item in worklist[:3]] == [3, 2, 1]


def test_pending_extraction_is_not_displaced_by_lower_id_ready_revalidation(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    content = _docx(["Срок аренды: 3 года."])
    _seed_document(factory, storage_root, content=content, document_id=1)
    _seed_document(factory, storage_root, content=content, document_id=100)
    with factory() as session, session.begin():
        assert mark_document_extraction_pending(session, 1) is True
        assert mark_document_extraction_pending(session, 100) is True
        state = session.get(AuctionDocumentExtractionState, 1)
        state.status = "ready"
        state.last_validated_at = NOW - timedelta(days=2)

    with factory() as session:
        worklist = read_document_extraction_worklist(
            session,
            checked_at=NOW,
            revalidate_after=timedelta(hours=24),
            scan_limit=10,
        )

    assert [item.document_id for item in worklist[:2]] == [100, 1]


def test_more_than_48_old_rows_do_not_hide_new_current_extraction(tmp_path) -> None:
    factory = _factory(tmp_path)
    storage_root = tmp_path / "documents"
    content = _docx(["Срок аренды: 3 года."])
    _seed_document(factory, storage_root, content=content)
    digest = hashlib.sha256(content).hexdigest()
    old_payload = {
        "document_id": "7",
        "content_sha256": digest,
        "result": {"status": "ok", "candidates": [], "conflicts": []},
    }
    with factory() as session, session.begin():
        for index in range(49):
            session.add(
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type=EVIDENCE_TYPE,
                    status="found",
                    observed_at=NOW - timedelta(days=index + 1),
                    raw_payload_json=json.dumps(old_payload),
                )
            )
    written = extract_downloaded_auction_documents(
        factory, storage_root=storage_root, now=NOW
    )
    assert written.written == 1
    assert written.coverage[0].coverage is not None
    assert written.coverage[0].coverage.status == "complete"
    assert "extraction_history_truncated" in written.coverage[0].coverage.reasons

    with factory() as session:
        inputs = load_contract_coverage_inputs(session, "lot-452662")
    reconciled = build_authoritative_contract_coverage(inputs, assembled_at=NOW)
    assert reconciled.status == "complete"


def test_10k_quiescent_states_use_bounded_indexed_state_queries_and_no_extractor(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    engine = factory.kw["bind"]
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-scale",
                source="e-qazyna",
                source_lot_id="scale",
                title="Scale lot",
                source_url="https://example.test/scale",
                last_seen_at=NOW,
            )
        )
    documents = [
        {
            "id": index,
            "lot_id": "lot-scale",
            "title": f"Document {index}",
            "source_url": f"https://example.test/doc/{index}",
            "file_type": "docx",
            "storage_status": "downloaded",
            "local_path": str(tmp_path / "documents" / f"{index}.docx"),
            "content_sha256": f"{index:064x}"[-64:],
            "downloaded_at": NOW,
            "created_at": NOW,
        }
        for index in range(1, 10_001)
    ]
    states = [
        {
            "document_id": item["id"],
            "lot_id": "lot-scale",
            "document_signature": f"{item['id']:064x}"[-64:],
            "content_hash": item["content_sha256"],
            "document_path": item["local_path"],
            "extractor_version": EXTRACTOR_VERSION,
            "writer_version": WRITER_VERSION,
            "status": "ready",
            "attempts": 1,
            "last_validated_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }
        for item in documents
    ]
    with factory() as session, session.begin():
        session.bulk_insert_mappings(AuctionDocument, documents)
        session.bulk_insert_mappings(AuctionDocumentExtractionState, states)
        session.add(
            AuctionDocumentExtractionCursor(
                cursor_key="default",
                backfill_document_id=10_000,
                backfill_complete=True,
                watermark_downloaded_at=NOW,
                watermark_document_id=10_000,
            )
        )

    statements = 0

    def count_statement(*_args) -> None:
        nonlocal statements
        statements += 1

    event.listen(engine, "before_cursor_execute", count_statement)
    calls = 0

    def must_not_extract(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("quiescent state must not reach file extraction")

    try:
        result = extract_downloaded_auction_documents(
            factory,
            storage_root=tmp_path / "documents",
            now=NOW + timedelta(minutes=1),
            extractor=must_not_extract,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    assert result.selected == 0
    assert calls == 0
    assert statements <= 7

    with engine.connect() as connection:
        retry_plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT document_id "
            "FROM auction_document_extraction_states "
            "WHERE status = 'retryable' AND next_attempt_at <= ? "
            "ORDER BY next_attempt_at, document_id LIMIT 101",
            (NOW.replace(tzinfo=None),),
        ).all()
        validation_plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT document_id "
            "FROM auction_document_extraction_states "
            "WHERE status = 'ready' AND last_validated_at <= ? "
            "ORDER BY last_validated_at, document_id LIMIT 101",
            (NOW.replace(tzinfo=None),),
        ).all()
    assert "ix_auction_document_extraction_state_work" in str(retry_plan)
    assert "ix_auction_document_extraction_state_validation" in str(validation_plan)
