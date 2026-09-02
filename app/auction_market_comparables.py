from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

ENGINE_VERSION = "strict-market-comparables.v2-same-year"
MAX_HARD_CANDIDATES = 500

RightType = Literal["ownership", "lease"]
PriceKind = Literal["verified_sale", "listing"]
Readiness = Literal["none", "partial", "ready", "unknown"]
ResultStatus = Literal["ok", "insufficient_data", "invalid_target", "invalid_input"]


@dataclass(frozen=True, slots=True)
class ComparableTarget:
    target_id: str
    right_type: RightType
    purpose_group: str
    area_ha: float
    valuation_at: datetime
    locality: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    lease_term_years: float | None = None
    access_readiness: Readiness = "unknown"
    infrastructure_readiness: Readiness = "unknown"


@dataclass(frozen=True, slots=True)
class ComparableCandidate:
    source_id: str
    source_record_id: str
    source_url: str
    title: str
    right_type: RightType
    purpose_group: str
    area_ha: float
    price_kzt: float
    price_kind: PriceKind
    observed_at: datetime | None
    locality: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    lease_term_years: float | None = None
    access_readiness: Readiness = "unknown"
    infrastructure_readiness: Readiness = "unknown"
    object_id: str | None = None


@dataclass(frozen=True, slots=True)
class ComparableConfig:
    radius_km: float = 5.0
    area_tolerance_fraction: float = 0.30
    max_age_days: int = 365
    high_quality_max_age_days: int = 180
    high_quality_area_tolerance_fraction: float = 0.20
    max_readiness_step_difference: int = 1
    access_adjustment_per_step: float = 0.05
    infrastructure_adjustment_per_step: float = 0.075
    lease_year_adjustment: float = 0.01
    lease_adjustment_cap: float = 0.10
    minimum_high_quality_verified: int = 3
    max_candidates: int = 200


@dataclass(frozen=True, slots=True)
class ComparableAdjustment:
    dimension: str
    factor: float
    rationale: str


@dataclass(frozen=True, slots=True)
class ComparableEvaluation:
    source_id: str
    source_record_id: str
    source_url: str
    object_id: str | None
    price_kind: PriceKind
    observed_at: datetime
    age_days: int | None
    distance_km: float | None
    eligible: bool
    exclusion_reason: str | None
    duplicate_of: str | None
    quality_grade: str
    price_kzt: float | None
    price_per_ha_kzt: float | None
    adjusted_price_per_ha_kzt: float | None
    adjusted_target_value_kzt: float | None
    adjustments: tuple[ComparableAdjustment, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.isoformat() if self.observed_at else None
        return payload


@dataclass(frozen=True, slots=True)
class MarketEstimate:
    median_kzt: float
    range_low_kzt: float
    range_high_kzt: float
    median_price_per_ha_kzt: float
    range_low_price_per_ha_kzt: float
    range_high_price_per_ha_kzt: float
    verified_comparables_used: int


@dataclass(frozen=True, slots=True)
class MarketComparableResult:
    status: ResultStatus
    estimate: MarketEstimate | None
    confidence: str
    high_quality_verified_count: int
    verified_eligible_count: int
    listing_eligible_count: int
    evaluations: tuple[ComparableEvaluation, ...]
    detail: str
    engine_version: str = ENGINE_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "estimate": asdict(self.estimate) if self.estimate else None,
            "confidence": self.confidence,
            "high_quality_verified_count": self.high_quality_verified_count,
            "verified_eligible_count": self.verified_eligible_count,
            "listing_eligible_count": self.listing_eligible_count,
            "evaluations": [evaluation.as_dict() for evaluation in self.evaluations],
            "detail": self.detail,
            "engine_version": self.engine_version,
        }


_READINESS = {"none": 0, "partial": 1, "ready": 2}


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > limit:
        return None
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return normalized if normalized and len(normalized) <= limit else None


def _finite(value: object, *, lower: float, upper: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and lower <= float(value) <= upper
    )


