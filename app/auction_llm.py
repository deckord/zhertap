from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.auction_document_extractor import (
    EXTRACTOR_VERSION,
    DocumentExtractionResult,
    DocumentFactCandidate,
    DocumentMetadata,
    ExtractionLimits,
    document_candidate_conflicts,
    extract_auction_document,
    extract_auction_document_text,
    reconcile_document_candidates,
)

ALLOWED_FACT_STATUSES = {
    "confirmed",
    "preliminary",
    "not_found",
    "conflict",
    "requires_check",
}
ALLOWED_FACT_FIELDS = {
    "annual_payment_kzt",
    "area_hectares",
    "arrest_status",
    "cadastral_number",
    "development_deadline",
    "development_obligation",
    "divisibility",
    "encumbrances",
    "genplan_text_mention",
    "guarantee_payment_kzt",
    "intended_use",
    "lease_term_years",
    "one_time_payment_kzt",
    "red_lines_text_mention",
    "renewal_condition",
    "responsibility_penalty",
    "responsible_authority",
    "restrictions",
    "right_type",
    "target_purpose",
    "termination_ground",
    "transfer_right",
}
MAX_RESPONSE_BYTES = 128_000
LLM_EXTRACTOR_VERSION = f"{EXTRACTOR_VERSION}+llm"
LLM_CANDIDATE_STATUSES = {"confirmed", "preliminary", "conflict", "requires_check"}
LOT_CONTEXT_KEYS = {
    "right_type",
    "lease_term_years",
    "target_purpose",
    "intended_use",
    "area_hectares",
    "cadastral_number",
    "guarantee_payment_kzt",
    "annual_payment_kzt",
    "one_time_payment_kzt",
}


class AuctionLlmError(RuntimeError):
    """Raised when the local LLM returns unusable extraction output."""


@dataclass(frozen=True)
class AuctionLlmFact:
    field: str
    value: str | int | float | bool | None
    status: str
    confidence: float
    source_document: str
    page: int | None
    section: str | None
    evidence: str
    user_explanation: str


@dataclass(frozen=True)
class AuctionLlmDocumentAnalysis:
    facts: tuple[AuctionLlmFact, ...]
    summary: str
    risks: tuple[str, ...]
    unknowns: tuple[str, ...]
    model: str
    raw_json: dict[str, Any]


class AuctionLlmClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int,
        max_text_chars: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_text_chars = max_text_chars
        self.transport = transport

    def analyze_document_text(
        self,
        *,
        text: str,
        source_document: str,
        document_type: str = "auction_document",
        lot_context: dict[str, object] | None = None,
    ) -> AuctionLlmDocumentAnalysis:
        payload = {
            "model": self.model,
            "stream": False,
            "format": _document_schema(),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты анализируешь юридические документы земельных аукционов "
                        "Казахстана для покупателя. Верни только JSON по заданной схеме. "
                        "Все поля summary, risks, unknowns, user_explanation и текстовые "
                        "values пиши только на русском языке. Не переводи казахстанские "
                        "юридические термины на английский и не пересказывай название "
                        "файла. В summary дай максимум 3 коротких предложения только о "
                        "том, что влияет на решение: обязанности покупателя, сроки "
                        "освоения, дополнительные платежи, штрафы и неустойки, основания "
                        "расторжения, ограничения, обременения и запреты. Не повторяй "
                        "право, площадь, кадастровый номер и назначение, если документ "
                        "не противоречит карточке. Сравни документ с lot_context: полные "
                        "совпадения не повторяй, а расхождения верни со статусом conflict "
                        "и точной цитатой. Если существенных условий нет, прямо "
                        "напиши это. Risks должны быть конкретными и следовать из текста; "
                        "unknowns должны перечислять только то, что нельзя подтвердить "
                        "документом. Извлекай факты только из точных цитат. Не выводи "
                        "GIS-расстояния, итоговый юридический вердикт, ставку или "
                        "инвестиционную рекомендацию. Не додумывай отсутствующие данные. "
                        "Верни максимум 2 новых найденных факта и не создавай строки not_found "
                        "для отсутствующих полей. Каждая цитата evidence — до 120 символов."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "document_type": document_type,
                            "source_document": source_document,
                            "lot_context": _bounded_lot_context(lot_context),
                            "target_fields": sorted(ALLOWED_FACT_FIELDS),
                            "value_rules": {
                                "right_type": "ownership or lease",
                                "lease_term_years": "number of years; months converted to years",
                                "money_fields": "numeric KZT amount without currency text",
                                "unknown": "null with status not_found or requires_check",
                            },
                            "text": _bounded_text(text, min(self.max_text_chars, 3_000)),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "options": {
                "temperature": 0,
                "num_predict": 1024,
                "num_ctx": 4096,
            },
        }
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            response = client.post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise AuctionLlmError("LLM response is too large")
            envelope = response.json()
        content = envelope.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise AuctionLlmError("LLM response did not contain message.content JSON")
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AuctionLlmError("LLM response content is not valid JSON") from exc
        return _parse_document_analysis(decoded, model=self.model, source_document=source_document)


