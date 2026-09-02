from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import auction_v2
from app.auction_v2 import sync_auction_v2_documents
from app.db import Base
from app.models import AuctionDocument, AuctionLot


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _lot(*, active: bool, status: str | None, document_status: str) -> AuctionLot:
    lot_id = str(uuid.uuid4())
    lot = AuctionLot(
        id=lot_id,
        source="e-qazyna",
        source_lot_id=f"source-{lot_id}",
        title="Земельный участок",
        source_url=f"https://sauda.e-qazyna.kz/ru/auction/{lot_id}",
        object_type="land",
        active=active,
        source_search_status=status,
        created_at=NOW,
    )
    lot.documents.append(
        AuctionDocument(
            title="Проект договора аренды.pdf",
            source_url=f"https://sauda.e-qazyna.kz/files/{lot_id}.pdf",
            file_type="pdf",
            storage_status=document_status,
            created_at=NOW,
        )
    )
    return lot


def test_document_download_prioritizes_linked_live_lot_over_failed_archive(
    monkeypatch, tmp_path
) -> None:
    requested: list[str] = []

    def fake_download(_client, document, **_kwargs):
        requested.append(document.source_url)
        return b"%PDF-1.4\nlegal terms"

    monkeypatch.setattr(auction_v2, "_download_auction_document_content", fake_download)
    monkeypatch.setattr(auction_v2.settings, "auction_v2_document_storage_dir", str(tmp_path))

    with build_session() as session:
        archived = _lot(active=False, status=None, document_status="failed")
        live = _lot(active=True, status="ApplicationsAccept", document_status="linked")
        session.add_all([archived, live])
        session.commit()

        transport = httpx.MockTransport(lambda _request: httpx.Response(500))
        with httpx.Client(transport=transport) as client:
            result = sync_auction_v2_documents(
                session,
                limit=1,
                enabled=True,
                client=client,
            )

        assert result.checked == 1
        assert result.downloaded == 1
        assert requested == [live.documents[0].source_url]
        assert live.documents[0].storage_status == "downloaded"
        assert archived.documents[0].storage_status == "failed"


def test_continuation_mode_does_not_retry_failed_signed_urls(monkeypatch, tmp_path) -> None:
    def fail_download(*_args, **_kwargs):
        raise AssertionError("failed URL must not be retried by the extraction continuation")

    monkeypatch.setattr(auction_v2, "_download_auction_document_content", fail_download)
    monkeypatch.setattr(auction_v2.settings, "auction_v2_document_storage_dir", str(tmp_path))

    with build_session() as session:
        archived = _lot(active=False, status=None, document_status="failed")
        session.add(archived)
        session.commit()

        result = sync_auction_v2_documents(
            session,
            limit=10,
            enabled=True,
            retry_failed=False,
        )

        assert result.checked == 0
        assert result.downloaded == 0
        assert result.errors == 0
        assert archived.documents[0].storage_status == "failed"
