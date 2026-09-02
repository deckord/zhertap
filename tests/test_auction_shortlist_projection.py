import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auction_decision_snapshot import DECISION_ENGINE_VERSION
from app.auction_shortlist_projection import project_shortlist_results
from app.auction_verdict import RULES_VERSION
from app.db import Base
from app.models import (
    AuctionDecisionSnapshot,
    AuctionDocument,
    AuctionEvidence,
    AuctionLot,
    AuctionLotGeoCheck,
)

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _lot(index: int = 1) -> AuctionLot:
    return AuctionLot(
        id=str(uuid.uuid4()),
        source_lot_id=f"shortlist-{index}",
        source_search_status="ApplicationsAccept",
        title="Земельный участок",
        area_ha=1.0,
        start_price_kzt=8_000_000,
        source_url=f"https://sauda.e-qazyna.kz/auction/{index}",
        active=True,
    )


def _payload(lot: AuctionLot, *, provenance: bool = True) -> str:
    payload = {
        "status": "ok",
        "confidence": "high",
        "engine_version": "strict-market-comparables.v2-same-year",
        "estimate": {
            "median_price_per_ha_kzt": 10_000_000.0,
            "verified_comparables_used": 3,
        },
        "evaluations": [
            {
                "source_id": "eqazyna",
                "source_record_id": f"sale-{index}",
                "price_kind": "verified_sale",
                "observed_at": f"2026-0{index + 1}-01T00:00:00+00:00",
                "eligible": True,
                "quality_grade": "A",
            }
            for index in range(3)
        ],
        "target": {
            "target_id": lot.id,
            "area_ha": 1.0,
            "valuation_at": NOW.isoformat(),
        },
        "provenance_refs": (
            [
                "https://sauda.e-qazyna.kz/protocol/sale-1",
                "https://sauda.e-qazyna.kz/protocol/sale-2",
                "https://sauda.e-qazyna.kz/protocol/sale-3",
            ]
            if provenance
            else []
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _persist_material(
    session: Session, lot: AuctionLot, *, provenance: bool = True, stale: bool = False
) -> tuple[AuctionDecisionSnapshot, AuctionLotGeoCheck]:
    lot.documents.append(
        AuctionDocument(
            title="Извещение",
            source_url="https://sauda.e-qazyna.kz/doc.pdf",
            file_type="pdf",
        )
    )
    geo = AuctionLotGeoCheck(
        lot_id=lot.id,
        boundary_status="verified",
        cadastre_status="verified",
        osm_status="checked",
        engineering_status="checked",
    )
    raw = _payload(lot, provenance=provenance)
    evidence = AuctionEvidence(
        lot_id=lot.id,
        evidence_type="strict_market_estimate",
        status="found",
        title="Строгая рыночная оценка W9",
        value_text=hashlib.sha256(raw.encode()).hexdigest(),
        raw_payload_json=raw,
        observed_at=NOW,
    )
    session.add_all([lot, geo, evidence])
    session.flush()
    snapshot = AuctionDecisionSnapshot(
        lot_id=lot.id,
        engine_version=DECISION_ENGINE_VERSION,
        rules_version=RULES_VERSION,
        verdict_engine_version="auction-verdict.v1",
        scenario_engine_version="auction-scenario-rules.v1",
        price_engine_version="auction-price-ceiling.v1",
        input_hash="a" * 64,
        is_current=True,
        stale=stale,
        verdict="requires_check",
        data_readiness="partial",
        scenario_key="other",
        repeat_attempt_count=0,
        has_repeat=False,
        evidence_generation_ids_json="{}",
        source_freshness_json="{}",
        stale_reasons_json="[]",
        payload_json="{}",
        computed_at=NOW,
        last_validated_at=NOW,
        validated_evidence_id=evidence.id,
        checked_at=NOW,
        created_at=NOW,
    )
    session.add(snapshot)
    session.commit()
    return snapshot, geo


def test_projection_qualifies_only_current_canonical_same_year_market_evidence() -> None:
    with _session() as session:
        lot = _lot()
        snapshot, geo = _persist_material(session, lot)

        result = project_shortlist_results(
            session, [(lot, snapshot, geo)], evaluated_at=NOW
        )[lot.id]

        assert result.eligible is True
        assert result.interesting is True
        assert result.manual_required is False
        assert result.readiness_line == "Данные достаточны для проверки"
        assert result.reasons[0].metric == "80000 vs 100000 KZT/sotka; 20.0% below; n=3"
        assert result.reasons[0].source_url.endswith("/protocol/sale-1")
        assert "тот же календарный год" in result.reasons[0].comparison_method


def test_projection_fails_closed_for_stale_snapshot_even_with_legacy_readiness() -> None:
    with _session() as session:
        lot = _lot()
        snapshot, geo = _persist_material(session, lot, stale=True)

        result = project_shortlist_results(
            session, [(lot, snapshot, geo)], evaluated_at=NOW
        )[lot.id]

        assert result.interesting is False
        assert result.manual_required is False
        assert result.summary == (
            "Данных достаточно для проверки, но нет подтверждённой причины "
            "выделить его среди похожих лотов"
        )


def test_projection_marks_missing_official_comparable_url_for_manual_check() -> None:
    with _session() as session:
        lot = _lot()
        snapshot, geo = _persist_material(session, lot, provenance=False)

        result = project_shortlist_results(
            session, [(lot, snapshot, geo)], evaluated_at=NOW
        )[lot.id]

        assert result.interesting is False
        assert result.manual_required is True
        assert result.unchecked == ("Источник сравнения недоступен; проверить вручную",)


def test_projection_loads_latest_evidence_for_page_in_one_query() -> None:
    with _session() as session:
        rows = []
        for index in range(1, 4):
            lot = _lot(index)
            snapshot, geo = _persist_material(session, lot)
            rows.append((lot, snapshot, geo))
        evidence_selects = 0

        def count_evidence_selects(_conn, _cursor, statement, _params, _ctx, _many):
            nonlocal evidence_selects
            normalized = statement.lower()
            if normalized.lstrip().startswith("select") and "auction_evidence" in normalized:
                evidence_selects += 1

        event.listen(session.get_bind(), "before_cursor_execute", count_evidence_selects)
        try:
            results = project_shortlist_results(session, rows, evaluated_at=NOW)
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", count_evidence_selects)

        assert len(results) == 3
        assert all(item.interesting for item in results.values())
        assert evidence_selects == 1
