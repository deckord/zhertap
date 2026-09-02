from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuctionDueDiligenceRequest, AuctionLot


@dataclass(frozen=True, slots=True)
class DueDiligenceRequestDraft:
    check_code: str
    authority: str
    question: str
    why: str
    status: str
    lot_context: dict[str, Any]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_REQUEST_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "electricity": (
        "Энергоснабжающая организация / акимат",
        "Подтвердить наличие электрических сетей, владельца, напряжения, охранной зоны,"
        " технической возможности подключения и условий/стоимости подключения.",
        "Расстояние до ЛЭП или объекта на карте не подтверждает возможность подключения.",
    ),
    "access": (
        "Отдел земельных отношений / акимат",
        "Подтвердить наличие юридически оформленного подъезда, сервитута или дороги общего"
        " пользования к участку, включая условия круглогодичного проезда.",
        "Физическая дорога на карте не равна юридически подтвержденному доступу.",
    ),
    "flood": (
        "ДЧС / бассейновая инспекция / акимат",
        "Проверить попадание участка в водоохранную зону, водоохранную полосу, зону"
        " подтопления или паводкового риска и указать применимые ограничения.",
        "Отсутствие объекта воды в OSM не подтверждает отсутствие паводкового риска.",
    ),
    "restrictions": (
        "Отдел земельных отношений / акимат",
        "Предоставить сведения об обременениях, арестах, сервитутах, красных линиях,"
        " ограничениях использования и обязательных условиях освоения участка.",
        "Пустая или неполная открытая карта не доказывает юридическую свободу участка.",
    ),
}


def _text(value: object, fallback: str = "не указан") -> str:
    text = " ".join(str(value or "").split())
    return text[:500] if text else fallback


def build_request_draft(lot: Any, *, check_code: str) -> DueDiligenceRequestDraft:
    template = _REQUEST_TEMPLATES.get(check_code)
    if template is None:
        raise ValueError(f"unknown_check_code:{check_code}")
    authority, request_text, why = template
    auction_number = _text(
        getattr(lot, "auction_number", None) or getattr(lot, "source_lot_id", None)
    )
    cadastre = _text(getattr(lot, "cadastre_number", None))
    region = _text(getattr(lot, "region", None))
    district = _text(getattr(lot, "district", None))
    locality = _text(getattr(lot, "locality", None))
    area = getattr(lot, "area_ha", None)
    area_text = f"{float(area):g} га" if area is not None else "площадь не указана"
    purpose = _text(getattr(lot, "purpose", None))
    rights = _text(getattr(lot, "land_rights", None))
    lease_term = getattr(lot, "lease_term_years", None)
    lease_text = f"{float(lease_term):g} лет" if lease_term is not None else "срок не указан"
    context = {
        "lot_id": _text(getattr(lot, "id", None)),
        "auction_number": auction_number,
        "cadastre": cadastre,
        "region": region,
        "district": district,
        "locality": locality,
        "area": area_text,
        "purpose": purpose,
        "rights": rights,
        "lease_term": lease_text,
    }
    question = (
        f"По земельному лоту №{auction_number}, кадастровый номер {cadastre}, "
        f"адрес/территория: {region}, {district}, {locality}, площадь {area_text}, "
        f"назначение: {purpose}, право: {rights}, срок аренды: {lease_text}. "
        f"{request_text}"
    )
    return DueDiligenceRequestDraft(
        check_code=check_code,
        authority=authority,
        question=question,
        why=why,
        status="draft",
        lot_context=context,
    )


REQUEST_STATUSES = {
    "draft",
    "prepared",
    "sent",
    "waiting",
    "received",
    "verified",
    "risk",
    "cancelled",
}


def create_due_diligence_request(
    session: Session,
    *,
    account_id: str,
    lot_id: str,
    check_code: str,
    response_due_at: datetime | None = None,
) -> AuctionDueDiligenceRequest:
    lot = session.get(AuctionLot, lot_id)
    if lot is None:
        raise ValueError("lot_not_found")
    draft = build_request_draft(lot, check_code=check_code)
    request = AuctionDueDiligenceRequest(
        account_id=account_id,
        lot_id=lot_id,
        check_code=draft.check_code,
        authority=draft.authority,
        question=draft.question,
        why=draft.why,
        status=draft.status,
        response_due_at=response_due_at,
    )
    session.add(request)
    session.flush()
    return request