def _aware(value: datetime) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _coordinates(latitude: float | None, longitude: float | None) -> bool:
    return _finite(latitude, lower=40, upper=56) and _finite(
        longitude, lower=46, upper=88
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _lease_band(years: float) -> int:
    if years <= 3:
        return 1
    if years <= 10:
        return 2
    return 3


def _bounded_config(config: ComparableConfig) -> ComparableConfig:
    return ComparableConfig(
        radius_km=max(0.1, min(float(config.radius_km), 100.0)),
        area_tolerance_fraction=max(0.05, min(float(config.area_tolerance_fraction), 0.50)),
        max_age_days=max(1, min(int(config.max_age_days), 3650)),
        high_quality_max_age_days=max(
            1, min(int(config.high_quality_max_age_days), int(config.max_age_days), 730)
        ),
        high_quality_area_tolerance_fraction=max(
            0.01,
            min(
                float(config.high_quality_area_tolerance_fraction),
                float(config.area_tolerance_fraction),
            ),
        ),
        max_readiness_step_difference=max(
            0, min(int(config.max_readiness_step_difference), 1)
        ),
        access_adjustment_per_step=max(
            0.0, min(float(config.access_adjustment_per_step), 0.15)
        ),
        infrastructure_adjustment_per_step=max(
            0.0, min(float(config.infrastructure_adjustment_per_step), 0.20)
        ),
        lease_year_adjustment=max(0.0, min(float(config.lease_year_adjustment), 0.03)),
        lease_adjustment_cap=max(0.0, min(float(config.lease_adjustment_cap), 0.25)),
        minimum_high_quality_verified=max(
            3, min(int(config.minimum_high_quality_verified), 10)
        ),
        max_candidates=max(1, min(int(config.max_candidates), MAX_HARD_CANDIDATES)),
    )


def _config_error(config: ComparableConfig) -> str | None:
    numeric_values = (
        config.radius_km,
        config.area_tolerance_fraction,
        config.max_age_days,
        config.high_quality_max_age_days,
        config.high_quality_area_tolerance_fraction,
        config.max_readiness_step_difference,
        config.access_adjustment_per_step,
        config.infrastructure_adjustment_per_step,
        config.lease_year_adjustment,
        config.lease_adjustment_cap,
        config.minimum_high_quality_verified,
        config.max_candidates,
    )
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        for value in numeric_values
    ):
        return "invalid_nonfinite_config"
    return None


def _target_error(target: ComparableTarget) -> str | None:
    if _text(target.target_id, 128) is None:
        return "invalid_target_id"
    if target.right_type not in {"ownership", "lease"}:
        return "invalid_right_type"
    if _text(target.purpose_group, 160) is None:
        return "invalid_purpose_group"
    if target.locality is not None and _text(target.locality, 160) is None:
        return "invalid_locality"
    if not _finite(target.area_ha, lower=0.0001, upper=1_000_000):
        return "invalid_area"
    if not _aware(target.valuation_at):
        return "valuation_at_not_timezone_aware"
    if target.right_type == "lease" and not _finite(
        target.lease_term_years, lower=0.01, upper=99
    ):
        return "invalid_lease_term"
    if (
        target.access_readiness not in _READINESS
        or target.infrastructure_readiness not in _READINESS
    ):
        return "unknown_target_readiness"
    if (target.latitude is not None or target.longitude is not None) and not _coordinates(
        target.latitude, target.longitude
    ):
        return "invalid_target_coordinates"
    has_coordinates = _coordinates(target.latitude, target.longitude)
    if not has_coordinates and _text(target.locality or "", 160) is None:
        return "missing_target_geography"
    return None


def _stable_key(candidate: ComparableCandidate) -> tuple[str, ...]:
    return (
        _text(candidate.object_id or "", 128) or "~",
        _text(candidate.source_id, 128) or "~",
        _text(candidate.source_record_id, 128) or "~",
        candidate.observed_at.isoformat() if _aware(candidate.observed_at) else "~",
        str(candidate.price_kind),
        repr(candidate.price_kzt),
        _text(candidate.title, 320) or "~",
    )


def _identity(candidate: ComparableCandidate) -> str:
    object_id = _text(candidate.object_id or "", 128)
    if object_id:
        return f"object:{object_id}"
    return f"source:{_text(candidate.source_id, 128)}:{_text(candidate.source_record_id, 128)}"


def _rank(candidate: ComparableCandidate) -> tuple[int, float, str, float, str]:
    timestamp = (
        candidate.observed_at.timestamp() if _aware(candidate.observed_at) else float("-inf")
    )
    return (
        1 if candidate.price_kind == "verified_sale" else 0,
        timestamp,
        str(candidate.source_url),
        float(candidate.price_kzt)
        if _finite(candidate.price_kzt, lower=1, upper=1_000_000_000_000_000)
        else float("-inf"),
        str(candidate.title)[:320],
    )


