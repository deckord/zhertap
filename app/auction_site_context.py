from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

DimensionStatus = Literal["ready", "attention", "blocked", "unknown", "error"]
Readiness = Literal["ready", "partial", "not_ready", "unknown", "error"]
SUPPORTED_PROFILES = {
    "retail",
    "roadside",
    "warehouse",
    "hospitality",
    "camping",
    "residential",
    "data_center",
    "agriculture",
    "other",
}
RADIUS_BUCKETS_M = (500, 1_000, 3_000, 5_000)
SERVICE_CODES = {"electricity", "water", "sewer", "gas", "internet"}
ENVIRONMENT_CATEGORIES = {
    "settlement",
    "competitor",
    "school",
    "stop",
    "industry",
    "rail",
    "water",
    "nature",
    "tourism",
    "cemetery",
    "landfill",
    "power_line",
}
PROFILE_WEIGHTS = {
    "retail": (0.40, 0.25, 0.35),
    "roadside": (0.50, 0.25, 0.25),
    "warehouse": (0.45, 0.35, 0.20),
    "hospitality": (0.30, 0.30, 0.40),
    "camping": (0.35, 0.25, 0.40),
    "residential": (0.30, 0.35, 0.35),
    "data_center": (0.25, 0.60, 0.15),
    "agriculture": (0.30, 0.40, 0.30),
    "other": (0.34, 0.33, 0.33),
}
PROFILE_SERVICES = {
    "retail": {"electricity", "water", "internet"},
    "roadside": {"electricity", "water"},
    "warehouse": {"electricity"},
    "hospitality": {"electricity", "water", "sewer", "internet"},
    "camping": {"electricity", "water", "sewer"},
    "residential": {"electricity", "water", "sewer"},
    "data_center": {"electricity", "water", "internet"},
    "agriculture": {"electricity", "water"},
    "other": set(),
}


@dataclass(frozen=True, slots=True)
class SiteContextLimits:
    max_features: int = 500
    max_services: int = 16
    max_string_length: int = 240
    max_radius_m: float = 5_000
    max_distance_m: float = 100_000
    max_cost_kzt: float = 10_000_000_000


@dataclass(frozen=True, slots=True)
class DimensionAssessment:
    status: DimensionStatus
    readiness: Readiness
    weight: float
    facts: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    confidence: float | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SiteContextAnalysis:
    profile: str
    physical_access: DimensionAssessment
    legal_access: DimensionAssessment
    infrastructure: DimensionAssessment
    environment: DimensionAssessment
    profile_weights: dict[str, float]


class ContextValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, limits: SiteContextLimits) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextValidationError("invalid_string", f"{field} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > limits.max_string_length:
        raise ContextValidationError("string_too_long", f"{field} exceeds string limit")
    return cleaned


def _number(
    value: object,
    field: str,
    *,
    maximum: float,
    allow_none: bool = True,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContextValidationError("invalid_number", f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise ContextValidationError("invalid_number", f"{field} is outside allowed bounds")
    return number


def _boolean(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ContextValidationError("invalid_boolean", f"{field} must be boolean or null")
    return value


def _metadata(
    value: object,
    limits: SiteContextLimits,
) -> tuple[bool, str, float | None]:
    if not isinstance(value, dict):
        raise ContextValidationError("evidence_missing", "Evidence metadata is required")
    provenance = _text(value.get("provenance"), "provenance", limits)
    observed_at = _text(value.get("observed_at"), "observed_at", limits)
    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextValidationError("invalid_timestamp", "observed_at must be ISO-8601") from exc
    complete = _boolean(value.get("coverage_complete"), "coverage_complete")
    confidence = _number(value.get("confidence"), "confidence", maximum=1)
    return bool(complete), f"{provenance} @ {observed_at}", confidence


def _error(weight: float, exc: ContextValidationError) -> DimensionAssessment:
    return DimensionAssessment(
        status="error",
        readiness="error",
        weight=weight,
        error_code=exc.code,
        error_message=str(exc),
    )


def _physical_access(
    value: object,
    weight: float,
    limits: SiteContextLimits,
) -> DimensionAssessment:
    if value is None:
        return DimensionAssessment(status="unknown", readiness="unknown", weight=weight)
    try:
        if not isinstance(value, dict):
            raise ContextValidationError("invalid_access", "Physical access must be an object")
        complete, provenance, confidence = _metadata(value.get("evidence"), limits)
        connected = _boolean(value.get("connected"), "connected")
        distance = _number(
            value.get("road_distance_m"),
            "road_distance_m",
            maximum=limits.max_distance_m,
        )
        frontage = _number(
            value.get("frontage_m"),
            "frontage_m",
            maximum=limits.max_distance_m,
        )
        facts = []
        warnings = []
        blockers = []
        if distance is not None:
            facts.append(f"Road distance: {distance:g} m")
        for key, label in (("surface", "Surface"), ("road_class", "Road class")):
            if value.get(key) is not None:
                facts.append(f"{label}: {_text(value[key], key, limits)}")
        if frontage is not None:
            facts.append(f"Frontage: {frontage:g} m")
        if value.get("turn_constraints") is not None:
            warnings.append(_text(value["turn_constraints"], "turn_constraints", limits))
        if connected is False:
            blockers.append(
                "Road is near or mapped, but no physical connection to the parcel is proven"
            )
        elif connected is None:
            warnings.append("Physical road connectivity is unknown")
        if not complete:
            warnings.append("Physical-access provider coverage is incomplete")
        status: DimensionStatus = (
            "blocked" if blockers else "ready" if connected and complete else "attention"
        )
        readiness: Readiness = (
            "not_ready" if blockers else "ready" if status == "ready" else "partial"
        )
        return DimensionAssessment(
            status=status,
            readiness=readiness,
            weight=weight,
            facts=tuple(facts),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            provenance=(provenance,),
            confidence=confidence,
        )
    except ContextValidationError as exc:
        return _error(weight, exc)


def _legal_access(
    value: object,
    weight: float,
    limits: SiteContextLimits,
) -> DimensionAssessment:
    if value is None:
        return DimensionAssessment(status="unknown", readiness="unknown", weight=weight)
    try:
        if not isinstance(value, dict):
            raise ContextValidationError("invalid_access", "Legal access must be an object")
        complete, provenance, confidence = _metadata(value.get("evidence"), limits)
        public = _boolean(value.get("public_road_access"), "public_road_access")
        easement = _boolean(value.get("easement_confirmed"), "easement_confirmed")
        servitude = _boolean(value.get("servitude_required"), "servitude_required")
        facts = []
        warnings = []
        blockers = []
        if public is True:
            facts.append("Legal access from a public road is evidenced")
        if easement is True:
            facts.append("Registered access easement is evidenced")
        if servitude is True:
            warnings.append("A servitude is required for access")
        proven = public is True or easement is True
        if complete and public is False and easement is False:
            blockers.append("No legal public-road access or registered easement was found")
        elif not proven:
            warnings.append("Legal access remains unconfirmed")
        if not complete:
            warnings.append("Legal-access evidence coverage is incomplete")
        status: DimensionStatus = "blocked" if blockers else "ready" if proven else "unknown"
        readiness: Readiness = "not_ready" if blockers else "ready" if proven else "unknown"
        return DimensionAssessment(
            status=status,
            readiness=readiness,
            weight=weight,
            facts=tuple(facts),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            provenance=(provenance,),
            confidence=confidence,
        )
    except ContextValidationError as exc:
        return _error(weight, exc)


def _infrastructure(
    value: object,
    profile: str,
    weight: float,
    limits: SiteContextLimits,
) -> DimensionAssessment:
    if value is None:
        return DimensionAssessment(status="unknown", readiness="unknown", weight=weight)
    try:
        if not isinstance(value, dict):
            raise ContextValidationError(
                "invalid_infrastructure", "Infrastructure must be an object"
            )
        services = value.get("services")
        if not isinstance(services, dict) or len(services) > limits.max_services:
            raise ContextValidationError("invalid_services", "Services are missing or exceed limit")
        dimension_metadata = value.get("evidence")
        dimension_complete: bool | None = None
        dimension_provenance: str | None = None
        dimension_confidence: float | None = None
        if dimension_metadata is not None:
            dimension_complete, dimension_provenance, dimension_confidence = _metadata(
                dimension_metadata,
                limits,
            )
        unknown_codes = set(services) - SERVICE_CODES
        if unknown_codes:
            raise ContextValidationError(
                "unsupported_service", "Unsupported infrastructure service"
            )
        required = PROFILE_SERVICES[profile]
        facts = []
        warnings = []
        blockers = []
        provenances = [dimension_provenance] if dimension_provenance else []
        confidences = [dimension_confidence] if dimension_confidence is not None else []
        for code in sorted(SERVICE_CODES):
            service = services.get(code)
            if service is None:
                if code in required:
                    warnings.append(f"{code}: connection, capacity and cost are unknown")
                continue
            if not isinstance(service, dict):
                raise ContextValidationError("invalid_service", f"{code} must be an object")
            complete, provenance, confidence = _metadata(service.get("evidence"), limits)
            provenances.append(provenance)
            if confidence is not None:
                confidences.append(confidence)
            distance = _number(
                service.get("distance_m"),
                f"{code}.distance_m",
                maximum=limits.max_distance_m,
            )
            status = service.get("connection_status", "unknown")
            capacity = service.get("capacity_status", "unknown")
            if status not in {"confirmed", "available", "planned", "unavailable", "unknown"}:
                raise ContextValidationError(
                    "invalid_connection_status", f"{code} status is invalid"
                )
            if capacity not in {"confirmed", "sufficient", "insufficient", "unknown"}:
                raise ContextValidationError(
                    "invalid_capacity_status", f"{code} capacity is invalid"
                )
            cost_min = _number(
                service.get("cost_min_kzt"),
                f"{code}.cost_min_kzt",
                maximum=limits.max_cost_kzt,
            )
            cost_max = _number(
                service.get("cost_max_kzt"),
                f"{code}.cost_max_kzt",
                maximum=limits.max_cost_kzt,
            )
            if cost_min is not None and cost_max is not None and cost_min > cost_max:
                raise ContextValidationError("invalid_cost_range", f"{code} cost range is inverted")
            if distance is not None:
                facts.append(f"{code}: mapped distance {distance:g} m")
            if status in {"confirmed", "available"}:
                facts.append(f"{code}: connection {status}")
            elif status == "unavailable" and code in required:
                blockers.append(f"{code}: required connection is unavailable")
            elif code in required:
                warnings.append(f"{code}: connection is {status}; distance alone proves nothing")
            if capacity == "insufficient" and code in required:
                blockers.append(f"{code}: required capacity is insufficient")
            elif capacity == "unknown" and code in required:
                warnings.append(f"{code}: capacity is unknown")
            if cost_min is None or cost_max is None:
                if code in required:
                    warnings.append(f"{code}: connection cost range is unknown")
            else:
                facts.append(f"{code}: connection cost {cost_min:g}–{cost_max:g} KZT")
            if not complete and code in required:
                warnings.append(f"{code}: evidence coverage is incomplete")
        if dimension_complete is False:
            warnings.append("Infrastructure inventory coverage is explicitly incomplete")
        elif dimension_complete is None:
            warnings.append("Infrastructure inventory coverage is unknown")
        if not services and dimension_complete is None:
            return DimensionAssessment(
                status="unknown",
                readiness="unknown",
                weight=weight,
                warnings=tuple(warnings),
            )
        status: DimensionStatus = "blocked" if blockers else "attention" if warnings else "ready"
        readiness: Readiness = "not_ready" if blockers else "partial" if warnings else "ready"
        return DimensionAssessment(
            status=status,
            readiness=readiness,
            weight=weight,
            facts=tuple(facts),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            provenance=tuple(dict.fromkeys(provenances)),
            confidence=min(confidences) if confidences else None,
        )
    except ContextValidationError as exc:
        return _error(weight, exc)


def _environment(
    value: object,
    profile: str,
    weight: float,
    limits: SiteContextLimits,
) -> DimensionAssessment:
    if value is None:
        return DimensionAssessment(status="unknown", readiness="unknown", weight=weight)
    try:
        if not isinstance(value, dict):
            raise ContextValidationError("invalid_environment", "Environment must be an object")
        features = value.get("features")
        coverage = value.get("coverage")
        if not isinstance(features, list):
            raise ContextValidationError("invalid_features", "Environment features must be a list")
        if len(features) > limits.max_features:
            raise ContextValidationError("too_many_features", "Environment feature limit exceeded")
        if not isinstance(coverage, dict):
            raise ContextValidationError("coverage_missing", "Radius coverage metadata is required")
        coverage_complete = {}
        provenances = []
        confidences = []
        for radius in RADIUS_BUCKETS_M:
            metadata = coverage.get(str(radius)) or coverage.get(radius)
            if metadata is None:
                coverage_complete[radius] = False
                continue
            complete, provenance, confidence = _metadata(metadata, limits)
            coverage_complete[radius] = complete
            provenances.append(provenance)
            if confidence is not None:
                confidences.append(confidence)
        facts = []
        warnings = []
        blockers = []
        for feature in features:
            if not isinstance(feature, dict):
                raise ContextValidationError(
                    "invalid_feature", "Environment feature must be an object"
                )
            category = _text(feature.get("category"), "category", limits).casefold()
            if category not in ENVIRONMENT_CATEGORIES:
                raise ContextValidationError(
                    "unsupported_environment_category",
                    "Environment category must use the normalized provider taxonomy",
                )
            name = _text(feature.get("name", category), "name", limits)
            distance = _number(
                feature.get("distance_m"),
                "feature.distance_m",
                maximum=limits.max_radius_m,
                allow_none=False,
            )
            assert distance is not None
            bucket = next(radius for radius in RADIUS_BUCKETS_M if distance <= radius)
            facts.append(f"{category}: {name}, {distance:g} m (within {bucket:g} m)")
            if category == "landfill" and distance <= 1_000:
                blockers.append("Landfill within 1 km")
            elif category == "cemetery" and distance <= 500:
                warnings.append("Cemetery within 500 m")
            elif (
                category == "industry"
                and distance <= 1_000
                and profile
                in {
                    "residential",
                    "hospitality",
                    "camping",
                }
            ):
                warnings.append("Industrial object within 1 km")
            elif category == "rail" and distance <= 500:
                warnings.append("Railway within 500 m")
            elif category in {"water", "nature", "tourism"} and profile in {
                "hospitality",
                "camping",
            }:
                facts.append(f"Profile-relevant attraction: {category}")
        incomplete = [str(radius) for radius, complete in coverage_complete.items() if not complete]
        if incomplete:
            warnings.append("Environment coverage incomplete for radii: " + ", ".join(incomplete))
        status: DimensionStatus = "blocked" if blockers else "attention" if warnings else "ready"
        readiness: Readiness = "not_ready" if blockers else "partial" if warnings else "ready"
        return DimensionAssessment(
            status=status,
            readiness=readiness,
            weight=weight,
            facts=tuple(facts),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            provenance=tuple(dict.fromkeys(provenances)),
            confidence=min(confidences) if confidences else None,
        )
    except (ContextValidationError, StopIteration) as exc:
        if isinstance(exc, ContextValidationError):
            return _error(weight, exc)
        return _error(
            weight, ContextValidationError("radius_out_of_bounds", "Feature radius invalid")
        )


def analyze_site_context(
    profile: str,
    *,
    physical_access: object | None = None,
    legal_access: object | None = None,
    infrastructure: object | None = None,
    environment: object | None = None,
    limits: SiteContextLimits = SiteContextLimits(),
) -> SiteContextAnalysis:
    """Analyze normalized precomputed facts without I/O.

    Service and environment codes must use ``SERVICE_CODES`` and
    ``ENVIRONMENT_CATEGORIES``. Localized provider labels are mapped upstream;
    unknown labels are explicit errors, never silently treated as harmless.
    """
    normalized_profile = profile.strip().casefold() if isinstance(profile, str) else ""
    if normalized_profile not in SUPPORTED_PROFILES:
        normalized_profile = "other"
    access_weight, infrastructure_weight, environment_weight = PROFILE_WEIGHTS[normalized_profile]
    # Access weight is intentionally shared by two independent assessments; it is
    # descriptive profile metadata, never summed into a universal score.
    return SiteContextAnalysis(
        profile=normalized_profile,
        physical_access=_physical_access(physical_access, access_weight, limits),
        legal_access=_legal_access(legal_access, access_weight, limits),
        infrastructure=_infrastructure(
            infrastructure,
            normalized_profile,
            infrastructure_weight,
            limits,
        ),
        environment=_environment(environment, normalized_profile, environment_weight, limits),
        profile_weights={
            "access": access_weight,
            "infrastructure": infrastructure_weight,
            "environment": environment_weight,
        },
    )
