from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

VOLATILE_DOCUMENT_QUERY_PARAMS = {
    "_",
    "cache",
    "expires",
    "expires_at",
    "expiresat",
    "signature",
    "sig",
    "timestamp",
    "token",
    "ts",
}


@dataclass(slots=True)
class AuctionDocumentDedupResult:
    lots_checked: int = 0
    documents_before: int = 0
    documents_after: int = 0
    documents_removed: int = 0


def canonical_auction_document_url(source_url: str | None) -> str:
    text = str(source_url or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    stable_query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if name.casefold() not in VOLATILE_DOCUMENT_QUERY_PARAMS
    ]
    stable_query.sort(key=lambda item: (item[0].casefold(), item[1]))
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            urlencode(stable_query, doseq=True),
            "",
        )
    )


def auction_document_key(document_or_url: Any, title: str | None = None) -> str:
    source_url = (
        getattr(document_or_url, "source_url", None)
        if not isinstance(document_or_url, str)
        else document_or_url
    )
    canonical_url = canonical_auction_document_url(source_url)
    if canonical_url:
        return f"url:{canonical_url}"
    raw_title = title if title is not None else getattr(document_or_url, "title", "")
    normalized_title = re.sub(r"\s+", " ", str(raw_title or "")).strip().casefold()
    return f"title:{normalized_title}" if normalized_title else ""


def unique_auction_documents(documents: list[Any]) -> list[Any]:
    groups: dict[str, list[Any]] = {}
    order: dict[str, int] = {}
    for index, document in enumerate(documents):
        key = auction_document_key(document) or f"row:{index}"
        groups.setdefault(key, []).append(document)
        order.setdefault(key, index)
    keepers = {
        key: _preferred_document(group)
        for key, group in groups.items()
        if group
    }
    return [
        keepers[key]
        for key, _index in sorted(order.items(), key=lambda item: item[1])
        if key in keepers
    ]


def deduplicate_lot_documents(lot: Any) -> set[str]:
    documents = list(getattr(lot, "documents", []) or [])
    if len(documents) < 2:
        return set()
    groups: dict[str, list[Any]] = {}
    order: dict[str, int] = {}
    for index, document in enumerate(documents):
        key = auction_document_key(document) or f"row:{index}"
        groups.setdefault(key, []).append(document)
        order.setdefault(key, index)

    keepers: dict[str, Any] = {}
    removed_urls: set[str] = set()
    for key, group in groups.items():
        keeper = _preferred_document(group)
        keepers[key] = keeper
        for document in group:
            if document is keeper:
                continue
            _merge_document_metadata(keeper, document)
            source_url = str(getattr(document, "source_url", "") or "")
            if source_url:
                removed_urls.add(source_url)

    lot.documents = [
        keepers[key]
        for key, _index in sorted(order.items(), key=lambda item: item[1])
        if key in keepers
    ]
    return removed_urls


def deduplicate_auction_documents(
    session: Session,
    *,
    lot_id: str | None = None,
) -> AuctionDocumentDedupResult:
    from app.models import AuctionLot

    query = select(AuctionLot.id)
    if lot_id:
        query = query.where(AuctionLot.id == lot_id)
    lot_ids = list(session.scalars(query.order_by(AuctionLot.id)).all())
    result = AuctionDocumentDedupResult()
    for current_lot_id in lot_ids:
        lot = session.scalar(
            select(AuctionLot)
            .options(selectinload(AuctionLot.documents))
            .where(AuctionLot.id == current_lot_id)
        )
        if lot is None:
            continue
        before = len(lot.documents)
        result.lots_checked += 1
        result.documents_before += before
        deduplicate_lot_documents(lot)
        after = len(lot.documents)
        result.documents_after += after
        result.documents_removed += before - after
    return result


def _preferred_document(documents: list[Any]) -> Any:
    return max(documents, key=_document_rank)


def _document_rank(document: Any) -> tuple[int, float, int]:
    status_rank = {
        "downloaded": 4,
        "linked": 3,
        "failed": 2,
    }.get(str(getattr(document, "storage_status", "") or "").casefold(), 1)
    created_at = getattr(document, "created_at", None)
    document_id = int(getattr(document, "id", 0) or 0)
    created_ts = created_at.timestamp() if hasattr(created_at, "timestamp") else 0.0
    return status_rank, created_ts, document_id


def _merge_document_metadata(keeper: Any, duplicate: Any) -> None:
    if not getattr(keeper, "file_type", None) and getattr(duplicate, "file_type", None):
        keeper.file_type = duplicate.file_type
    if not getattr(keeper, "title", None) and getattr(duplicate, "title", None):
        keeper.title = duplicate.title
    if getattr(duplicate, "storage_status", None) == "downloaded" and getattr(
        keeper, "storage_status", None
    ) != "downloaded":
        keeper.storage_status = duplicate.storage_status
        keeper.local_path = getattr(duplicate, "local_path", None)
        keeper.content_sha256 = getattr(duplicate, "content_sha256", None)
        keeper.downloaded_at = getattr(duplicate, "downloaded_at", None)
        keeper.download_error = getattr(duplicate, "download_error", None)
