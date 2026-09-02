from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.orm import Session

from app.auction_spatial_evidence_store import SqlAlchemySpatialEvidenceStore
from app.auction_spatial_evidence_writer import (
    SpatialEvidenceWriterError,
    SpatialFeedIdentity,
    SpatialManifestExpectation,
    SpatialProcessingFailure,
    event_signature,
    prepare_spatial_observation,
    spatial_adapter_generation,
)
from app.auction_spatial_source_adapters import (
    PRODUCER_VERSION,
    SpatialSourceEnvelope,
    SpatialTrustedReceipt,
)
from app.config import settings
from app.db import Base
from app.models import (
    AuctionEvidence,
    AuctionLot,
    AuctionSpatialDecisionSignal,
    AuctionSpatialFeedState,
    AuctionSpatialGenerationManifest,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
LOT_ID = "lot-spatial-store"
REGISTRY_VERSION = "abay-gis/2026.1"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        value.add(
            AuctionLot(
                id=LOT_ID,
                source="e-qazyna",
                source_lot_id="452662",
                title="Lot 452662",
                source_url="https://e-qazyna.kz/452662",
            )
        )
        value.commit()
        yield value


def identity(module: str) -> SpatialFeedIdentity:
    return SpatialFeedIdentity(LOT_ID, module, "abay-gis", f"{module}-feed")


def expectation() -> SpatialManifestExpectation:
    return SpatialManifestExpectation(
        LOT_ID,
        {
            "restrictions": (identity("restrictions").key,),
            "site": (identity("site").key,),
            "planning": (identity("planning").key,),
        },
        "spatial-manifest/2026.1",
    )


def prepared(module: str, marker: str, *, status: str = "found", expiry_days: int = 7):
    canonical = marker * 64
    receipt_hash = chr(ord(marker) + 1) * 64
    receipt = SpatialTrustedReceipt(
        "abay-gis",
        f"{module}-feed",
        receipt_hash,
        canonical,
        "signed_feed",
    )
    generation = spatial_adapter_generation(canonical, receipt_hash, REGISTRY_VERSION)
    envelope = SpatialSourceEnvelope(
        module,
        {"generation_id": generation, "marker": marker},
        status,
        NOW - timedelta(hours=1),
        f"https://gis.gov.kz/{module}",
        generation,
        PRODUCER_VERSION,
    )
    return prepare_spatial_observation(
        identity=identity(module),
        envelope=envelope,
        canonical_feed_sha256=canonical,
        receipt=receipt,
        registry_version=REGISTRY_VERSION,
        expires_at=NOW + timedelta(days=expiry_days),
        prepared_at=NOW,
    )


def dirty(item) -> str:
    return event_signature(
        lot_id=item.identity.lot_id,
        module=item.identity.module,
        provider_id=item.identity.provider_id,
        feed_id=item.identity.feed_id,
        source_version=item.envelope.producer_version,
        content_sha256=item.canonical_feed_sha256,
    )


def write(store, item):
    expected = expectation()
    pending = store.mark_pending(
        item.identity,
        expected,
        input_signature=dirty(item),
        changed_at=NOW,
    )
    claims = store.claim_due(checked_at=NOW, limit=10, owner_token="worker").claims
    claim = next(value for value in claims if value.identity == item.identity)
    return pending, store.persist_observation_atomic(claim, item, expected, checked_at=NOW)


def test_atomic_trio_persists_evidence_manifest_and_after_commit_outbox(session) -> None:
    store = SqlAlchemySpatialEvidenceStore(session)
    _, first = write(store, prepared("restrictions", "a"))
    _, second = write(store, prepared("site", "c"))
    _, third = write(store, prepared("planning", "e"))
    assert not first.enqueue_w14 and not second.enqueue_w14
    assert third.enqueue_w14 and third.manifest.status == "complete"

    assert session.scalar(select(func.count(AuctionEvidence.id))) == 3
    manifest = session.get(AuctionSpatialGenerationManifest, LOT_ID)
    assert manifest is not None and manifest.settled and manifest.watermark == 4
    signals = list(session.scalars(select(AuctionSpatialDecisionSignal)))
    assert len(signals) == 1
    assert signals[0].manifest_watermark == manifest.watermark
    assert signals[0].status == "pending"


def test_complete_to_pending_and_retry_each_create_durable_invalidation(session) -> None:
    store = SqlAlchemySpatialEvidenceStore(session)
    for module, marker in (("restrictions", "a"), ("site", "c"), ("planning", "e")):
        write(store, prepared(module, marker))
    changed = prepared("planning", "7")
    pending = store.mark_pending(
        changed.identity,
        expectation(),
        input_signature=dirty(changed),
        changed_at=NOW,
    )
    assert pending.enqueue_w14 and pending.manifest.status == "incomplete"
    claim = store.claim_due(checked_at=NOW, limit=10, owner_token="failure").claims[0]
    failed = store.persist_failure_atomic(
        claim,
        SpatialProcessingFailure("upstream_503", "temporary", True),
        expectation(),
        checked_at=NOW,
    )
    assert failed.enqueue_w14 and failed.manifest.status == "conflict"

    signals = list(
        session.scalars(
            select(AuctionSpatialDecisionSignal).order_by(AuctionSpatialDecisionSignal.id)
        )
    )
    assert len(signals) == 3
    assert len({item.manifest_watermark for item in signals}) == 3


def test_expiry_invalidates_manifest_before_claim_is_returned(session) -> None:
    store = SqlAlchemySpatialEvidenceStore(session)
    write(store, prepared("restrictions", "a"))
    write(store, prepared("site", "c", expiry_days=1))
    write(store, prepared("planning", "e"))
    due = store.claim_due(
        checked_at=NOW + timedelta(days=1), limit=10, owner_token="expiry"
    )
    assert any(claim.identity.module == "site" for claim in due.claims)
    assert len(due.invalidated_manifests) == 1
    assert due.invalidated_manifests[0].status == "conflict"

    manifest = session.get(AuctionSpatialGenerationManifest, LOT_ID)
    assert manifest is not None and manifest.status == "conflict"
    assert session.scalar(select(func.count(AuctionSpatialDecisionSignal.id))) == 2


def test_claim_for_a_rejects_b_and_a_to_b_to_a_is_immutable(session) -> None:
    store = SqlAlchemySpatialEvidenceStore(session)
    source_a = prepared("restrictions", "a")
    source_b = prepared("restrictions", "c", status="conflict")
    store.mark_pending(
        source_a.identity,
        expectation(),
        input_signature=dirty(source_a),
        changed_at=NOW,
    )
    claim = store.claim_due(checked_at=NOW, limit=1, owner_token="worker").claims[0]
    with pytest.raises(SpatialEvidenceWriterError, match="does not bind"):
        store.persist_observation_atomic(claim, source_b, expectation(), checked_at=NOW)
    session.rollback()

    # The governing claim remains usable after the rejected pre-transaction payload.
    store.persist_observation_atomic(claim, source_a, expectation(), checked_at=NOW)
    _, middle = write(store, source_b)
    _, restored = write(store, source_a)
    assert middle.manifest.status == "conflict"
    assert restored.status == "written"
    rows = list(
        session.scalars(
            select(AuctionEvidence)
            .where(AuctionEvidence.lot_id == LOT_ID)
            .order_by(AuctionEvidence.id)
        )
    )
    assert [row.status for row in rows] == ["found", "conflict", "found"]


def test_schema_contract_and_migration_lineage() -> None:
    table = AuctionSpatialFeedState.__table__
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes
    }
    assert indexes["ix_auction_spatial_feed_retry_due"] == (
        "status",
        "next_attempt_at",
        "id",
    )
    assert indexes["ix_auction_spatial_feed_claim_due"] == (
        "status",
        "claim_expires_at",
        "id",
    )
    assert indexes["ix_auction_spatial_feed_validation_due"] == (
        "status",
        "next_validation_at",
        "expires_at",
        "id",
    )
    migration = Path(
        "migrations/versions/ec8a2f4d6b91_auction_spatial_evidence_state.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "ec8a2f4d6b91"' in migration
    assert 'down_revision: str | Sequence[str] | None = "da7c4e9b1f62"' in migration
    assert "uq_auction_spatial_signal_watermark" in migration


def test_claim_sql_locks_lot_before_full_feed_row(session) -> None:
    store = SqlAlchemySpatialEvidenceStore(session)
    item = prepared("planning", "a")
    store.mark_pending(
        item.identity,
        expectation(),
        input_signature=dirty(item),
        changed_at=NOW,
    )
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(session.get_bind(), "before_cursor_execute", capture)
    try:
        result = store.claim_due(checked_at=NOW, limit=1, owner_token="lock-order")
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", capture)
    assert result.claims
    lot_lock = next(index for index, sql in enumerate(statements) if "from auction_lots" in sql)
    full_feed_lock = next(
        index
        for index, sql in enumerate(statements)
        if index > lot_lock
        and "from auction_spatial_feed_states" in sql
        and "provider_id" in sql
    )
    assert lot_lock < full_feed_lock


def test_real_alembic_upgrade_downgrade_upgrade_roundtrip(tmp_path, monkeypatch) -> None:
    database = tmp_path / "spatial-migration.sqlite3"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{database.as_posix()}")
    monkeypatch.setattr(settings, "app_env", "test")
    config = Config("alembic.ini")
    command.upgrade(config, "ec8a2f4d6b91")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    db_inspector = inspect(engine)
    assert "manifest_watermark" not in {
        column["name"]
        for column in db_inspector.get_columns("auction_spatial_generation_manifests")
    }
    assert "manifest_watermark" in {
        column["name"]
        for column in db_inspector.get_columns("auction_spatial_decision_signals")
    }
    command.downgrade(config, "da7c4e9b1f62")
    db_inspector.clear_cache()
    assert "auction_spatial_feed_states" not in db_inspector.get_table_names()
    command.upgrade(config, "ec8a2f4d6b91")
    db_inspector.clear_cache()
    assert "auction_spatial_feed_states" in db_inspector.get_table_names()
