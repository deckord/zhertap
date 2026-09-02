import json
from datetime import UTC, datetime

import fitz
import httpx

from app.auction_document_extractor import DocumentMetadata
from app.auction_llm import AuctionLlmClient, extract_auction_document_with_llm


def test_llm_success_is_marked_even_when_it_adds_no_new_candidates() -> None:
    body = {
        "facts": [
            {
                "field": "lease_term_years",
                "value": 5,
                "status": "confirmed",
                "confidence": 0.8,
                "source_document": "lot.pdf",
                "page": 1,
                "section": "terms",
                "evidence": "Срок аренды: 5 лет.",
                "user_explanation": "",
            }
        ],
        "summary": "",
        "risks": [],
        "unknowns": [],
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": json.dumps(body)}})

    metadata = DocumentMetadata(
        document_id=9,
        title="lot.pdf",
        source_url="https://example.test/lot.pdf",
        file_type="pdf",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Срок аренды: 5 лет.")
    source = document.tobytes()
    document.close()

    result = extract_auction_document_with_llm(
        source,
        metadata,
        client=AuctionLlmClient(
            base_url="http://127.0.0.1:11434",
            model="qwen3:8b",
            timeout_seconds=30,
            max_text_chars=10_000,
            transport=httpx.MockTransport(handler),
        ),
    )

    assert result.extractor_version.endswith("+llm")
    assert result.candidates
    assert result.as_dict()["extractor_version"].endswith("+llm")
