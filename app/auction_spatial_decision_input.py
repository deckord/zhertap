"""Bounded read-only adapter from persisted spatial evidence to W10 inputs."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auction_parcel_geometry import analyze_parcel_geometry
from app.auction_planning_context import ADVERSE_KINDS, REQUIRED_COVERAGE, analyze_planning_context
from app.auction_restriction_context import (
    REQUIRED_RESTRICTION_LAYERS,
    analyze_restriction_context,
)
from app.auction_scenario_rules import evaluate_scenario_rules
from app.auction_site_context import SUPPORTED_PROFILES, analyze_site_context
from app.auction_taxonomy import (
    UNCLASSIFIED_SCENARIO,
    select_decision_scenario_for_profile,
)
from app.models import AuctionEvidence

ASSEMBLER_VERSION = "spatial-decision-input/2026.2"
MAX_ROWS = 24
MAX_ITEM_BYTES = 256_000
MAX_TOTAL_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 512_000
MAX_PROVENANCE = 100
MAX_SOURCE_URL_LENGTH = 1_000
MAX_CLOCK_SKEW = timedelta(minutes=5)
SOURCE_TYPES = {
    "parcel": "decision_input:parcel_geometry_source",
    "restrictions": "decision_input:restriction_source",
    "site": "decision_input:site_source",
    "planning": "decision_input:planning_source",
    "legal": "decision_input:legal_passport",
}
ACTUAL_EVIDENCE_TYPES = ("cadastre_boundary",)


@dataclass(frozen=True, slots=True)
class SpatialEvidenceInput:
    key: str
    evidence_id: int
    payload: dict[str, object]
    observed_at: datetime
    source_url: str | None
    status: Literal["found", "conflict"]


@dataclass(frozen=True, slots=True)
class SpatialDecisionInputResult:
    status: Literal["ready", "requires_check", "error"]
    assembler_version: str
    scenario_key: str
    decision_inputs: dict[str, object]
    source_freshness: dict[str, object]
    evidence_generation_ids: dict[str, object]
    stale_reasons: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    error_code: str | None = None
    error_message: str | None = None

    def as_persistable_dict(self) -> dict[str, object]:
        payload = {
            **self.decision_inputs,
            "source_freshness": self.source_freshness,
            "evidence_generation_ids": self.evidence_generation_ids,
            "stale_reasons": list(self.stale_reasons),
            "spatial_assembler_version": self.assembler_version,
        }
        _strict_json(payload, max_bytes=MAX_OUTPUT_BYTES, label="persistable output")
        return payload


class SpatialInputError(ValueError):
    pass


def _strict_json(value: object, *, max_bytes: int, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SpatialInputError(f"{label} is not strict JSON") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise SpatialInputError(f"{label} exceeds byte limit")
    return encoded


def load_spatial_evidence(session: Session, lot_id: str) -> dict[str, SpatialEvidenceInput]:
    """Read latest bounded evidence using SQL substr; performs no writes."""
    rows = session.execute(
        select(
            AuctionEvidence.id,
            AuctionEvidence.evidence_type,
            AuctionEvidence.status,
            AuctionEvidence.observed_at,
            func.substr(AuctionEvidence.source_url, 1, MAX_SOURCE_URL_LENGTH + 1).label(
                "bounded_source_url"
            ),
            func.substr(AuctionEvidence.raw_payload_json, 1, MAX_ITEM_BYTES + 1).label(
                "bounded_payload"
            ),
        )
        .where(
            AuctionEvidence.lot_id == lot_id,
            AuctionEvidence.evidence_type.in_(
                (*tuple(SOURCE_TYPES.values()), *ACTUAL_EVIDENCE_TYPES)
            ),
            AuctionEvidence.status.in_(("found", "conflict")),
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        .limit(MAX_ROWS)
    )
    aliases = {value: key for key, value in SOURCE_TYPES.items()}
    result: dict[str, SpatialEvidenceInput] = {}
    priorities: dict[str, int] = {}
    aggregate = 0
    for row in rows:
        key = aliases.get(row.evidence_type, "parcel")
        priority = 2 if row.evidence_type in aliases else 1
        if priorities.get(key, -1) >= priority or row.bounded_payload is None:
            continue
        if row.bounded_source_url and len(row.bounded_source_url) > MAX_SOURCE_URL_LENGTH:
            continue
        size = len(row.bounded_payload.encode("utf-8"))
        if size > MAX_ITEM_BYTES:
            continue
        aggregate += size
        if aggregate > MAX_TOTAL_BYTES:
            break
        try:
            payload = json.loads(row.bounded_payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if row.evidence_type == "cadastre_boundary":
            geometry = payload.get("geometry_geojson")
            if not isinstance(geometry, dict):
                continue
            source_layer = payload.get("source_layer")
            payload = {
                "parcel_geojson": geometry,
                "generation_id": f"auction_evidence:{row.id}",
                "source": {
                    "authoritative": True,
                    "coverage_complete": True,
                    "version": str(source_layer or "egkn-current")[:120],
                    "provenance": "ЕГКН cadastre_boundary",
                },
            }
        observed_at = row.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        result[key] = SpatialEvidenceInput(
            key=key,
            evidence_id=int(row.id),
            payload=payload,
            observed_at=observed_at,
            source_url=row.bounded_source_url,
            status=row.status,
        )
        priorities[key] = priority
    return result


def _parcel_source_complete(record: SpatialEvidenceInput | None) -> bool:
    if record is None:
        return False
    source = record.payload.get("source")
    return (
        isinstance(source, dict)
        and source.get("authoritative") is True
        and source.get("coverage_complete") is True
        and isinstance(source.get("version"), str)
        and bool(source["version"].strip())
        and len(source["version"]) <= 120
        and isinstance(source.get("provenance"), str)
        and bool(source["provenance"].strip())
        and len(source["provenance"]) <= 240
    )


def _provenance(record: SpatialEvidenceInput | None) -> list[str]:
    if record is None:
        return []
    refs = [f"auction_evidence:{record.evidence_id}"]
    if record.source_url:
        refs.append(record.source_url)
    source = record.payload.get("source")
    if isinstance(source, dict) and isinstance(source.get("provenance"), str):
        refs.append(source["provenance"])
    return refs


def _lease_years(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 1_000 else None


def _normalize_legal(value: object) -> tuple[dict[str, object], dict[str, object], list[str]]:
    if not isinstance(value, dict):
        return (
            {"status": "unknown", "use_allowed": None, "provenance_refs": []},
            {
                "type": "unknown",
                "lease_years": None,
                "transferable": None,
                "renewable": None,
                "sublease_allowed": None,
                "provenance_refs": [],
            },
            [],
        )
    if value.get("status") in {"clear", "unknown", "conflict", "error"}:
        right = value.get("right") if isinstance(value.get("right"), dict) else {}
        refs = value.get("provenance_refs")
        refs = (
            [item for item in refs if isinstance(item, str)][:MAX_PROVENANCE]
            if isinstance(refs, list)
            else []
        )
        return (
            {
                "status": value["status"],
                "use_allowed": value.get("use_allowed")
                if isinstance(value.get("use_allowed"), bool)
                else None,
                "provenance_refs": refs,
            },
            {
                "type": right.get("type")
                if right.get("type") in {"ownership", "lease"}
                else "unknown",
                "lease_years": _lease_years(right.get("lease_years")),
                "transferable": right.get("transferable")
                if isinstance(right.get("transferable"), bool)
                else None,
                "renewable": right.get("renewable")
                if isinstance(right.get("renewable"), bool)
                else None,
                "sublease_allowed": right.get("sublease_allowed")
                if isinstance(right.get("sublease_allowed"), bool)
                else None,
                "provenance_refs": refs,
            },
            refs,
        )
    facts = value.get("facts")
    if not isinstance(facts, dict):
        return _normalize_legal(None)
    critical = ("right_type", "purpose", "restrictions", "arrests", "encumbrances")
    statuses = [
        facts.get(key, {}).get("status") if isinstance(facts.get(key), dict) else None
        for key in critical
    ]
    status = (
        "conflict"
        if "conflict" in statuses
        else "clear"
        if all(item == "found" for item in statuses)
        else "unknown"
    )
    right_fact = facts.get("right_type") if isinstance(facts.get("right_type"), dict) else {}
    lease_fact = (
        facts.get("lease_term_years") if isinstance(facts.get("lease_term_years"), dict) else {}
    )
    refs = []
    for fact in facts.values():
        if isinstance(fact, dict) and isinstance(fact.get("source_url"), str):
            refs.append(fact["source_url"])
    refs = list(dict.fromkeys(refs))[:MAX_PROVENANCE]
    right_type = right_fact.get("value") if right_fact.get("status") == "found" else "unknown"
    return (
        {"status": status, "use_allowed": None, "provenance_refs": refs},
        {
            "type": right_type if right_type in {"ownership", "lease"} else "unknown",
            "lease_years": (
                _lease_years(lease_fact.get("value"))
                if lease_fact.get("status") == "found"
                else None
            ),
            "transferable": None,
            "renewable": None,
            "sublease_allowed": None,
            "provenance_refs": refs,
        },
        refs,
    )


def _freshness(
    record: SpatialEvidenceInput | None,
    *,
    now: datetime,
    max_age: timedelta,
) -> dict[str, object]:
    if record is None:
        return {"status": "unknown", "observed_at": None}
    if record.observed_at > now + MAX_CLOCK_SKEW:
        status = "error"
    else:
        status = "stale" if now - record.observed_at > max_age else "fresh"
    if record.status == "conflict":
        status = "error"
    return {
        "status": status,
        "observed_at": record.observed_at.isoformat(),
        "evidence_id": record.evidence_id,
    }


def assemble_spatial_decision_inputs(
    evidence: dict[str, SpatialEvidenceInput],
    *,
    profile: str,
    legal_passport: object | None = None,
    now: datetime | None = None,
    max_age_days: int = 30,
) -> SpatialDecisionInputResult:
    """Run W4→W7 and emit strict persistable W10 input without inferring coverage."""
    try:
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise SpatialInputError("now must be timezone-aware")
        aggregate_input_bytes = 0
        if len(evidence) > len(SOURCE_TYPES):
            raise SpatialInputError("too many spatial evidence inputs")
        for key, record in evidence.items():
            if key not in SOURCE_TYPES or not isinstance(record, SpatialEvidenceInput):
                raise SpatialInputError("spatial evidence key or record is invalid")
            if (
                isinstance(record.evidence_id, bool)
                or not isinstance(record.evidence_id, int)
                or record.evidence_id < 0
                or record.observed_at.tzinfo is None
                or record.observed_at.utcoffset() is None
                or record.status not in {"found", "conflict"}
                or (record.source_url is not None and len(record.source_url) > 2048)
            ):
                raise SpatialInputError("spatial evidence metadata is invalid")
            encoded = _strict_json(
                record.payload,
                max_bytes=MAX_ITEM_BYTES,
                label=f"{key} evidence",
            )
            aggregate_input_bytes += len(encoded.encode("utf-8"))
            if aggregate_input_bytes > MAX_TOTAL_BYTES:
                raise SpatialInputError("spatial evidence aggregate exceeds byte limit")
        if legal_passport is not None:
            legal_json = _strict_json(
                legal_passport,
                max_bytes=MAX_ITEM_BYTES,
                label="legal passport",
            )
            aggregate_input_bytes += len(legal_json.encode("utf-8"))
            if aggregate_input_bytes > MAX_TOTAL_BYTES:
                raise SpatialInputError("spatial/legal input aggregate exceeds byte limit")
        if isinstance(max_age_days, bool) or not isinstance(max_age_days, int):
            raise SpatialInputError("max_age_days must be an integer")
        bounded_age = max(1, min(max_age_days, 3650))
        source_profile = profile.casefold().strip() if isinstance(profile, str) else "other"
        if len(source_profile) > 64:
            raise SpatialInputError("profile exceeds text limit")
        selection = select_decision_scenario_for_profile(source_profile)
        normalized_profile = source_profile
        if normalized_profile not in SUPPORTED_PROFILES:
            normalized_profile = "other"
        scenario_key = selection.scenario_key or UNCLASSIFIED_SCENARIO
        parcel_record = evidence.get("parcel")
        restriction_record = evidence.get("restrictions")
        site_record = evidence.get("site")
        planning_record = evidence.get("planning")
        legal_record = evidence.get("legal")
        legal_value = (
            legal_passport
            if legal_passport is not None
            else (legal_record.payload if legal_record else None)
        )
        legal_context, right_context, legal_refs = _normalize_legal(legal_value)

        parcel_payload = parcel_record.payload if _parcel_source_complete(parcel_record) else {}
        parcel_geojson = parcel_payload.get("parcel_geojson")
        geometry = analyze_parcel_geometry(
            parcel_geojson,
            road_edge_geojson=parcel_payload.get("road_edge_geojson"),
            road_edge_confidence=parcel_payload.get("road_edge_confidence"),
            road_edge_provenance=parcel_payload.get("road_edge_provenance"),
        )
        restriction_payload = restriction_record.payload if restriction_record else {}
        expected_layers_raw = restriction_payload.get("expected_layers")
        if expected_layers_raw is not None and (
            not isinstance(expected_layers_raw, list)
            or any(not isinstance(item, str) for item in expected_layers_raw)
        ):
            raise SpatialInputError("restriction expected-layer checklist is invalid")
        expected_layers = (
            tuple(expected_layers_raw)
            if isinstance(expected_layers_raw, list)
            else REQUIRED_RESTRICTION_LAYERS
        )
        restrictions = analyze_restriction_context(
            parcel_geojson,
            restriction_sources=restriction_payload.get("restriction_sources"),
            restriction_features=restriction_payload.get("restriction_features"),
            expected_layers=expected_layers,
        )
        site_payload = site_record.payload if site_record else {}
        site = analyze_site_context(
            normalized_profile,
            physical_access=site_payload.get("physical_access"),
            legal_access=site_payload.get("legal_access"),
            infrastructure=site_payload.get("infrastructure"),
            environment=site_payload.get("environment"),
        )
        planning_payload = planning_record.payload if planning_record else {}
        planning = analyze_planning_context(
            parcel_geojson,
            planning_sources=planning_payload.get("planning_sources"),
            planning_features=planning_payload.get("planning_features"),
        )

        geometry_refs = _provenance(parcel_record)
        restriction_refs = _provenance(restriction_record)
        site_refs = _provenance(site_record)
        planning_refs = _provenance(planning_record)
        restriction_complete = bool(restrictions.layers) and all(
            layer.coverage_complete for layer in restrictions.layers
        )
        whole_parcel_prohibited = (
            restriction_complete
            and restrictions.usable_area_m2 is not None
            and restrictions.usable_area_m2 <= 0
        )
        pdp_coverage = {
            (item.document_type, item.layer): item.complete for item in planning.coverage
        }
        pdp_complete = all(
            pdp_coverage.get((document, layer)) is True
            for document, layer in REQUIRED_COVERAGE
            if document == "pdp"
        )
        current_allowed_values = [
            relation.allowed_use
            for relation in planning.current_relations
            if relation.intersects and relation.kind == "current_zone"
        ]
        current_use_allowed = (
            False
            if False in current_allowed_values
            else True
            if current_allowed_values and all(value is True for value in current_allowed_values)
            else None
        )
        future_adverse = list(
            dict.fromkeys(
                relation.kind
                for relation in planning.future_relations
                if relation.intersects and relation.kind in ADVERSE_KINDS
            )
        )
        site_provenance = list(
            dict.fromkeys(
                site_refs
                + list(site.physical_access.provenance)
                + list(site.legal_access.provenance)
                + list(site.infrastructure.provenance)
                + list(site.environment.provenance)
            )
        )[:MAX_PROVENANCE]
        capacity_status = (
            site.infrastructure.status
            if site.infrastructure.status in {"ready", "blocked", "error"}
            else "unknown"
        )
        scenario_input = {
            "profile": normalized_profile,
            "right": right_context,
            "legal_passport": legal_context,
            "restriction_context": {
                "status": restrictions.status,
                "coverage_complete": restriction_complete,
                "usable_area_m2": restrictions.usable_area_m2,
                "authoritative_blockers": list(restrictions.blockers),
                "critical_blockers": (
                    ["WHOLE_PARCEL_RESTRICTION"] if whole_parcel_prohibited else []
                ),
                "whole_parcel_prohibited": True if whole_parcel_prohibited else None,
                "provenance_refs": restriction_refs,
            },
            "site_context": {
                "physical_access_status": site.physical_access.status,
                "legal_access_status": site.legal_access.status,
                "infrastructure_status": site.infrastructure.status,
                "capacity_status": capacity_status,
                "provenance_refs": site_provenance,
            },
            "planning_context": {
                "status": planning.status,
                "current_use_allowed": current_use_allowed,
                "pdp_complete": pdp_complete,
                "future_adverse": future_adverse,
                "provenance_refs": planning_refs,
            },
            "geometry_context": {
                "status": geometry.status,
                "provenance_refs": geometry_refs,
            },
        }
        records = {
            "geometry_context": parcel_record,
            "restriction_context": restriction_record,
            "site_context": site_record,
            "planning_context": planning_record,
            "legal_passport": legal_record,
        }
        max_age = timedelta(days=bounded_age)
        freshness = {
            key: _freshness(record, now=checked_at, max_age=max_age)
            for key, record in records.items()
        }
        if parcel_record is not None and not _parcel_source_complete(parcel_record):
            freshness["geometry_context"] = {
                "status": "error",
                "observed_at": parcel_record.observed_at.isoformat(),
                "evidence_id": parcel_record.evidence_id,
            }
        if legal_passport is not None and legal_record is None:
            generated_at = (
                legal_passport.get("generated_at") if isinstance(legal_passport, dict) else None
            )
            if isinstance(generated_at, str):
                try:
                    parsed = datetime.fromisoformat(generated_at)
                except ValueError:
                    parsed = None
                if parsed is not None and parsed.tzinfo is not None:
                    legal_freshness_status = (
                        "error"
                        if parsed > checked_at + MAX_CLOCK_SKEW
                        else "stale"
                        if checked_at - parsed > max_age
                        else "fresh"
                    )
                    freshness["legal_passport"] = {
                        "status": legal_freshness_status,
                        "observed_at": parsed.isoformat(),
                    }
        stale_reasons = [
            f"source_{item['status']}:{key}"
            for key, item in freshness.items()
            if item["status"] != "fresh"
        ]
        generation_ids = {}
        for key, record in records.items():
            generation = record.payload.get("generation_id") if record else None
            if (
                isinstance(generation, int)
                and not isinstance(generation, bool)
                and 0 <= generation <= 10**15
            ) or (
                isinstance(generation, str) and bool(generation.strip()) and len(generation) <= 128
            ):
                generation_ids[key] = generation
        module_outputs = {
            "scenario_input": scenario_input,
            "legal_passport": legal_context,
            "geometry_context": asdict(geometry),
            "restriction_context": asdict(restrictions),
            "site_context": asdict(site),
            "planning_context": asdict(planning),
            "scenario_selection": {
                **selection.as_payload(provenance_refs=tuple(legal_refs)),
                "policy_version": ASSEMBLER_VERSION,
                "assumption": "canonical evidenced-purpose scenario selector",
            },
        }
        if selection.status != "selected":
            scenario_input["legal_passport"]["status"] = "unknown"
        if selection.scenario_key is None:
            scenario_status = "requires_check"
        else:
            analysis = evaluate_scenario_rules(
                scenario_input, scenarios=(selection.scenario_key,)
            )
            scenario_status = (
                analysis.results[0].status
                if analysis.status == "ok" and analysis.results
                else "requires_check"
            )
        status = (
            "ready"
            if scenario_status == "eligible"
            and not stale_reasons
            and selection.status == "selected"
            else "requires_check"
        )
        provenance = tuple(
            dict.fromkeys(
                legal_refs + geometry_refs + restriction_refs + site_provenance + planning_refs
            )
        )[:MAX_PROVENANCE]
        result = SpatialDecisionInputResult(
            status=status,
            assembler_version=ASSEMBLER_VERSION,
            scenario_key=scenario_key,
            decision_inputs=module_outputs,
            source_freshness=freshness,
            evidence_generation_ids=generation_ids,
            stale_reasons=tuple(stale_reasons),
            provenance_refs=provenance,
        )
        result.as_persistable_dict()
        return result
    except SpatialInputError as exc:
        return SpatialDecisionInputResult(
            status="error",
            assembler_version=ASSEMBLER_VERSION,
            scenario_key=UNCLASSIFIED_SCENARIO,
            decision_inputs={},
            source_freshness={},
            evidence_generation_ids={},
            stale_reasons=(),
            provenance_refs=(),
            error_code="invalid_spatial_input",
            error_message=str(exc),
        )
