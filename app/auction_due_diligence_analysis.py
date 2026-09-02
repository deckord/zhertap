from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from app.auction_document_extractor import DocumentMetadata, extract_auction_document


def _file_type(title: str) -> str:
    suffix = Path(title).suffix.lower().lstrip(".")
    return suffix or "unknown"


def analyze_due_diligence_response(
    content: bytes,
    *,
    attachment_id: str,
    title: str,
    source_url: str,
    observed_at: datetime | None,
) -> dict[str, object]:
    """Extract bounded candidates from a user response without confirming them."""
    file_type = _file_type(title)
    if file_type == "pdf" and b"%PDF-" not in content[:1024]:
        return {
            "status": "corrupt",
            "candidates": [],
            "conflicts": [],
            "content_hash": hashlib.sha256(content).hexdigest(),
            "pages_processed": 0,
            "text_chars_processed": 0,
            "detail": "pdf signature is missing",
            "attachment_id": attachment_id,
            "analyzed_at": datetime.now(UTC).isoformat(),
            "fact_status": "candidate_only",
        }
    now = datetime.now(UTC)
    result = extract_auction_document(
        content,
        DocumentMetadata(
            document_id=attachment_id,
            title=title[:320],
            source_url=source_url[:2048],
            file_type=file_type,
            observed_at=observed_at,
        ),
        extracted_at=now,
    )
    payload = result.as_dict()
    payload["attachment_id"] = attachment_id
    payload["content_hash"] = payload.get("content_hash") or hashlib.sha256(content).hexdigest()
    payload["analyzed_at"] = now.isoformat()
    payload["fact_status"] = "candidate_only"
    return payload
