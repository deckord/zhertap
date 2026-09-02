"""Deterministic evidence gate for the active-auction interesting-lots queue.

Readiness is projected separately and can never make a lot interesting.  Only an
explicit, auditable comparative thesis or an official auction event may pass the
gate.  The module is deliberately pure: persistence and UI adapters can consume
its immutable result without inheriting the legacy auction score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

ACTIVE_SHORTLIST_STATUSES = frozenset({"ApplicationsAccept", "Pending", "Running"})
MIN_VERIFIED_COMPARABLES = 3
MIN_PRICE_ADVANTAGE_PERCENT = 15.0
MIN_RARE_FEATURE_ALTERNATIVES = 4
MAX_RARE_FEATURE_SHARE = 0.25

Classification = Literal["confirmed", "indicator", "risk", "unchecked", "action"]


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    boundaries: bool = False
    cadastre: bool = False
    documents: bool = False
    road: bool = False
    water: bool = False

    @property
    def sufficient(self) -> bool:
        # Road and water are useful evidence but are not prerequisites for checking
        # the legal object and therefore do not affect this readiness statement.
        return self.boundaries and self.cadastre and self.documents


@dataclass(frozen=True, slots=True)
class ComparativeEvidence:
    source: str
    source_url: str
    observed_at: datetime
    target_price_per_sotka_kzt: float
    cohort_median_price_per_sotka_kzt: float
    verified_comparables_count: int
    target_year: int
    cohort_year: int
    comparison_method: str
    source_available: bool = True


@dataclass(frozen=True, slots=True)
class AuctionEventEvidence:
    source: str
    source_url: str
    observed_at: datetime
    event_type: Literal["repeat", "price_change", "repeat_price_change", "deadline_change"]
    attempts_count: int
    comparison_method: str
    identity_confidence: Literal["high", "medium", "low", "none"]
    previous_price_kzt: float | None = None
    current_price_kzt: float | None = None
    source_available: bool = True


@dataclass(frozen=True, slots=True)
class OfficialDevelopmentEvidence:
    source: str
    source_url: str
    observed_at: datetime
    project_name: str
    polygon_relation: Literal["intersects", "contains", "nearby_only", "unknown"]
    active_alternatives_count: int
    alternatives_with_project_count: int
    comparison_method: str
    source_available: bool = True


@dataclass(frozen=True, slots=True)
class RareFeatureEvidence:
    """A useful fact demonstrated to be uncommon in the same active filter."""

    source: str
    source_url: str
    observed_at: datetime
    feature_kind: Literal["access", "infrastructure", "right", "deadline"]
    feature_label: str
    target_metric: str
    active_alternatives_count: int
    alternatives_with_feature_count: int
    comparison_method: str
    target_feature_confirmed: bool = True
    source_available: bool = True


@dataclass(frozen=True, slots=True)
class ShortlistLotInput:
    lot_id: str
    source_status: str
    evaluated_at: datetime
    readiness: ReadinessEvidence = ReadinessEvidence()
    comparative: ComparativeEvidence | None = None
    event: AuctionEventEvidence | None = None
    development: OfficialDevelopmentEvidence | None = None
    rare_feature: RareFeatureEvidence | None = None
    legacy_score: float | None = None


@dataclass(frozen=True, slots=True)
class ShortlistReason:
    kind: Literal[
        "comparative_price", "auction_event", "official_development", "rare_feature"
    ]
    classification: Literal["confirmed", "indicator"]
    statement: str
    source: str
    source_url: str
    source_date: str
    metric: str
    compared_with: str
    comparison_method: str


@dataclass(frozen=True, slots=True)
class ShortlistResult:
    lot_id: str
    eligible: bool
    interesting: bool
    manual_required: bool
    reasons: tuple[ShortlistReason, ...]
    readiness_line: str
    summary: str
    confirmed: tuple[str, ...]
    indicators: tuple[str, ...]
    risks: tuple[str, ...]
    unchecked: tuple[str, ...]
    actions: tuple[str, ...]


def _finite_positive(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _auditable_source(source: str, source_url: str, observed_at: datetime, now: datetime) -> bool:
    parsed = urlparse(source_url)
    return (
        bool(source.strip())
        and parsed.scheme == "https"
        and bool(parsed.netloc)
        and observed_at.utcoffset() is not None
        and now.utcoffset() is not None
        and observed_at <= now
    )


def _comparative_reason(
    evidence: ComparativeEvidence, evaluated_at: datetime
) -> ShortlistReason | None:
    if not _auditable_source(
        evidence.source, evidence.source_url, evidence.observed_at, evaluated_at
    ):
        return None
    if not evidence.comparison_method.strip():
        return None
    if evidence.target_year != evidence.cohort_year:
        return None
    if evidence.verified_comparables_count < MIN_VERIFIED_COMPARABLES:
        return None
    if not _finite_positive(evidence.target_price_per_sotka_kzt) or not _finite_positive(
        evidence.cohort_median_price_per_sotka_kzt
    ):
        return None
    discount = (
        1
        - float(evidence.target_price_per_sotka_kzt)
        / float(evidence.cohort_median_price_per_sotka_kzt)
    ) * 100
    if discount < MIN_PRICE_ADVANTAGE_PERCENT:
        return None
    target = round(float(evidence.target_price_per_sotka_kzt))
    median = round(float(evidence.cohort_median_price_per_sotka_kzt))
    return ShortlistReason(
        kind="comparative_price",
        classification="confirmed",
        statement=(
            f"Цена за сотку на {discount:.1f}% ниже медианы строгой проверенной "
            f"выборки того же года (n={evidence.verified_comparables_count})"
        ),
        source=evidence.source.strip(),
        source_url=evidence.source_url,
        source_date=evidence.observed_at.date().isoformat(),
        metric=(
            f"{target} vs {median} KZT/sotka; {discount:.1f}% below; "
            f"n={evidence.verified_comparables_count}"
        ),
        compared_with=f"strict verified comparable cohort for {evidence.cohort_year}",
        comparison_method=evidence.comparison_method.strip(),
    )


def _event_reason(evidence: AuctionEventEvidence, evaluated_at: datetime) -> ShortlistReason | None:
    if not _auditable_source(
        evidence.source, evidence.source_url, evidence.observed_at, evaluated_at
    ):
        return None
    if evidence.identity_confidence != "high" or not evidence.comparison_method.strip():
        return None
    if evidence.attempts_count < 1:
        return None

    has_repeat = evidence.event_type in {"repeat", "repeat_price_change"}
    has_price_change = evidence.event_type in {"price_change", "repeat_price_change"}
    if has_repeat and evidence.attempts_count < 2:
        return None

    metric_parts = [f"attempts={evidence.attempts_count}"]
    event_description = "официально подтверждён повтор аукциона"
    if has_price_change:
        if not _finite_positive(evidence.previous_price_kzt) or not _finite_positive(
            evidence.current_price_kzt
        ):
            return None
        previous = float(evidence.previous_price_kzt)
        current = float(evidence.current_price_kzt)
        change = (current / previous - 1) * 100
        if math.isclose(change, 0.0, abs_tol=0.05):
            return None
        metric_parts.append(
            f"{round(previous)} -> {round(current)} KZT; change={change:.1f}%"
        )
        event_description = (
            "официально подтверждены повтор и изменение цены"
            if has_repeat
            else "официально подтверждено изменение цены"
        )
    elif evidence.event_type == "deadline_change":
        # A deadline change needs dedicated old/new timestamps.  Until that input
        # contract exists it must fail closed rather than imply urgency.
        return None
    elif not has_repeat:
        return None

    return ShortlistReason(
        kind="auction_event",
        classification="indicator",
        statement=f"{event_description}; это аукционная ситуация, не инвестиционная рекомендация",
        source=evidence.source.strip(),
        source_url=evidence.source_url,
        source_date=evidence.observed_at.date().isoformat(),
        metric="; ".join(metric_parts),
        compared_with="previous official publication of the same land object",
        comparison_method=evidence.comparison_method.strip(),
    )


def _development_reason(
    evidence: OfficialDevelopmentEvidence, evaluated_at: datetime
) -> ShortlistReason | None:
    if not _auditable_source(
        evidence.source, evidence.source_url, evidence.observed_at, evaluated_at
    ):
        return None
    if not evidence.project_name.strip() or not evidence.comparison_method.strip():
        return None
    # Proximity, a text geocode, or a point near the lot is not polygon evidence.
    if evidence.polygon_relation not in {"intersects", "contains"}:
        return None
    alternatives = evidence.active_alternatives_count
    alternatives_with_project = evidence.alternatives_with_project_count
    if (
        isinstance(alternatives, bool)
        or isinstance(alternatives_with_project, bool)
        or alternatives < 1
        or alternatives_with_project < 0
        or alternatives_with_project >= alternatives
    ):
        return None

    relation = evidence.polygon_relation
    return ShortlistReason(
        kind="official_development",
        classification="confirmed",
        statement=(
            f"Официальный проект «{evidence.project_name.strip()}» геометрически относится "
            f"к polygon лота и встречается не у всех активных альтернатив"
        ),
        source=evidence.source.strip(),
        source_url=evidence.source_url,
        source_date=evidence.observed_at.date().isoformat(),
        metric=(
            f"polygon_relation={relation}; alternatives_with_project="
            f"{alternatives_with_project}/{alternatives}"
        ),
        compared_with=f"{alternatives} active alternatives in the same filter",
        comparison_method=evidence.comparison_method.strip(),
    )


def _rare_feature_reason(
    evidence: RareFeatureEvidence, evaluated_at: datetime
) -> ShortlistReason | None:
    if not _auditable_source(
        evidence.source, evidence.source_url, evidence.observed_at, evaluated_at
    ):
        return None
    if (
        not evidence.target_feature_confirmed
        or not evidence.feature_label.strip()
        or not evidence.target_metric.strip()
        or not evidence.comparison_method.strip()
    ):
        return None
    alternatives = evidence.active_alternatives_count
    with_feature = evidence.alternatives_with_feature_count
    if (
        isinstance(alternatives, bool)
        or isinstance(with_feature, bool)
        or alternatives < MIN_RARE_FEATURE_ALTERNATIVES
        or with_feature < 0
        or with_feature > alternatives
    ):
        return None
    share = with_feature / alternatives
    if share > MAX_RARE_FEATURE_SHARE:
        return None
    return ShortlistReason(
        kind="rare_feature",
        classification="confirmed",
        statement=(
            f"Подтверждён редкий полезный признак «{evidence.feature_label.strip()}»: "
            f"среди активных альтернатив он есть у {with_feature} из {alternatives}"
        ),
        source=evidence.source.strip(),
        source_url=evidence.source_url,
        source_date=evidence.observed_at.date().isoformat(),
        metric=(
            f"target={evidence.target_metric.strip()}; alternatives_with_feature="
            f"{with_feature}/{alternatives}; share={share * 100:.1f}%"
        ),
        compared_with=f"{alternatives} active alternatives in the same filter",
        comparison_method=evidence.comparison_method.strip(),
    )


def evaluate_shortlist_lot(lot: ShortlistLotInput) -> ShortlistResult:
    eligible = lot.source_status in ACTIVE_SHORTLIST_STATUSES
    readiness_line = (
        "Данные достаточны для проверки"
        if lot.readiness.sufficient
        else "Данных недостаточно для полной проверки"
    )
    reasons: list[ShortlistReason] = []
    unchecked: list[str] = []
    actions: list[str] = []

    if eligible and lot.comparative is not None:
        if not lot.comparative.source_available:
            unchecked.append("Источник сравнения недоступен; проверить вручную")
            actions.append("Открыть официальный источник и повторить проверку сравнения")
        else:
            reason = _comparative_reason(lot.comparative, lot.evaluated_at)
            if reason is not None:
                reasons.append(reason)

    if eligible and lot.event is not None:
        if not lot.event.source_available:
            unchecked.append("Источник истории аукциона недоступен; проверить вручную")
            actions.append("Открыть официальный источник и повторить проверку события")
        else:
            reason = _event_reason(lot.event, lot.evaluated_at)
            if reason is not None:
                reasons.append(reason)

    if eligible and lot.development is not None:
        if not lot.development.source_available:
            unchecked.append("Официальный источник проекта недоступен; проверить вручную")
            actions.append("Открыть официальный источник и повторить проверку проекта")
        else:
            reason = _development_reason(lot.development, lot.evaluated_at)
            if reason is not None:
                reasons.append(reason)

    if eligible and lot.rare_feature is not None:
        if not lot.rare_feature.source_available:
            unchecked.append("Источник редкого признака недоступен; проверить вручную")
            actions.append("Открыть официальный источник и повторить сравнение признака")
        else:
            reason = _rare_feature_reason(lot.rare_feature, lot.evaluated_at)
            if reason is not None:
                reasons.append(reason)

    interesting = eligible and bool(reasons)
    if interesting:
        summary = "Есть подтверждённая сравнительная или событийная причина проверить лот"
    elif lot.readiness.sufficient:
        summary = (
            "Данных достаточно для проверки, но нет подтверждённой причины "
            "выделить его среди похожих лотов"
        )
    else:
        summary = "Нет подтверждённой причины выделить лот; требуется дополнить данные"

    return ShortlistResult(
        lot_id=lot.lot_id,
        eligible=eligible,
        interesting=interesting,
        manual_required=bool(unchecked),
        reasons=tuple(reasons),
        readiness_line=readiness_line,
        summary=summary,
        confirmed=tuple(
            reason.statement for reason in reasons if reason.classification == "confirmed"
        ),
        indicators=tuple(
            reason.statement for reason in reasons if reason.classification == "indicator"
        ),
        risks=(),
        unchecked=tuple(unchecked),
        actions=tuple(actions),
    )
