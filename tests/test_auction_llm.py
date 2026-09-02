from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime

import httpx
import pytest

from app.auction_document_extractor import (
    DocumentMetadata,
    ExtractionLimits,
    extract_auction_document,
)
from app.auction_llm import (
    AuctionLlmClient,
    AuctionLlmError,
    candidate_is_grounded,
    extract_auction_document_with_llm,
)


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


def test_ollama_client_reads_message_content_only_and_validates_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3:8b"
        assert payload["stream"] is False
        assert payload["format"]["type"] == "object"
        assert "только на русском языке" in payload["messages"][0]["content"]
        assert "не пересказывай название файла" in payload["messages"][0]["content"]
        assert "document_type" not in payload["format"]["properties"]["facts"]["items"][
            "properties"
        ]["field"]["enum"]
        content = json.loads(payload["messages"][1]["content"])
        assert content["source_document"] == "lease.pdf"
        assert content["lot_context"]["right_type"] == "lease"
        assert content["lot_context"]["lease_term_years"] == 10
        assert "lease_term_years" in content["target_fields"]
        assert "right_type" in content["target_fields"]
        body = {
            "facts": [
                {
                    "field": "lease_term_years",
                    "value": 10,
                    "status": "confirmed",
                    "confidence": 0.91,
                    "source_document": "lease.pdf",
                    "page": 3,
                    "section": "paragraph:8",
                    "evidence": "Срок аренды составляет 10 лет.",
                    "user_explanation": "В документе указан срок аренды 10 лет.",
                }
            ],
            "summary": "Договор содержит срок аренды.",
            "risks": ["Проверить ограничения по схеме."],
            "unknowns": ["Красные линии не определяются по тексту."],
        }
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(body, ensure_ascii=False),
                    "thinking": "ignored internal model trace",
                }
            },
        )

    client = AuctionLlmClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout_seconds=30,
        max_text_chars=10_000,
        transport=httpx.MockTransport(handler),
    )

    result = client.analyze_document_text(
        text="Срок аренды составляет 10 лет.",
        source_document="lease.pdf",
        lot_context={"right_type": "lease", "lease_term_years": 10},
    )

    assert result.model == "qwen3:8b"
    assert result.summary == "Договор содержит срок аренды."
    assert result.facts[0].field == "lease_term_years"
    assert result.facts[0].value == 10
    assert result.facts[0].status == "confirmed"
    assert result.unknowns == ("Красные линии не определяются по тексту.",)


def test_ollama_client_rejects_freeform_or_invalid_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = {
            "facts": [
                {
                    "field": "right_type",
                    "value": "аренда",
                    "status": "definitely_true",
                    "confidence": 1,
                    "source_document": "lot.pdf",
                    "page": None,
                    "section": None,
                    "evidence": "Право аренды.",
                    "user_explanation": "Модель вернула неподдерживаемый статус.",
                }
            ],
            "summary": "Bad status.",
            "risks": [],
            "unknowns": [],
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(body)}})

    client = AuctionLlmClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout_seconds=30,
        max_text_chars=10_000,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AuctionLlmError, match="status"):
        client.analyze_document_text(text="Право аренды.", source_document="lot.pdf")


def test_ollama_client_drops_unsupported_fact_field() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = {
            "facts": [
                {
                    "field": "document_type",
                    "value": "auction_document",
                    "status": "confirmed",
                    "confidence": 1,
                    "source_document": "lot.pdf",
                    "page": None,
                    "section": None,
                    "evidence": "Проект договора.",
                    "user_explanation": "Служебный тип документа не является фактом лота.",
                }
            ],
            "summary": "Bad field.",
            "risks": [],
            "unknowns": [],
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(body)}})

    client = AuctionLlmClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout_seconds=30,
        max_text_chars=10_000,
        transport=httpx.MockTransport(handler),
    )

    result = client.analyze_document_text(text="Проект договора.", source_document="lot.pdf")

    assert result.facts == ()


