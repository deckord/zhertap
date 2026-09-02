from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ScenarioStatus = Literal["eligible", "blocked", "requires_check"]
RULES_VERSION = "scenario-rules/2026.1"
BASE_SCENARIOS = ("resale", "operating_business", "land_rent", "sublease", "development")
PROFILE_SCENARIOS = {"camping": "camping", "hospitality": "hospitality"}
SUPPORTED_SCENARIOS = set(BASE_SCENARIOS) | set(PROFILE_SCENARIOS.values())
LONG_CAPEX_SCENARIOS = {"development", "hospitality"}
OPERATING_SCENARIOS = {"operating_business", "development", "camping", "hospitality"}


@dataclass(frozen=True, slots=True)
class ScenarioRuleLimits:
    max_scenarios: int = 10
    max_references: int = 50
    max_items: int = 100
    max_text_length: int = 240


@dataclass(frozen=True, slots=True)
class RuleFinding:
    rule_id: str
    message: str
    provenance_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: str
    status: ScenarioStatus
    blockers: tuple[RuleFinding, ...] = ()
    checks: tuple[RuleFinding, ...] = ()
    assumptions: tuple[RuleFinding, ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioRulesAnalysis:
    rules_version: str
    status: Literal["ok", "error"]
    results: tuple[ScenarioResult, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class ScenarioValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, limits: ScenarioRuleLimits) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioValidationError("invalid_string", f"{field} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > limits.max_text_length:
        raise ScenarioValidationError("string_too_long", f"{field} exceeds text limit")
    return cleaned


def _optional_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ScenarioValidationError("invalid_boolean", f"{field} must be boolean or null")
    return value


def _optional_number(value: object, field: str, maximum: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioValidationError("invalid_number", f"{field} must be numeric or null")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise ScenarioValidationError("invalid_number", f"{field} is outside allowed bounds")
    return number


def _references(value: object, field: str, limits: ScenarioRuleLimits) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limits.max_references:
        raise ScenarioValidationError("invalid_references", f"{field} references are invalid")
    return tuple(_text(item, field, limits) for item in value)


def _string_items(value: object, field: str, limits: ScenarioRuleLimits) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limits.max_items:
        raise ScenarioValidationError("invalid_items", f"{field} items are invalid")
    return tuple(_text(item, field, limits) for item in value)


def _object(value: object, field: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScenarioValidationError("invalid_object", f"{field} must be an object")
    return value


def _finding(rule_id: str, message: str, refs: tuple[str, ...]) -> RuleFinding:
    return RuleFinding(rule_id=rule_id, message=message, provenance_refs=refs)


def evaluate_scenario_rules(
    normalized_input: object,
    *,
    scenarios: tuple[str, ...] | None = None,
    rules_version: str = RULES_VERSION,
    limits: ScenarioRuleLimits = ScenarioRuleLimits(),
) -> ScenarioRulesAnalysis:
    """Apply deterministic eligibility rules; returns no investment recommendation."""
    if rules_version != RULES_VERSION:
        return ScenarioRulesAnalysis(
            rules_version=rules_version,
            status="error",
            error_code="unsupported_rules_version",
            error_message=f"Supported rules version is {RULES_VERSION}",
        )
    try:
        if not isinstance(normalized_input, dict):
            raise ScenarioValidationError("invalid_input", "Scenario input must be an object")
        profile = _text(normalized_input.get("profile", "other"), "profile", limits).casefold()
        requested = list(scenarios or BASE_SCENARIOS)
        profile_scenario = PROFILE_SCENARIOS.get(profile)
        if scenarios is None and profile_scenario:
            requested.append(profile_scenario)
        if not requested or len(requested) > limits.max_scenarios:
            raise ScenarioValidationError(
                "invalid_scenarios", "Scenario list is empty or oversized"
            )
        if len(set(requested)) != len(requested) or any(
            scenario not in SUPPORTED_SCENARIOS for scenario in requested
        ):
            raise ScenarioValidationError("invalid_scenarios", "Scenario list is invalid")

        right = _object(normalized_input.get("right"), "right")
        legal = _object(normalized_input.get("legal_passport"), "legal_passport")
        restriction = _object(normalized_input.get("restriction_context"), "restriction_context")
        site = _object(normalized_input.get("site_context"), "site_context")
        planning = _object(normalized_input.get("planning_context"), "planning_context")
        geometry = _object(normalized_input.get("geometry_context"), "geometry_context")

        right_type = right.get("type", "unknown")
        if right_type not in {"ownership", "lease", "unknown"}:
            raise ScenarioValidationError("invalid_right_type", "Right type is invalid")
        lease_years = _optional_number(right.get("lease_years"), "lease_years", 1_000)
        transferable = _optional_bool(right.get("transferable"), "transferable")
        renewable = _optional_bool(right.get("renewable"), "renewable")
        sublease_allowed = _optional_bool(right.get("sublease_allowed"), "sublease_allowed")
        right_refs = _references(right.get("provenance_refs"), "right", limits)

        legal_status = legal.get("status", "unknown")
        if legal_status not in {"clear", "unknown", "conflict", "error"}:
            raise ScenarioValidationError("invalid_legal_status", "Legal status is invalid")
        use_allowed = _optional_bool(legal.get("use_allowed"), "use_allowed")
        legal_refs = _references(legal.get("provenance_refs"), "legal", limits)

        restriction_status = restriction.get("status", "unknown")
        if restriction_status not in {
            "clear",
            "restricted",
            "partial",
            "unknown",
            "conflict",
            "error",
        }:
            raise ScenarioValidationError(
                "invalid_restriction_status", "Restriction status is invalid"
            )
        authoritative_blockers = _string_items(
            restriction.get("authoritative_blockers"), "restriction.blockers", limits
        )
        critical_blockers = _string_items(
            restriction.get("critical_blockers"), "restriction.critical_blockers", limits
        )
        whole_parcel_prohibited = _optional_bool(
            restriction.get("whole_parcel_prohibited"),
            "restriction.whole_parcel_prohibited",
        )
        usable_area = _optional_number(
            restriction.get("usable_area_m2"), "usable_area_m2", 1_000_000_000_000
        )
        restriction_complete = _optional_bool(
            restriction.get("coverage_complete"), "restriction.coverage_complete"
        )
        restriction_refs = _references(restriction.get("provenance_refs"), "restriction", limits)
        scenario_minimums_raw = restriction.get("scenario_minimum_usable_area_m2", {})
        if (
            not isinstance(scenario_minimums_raw, dict)
            or len(scenario_minimums_raw) > limits.max_items
        ):
            raise ScenarioValidationError(
                "invalid_scenario_minimums",
                "Scenario usable-area minimums are invalid or oversized",
            )
        scenario_minimums = {}
        for scenario_code, minimum in scenario_minimums_raw.items():
            if scenario_code not in SUPPORTED_SCENARIOS:
                raise ScenarioValidationError(
                    "invalid_scenario_minimum",
                    "Usable-area minimum references unsupported scenario",
                )
            scenario_minimums[scenario_code] = _optional_number(
                minimum,
                f"minimum.{scenario_code}",
                1_000_000_000_000,
            )

        access_status = site.get("physical_access_status", "unknown")
        legal_access_status = site.get("legal_access_status", "unknown")
        infrastructure_status = site.get("infrastructure_status", "unknown")
        capacity_status = site.get("capacity_status", "unknown")
        allowed_site_statuses = {"ready", "attention", "blocked", "unknown", "error"}
        if any(
            value not in allowed_site_statuses
            for value in (
                access_status,
                legal_access_status,
                infrastructure_status,
                capacity_status,
            )
        ):
            raise ScenarioValidationError("invalid_site_status", "Site status is invalid")
        site_refs = _references(site.get("provenance_refs"), "site", limits)

        planning_status = planning.get("status", "unknown")
        if planning_status not in {"clear", "partial", "unknown", "conflict", "error"}:
            raise ScenarioValidationError("invalid_planning_status", "Planning status is invalid")
        current_use_allowed = _optional_bool(
            planning.get("current_use_allowed"), "current_use_allowed"
        )
        pdp_complete = _optional_bool(planning.get("pdp_complete"), "pdp_complete")
        future_adverse = _string_items(
            planning.get("future_adverse"), "planning.future_adverse", limits
        )
        planning_refs = _references(planning.get("provenance_refs"), "planning", limits)

        geometry_status = geometry.get("status", "unknown")
        if geometry_status not in {"ok", "unknown", "error"}:
            raise ScenarioValidationError("invalid_geometry_status", "Geometry status is invalid")
        geometry_refs = _references(geometry.get("provenance_refs"), "geometry", limits)

        results = []
        for scenario in requested:
            blockers: list[RuleFinding] = []
            checks: list[RuleFinding] = []
            assumptions: list[RuleFinding] = []

            if legal_status == "conflict":
                blockers.append(
                    _finding("LEGAL_CONFLICT", "Legal passport contains a conflict", legal_refs)
                )
            elif legal_status in {"unknown", "error"}:
                checks.append(
                    _finding("LEGAL_STATUS_CHECK", "Legal passport is not definitive", legal_refs)
                )
            if use_allowed is False and scenario in OPERATING_SCENARIOS:
                blockers.append(
                    _finding(
                        "CURRENT_USE_FORBIDDEN", "Current legal use forbids scenario", legal_refs
                    )
                )
            elif use_allowed is None and scenario in OPERATING_SCENARIOS:
                checks.append(
                    _finding("CURRENT_USE_CHECK", "Current legal use is unknown", legal_refs)
                )

            scenario_minimum = scenario_minimums.get(scenario, 0.0)
            unusable = (
                bool(critical_blockers)
                or whole_parcel_prohibited is True
                or (
                    restriction_complete is True
                    and usable_area is not None
                    and scenario_minimum is not None
                    and usable_area <= scenario_minimum
                )
            )
            if restriction_status == "conflict":
                blockers.append(
                    _finding(
                        "RESTRICTION_CONFLICT",
                        "Authoritative restriction sources conflict",
                        restriction_refs,
                    )
                )
            elif restriction_status == "error":
                checks.append(
                    _finding(
                        "RESTRICTION_ERROR_CHECK",
                        "Restriction analysis has an error",
                        restriction_refs,
                    )
                )
            if unusable:
                blockers.append(
                    _finding(
                        "AUTHORITATIVE_UNUSABLE_RESTRICTION",
                        "Critical restriction or usable-area minimum blocks scenario",
                        restriction_refs,
                    )
                )
            elif authoritative_blockers:
                checks.append(
                    _finding(
                        "AUTHORITATIVE_RESTRICTION_FACT_CHECK",
                        "Review authoritative restriction facts: "
                        + ", ".join(authoritative_blockers),
                        restriction_refs,
                    )
                )
            elif restriction_complete is not True or restriction_status in {"partial", "unknown"}:
                checks.append(
                    _finding(
                        "RESTRICTION_COVERAGE_CHECK",
                        "Restriction coverage or usable area is incomplete",
                        restriction_refs,
                    )
                )

            if current_use_allowed is False and scenario in OPERATING_SCENARIOS:
                blockers.append(
                    _finding(
                        "PLANNING_CURRENT_USE_CONFLICT",
                        "Current planning use conflicts with scenario",
                        planning_refs,
                    )
                )
            if planning_status == "conflict" and not future_adverse:
                blockers.append(
                    _finding(
                        "PLANNING_UNSPECIFIED_CONFLICT",
                        "Planning context has an unresolved authoritative conflict",
                        planning_refs,
                    )
                )
            elif planning_status == "error":
                checks.append(
                    _finding(
                        "PLANNING_ERROR_CHECK", "Planning analysis has an error", planning_refs
                    )
                )
            if future_adverse:
                message = "Future planning constraints: " + ", ".join(future_adverse)
                if scenario in {"development", "camping", "hospitality"}:
                    blockers.append(_finding("FUTURE_PLANNING_ADVERSE", message, planning_refs))
                else:
                    checks.append(_finding("FUTURE_PLANNING_CHECK", message, planning_refs))
            if pdp_complete is not True:
                checks.append(
                    _finding(
                        "PDP_COVERAGE_CHECK", "PDP coverage is missing or incomplete", planning_refs
                    )
                )

            if legal_access_status == "blocked" and scenario in OPERATING_SCENARIOS:
                blockers.append(
                    _finding("LEGAL_ACCESS_BLOCKED", "Legal access is blocked", site_refs)
                )
            elif legal_access_status != "ready":
                checks.append(
                    _finding("LEGAL_ACCESS_CHECK", "Legal access is not confirmed", site_refs)
                )
            if access_status == "blocked":
                if scenario in OPERATING_SCENARIOS:
                    blockers.append(
                        _finding(
                            "PHYSICAL_ACCESS_BLOCKED",
                            "Physical access is blocked",
                            site_refs,
                        )
                    )
                else:
                    checks.append(
                        _finding(
                            "PHYSICAL_ACCESS_MARKETABILITY_CHECK",
                            "Blocked physical access affects transfer or rental feasibility",
                            site_refs,
                        )
                    )
            elif access_status != "ready":
                checks.append(
                    _finding("PHYSICAL_ACCESS_CHECK", "Physical access is not confirmed", site_refs)
                )
            if infrastructure_status != "ready" and scenario in OPERATING_SCENARIOS:
                checks.append(
                    _finding(
                        "INFRASTRUCTURE_CHECK",
                        "Infrastructure readiness is not confirmed",
                        site_refs,
                    )
                )
            if capacity_status != "ready" and scenario in OPERATING_SCENARIOS:
                checks.append(
                    _finding("CAPACITY_CHECK", "Required capacity is not confirmed", site_refs)
                )
            if geometry_status != "ok":
                checks.append(
                    _finding(
                        "GEOMETRY_CHECK", "Parcel geometry is unknown or invalid", geometry_refs
                    )
                )

            if scenario == "resale":
                if right_type == "lease":
                    blockers.append(
                        _finding(
                            "RESALE_REQUIRES_OWNERSHIP",
                            "Lease right cannot be treated as resale of owned land",
                            right_refs,
                        )
                    )
                elif right_type == "unknown":
                    checks.append(
                        _finding("RIGHT_TYPE_CHECK", "Ownership right is unknown", right_refs)
                    )
            if scenario in {"land_rent", "sublease"} and right_type == "lease":
                if transferable is False:
                    blockers.append(
                        _finding(
                            "LEASE_TRANSFER_FORBIDDEN",
                            "Lease right is explicitly non-transferable",
                            right_refs,
                        )
                    )
                elif transferable is None:
                    checks.append(
                        _finding(
                            "LEASE_TRANSFER_PERMISSION_CHECK",
                            "Transferability of the lease right is unknown",
                            right_refs,
                        )
                    )
                if sublease_allowed is False:
                    blockers.append(
                        _finding(
                            "SUBLEASE_FORBIDDEN", "Sublease is explicitly forbidden", right_refs
                        )
                    )
                elif sublease_allowed is None:
                    checks.append(
                        _finding(
                            "SUBLEASE_PERMISSION_CHECK",
                            "Sublease permission is unknown",
                            right_refs,
                        )
                    )
            if scenario == "sublease" and right_type == "ownership":
                assumptions.append(
                    _finding(
                        "SUBLEASE_MODE_ASSUMPTION",
                        "Scenario is treated as granting a derived lease from ownership",
                        right_refs,
                    )
                )
            if right_type == "unknown":
                checks.append(
                    _finding("RIGHT_TYPE_CHECK", "Land right type is unknown", right_refs)
                )

            short_lease = right_type == "lease" and lease_years is not None and lease_years <= 5
            if right_type == "lease" and lease_years is None:
                checks.append(_finding("LEASE_TERM_CHECK", "Lease term is unknown", right_refs))
            if short_lease and scenario in LONG_CAPEX_SCENARIOS:
                if transferable is not True or renewable is not True:
                    blockers.append(
                        _finding(
                            "SHORT_LEASE_LONG_CAPEX_BLOCK",
                            "Short lease lacks explicit transferability and renewal for long CAPEX",
                            right_refs,
                        )
                    )
                else:
                    checks.append(
                        _finding(
                            "SHORT_LEASE_PAYBACK_CHECK",
                            "Verify long-CAPEX payback within the secured lease horizon",
                            right_refs,
                        )
                    )
            elif short_lease and scenario == "operating_business":
                checks.append(
                    _finding(
                        "SHORT_LEASE_PAYBACK_CHECK",
                        "Verify operating-business payback within short lease",
                        right_refs,
                    )
                )

            if scenario == "camping":
                assumptions.append(
                    _finding(
                        "CAMPING_PROFILE_ASSUMPTION",
                        "Scenario assumes camping/recreation use, not owned-land resale",
                        legal_refs,
                    )
                )
                if right_type == "lease" and lease_years is not None and lease_years <= 3:
                    checks.extend(
                        (
                            _finding(
                                "CAMPING_LEGAL_TERMS_CHECK",
                                "Verify temporary structures and development duties",
                                legal_refs,
                            ),
                            _finding(
                                "CAMPING_RENEWAL_CHECK",
                                "Verify enforceable renewal terms",
                                right_refs,
                            ),
                            _finding(
                                "CAMPING_PAYBACK_CHECK",
                                "Verify camping CAPEX payback within three-year lease",
                                right_refs,
                            ),
                        )
                    )
            if scenario == "hospitality":
                assumptions.append(
                    _finding(
                        "HOSPITALITY_PROFILE_ASSUMPTION",
                        "Scenario assumes permanent hospitality operations",
                        legal_refs,
                    )
                )

            blockers = list(dict.fromkeys(blockers))
            checks = list(dict.fromkeys(checks))
            assumptions = list(dict.fromkeys(assumptions))
            status: ScenarioStatus = (
                "blocked" if blockers else "requires_check" if checks else "eligible"
            )
            results.append(
                ScenarioResult(
                    scenario=scenario,
                    status=status,
                    blockers=tuple(blockers),
                    checks=tuple(checks),
                    assumptions=tuple(assumptions),
                )
            )
        return ScenarioRulesAnalysis(
            rules_version=RULES_VERSION,
            status="ok",
            results=tuple(results),
        )
    except ScenarioValidationError as exc:
        return ScenarioRulesAnalysis(
            rules_version=RULES_VERSION,
            status="error",
            error_code=exc.code,
            error_message=str(exc),
        )