def list_due_diligence_requests(
    session: Session,
    *,
    account_id: str,
    lot_id: str,
) -> list[AuctionDueDiligenceRequest]:
    return list(
        session.scalars(
            select(AuctionDueDiligenceRequest)
            .where(
                AuctionDueDiligenceRequest.account_id == account_id,
                AuctionDueDiligenceRequest.lot_id == lot_id,
            )
            .order_by(
                AuctionDueDiligenceRequest.response_due_at.is_(None),
                AuctionDueDiligenceRequest.response_due_at,
                AuctionDueDiligenceRequest.created_at.desc(),
            )
        ).all()
    )


MANUAL_CHECK_REQUEST_STATUS = {
    "no_data": "draft",
    "in_progress": "waiting",
    "done": "verified",
}


def record_manual_check_request(
    session: Session,
    *,
    account_id: str,
    lot_id: str,
    check_code: str,
    check_status: str,
    note: str | None = None,
    has_attachment: bool = False,
) -> AuctionDueDiligenceRequest:
    """Persist the owner's manual-check journal without generating an appeal.

    The manual checklist is the user-facing source of truth. This registry row is
    only an auditable timeline entry, scoped to the owner and lot. A later upload
    reuses the same open entry instead of creating duplicate response records.
    """
    if check_code not in {code for code, _label in _REQUEST_TEMPLATES.items()}:
        raise ValueError("unknown_check_code")
    target_status = MANUAL_CHECK_REQUEST_STATUS.get(check_status)
    if target_status is None:
        raise ValueError("invalid_manual_check_status")
    if has_attachment and target_status == "verified":
        # A file has been received, but its extracted facts still need review.
        target_status = "received"
    request = session.scalar(
        select(AuctionDueDiligenceRequest)
        .where(
            AuctionDueDiligenceRequest.account_id == account_id,
            AuctionDueDiligenceRequest.lot_id == lot_id,
            AuctionDueDiligenceRequest.check_code == check_code,
            AuctionDueDiligenceRequest.status != "cancelled",
        )
        .order_by(AuctionDueDiligenceRequest.created_at.desc())
    )
    now = datetime.now(UTC)
    if request is None:
        request = AuctionDueDiligenceRequest(
            account_id=account_id,
            lot_id=lot_id,
            check_code=check_code,
            authority="Определяется из загруженного ответа",
            question="Полученный пользователем официальный ответ",
            why="Сохранить и проанализировать ответ без генерации обращения",
        )
        session.add(request)
    request.status = target_status
    request.response_summary = (note or "").strip()[:10_000] or None
    if target_status in {"waiting", "verified"} and request.submitted_at is None:
        request.submitted_at = now
    if has_attachment or target_status == "verified":
        request.received_at = request.received_at or now
    request.updated_at = now
    session.flush()
    return request


def update_due_diligence_request(
    session: Session,
    *,
    account_id: str,
    request_id: str,
    status: str | None = None,
    external_reference: str | None = None,
    response_due_at: datetime | None = None,
    response_summary: str | None = None,
    submitted_at: datetime | None = None,
    received_at: datetime | None = None,
) -> AuctionDueDiligenceRequest:
    if status is not None and status not in REQUEST_STATUSES:
        raise ValueError("invalid_request_status")
    request = session.scalar(
        select(AuctionDueDiligenceRequest).where(
            AuctionDueDiligenceRequest.id == request_id,
            AuctionDueDiligenceRequest.account_id == account_id,
        )
    )
    if request is None:
        raise ValueError("request_not_found")
    if status is not None:
        request.status = status
    if external_reference is not None:
        request.external_reference = external_reference.strip()[:160] or None
    if response_due_at is not None:
        request.response_due_at = response_due_at
    if response_summary is not None:
        request.response_summary = response_summary.strip()[:10_000] or None
    if submitted_at is not None:
        request.submitted_at = submitted_at
    if received_at is not None:
        request.received_at = received_at
    request.updated_at = datetime.now(UTC)
    session.flush()
    return request


