"""Worker-side decision materialization.

``engine_version`` identifies this orchestration/fingerprint policy; the separately stored
W10, W11, and W12 versions identify the scenario, price/formula, and verdict policies.
Only the exact current orchestration + W12 rules pair is exposed by the read adapter.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auction_history import auction_object_identity
from app.auction_price_ceiling import ENGINE_VERSION as PRICE_ENGINE_VERSION
from app.auction_price_ceiling import calculate_price_ceiling
from app.auction_scenario_rules import RULES_VERSION as SCENARIO_RULES_VERSION
from app.auction_scenario_rules import evaluate_scenario_rules
from app.auction_taxonomy import (
    SCENARIO_SELECTOR_VERSION,
    UNCLASSIFIED_SCENARIO,
    select_decision_scenario_for_profile,
)
from app.auction_verdict import ENGINE_VERSION as VERDICT_ENGINE_VERSION
from app.auction_verdict import RULES_VERSION as VERDICT_RULES_VERSION
from app.auction_verdict import evaluate_auction_verdict
from app.models import AuctionDecisionSnapshot, AuctionEvidence, AuctionLot

DECISION_ENGINE_VERSION = "decision-snapshot/2026.3"
MAX_INPUT_BYTES = 512_000
MAX_PAYLOAD_BYTES = 256_000
MAX_PERSISTED_ITEM_BYTES = 256_000
MAX_PERSISTED_TOTAL_BYTES = 1_000_000
MAX_MODULES = 32
MAX_STALE_REASONS = 64
MAX_REPEAT_ATTEMPTS = 10_000
PERSISTED_INPUT_PREFIX = "decision_input:"
REQUIRED_EVIDENCE_MODULES = (
    "legal_passport",
    "geometry_context",
    "restriction_context",
    "site_context",
    "planning_context",
    "history_reference",
    "market_estimate",
)
_SQLITE_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class DecisionSnapshotMaterial:
    lot_id: str
    input_hash: str
    verdict: str
    data_readiness: str
    scenario_key: str
    repeat_attempt_count: int
    bid_ceiling_kzt: int | None
    fair_value_low_kzt: int | None
    fair_value_high_kzt: int | None
    formula_version: str | None
    evidence_generation_ids_json: str
    source_freshness_json: str
    stale_reasons_json: str
    payload_json: str
    computed_at: datetime
    checked_at: datetime
    validated_evidence_id: int
    stale: bool


@dataclass(frozen=True, slots=True)
class DecisionLotInput:
    id: str
    updated_at: datetime
    start_price_kzt: float | None
    sale_price_kzt: float | None


class DecisionSnapshotError(ValueError):
    pass


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise DecisionSnapshotError("naive datetime is not allowed")
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _canonical_json(value: object, *, max_bytes: int, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DecisionSnapshotError(f"{label} is not strict JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise DecisionSnapshotError(f"{label} exceeds {max_bytes} bytes")
    return encoded


def _aware(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise DecisionSnapshotError("checked_at must be timezone-aware")
    return result


def _exact_kzt(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 10**15:
        return value
    if isinstance(value, float) and value.is_integer() and 0 <= value <= 10**15:
        return int(value)
    return None


def _fetch_persisted_input_payloads(
    session: Session, lot_id: str
) -> tuple[dict[str, str], int]:
    rows = session.execute(
        select(
            AuctionEvidence.evidence_type,
            AuctionEvidence.id,
            func.substr(
                AuctionEvidence.raw_payload_json,
                1,
                MAX_PERSISTED_ITEM_BYTES + 1,
            ).label("bounded_payload"),
        )
        .where(
            AuctionEvidence.lot_id == lot_id,
            AuctionEvidence.evidence_type.like(f"{PERSISTED_INPUT_PREFIX}%"),
            AuctionEvidence.status.in_(("found", "conflict")),
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        .limit(MAX_MODULES * 4)
    )
    result: dict[str, str] = {}
    total_bytes = 0
    validated_evidence_id = 0
    for row in rows:
        validated_evidence_id = max(validated_evidence_id, int(row.id))
        key = row.evidence_type.removeprefix(PERSISTED_INPUT_PREFIX)
        if key in result or not key or row.bounded_payload is None:
            continue
        item_bytes = len(row.bounded_payload.encode("utf-8"))
        if item_bytes > MAX_PERSISTED_ITEM_BYTES:
            continue
        total_bytes += item_bytes
        if total_bytes > MAX_PERSISTED_TOTAL_BYTES:
            break
        result[key] = row.bounded_payload
    return result, validated_evidence_id


def _parse_persisted_inputs(payloads: dict[str, str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, payload in payloads.items():
        try:
            result[key] = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
    return result


def _repeat_attempt_count(session: Session, lot: AuctionLot) -> int:
    identity = auction_object_identity(lot)
    if identity.value is None:
        return 0
    if identity.kind == "land_object_id":
        identity_condition = AuctionLot.land_object_id == identity.value
    elif identity.kind == "source_object_url":
        identity_condition = AuctionLot.source_object_url == lot.source_object_url
    else:
        identity_condition = AuctionLot.cadastre_number == identity.value
    attempts = int(
        session.scalar(select(func.count(AuctionLot.id)).where(identity_condition))
        or 0
    )
    repeat_count = max(0, attempts - 1)
    if repeat_count > MAX_REPEAT_ATTEMPTS:
        raise DecisionSnapshotError("repeat attempt count exceeds materialization bound")
    return repeat_count


def _validated_metadata(
    outputs: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], list[str]]:
    generation_ids = outputs.get("evidence_generation_ids", {})
    freshness = outputs.get("source_freshness", {})
    supplied_stale = outputs.get("stale_reasons", [])
    if not isinstance(generation_ids, dict) or len(generation_ids) > MAX_MODULES:
        raise DecisionSnapshotError("evidence_generation_ids must be a bounded object")
    if not isinstance(freshness, dict) or len(freshness) > MAX_MODULES:
        raise DecisionSnapshotError("source_freshness must be a bounded object")
    if not isinstance(supplied_stale, list) or len(supplied_stale) > MAX_STALE_REASONS:
        raise DecisionSnapshotError("stale_reasons must be a bounded list")
    stale_reasons = []
    for reason in supplied_stale:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 160:
            raise DecisionSnapshotError("stale reason is invalid")
        stale_reasons.append(reason.strip())
    normalized_freshness: dict[str, object] = {}
    for source, item in freshness.items():
        if not isinstance(source, str) or len(source) > 80 or not isinstance(item, dict):
            raise DecisionSnapshotError("source freshness entry is invalid")
        normalized_item = dict(item)
        status = item.get("status")
        if status not in {"fresh", "stale", "unknown", "error"}:
            raise DecisionSnapshotError("source freshness status is invalid")
        observed_at = item.get("observed_at")
        if observed_at is not None:
            if not isinstance(observed_at, str):
                raise DecisionSnapshotError("source observed_at must be ISO text")
            try:
                parsed = datetime.fromisoformat(observed_at)
            except ValueError as exc:
                raise DecisionSnapshotError("source observed_at is invalid") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise DecisionSnapshotError("source observed_at must be timezone-aware")
        elif status == "fresh":
            status = "unknown"
            normalized_item["status"] = status
        normalized_freshness[source] = normalized_item
        if status != "fresh":
            stale_reasons.append(f"source_{status}:{source}")
    for required in REQUIRED_EVIDENCE_MODULES:
        if required in outputs and required not in normalized_freshness:
            stale_reasons.append(f"source_unknown:{required}")
    return generation_ids, normalized_freshness, list(dict.fromkeys(stale_reasons))


def _selected_scenario(outputs: dict[str, object]) -> dict[str, object]:
    selection = outputs.get("scenario_selection")
    if not isinstance(selection, dict):
        return {
            "status": "requires_check",
            "scenario_key": UNCLASSIFIED_SCENARIO,
            "reason_codes": ["SCENARIO_SELECTION_MISSING"],
            "provenance_refs": [],
        }
    raw_refs = selection.get("provenance_refs", [])
    refs = (
        list(dict.fromkeys(item for item in raw_refs if isinstance(item, str)))[:100]
        if isinstance(raw_refs, list)
        else []
    )
    if selection.get("selector_version") != SCENARIO_SELECTOR_VERSION:
        return {
            "status": "requires_check",
            "scenario_key": UNCLASSIFIED_SCENARIO,
            "reason_codes": ["SCENARIO_SELECTION_VERSION_MISMATCH"],
            "provenance_refs": refs,
        }
    selection_status = selection.get("status")
    selected_key = selection.get("scenario_key")
    profile = selection.get("profile")
    if not isinstance(profile, str) or not profile or len(profile) > 64:
        return {
            "status": "requires_check",
            "scenario_key": UNCLASSIFIED_SCENARIO,
            "reason_codes": ["SCENARIO_SELECTION_INVALID"],
            "provenance_refs": refs,
        }
    canonical = select_decision_scenario_for_profile(profile)
    confirmation_required = selection.get("purpose_confirmation_required")
    selected_consistent = (
        selection_status == "selected"
        and selected_key == canonical.scenario_key
        and canonical.status == "selected"
        and confirmation_required is False
    )
    uncertain_consistent = (
        selection_status == "requires_check"
        and selected_key is None
        and canonical.status == "requires_check"
        and confirmation_required is True
    )
    if uncertain_consistent:
        raw_reasons = selection.get("reason_codes", [])
        reasons = (
            [item for item in raw_reasons if isinstance(item, str)][:20]
            if isinstance(raw_reasons, list)
            else []
        )
        return {
            "status": "requires_check",
            "scenario_key": UNCLASSIFIED_SCENARIO,
            "reason_codes": reasons or ["SCENARIO_UNCLASSIFIED"],
            "provenance_refs": refs,
        }
    if not selected_consistent or not isinstance(selected_key, str):
        return {
            "status": "requires_check",
            "scenario_key": UNCLASSIFIED_SCENARIO,
            "reason_codes": ["SCENARIO_SELECTION_INVALID"],
            "provenance_refs": refs,
        }
    scenario_input = outputs.get("scenario_input")
    if not isinstance(scenario_input, dict):
        return {
            "status": "requires_check",
            "scenario_key": selected_key,
            "provenance_refs": refs,
        }
    raw_analysis = asdict(
        evaluate_scenario_rules(
            scenario_input,
            scenarios=(selected_key,),
        )
    )
    if not isinstance(raw_analysis, dict):
        return {"status": "requires_check", "scenario_key": selected_key, "provenance_refs": refs}
    if raw_analysis.get("rules_version") != SCENARIO_RULES_VERSION:
        return {"status": "requires_check", "scenario_key": selected_key, "provenance_refs": refs}
    results = raw_analysis.get("results")
    if not isinstance(results, (list, tuple)):
        return {"status": "requires_check", "scenario_key": selected_key, "provenance_refs": refs}
    for item in results:
        if isinstance(item, dict) and item.get("scenario") == selected_key:
            status = item.get("status")
            if status not in {"eligible", "requires_check", "blocked"}:
                break
            finding_refs = list(refs)
            for group in (item.get("blockers", []), item.get("checks", [])):
                if isinstance(group, (list, tuple)):
                    for fact in group:
                        if isinstance(fact, dict) and isinstance(
                            fact.get("provenance_refs"), (list, tuple)
                        ):
                            finding_refs.extend(fact["provenance_refs"])
            return {
                "status": status,
                "scenario_key": selected_key,
                "provenance_refs": list(dict.fromkeys(finding_refs)),
            }
    return {"status": "requires_check", "scenario_key": selected_key, "provenance_refs": refs}


def _price_analysis(
    outputs: dict[str, object], selected_scenario: dict[str, object]
) -> dict[str, object]:
    supplied_price_input = outputs.get("price_input")
    if not isinstance(supplied_price_input, dict):
        return {
            "status": "insufficient",
            "recommended_ceiling_kzt": None,
            "provenance_refs": [],
        }
    price_input = dict(supplied_price_input)
    price_input["scenario"] = selected_scenario
    raw = asdict(calculate_price_ceiling(price_input))
    if not isinstance(raw, dict) or raw.get("engine_version") != PRICE_ENGINE_VERSION:
        return {
            "status": "insufficient",
            "recommended_ceiling_kzt": None,
            "provenance_refs": [],
        }
    status = raw.get("status")
    if status not in {"calculated", "insufficient", "blocked", "error"}:
        return {
            "status": "insufficient",
            "recommended_ceiling_kzt": None,
            "provenance_refs": [],
        }
    return raw


def _range_edges(value: object) -> tuple[int | None, int | None]:
    if not isinstance(value, dict):
        return None, None
    return _exact_kzt(value.get("low")), _exact_kzt(value.get("high"))


def build_decision_material(
    lot: AuctionLot | DecisionLotInput,
    *,
    repeat_attempt_count: int,
    scenario_key: str,
    module_outputs: dict[str, object],
    validated_evidence_id: int = 0,
    checked_at: datetime | None = None,
) -> DecisionSnapshotMaterial:
    if not scenario_key or len(scenario_key) > 64 or len(module_outputs) > MAX_MODULES:
        raise DecisionSnapshotError("scenario or module output bounds are invalid")
    if (
        isinstance(repeat_attempt_count, bool)
        or not isinstance(repeat_attempt_count, int)
        or not 0 <= repeat_attempt_count <= MAX_REPEAT_ATTEMPTS
    ):
        raise DecisionSnapshotError("repeat_attempt_count is outside bounds")
    if (
        isinstance(validated_evidence_id, bool)
        or not isinstance(validated_evidence_id, int)
        or validated_evidence_id < 0
    ):
        raise DecisionSnapshotError("validated_evidence_id is invalid")
    checked = _aware(checked_at)
    input_json = _canonical_json(module_outputs, max_bytes=MAX_INPUT_BYTES, label="module input")
    generation_ids, freshness, stale_reasons = _validated_metadata(module_outputs)
    missing_modules = [key for key in REQUIRED_EVIDENCE_MODULES if key not in module_outputs]
    selected_scenario = _selected_scenario(module_outputs)
    effective_scenario_key = str(
        selected_scenario.get("scenario_key") or UNCLASSIFIED_SCENARIO
    )
    price_analysis = _price_analysis(module_outputs, selected_scenario)
    unresolved = [
        {"code": f"MISSING_MODULE_{key.upper()}", "evidence_refs": []}
        for key in missing_modules
    ]
    unresolved.extend(
        {"code": f"STALE_{reason.upper()}", "evidence_refs": []}
        for reason in stale_reasons
    )
    supplied_evidence = module_outputs.get("verdict_evidence", {})
    if not isinstance(supplied_evidence, dict):
        raise DecisionSnapshotError("verdict_evidence must be an object")
    critical_blockers = supplied_evidence.get("critical_blockers", [])
    material_risks = supplied_evidence.get("material_risks", [])
    if not isinstance(critical_blockers, list) or not isinstance(material_risks, list):
        raise DecisionSnapshotError("verdict evidence facts must be lists")
    current_price = _exact_kzt(lot.sale_price_kzt)
    if current_price is None:
        current_price = _exact_kzt(lot.start_price_kzt)
    verdict_input = {
        "scenario": selected_scenario,
        "price_analysis": {
            "status": price_analysis["status"],
            "recommended_ceiling_kzt": price_analysis.get("recommended_ceiling_kzt"),
            "provenance_refs": price_analysis.get("provenance_refs", []),
        },
        "evidence": {
            "critical_facts_complete": not missing_modules and not stale_reasons,
            "critical_blockers": critical_blockers,
            "unresolved_critical": unresolved,
            "material_risks": material_risks,
            "provenance_refs": supplied_evidence.get("provenance_refs", []),
        },
        "pricing": {
            "transaction_mode": "auction",
            "current_price_kzt": current_price,
            "provenance_refs": [f"auction_lot:{lot.id}"],
        },
        "secondary_score": module_outputs.get("secondary_score"),
    }
    verdict_analysis = evaluate_auction_verdict(verdict_input)
    if verdict_analysis.engine_status != "ok" or verdict_analysis.verdict is None:
        raise DecisionSnapshotError(
            f"verdict input rejected: {verdict_analysis.error_code or 'unknown'}"
        )
    readiness = (
        "complete"
        if (
            not missing_modules
            and not stale_reasons
            and verdict_analysis.verdict != "requires_check"
        )
        else "insufficient"
        if missing_modules
        else "partial"
    )
    fair_low, fair_high = _range_edges(price_analysis.get("fair_value_kzt"))
    bid_ceiling = (
        _exact_kzt(price_analysis.get("recommended_ceiling_kzt"))
        if verdict_analysis.verdict == "participate_up_to"
        else None
    )
    action_by_verdict = {
        "participate": "participate",
        "participate_up_to": "participate_up_to",
        "requires_check": "manual_review",
        "high_risk": "manual_review",
        "do_not_participate": "stop",
    }
    contract_status_by_verdict = {
        "participate": "ready",
        "participate_up_to": "ready",
        "requires_check": "manual_required",
        "high_risk": "high_risk",
        "do_not_participate": "blocked",
    }

    def evidence_facts(items: list[object]) -> list[dict[str, object]]:
        return [
            {
                "code": item["code"],
                "evidence_refs": list(item.get("evidence_refs", [])),
            }
            for item in items
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        ]

    unknown_facts = evidence_facts(unresolved)
    blocker_facts = evidence_facts(critical_blockers)
    risk_facts = evidence_facts(material_risks)
    # Missing/stale module facts normally explain ``requires_check``. Some engine
    # gates (for example an unclassified scenario or unavailable current price) do
    # not originate in that list, so preserve their explicit reason rather than
    # emitting a manual-review action with an empty explanation.
    if verdict_analysis.verdict == "requires_check" and not unknown_facts:
        scenario_refs = selected_scenario.get("provenance_refs", [])
        price_refs = price_analysis.get("provenance_refs", [])
        supplied_refs = supplied_evidence.get("provenance_refs", [])
        for reason in verdict_analysis.reason_codes:
            if reason.startswith("SCENARIO_"):
                refs = scenario_refs
            elif reason.startswith(("PRICE_", "RECOMMENDED_CEILING_")):
                refs = price_refs
            elif reason == "CURRENT_PRICE_UNKNOWN":
                refs = [f"auction_lot:{lot.id}"]
            else:
                refs = supplied_refs
            unknown_facts.append({"code": reason, "evidence_refs": list(refs)})

    if blocker_facts:
        action_refs = [ref for fact in blocker_facts for ref in fact["evidence_refs"]]
    elif verdict_analysis.verdict == "requires_check" and unknown_facts:
        action_refs = [ref for fact in unknown_facts for ref in fact["evidence_refs"]]
    elif risk_facts:
        action_refs = [ref for fact in risk_facts for ref in fact["evidence_refs"]]
    elif unknown_facts:
        action_refs = [ref for fact in unknown_facts for ref in fact["evidence_refs"]]
    else:
        action_refs = list(verdict_analysis.evidence_refs)
    action_refs = list(dict.fromkeys(action_refs))

    decision_evidence_contract = {
        "contract_version": "decision-evidence/2026.2",
        "status": contract_status_by_verdict[verdict_analysis.verdict],
        "unknowns": unknown_facts,
        "risks": risk_facts,
        "blockers": blocker_facts,
        "action": {
            "code": action_by_verdict[verdict_analysis.verdict],
            "reason_codes": list(verdict_analysis.reason_codes),
            "evidence_refs": action_refs,
            "recommended_ceiling_kzt": bid_ceiling,
        },
    }
    audit_payload = {
        "decision_engine_version": DECISION_ENGINE_VERSION,
        "scenario_key": effective_scenario_key,
        "scenario": selected_scenario,
        "price_analysis": price_analysis,
        "verdict_analysis": asdict(verdict_analysis),
        "decision_evidence_contract": decision_evidence_contract,
        "missing_modules": missing_modules,
        "stale_reasons": stale_reasons,
        "repeat_attempt_count": repeat_attempt_count,
        "evidence_generation_ids": generation_ids,
        "source_freshness": freshness,
        "input_hash": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
    }
    payload_json = _canonical_json(
        audit_payload, max_bytes=MAX_PAYLOAD_BYTES, label="snapshot payload"
    )
    input_hash = hashlib.sha256(
        _canonical_json(
            {
                "lot_id": lot.id,
                "lot_updated_at": (
                    lot.updated_at.replace(tzinfo=UTC)
                    if lot.updated_at.tzinfo is None
                    else lot.updated_at
                ),
                "scenario_key": effective_scenario_key,
                "repeat_attempt_count": repeat_attempt_count,
                "module_input_hash": audit_payload["input_hash"],
                "decision_engine_version": DECISION_ENGINE_VERSION,
                "verdict_engine_version": VERDICT_ENGINE_VERSION,
                "verdict_rules_version": VERDICT_RULES_VERSION,
            },
            max_bytes=MAX_INPUT_BYTES,
            label="fingerprint input",
        ).encode("utf-8")
    ).hexdigest()
    return DecisionSnapshotMaterial(
        lot_id=lot.id,
        input_hash=input_hash,
        verdict=verdict_analysis.verdict,
        data_readiness=readiness,
        scenario_key=effective_scenario_key,
        repeat_attempt_count=repeat_attempt_count,
        bid_ceiling_kzt=bid_ceiling,
        fair_value_low_kzt=fair_low,
        fair_value_high_kzt=fair_high,
        formula_version=(
            str(price_analysis.get("engine_version"))
            if price_analysis.get("engine_version") == PRICE_ENGINE_VERSION
            else None
        ),
        evidence_generation_ids_json=_canonical_json(
            generation_ids, max_bytes=MAX_PAYLOAD_BYTES, label="generation ids"
        ),
        source_freshness_json=_canonical_json(
            freshness, max_bytes=MAX_PAYLOAD_BYTES, label="source freshness"
        ),
        stale_reasons_json=_canonical_json(
            stale_reasons, max_bytes=MAX_PAYLOAD_BYTES, label="stale reasons"
        ),
        payload_json=payload_json,
        computed_at=checked,
        checked_at=checked,
        validated_evidence_id=validated_evidence_id,
        stale=bool(stale_reasons),
    )


def _persist_material(
    session: Session, material: DecisionSnapshotMaterial
) -> AuctionDecisionSnapshot:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"auction-decision:{material.lot_id}:{VERDICT_RULES_VERSION}"},
        )
    session.scalar(
        select(AuctionLot.id)
        .where(AuctionLot.id == material.lot_id)
        .with_for_update()
        .limit(1)
    )
    existing = session.scalar(
        select(AuctionDecisionSnapshot).where(
            AuctionDecisionSnapshot.lot_id == material.lot_id,
            AuctionDecisionSnapshot.engine_version == DECISION_ENGINE_VERSION,
            AuctionDecisionSnapshot.rules_version == VERDICT_RULES_VERSION,
            AuctionDecisionSnapshot.input_hash == material.input_hash,
        )
    )
    if existing is not None:
        if not existing.is_current:
            session.execute(
                update(AuctionDecisionSnapshot)
                .where(
                    AuctionDecisionSnapshot.lot_id == material.lot_id,
                    AuctionDecisionSnapshot.engine_version == DECISION_ENGINE_VERSION,
                    AuctionDecisionSnapshot.rules_version == VERDICT_RULES_VERSION,
                    AuctionDecisionSnapshot.is_current.is_(True),
                )
                .values(is_current=False, stale=True)
            )
            # Content timestamps/payload remain immutable; only lifecycle flags are restored.
            existing.is_current = True
            existing.stale = material.stale
        existing.last_validated_at = material.checked_at
        existing.validated_evidence_id = max(
            existing.validated_evidence_id,
            material.validated_evidence_id,
        )
        session.flush()
        return existing
    session.execute(
        update(AuctionDecisionSnapshot)
        .where(
            AuctionDecisionSnapshot.lot_id == material.lot_id,
            AuctionDecisionSnapshot.engine_version == DECISION_ENGINE_VERSION,
            AuctionDecisionSnapshot.rules_version == VERDICT_RULES_VERSION,
            AuctionDecisionSnapshot.is_current.is_(True),
        )
        .values(is_current=False, stale=True)
    )
    model = AuctionDecisionSnapshot(
        lot_id=material.lot_id,
        engine_version=DECISION_ENGINE_VERSION,
        rules_version=VERDICT_RULES_VERSION,
        verdict_engine_version=VERDICT_ENGINE_VERSION,
        scenario_engine_version=SCENARIO_RULES_VERSION,
        price_engine_version=PRICE_ENGINE_VERSION,
        formula_version=material.formula_version,
        input_hash=material.input_hash,
        is_current=True,
        stale=material.stale,
        verdict=material.verdict,
        data_readiness=material.data_readiness,
        scenario_key=material.scenario_key,
        repeat_attempt_count=material.repeat_attempt_count,
        has_repeat=material.repeat_attempt_count > 0,
        bid_ceiling_kzt=material.bid_ceiling_kzt,
        fair_value_low_kzt=material.fair_value_low_kzt,
        fair_value_high_kzt=material.fair_value_high_kzt,
        evidence_generation_ids_json=material.evidence_generation_ids_json,
        source_freshness_json=material.source_freshness_json,
        stale_reasons_json=material.stale_reasons_json,
        payload_json=material.payload_json,
        computed_at=material.computed_at,
        last_validated_at=material.checked_at,
        validated_evidence_id=material.validated_evidence_id,
        checked_at=material.checked_at,
        created_at=material.computed_at,
    )
    session.add(model)
    session.flush()
    return model


def recompute_decision_snapshot(
    session_factory: Callable[[], Session],
    lot_id: str,
    *,
    scenario_key: str,
    module_outputs: dict[str, object] | None = None,
    checked_at: datetime | None = None,
) -> AuctionDecisionSnapshot:
    """Worker-only bounded recompute. It performs no network I/O and uses short DB transactions."""
    with session_factory() as read_session:
        lot = read_session.get(AuctionLot, lot_id)
        if lot is None:
            raise DecisionSnapshotError("auction lot not found")
        persisted_payloads, validated_evidence_id = _fetch_persisted_input_payloads(
            read_session, lot_id
        )
        repeat_count = _repeat_attempt_count(read_session, lot)
        lot_input = DecisionLotInput(
            id=lot.id,
            updated_at=(
                lot.updated_at.replace(tzinfo=UTC)
                if lot.updated_at.tzinfo is None
                else lot.updated_at
            ),
            start_price_kzt=lot.start_price_kzt,
            sale_price_kzt=lot.sale_price_kzt,
        )
    persisted = _parse_persisted_inputs(persisted_payloads)
    outputs = {**persisted, **(module_outputs or {})}
    material = build_decision_material(
        lot_input,
        repeat_attempt_count=repeat_count,
        scenario_key=scenario_key,
        module_outputs=outputs,
        validated_evidence_id=validated_evidence_id,
        checked_at=checked_at,
    )
    try:
        with session_factory() as dialect_session:
            sqlite = dialect_session.get_bind().dialect.name == "sqlite"
        lock = _SQLITE_WRITE_LOCK if sqlite else None
        if lock is not None:
            lock.acquire()
        try:
            with session_factory() as write_session, write_session.begin():
                model = _persist_material(write_session, material)
                snapshot_id = model.id
        finally:
            if lock is not None:
                lock.release()
        with session_factory() as result_session:
            result = result_session.get(AuctionDecisionSnapshot, snapshot_id)
            if result is None:
                raise DecisionSnapshotError("persisted snapshot not found")
            result_session.expunge(result)
            return result
    except IntegrityError as conflict_exc:
        with session_factory() as conflict_session, conflict_session.begin():
            existing = _persist_material(conflict_session, material)
            snapshot_id = existing.id
        with session_factory() as result_session:
            result = result_session.get(AuctionDecisionSnapshot, snapshot_id)
            if result is None or not result.is_current:
                raise DecisionSnapshotError(
                    "idempotency conflict did not produce current snapshot"
                ) from conflict_exc
            result_session.expunge(result)
            return result


def read_current_decision_snapshot(
    session: Session,
    lot_id: str,
) -> AuctionDecisionSnapshot | None:
    """Read-only GET adapter: never computes and never writes."""
    return session.scalar(
        select(AuctionDecisionSnapshot)
        .where(
            AuctionDecisionSnapshot.lot_id == lot_id,
            AuctionDecisionSnapshot.engine_version == DECISION_ENGINE_VERSION,
            AuctionDecisionSnapshot.rules_version == VERDICT_RULES_VERSION,
            AuctionDecisionSnapshot.is_current.is_(True),
        )
        .limit(1)
    )
