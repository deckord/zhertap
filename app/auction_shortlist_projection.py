"""Batched, fail-closed projection from canonical auction evidence to shortlist inputs."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auction_market_comparables import ENGINE_VERSION as MARKET_ENGINE_VERSION
from app.auction_shortlist import (
    ComparativeEvidence,
    ReadinessEvidence,
    ShortlistLotInput,
    ShortlistResult,
    evaluate_shortlist_lot,
)
from app.models import AuctionDecisionSnapshot, AuctionEvidence, AuctionLot, AuctionLotGeoCheck

MARKET_EVIDENCE_TYPE = "strict_market_estimate"
MAX_MARKET_EVIDENCE_BYTES = 64_000


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _positive(value: object) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    ):
        return float(value)
    return None


def _https_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2_048:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and bool(parsed.netloc) else None


def load_latest_strict_market_evidence(
    session: Session, lot_ids: Iterable[str]
) -> dict[str, AuctionEvidence]:
    """Load at most one canonical market row per lot in one bounded query."""

    bounded_ids = tuple(dict.fromkeys(str(item) for item in lot_ids if item))
    if not bounded_ids:
        return {}
    latest = (
        select(
            AuctionEvidence.lot_id.label("lot_id"),
            func.max(AuctionEvidence.id).label("evidence_id"),
        )
        .where(
            AuctionEvidence.lot_id.in_(bounded_ids),
            AuctionEvidence.evidence_type == MARKET_EVIDENCE_TYPE,
        )
        .group_by(AuctionEvidence.lot_id)
        .subquery()
    )
    rows = session.scalars(
        select(AuctionEvidence).join(
            latest,
            (AuctionEvidence.lot_id == latest.c.lot_id)
            & (AuctionEvidence.id == latest.c.evidence_id),
        )
    ).all()
    return {row.lot_id: row for row in rows}


def _market_comparative(
    *,
    lot: AuctionLot,
    snapshot: AuctionDecisionSnapshot | None,
    evidence: AuctionEvidence | None,
    evaluated_at: datetime,
) -> ComparativeEvidence | None:
    if evidence is None or evidence.status != "found" or snapshot is None:
        return None
    if (
        not snapshot.is_current
        or snapshot.stale
        or snapshot.validated_evidence_id < evidence.id
        or (_aware(snapshot.checked_at) or datetime.min.replace(tzinfo=UTC))
        < (_aware(evidence.observed_at) or datetime.max.replace(tzinfo=UTC))
    ):
        return None
    raw = evidence.raw_payload_json
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MARKET_EVIDENCE_BYTES:
        return None
    if evidence.value_text != hashlib.sha256(raw.encode("utf-8")).hexdigest():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    estimate = payload.get("estimate")
    evaluations = payload.get("evaluations")
    target = payload.get("target")
    if (
        payload.get("engine_version") != MARKET_ENGINE_VERSION
        or payload.get("status") != "ok"
        or payload.get("confidence") not in {"medium", "high"}
        or not isinstance(estimate, dict)
        or not isinstance(evaluations, list)
        or len(evaluations) > 500
        or not isinstance(target, dict)
        or target.get("target_id") != lot.id
    ):
        return None
    used = estimate.get("verified_comparables_used")
    if not isinstance(used, int) or isinstance(used, bool) or used < 3:
        return None
    target_area = _positive(target.get("area_ha"))
    lot_area = _positive(lot.area_ha)
    start_price = _positive(lot.start_price_kzt)
    median_per_ha = _positive(estimate.get("median_price_per_ha_kzt"))
    valuation_raw = target.get("valuation_at")
    try:
        valuation_at = (
            datetime.fromisoformat(valuation_raw)
            if isinstance(valuation_raw, str)
            else None
        )
    except ValueError:
        valuation_at = None
    valuation_at = _aware(valuation_at)
    if (
        target_area is None
        or lot_area is None
        or start_price is None
        or median_per_ha is None
        or valuation_at is None
        or not math.isclose(target_area, lot_area, rel_tol=1e-9, abs_tol=1e-9)
    ):
        return None
    qualifying_dates: list[datetime] = []
    for item in evaluations:
        if not isinstance(item, dict) or not (
            item.get("eligible") is True
            and item.get("quality_grade") == "A"
            and item.get("price_kind") == "verified_sale"
        ):
            continue
        raw_stamp = item.get("observed_at")
        try:
            stamp = datetime.fromisoformat(raw_stamp) if isinstance(raw_stamp, str) else None
        except ValueError:
            stamp = None
        aware_stamp = _aware(stamp)
        if aware_stamp is not None:
            qualifying_dates.append(aware_stamp)
    if len(qualifying_dates) < used or any(
        stamp.year != valuation_at.year for stamp in qualifying_dates
    ):
        return None
    provenance = payload.get("provenance_refs")
    urls = (
        [url for item in provenance if (url := _https_url(item)) is not None]
        if isinstance(provenance, list)
        else []
    )
    source_available = bool(urls)
    source_url = urls[0] if urls else lot.source_url
    return ComparativeEvidence(
        source="Каноническая строгая выборка подтверждённых продаж",
        source_url=source_url,
        observed_at=min(qualifying_dates),
        target_price_per_sotka_kzt=start_price / (lot_area * 100),
        cohort_median_price_per_sotka_kzt=median_per_ha / 100,
        verified_comparables_count=used,
        target_year=valuation_at.year,
        cohort_year=valuation_at.year,
        comparison_method=(
            "strict-market-comparables.v2-same-year: Grade A verified_sale, тот же календарный год, "
            "совпадающие право/назначение, допустимые география, площадь и readiness; медиана "
            "скорректированной цены за гектар пересчитана в цену за сотку"
        ),
        source_available=source_available,
    )


def project_shortlist_results(
    session: Session,
    rows: Iterable[tuple[AuctionLot, AuctionDecisionSnapshot | None, AuctionLotGeoCheck]],
    *,
    evaluated_at: datetime | None = None,
) -> dict[str, ShortlistResult]:
    """Project a catalog page without per-lot evidence queries or legacy-score input."""

    checked = _aware(evaluated_at) or datetime.now(UTC)
    material = list(rows)
    evidence_by_lot = load_latest_strict_market_evidence(
        session, (lot.id for lot, _snapshot, _geo in material)
    )
    results: dict[str, ShortlistResult] = {}
    for lot, snapshot, geo in material:
        readiness = ReadinessEvidence(
            boundaries=geo.boundary_status == "verified",
            cadastre=geo.cadastre_status in {"verified", "found"},
            documents=bool(lot.documents),
            road=geo.osm_status == "checked",
            water=geo.engineering_status == "checked",
        )
        result = evaluate_shortlist_lot(
            ShortlistLotInput(
                lot_id=lot.id,
                source_status=lot.source_search_status or lot.status or "",
                evaluated_at=checked,
                readiness=readiness,
                comparative=_market_comparative(
                    lot=lot,
                    snapshot=snapshot,
                    evidence=evidence_by_lot.get(lot.id),
                    evaluated_at=checked,
                ),
            )
        )
        results[lot.id] = result
    return results
