from datetime import UTC, datetime

import fitz

from app.auction_due_diligence_analysis import analyze_due_diligence_response


def _pdf_bytes() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "Обращение может быть расторгнуто при нарушении условий. Штраф начисляется за нарушение.",
    )
    content = document.tobytes()
    document.close()
    return content


def test_due_diligence_response_analysis_extracts_bounded_text_facts() -> None:
    result = analyze_due_diligence_response(
        _pdf_bytes(),
        attachment_id="att-1",
        title="Ответ органа.pdf",
        source_url="due-diligence://att-1",
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert result["status"] in {"ok", "unknown"}
    assert result["content_hash"]
    assert result["fact_status"] == "candidate_only"
    assert "pages_processed" in result
    assert "candidates" in result


def test_due_diligence_response_analysis_preserves_corrupt_unknown() -> None:
    result = analyze_due_diligence_response(
        b"<!doctype html>rate limited",
        attachment_id="att-2",
        title="Ответ.pdf",
        source_url="due-diligence://att-2",
        observed_at=None,
    )

    assert result["status"] == "corrupt"
    assert result["candidates"] == []