def due_diligence_attachment_cards(attachments: list[Any]) -> dict[str, dict[str, object]]:
    cards: dict[str, dict[str, object]] = {}
    for attachment in attachments:
        status = str(getattr(attachment, "extraction_status", "pending") or "pending")
        payload: dict[str, object] = {}
        raw = getattr(attachment, "extraction_json", None)
        if isinstance(raw, str) and raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                payload = {}
        candidates: list[dict[str, object]] = []
        for raw_candidate in payload.get("candidates", []):
            if not isinstance(raw_candidate, dict):
                continue
            page = raw_candidate.get("page")
            section = str(raw_candidate.get("section") or "").strip()
            provenance_parts = []
            if page is not None:
                provenance_parts.append(f"стр. {page}")
            if section:
                provenance_parts.append(section)
            candidates.append(
                {
                    "field": str(raw_candidate.get("field") or "факт"),
                    "value": raw_candidate.get("value"),
                    "confidence": raw_candidate.get("confidence"),
                    "evidence_excerpt": str(raw_candidate.get("evidence_excerpt") or "")[:500],
                    "provenance": " · ".join(provenance_parts) or "страница не указана",
                }
            )
        cards[str(attachment.id)] = {
            "status": str(payload.get("status") or status),
            "detail": str(payload.get("detail") or ""),
            "fact_status": str(payload.get("fact_status") or "candidate_only"),
            "candidates": candidates[:64],
        }
    return cards


def build_due_diligence_checklist(
    lot: Any,
    *,
    requests: list[Any],
    manual_checks: dict[str, Any],
    documents_count: int,
    planning_status: str,
) -> dict[str, object]:
    request_by_code = {
        str(item.check_code): str(item.status)
        for item in requests
        if getattr(item, "check_code", None)
    }
    text = " ".join(
        str(getattr(lot, field, "") or "")
        for field in ("land_rights", "purpose", "use_goal", "functional_purpose_level2")
    ).casefold()

    items: list[dict[str, object]] = [
        {
            "code": "lot_terms",
            "label": "Лот и условия торгов",
            "status": "done" if getattr(lot, "source_lot_id", None) else "unknown",
            "critical": True,
            "next": "Открыть официальную карточку E-Qazyna.",
        },
        {
            "code": "right",
            "label": "Право на землю",
            "status": "done" if getattr(lot, "land_rights", None) else "unknown",
            "critical": True,
            "next": "Сверить право и срок с договором/актом.",
        },
        {
            "code": "cadastre",
            "label": "Кадастровый номер",
            "status": "done" if getattr(lot, "cadastre_number", None) else "unknown",
            "critical": True,
            "next": "Проверить номер и границы в ЕГКН.",
        },
        {
            "code": "documents",
            "label": "Документы лота",
            "status": "done" if documents_count > 0 else "unknown",
            "critical": True,
            "next": "Открыть документы и проверить условия.",
        },
        {
            "code": "planning",
            "label": "Генплан, ПДП и красные линии",
            "status": "done" if planning_status == "clear" else planning_status or "unknown",
            "critical": True,
            "next": "Получить официальный слой или выполнить ручную сверку.",
        },
    ]

    for code, label, next_step in (
        ("electricity", "Электричество", "Запросить техническую возможность подключения."),
        ("access", "Юридический подъезд", "Подтвердить дорогу, сервитут или право проезда."),
        ("flood", "Вода и паводок", "Проверить водоохранную зону и подтопление."),
        (
            "restrictions",
            "Обременения и ограничения",
            "Запросить официальные сведения об ограничениях.",
        ),
    ):
        source = request_by_code.get(code)
        if source is None:
            source = str((manual_checks.get(code) or {}).get("status") or "unknown")
        items.append(
            {
                "code": code,
                "label": label,
                "status": source,
                "critical": True,
                "next": next_step,
            }
        )

    if "аренд" in text or "lease" in text:
        items.append(
            {
                "code": "lease_terms",
                "label": "Срок и условия аренды",
                "status": "done" if getattr(lot, "lease_term_years", None) else "unknown",
                "critical": True,
                "next": "Проверить продление, платежи, освоение и расторжение.",
            }
        )
    if any(marker in text for marker in ("магазин", "торгов", "рознич", "коммерц")):
        items.append(
            {
                "code": "retail_purpose",
                "label": "Сценарий торговли",
                "status": "requires_check",
                "critical": True,
                "next": "Подтвердить допустимость торгового объекта по планированию.",
            }
        )

    completed = sum(item["status"] in {"done", "verified"} for item in items)
    critical_open = sum(
        item["critical"] and item["status"] not in {"done", "verified"} for item in items
    )
    return {
        "items": items,
        "items_by_code": {str(item["code"]): item for item in items},
        "total": len(items),
        "completed": completed,
        "critical_open": critical_open,
        "completion_percent": round(completed * 100 / len(items)) if items else 0,
    }