def _invalid_candidate_reason(candidate: ComparableCandidate) -> str | None:
    if any(
        _text(value, limit) is None
        for value, limit in (
            (candidate.source_id, 128),
            (candidate.source_record_id, 128),
            (candidate.source_url, 2048),
            (candidate.title, 320),
            (candidate.purpose_group, 160),
        )
    ):
        return "invalid_or_unbounded_text"
    if candidate.locality is not None and _text(candidate.locality, 160) is None:
        return "invalid_or_unbounded_text"
    if candidate.object_id is not None and _text(candidate.object_id, 128) is None:
        return "invalid_or_unbounded_text"
    if candidate.right_type not in {"ownership", "lease"}:
        return "invalid_right_type"
    if candidate.price_kind not in {"verified_sale", "listing"}:
        return "invalid_price_kind"
    if not _finite(candidate.area_ha, lower=0.0001, upper=1_000_000):
        return "invalid_area"
    if not _finite(candidate.price_kzt, lower=1, upper=1_000_000_000_000_000):
        return "invalid_price"
    if not _aware(candidate.observed_at):
        return "observed_at_not_timezone_aware"
    if (candidate.latitude is not None or candidate.longitude is not None) and not _coordinates(
        candidate.latitude, candidate.longitude
    ):
        return "invalid_coordinates"
    return None