def candidate_is_grounded(field: str, value: object, evidence: str) -> bool:
    normalized_evidence = evidence.casefold()
    if field == "development_obligation" and not any(
        marker in normalized_evidence
        for marker in ("обязан", "обязуется", "должен", "необходимо", "подлежит")
    ):
        # A permission (for example, «имеет право хозяйствовать») must not be
        # surfaced to the buyer as a mandatory development condition.
        return False
    if field == "right_type":
        if str(value).casefold() == "lease":
            return any(marker in normalized_evidence for marker in ("аренд", "землепольз", "жалда"))
        if str(value).casefold() == "ownership":
            return any(marker in normalized_evidence for marker in ("собствен", "меншік"))
    value_numbers = {
        re.sub(r"[\s\u00a0\u202f]", "", item)
        for item in re.findall(r"\d+(?:[\s\u00a0\u202f]\d+)*(?:[.,]\d+)?", str(value or ""))
    }
    evidence_numbers = {
        re.sub(r"[\s\u00a0\u202f]", "", item)
        for item in re.findall(r"\d+(?:[\s\u00a0\u202f]\d+)*(?:[.,]\d+)?", evidence)
    }
    if value_numbers and not value_numbers.issubset(evidence_numbers):
        return False
    value_tokens = {
        token
        for token in re.findall(r"[a-zа-яәіңғүұқөһё]{4,}", str(value or "").casefold())
        if token not in {"который", "которая", "земельный", "участок", "земельного"}
    }
    if not value_tokens:
        return bool(value_numbers)
    evidence_tokens = set(re.findall(r"[a-zа-яәіңғүұқөһё]{4,}", normalized_evidence))
    required = min(3, max(1, (len(value_tokens) + 1) // 2))
    return len(value_tokens & evidence_tokens) >= required


def _fact_is_grounded(fact: object, evidence: str) -> bool:
    return candidate_is_grounded(
        str(getattr(fact, "field", "")),
        getattr(fact, "value", None),
        evidence,
    )


def extract_auction_document_with_llm(
    source: bytes | bytearray | Path | str,
    metadata: DocumentMetadata,
    *,
    client: AuctionLlmClient,
    limits: ExtractionLimits | None = None,
    extracted_at: datetime | None = None,
) -> DocumentExtractionResult:
    base = extract_auction_document(
        source,
        metadata,
        limits=limits,
        extracted_at=extracted_at,
    )
    if base.status not in {"ok", "unknown"} or base.content_hash is None:
        return base
    text_result = extract_auction_document_text(
        source,
        metadata,
        limits=limits,
        extracted_at=extracted_at,
    )
    if text_result.status != "ok" or not text_result.text.strip():
        return base
    try:
        llm_result = client.analyze_document_text(
            text=text_result.text,
            source_document=metadata.title or metadata.source_url,
            lot_context=metadata.lot_context,
        )
    except Exception:
        return base
    active_limits = limits or ExtractionLimits()
    base_candidates = list(base.candidates)
    llm_candidates: list[DocumentFactCandidate] = []
    seen = {
        (candidate.field, repr(candidate.value), candidate.page, candidate.section)
        for candidate in base_candidates
    }
    for fact in llm_result.facts:
        if len(llm_candidates) >= active_limits.max_candidates:
            break
        if fact.status not in LLM_CANDIDATE_STATUSES or not fact.evidence.strip():
            continue
        marker = (fact.field, repr(fact.value), fact.page, fact.section)
        if marker in seen:
            continue
        seen.add(marker)
        evidence = fact.evidence.strip()
        if not _fact_is_grounded(fact, evidence):
            continue
        if len(evidence) > active_limits.max_excerpt_chars:
            evidence = evidence[: active_limits.max_excerpt_chars - 1].rstrip() + "..."
        llm_candidates.append(
            DocumentFactCandidate(
                field=fact.field,
                value=fact.value,
                document_id=metadata.document_id,
                document_title=metadata.title,
                source_url=metadata.source_url,
                page=fact.page,
                section=fact.section,
                evidence_excerpt=evidence,
                quote_hash=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                content_hash=base.content_hash,
                extractor_version=LLM_EXTRACTOR_VERSION,
                confidence=min(float(fact.confidence), 0.86),
                observed_at=metadata.observed_at,
                extracted_at=extracted_at or datetime.now(UTC),
                status=fact.status,
            )
        )
    if not llm_candidates:
        candidates = list(reconcile_document_candidates(base_candidates, metadata.lot_context))
        return DocumentExtractionResult(
            status=base.status,
            candidates=tuple(candidates),
            conflicts=document_candidate_conflicts(candidates, metadata.lot_context),
            content_hash=base.content_hash,
            pages_processed=base.pages_processed,
            text_chars_processed=base.text_chars_processed,
            detail=base.detail or "LLM-анализ завершён, новых кандидатов не добавлено.",
            extractor_version=LLM_EXTRACTOR_VERSION,
            summary=llm_result.summary,
            risks=llm_result.risks,
            unknowns=llm_result.unknowns,
        )
    # Rule extraction can legitimately fill the entire evidence budget before the
    # model runs. Reserve bounded capacity for grounded model-only legal clauses;
    # otherwise an expensive successful analysis is silently discarded.
    reserve = min(len(llm_candidates), max(1, active_limits.max_candidates // 2))
    rule_capacity = max(0, active_limits.max_candidates - reserve)
    candidates = list(
        reconcile_document_candidates(
            base_candidates[:rule_capacity] + llm_candidates[:reserve],
            metadata.lot_context,
        )
    )
    return DocumentExtractionResult(
        status="ok",
        candidates=tuple(candidates),
        conflicts=document_candidate_conflicts(candidates, metadata.lot_context),
        content_hash=base.content_hash,
        pages_processed=base.pages_processed,
        text_chars_processed=base.text_chars_processed,
        detail=base.detail,
        extractor_version=LLM_EXTRACTOR_VERSION,
        summary=llm_result.summary,
        risks=llm_result.risks,
        unknowns=llm_result.unknowns,
    )


def _bounded_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _bounded_lot_context(value: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    bounded: dict[str, object] = {}
    for key in sorted(LOT_CONTEXT_KEYS):
        raw = value.get(key)
        if raw is None or isinstance(raw, bool):
            if raw is not None:
                bounded[key] = raw
            continue
        if isinstance(raw, (int, float)):
            bounded[key] = raw
            continue
        if isinstance(raw, str) and raw.strip():
            bounded[key] = raw.strip()[:300]
    return bounded


def _document_schema() -> dict[str, Any]:
    fact_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "field",
            "value",
            "status",
            "confidence",
            "page",
            "section",
            "evidence",
        ],
        "properties": {
            "field": {"type": "string", "enum": sorted(ALLOWED_FACT_FIELDS)},
            "value": {"type": ["string", "number", "boolean", "null"]},
            "status": {"type": "string", "enum": sorted(ALLOWED_FACT_STATUSES)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "source_document": {"type": "string"},
            "page": {"type": ["integer", "null"]},
            "section": {"type": ["string", "null"]},
            "evidence": {"type": "string", "maxLength": 120},
            "user_explanation": {"type": "string", "maxLength": 120},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["facts", "summary", "risks", "unknowns"],
        "properties": {
            "facts": {"type": "array", "maxItems": 2, "items": fact_schema},
            "summary": {"type": "string", "maxLength": 240},
            "risks": {
                "type": "array",
                "maxItems": 2,
                "items": {"type": "string", "maxLength": 140},
            },
            "unknowns": {
                "type": "array",
                "maxItems": 2,
                "items": {"type": "string", "maxLength": 140},
            },
        },
    }


def _parse_document_analysis(
    data: Any,
    *,
    model: str,
    source_document: str,
) -> AuctionLlmDocumentAnalysis:
    if not isinstance(data, dict):
        raise AuctionLlmError("LLM JSON root must be an object")
    facts_raw = data.get("facts")
    if not isinstance(facts_raw, list):
        raise AuctionLlmError("LLM JSON must contain facts list")
    facts: list[AuctionLlmFact] = []
    for item in facts_raw:
        try:
            facts.append(_parse_fact(item, source_document=source_document))
        except AuctionLlmError as exc:
            if "unsupported field" in str(exc):
                continue
            raise
    return AuctionLlmDocumentAnalysis(
        facts=tuple(facts),
        summary=_required_str(data, "summary"),
        risks=tuple(_string_items(data, "risks")),
        unknowns=tuple(_string_items(data, "unknowns")),
        model=model,
        raw_json=data,
    )


def _parse_fact(data: Any, *, source_document: str) -> AuctionLlmFact:
    if not isinstance(data, dict):
        raise AuctionLlmError("LLM fact must be an object")
    status = _required_str(data, "status")
    if status not in ALLOWED_FACT_STATUSES:
        raise AuctionLlmError(f"LLM fact has unsupported status: {status}")
    field = _required_str(data, "field")
    if field not in ALLOWED_FACT_FIELDS:
        raise AuctionLlmError(f"LLM fact has unsupported field: {field}")
    confidence_raw = data.get("confidence")
    if not isinstance(confidence_raw, (int, float)):
        raise AuctionLlmError("LLM fact confidence must be a number")
    confidence = max(0.0, min(1.0, float(confidence_raw)))
    page = data.get("page")
    if page is not None and not isinstance(page, int):
        raise AuctionLlmError("LLM fact page must be an integer or null")
    document = data.get("source_document") or source_document
    if not isinstance(document, str):
        raise AuctionLlmError("LLM fact source_document must be a string")
    section = data.get("section")
    if section is not None and not isinstance(section, str):
        raise AuctionLlmError("LLM fact section must be a string or null")
    return AuctionLlmFact(
        field=field,
        value=data.get("value"),
        status=status,
        confidence=confidence,
        source_document=document,
        page=page,
        section=section,
        evidence=_required_str(data, "evidence"),
        user_explanation=str(data.get("user_explanation") or "").strip(),
    )


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise AuctionLlmError(f"LLM JSON field {key} must be a string")
    return value.strip()


def _string_items(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise AuctionLlmError(f"LLM JSON field {key} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise AuctionLlmError(f"LLM JSON field {key} must contain strings")
    return [item.strip() for item in value if item.strip()]