def test_llm_extractor_adds_schema_valid_candidates_to_rule_based_result() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = {
            "facts": [
                {
                    "field": "development_obligation",
                    "value": "Построить производственную базу",
                    "status": "preliminary",
                    "confidence": 0.93,
                    "source_document": "lot.docx",
                    "page": None,
                    "section": "paragraph:2",
                    "evidence": "Победитель обязан построить производственную базу.",
                    "user_explanation": "Модель выделила обязательство по освоению.",
                },
                {
                    "field": "red_lines",
                    "value": None,
                    "status": "not_found",
                    "confidence": 0.2,
                    "source_document": "lot.docx",
                    "page": None,
                    "section": None,
                    "evidence": "",
                    "user_explanation": "Красные линии не выводятся из текста.",
                },
            ],
            "summary": "Есть обязательство по освоению.",
            "risks": [],
            "unknowns": ["Красные линии проверяются GIS-слоем."],
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(body)}})

    client = AuctionLlmClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout_seconds=30,
        max_text_chars=10_000,
        transport=httpx.MockTransport(handler),
    )
    metadata = DocumentMetadata(
        document_id=7,
        title="lot.docx",
        source_url="https://example.test/lot.docx",
        file_type="docx",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    source = _docx(["Срок аренды: 10 лет.", "Победитель обязан построить базу."])

    result = extract_auction_document_with_llm(
        source,
        metadata,
        client=client,
        limits=ExtractionLimits(max_candidates=10),
        extracted_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
    )

    assert {candidate.field for candidate in result.candidates} == {
        "right_type",
        "lease_term_years",
        "development_obligation",
    }
    llm_candidate = next(
        candidate for candidate in result.candidates if candidate.field == "development_obligation"
    )
    assert llm_candidate.extractor_version.endswith("+llm")
    assert llm_candidate.confidence == 0.86
    assert llm_candidate.status == "preliminary"
    assert next(
        item
        for item in result.as_dict()["candidates"]
        if item["field"] == "development_obligation"
    )["status"] == "preliminary"
    assert result.summary == "Есть обязательство по освоению."
    assert result.risks == ()
    assert result.unknowns == ("Красные линии проверяются GIS-слоем.",)
    assert result.as_dict()["summary"] == "Есть обязательство по освоению."


def test_llm_grounding_rejects_permissions_mislabeled_as_obligations() -> None:
    assert not candidate_is_grounded(
        "development_obligation",
        "Покупатель имеет право самостоятельно хозяйствовать на земле",
        "Покупатель имеет право самостоятельно хозяйствовать на земле.",
    )
    assert candidate_is_grounded(
        "development_obligation",
        "Покупатель обязан освоить участок в течение трех лет",
        "Покупатель обязан освоить участок в течение трех лет.",
    )


def test_llm_extractor_reserves_capacity_when_rule_candidates_fill_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        body = {
            "facts": [
                {
                    "field": "termination_ground",
                    "value": "Договор расторгается при неосвоении участка.",
                    "status": "preliminary",
                    "confidence": 0.9,
                    "source_document": "lot.docx",
                    "page": None,
                    "section": "paragraph:3",
                    "evidence": "Договор расторгается при неосвоении участка.",
                    "user_explanation": "Существенное условие договора.",
                }
            ],
            "summary": "Найдено основание расторжения.",
            "risks": [],
            "unknowns": [],
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(body)}})

    client = AuctionLlmClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout_seconds=30,
        max_text_chars=10_000,
        transport=httpx.MockTransport(handler),
    )
    metadata = DocumentMetadata(
        document_id=9,
        title="lot.docx",
        source_url="https://example.test/lot.docx",
        file_type="docx",
    )
    # The rule extractor fills both available slots before the LLM result arrives.
    source = _docx(
        [
            "Право аренды земельного участка.",
            "Срок аренды составляет 10 лет.",
            "Договор расторгается при неосвоении участка.",
        ]
    )

    result = extract_auction_document_with_llm(
        source,
        metadata,
        client=client,
        limits=ExtractionLimits(max_candidates=2),
        extracted_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
    )

    assert len(result.candidates) == 2
    termination = next(
        candidate for candidate in result.candidates if candidate.field == "termination_ground"
    )
    assert termination.extractor_version.endswith("+llm")
    assert termination.evidence_excerpt == "Договор расторгается при неосвоении участка."


def test_llm_extractor_falls_back_to_rule_based_result_on_model_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model unavailable")

    client = AuctionLlmClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout_seconds=30,
        max_text_chars=10_000,
        transport=httpx.MockTransport(handler),
    )
    metadata = DocumentMetadata(
        document_id=8,
        title="lot.docx",
        source_url="https://example.test/lot.docx",
        file_type="docx",
    )
    source = _docx(["Срок аренды: 5 лет."])
    extracted_at = datetime(2026, 8, 20, 11, tzinfo=UTC)

    result = extract_auction_document_with_llm(
        source,
        metadata,
        client=client,
        extracted_at=extracted_at,
    )
    base = extract_auction_document(source, metadata, extracted_at=extracted_at)

    assert result.as_dict() == base.as_dict()