def _empty_evaluation(
    candidate: ComparableCandidate,
    reason: str,
    *,
    duplicate_of: str | None = None,
    age_days: int | None = None,
    distance_km: float | None = None,
) -> ComparableEvaluation:
    return ComparableEvaluation(
        source_id=str(candidate.source_id)[:128],
        source_record_id=str(candidate.source_record_id)[:128],
        source_url=str(candidate.source_url)[:2048],
        object_id=str(candidate.object_id)[:128] if candidate.object_id is not None else None,
        price_kind=candidate.price_kind,
        observed_at=candidate.observed_at if _aware(candidate.observed_at) else None,
        age_days=age_days,
        distance_km=distance_km,
        eligible=False,
        exclusion_reason=reason,
        duplicate_of=duplicate_of,
        quality_grade="duplicate" if duplicate_of else "excluded",
        price_kzt=None,
        price_per_ha_kzt=None,
        adjusted_price_per_ha_kzt=None,
        adjusted_target_value_kzt=None,
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _outlier_indexes(evaluations: list[ComparableEvaluation]) -> set[int]:
    indexes = [
        index
        for index, item in enumerate(evaluations)
        if item.eligible
        and item.price_kind == "verified_sale"
        and item.quality_grade == "A"
        and item.adjusted_price_per_ha_kzt is not None
    ]
    if len(indexes) < 4:
        return set()
    values = [float(evaluations[index].adjusted_price_per_ha_kzt) for index in indexes]
    median = _percentile(values, 0.5)
    deviations = [abs(value - median) for value in values]
    mad = _percentile(deviations, 0.5)
    if mad <= 0:
        return {
            index
            for index in indexes
            if abs(float(evaluations[index].adjusted_price_per_ha_kzt) - median)
            > max(1.0, median * 0.25)
        }
    return {
        index
        for index in indexes
        if abs(float(evaluations[index].adjusted_price_per_ha_kzt) - median) / mad > 3.5
    }


def _evaluate_candidate(
    target: ComparableTarget,
    candidate: ComparableCandidate,
    config: ComparableConfig,
) -> ComparableEvaluation:
    invalid_reason = _invalid_candidate_reason(candidate)
    if invalid_reason:
        return _empty_evaluation(candidate, invalid_reason)
    age_days = (target.valuation_at.date() - candidate.observed_at.date()).days
    if age_days < 0:
        return _empty_evaluation(candidate, "future_observation", age_days=age_days)
    if age_days > config.max_age_days:
        return _empty_evaluation(candidate, "stale", age_days=age_days)
    candidate_in_target_timezone = candidate.observed_at.astimezone(
        target.valuation_at.tzinfo
    )
    if candidate_in_target_timezone.year != target.valuation_at.year:
        return _empty_evaluation(
            candidate,
            "different_calendar_year",
            age_days=age_days,
        )
    if candidate.right_type != target.right_type:
        return _empty_evaluation(candidate, "right_type_mismatch", age_days=age_days)
    if _text(candidate.purpose_group, 160) != _text(target.purpose_group, 160):
        return _empty_evaluation(candidate, "purpose_mismatch", age_days=age_days)
    area_deviation = abs(candidate.area_ha / target.area_ha - 1)
    if area_deviation > config.area_tolerance_fraction:
        return _empty_evaluation(candidate, "area_mismatch", age_days=age_days)

    distance_km = None
    if _coordinates(target.latitude, target.longitude) and _coordinates(
        candidate.latitude, candidate.longitude
    ):
        distance_km = _haversine_km(
            float(target.latitude),
            float(target.longitude),
            float(candidate.latitude),
            float(candidate.longitude),
        )
        if distance_km > config.radius_km:
            return _empty_evaluation(
                candidate, "outside_radius", age_days=age_days, distance_km=round(distance_km, 3)
            )
    elif _text(candidate.locality or "", 160) != _text(target.locality or "", 160):
        return _empty_evaluation(candidate, "geography_unknown_or_mismatch", age_days=age_days)

    if (
        candidate.access_readiness not in _READINESS
        or candidate.infrastructure_readiness not in _READINESS
    ):
        return _empty_evaluation(
            candidate,
            "unknown_access_or_infrastructure",
            age_days=age_days,
            distance_km=distance_km,
        )
    access_difference = _READINESS[target.access_readiness] - _READINESS[candidate.access_readiness]
    infrastructure_difference = (
        _READINESS[target.infrastructure_readiness]
        - _READINESS[candidate.infrastructure_readiness]
    )
    if abs(access_difference) > config.max_readiness_step_difference:
        return _empty_evaluation(candidate, "access_mismatch", age_days=age_days)
    if abs(infrastructure_difference) > config.max_readiness_step_difference:
        return _empty_evaluation(candidate, "infrastructure_mismatch", age_days=age_days)

    lease_difference = 0.0
    if target.right_type == "lease":
        if not _finite(candidate.lease_term_years, lower=0.01, upper=99):
            return _empty_evaluation(candidate, "unknown_lease_term", age_days=age_days)
        if _lease_band(float(candidate.lease_term_years)) != _lease_band(
            float(target.lease_term_years)
        ):
            return _empty_evaluation(candidate, "lease_term_band_mismatch", age_days=age_days)
        lease_difference = float(target.lease_term_years) - float(candidate.lease_term_years)

    adjustments = [
        ComparableAdjustment(
            "area_normalization",
            round(target.area_ha / candidate.area_ha, 6),
            "Comparable price normalized per hectare and scaled to target area.",
        )
    ]
    factor = 1.0
    if access_difference:
        access_factor = 1 + access_difference * config.access_adjustment_per_step
        factor *= access_factor
        adjustments.append(
            ComparableAdjustment(
                "access_readiness",
                round(access_factor, 6),
                "Configured readiness-step adjustment; unknown readiness is never inferred.",
            )
        )
    if infrastructure_difference:
        infrastructure_factor = (
            1 + infrastructure_difference * config.infrastructure_adjustment_per_step
        )
        factor *= infrastructure_factor
        adjustments.append(
            ComparableAdjustment(
                "infrastructure_readiness",
                round(infrastructure_factor, 6),
                "Configured readiness-step adjustment; unknown infrastructure is excluded.",
            )
        )
    if lease_difference:
        lease_delta = max(
            -config.lease_adjustment_cap,
            min(config.lease_adjustment_cap, lease_difference * config.lease_year_adjustment),
        )
        lease_factor = 1 + lease_delta
        factor *= lease_factor
        adjustments.append(
            ComparableAdjustment(
                "lease_term",
                round(lease_factor, 6),
                "Configured within-band remaining-term adjustment.",
            )
        )
    price_per_ha = candidate.price_kzt / candidate.area_ha
    adjusted_per_ha = price_per_ha * factor
    strong_geography = distance_km is not None and distance_km <= config.radius_km / 2
    exact_readiness = access_difference == 0 and infrastructure_difference == 0
    high_quality = (
        candidate.price_kind == "verified_sale"
        and age_days <= config.high_quality_max_age_days
        and area_deviation <= config.high_quality_area_tolerance_fraction
        and strong_geography
        and exact_readiness
    )
    grade = "A" if high_quality else ("B" if candidate.price_kind == "verified_sale" else "L")
    return ComparableEvaluation(
        source_id=candidate.source_id,
        source_record_id=candidate.source_record_id,
        source_url=candidate.source_url,
        object_id=candidate.object_id,
        price_kind=candidate.price_kind,
        observed_at=candidate.observed_at,
        age_days=age_days,
        distance_km=round(distance_km, 3) if distance_km is not None else None,
        eligible=True,
        exclusion_reason=None,
        duplicate_of=None,
        quality_grade=grade,
        price_kzt=round(candidate.price_kzt, 2),
        price_per_ha_kzt=round(price_per_ha, 2),
        adjusted_price_per_ha_kzt=round(adjusted_per_ha, 2),
        adjusted_target_value_kzt=round(adjusted_per_ha * target.area_ha, 2),
        adjustments=tuple(adjustments),
    )


def build_strict_market_comparables(
    target: ComparableTarget,
    candidates: list[ComparableCandidate] | tuple[ComparableCandidate, ...],
    *,
    config: ComparableConfig | None = None,
) -> MarketComparableResult:
    """Precompute a strict verified-sale estimate without scores, verdicts or bids."""
    target_error = _target_error(target)
    if target_error:
        return MarketComparableResult(
            "invalid_target", None, "none", 0, 0, 0, (), target_error
        )
    requested_config = config or ComparableConfig()
    config_error = _config_error(requested_config)
    if config_error:
        return MarketComparableResult(
            "invalid_input", None, "none", 0, 0, 0, (), config_error
        )
    active_config = _bounded_config(requested_config)
    if len(candidates) > active_config.max_candidates:
        return MarketComparableResult(
            "invalid_input",
            None,
            "none",
            0,
            0,
            0,
            (),
            f"candidate_count_exceeds_limit:{active_config.max_candidates}",
        )
    ordered = sorted(candidates, key=_stable_key)
    winners: dict[str, ComparableCandidate] = {}
    for candidate in ordered:
        identity = _identity(candidate)
        current = winners.get(identity)
        if current is None or _rank(candidate) > _rank(current):
            winners[identity] = candidate

    evaluations: list[ComparableEvaluation] = []
    for candidate in ordered:
        identity = _identity(candidate)
        winner = winners[identity]
        if candidate is not winner:
            evaluations.append(
                _empty_evaluation(
                    candidate,
                    "duplicate",
                    duplicate_of=f"{winner.source_id}:{winner.source_record_id}",
                )
            )
            continue
        evaluations.append(_evaluate_candidate(target, candidate, active_config))

    outliers = _outlier_indexes(evaluations)
    for index in sorted(outliers):
        item = evaluations[index]
        evaluations[index] = ComparableEvaluation(
            **{
                **asdict(item),
                "eligible": False,
                "exclusion_reason": "price_outlier",
                "quality_grade": "outlier",
                "adjustments": item.adjustments,
            }
        )

    verified = [
        item
        for item in evaluations
        if item.eligible and item.price_kind == "verified_sale"
    ]
    listings = [
        item for item in evaluations if item.eligible and item.price_kind == "listing"
    ]
    high_quality = [item for item in verified if item.quality_grade == "A"]
    if len(high_quality) < active_config.minimum_high_quality_verified:
        return MarketComparableResult(
            status="insufficient_data",
            estimate=None,
            confidence="none",
            high_quality_verified_count=len(high_quality),
            verified_eligible_count=len(verified),
            listing_eligible_count=len(listings),
            evaluations=tuple(evaluations),
            detail=(
                "At least three high-quality verified sales are required; listings are kept "
                "separate and cannot produce a sale-value estimate."
            ),
        )

    per_ha = [float(item.adjusted_price_per_ha_kzt) for item in high_quality]
    median_per_ha = _percentile(per_ha, 0.5)
    low_per_ha = _percentile(per_ha, 0.25)
    high_per_ha = _percentile(per_ha, 0.75)
    estimate = MarketEstimate(
        median_kzt=round(median_per_ha * target.area_ha, 2),
        range_low_kzt=round(low_per_ha * target.area_ha, 2),
        range_high_kzt=round(high_per_ha * target.area_ha, 2),
        median_price_per_ha_kzt=round(median_per_ha, 2),
        range_low_price_per_ha_kzt=round(low_per_ha, 2),
        range_high_price_per_ha_kzt=round(high_per_ha, 2),
        verified_comparables_used=len(high_quality),
    )
    confidence = "high" if len(high_quality) >= 5 else "medium"
    return MarketComparableResult(
        "ok",
        estimate,
        confidence,
        len(high_quality),
        len(verified),
        len(listings),
        tuple(evaluations),
        "Estimate uses grade-A verified sales only; grade-B and listings remain references.",
    )
