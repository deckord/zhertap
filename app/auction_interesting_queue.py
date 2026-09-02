"""Fail-closed evidence contract for the active-lot "worth checking" queue.

The module deliberately does not calculate a score or make an investment decision.
It only separates traceable reasons, indicators, risks, unknowns and actions.  It is
pure and bounded so a database query/worker can adopt it without provider calls.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Mapping, Sequence
from urllib.parse import urlparse

CONTRACT_VERSION = "interesting-queue/2"
ACTIVE_EQAZYNA_STATUSES = frozenset({"ApplicationsAccept", "Pending", "Running"})

Classification = Literal["confirmed", "indicator", "risk", "unverified", "action"]

_ALLOWED_CLASSIFICATIONS = frozenset({"confirmed", "indicator", "risk", "unverified", "action"})
_ALLOWED_STATUSES = frozenset({"found", "conflict", "warning", "manual_required", "unknown"})
_POSITIVE_CLASSIFICATIONS = frozenset({"confirmed", "indicator"})
_POSITIVE_STATUSES = frozenset({"found", "warning"})
_TRACEABLE_POSITIVE_SOURCES = frozenset(
    {
        "official_lot",
        "official_document",
        "jerler",
        "egkn",
        "official_gis",
        "osm",
        "official_planning_layer",
        "gov_kz",
        "strict_verified_comparable",
        "official_auction_history",
    }
)
_PROHIBITED_SOURCES = frozenset(
    {"ai_opinion", "raw_ai_opinion", "unverified_news", "private_ownership"}
)
_PROHIBITED_TITLE_FRAGMENTS = ("перспектив", "выгодн", "рекомендуем", "инвестицион")
_PLANNING_CATEGORIES = frozenset({"planning"})
_REASON_CATEGORIES = frozenset(
    {"comparative_price", "territorial_development", "rare_comparative_feature", "auction_event"}
)
_TERRITORIAL_STAGES = frozenset(
    {"announced", "designed", "funded", "approved", "construction", "completed", "launched", "cancelled"}
)
MAX_EVIDENCE_ITEMS = 100
MAX_TEXT_CHARS = 600


class EvidenceContractError(ValueError):
    """The producer emitted malformed, unbounded or prohibited evidence."""


@dataclass(frozen=True, slots=True)
class QueueEvidence:
    key: str
    category: str
    classification: Classification
    status: str
    title: str
    detail: str
    source_kind: str
    source_url: str | None
    observed_at: datetime | None
    authority_verified: bool
    coverage_verified: bool
    geometry_verified: bool
    comparison_basis: str | None
    metric: str | None
    comparison_method: str | None


@dataclass(frozen=True, slots=True)
class InterestingQueueCandidate:
    lot_id: str
    eligible: bool
    reasons: tuple[QueueEvidence, ...]
    readiness: tuple[QueueEvidence, ...]
    risks: tuple[QueueEvidence, ...]
    unchecked: tuple[QueueEvidence, ...]
    actions: tuple[QueueEvidence, ...]
    evaluated_at: datetime
    contract_version: str = CONTRACT_VERSION


def _text(value: object, field: str, *, required: bool = True) -> str:
    if value is None:
        if required:
            raise EvidenceContractError(f"{field} is required")
        return ""
    result = str(value).strip()
    if required and not result:
        raise EvidenceContractError(f"{field} is required")
    if len(result) > MAX_TEXT_CHARS:
        raise EvidenceContractError(f"{field} exceeds {MAX_TEXT_CHARS} characters")
    return result


def _optional_text(value: object, field: str) -> str | None:
    result = _text(value, field, required=False)
    return result or None


def _datetime(value: object, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceContractError(f"{field} is not ISO-8601") from exc
    else:
        raise EvidenceContractError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise EvidenceContractError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _boolean(value: object) -> bool:
    return value is True


def _manual(item: QueueEvidence, detail: str) -> QueueEvidence:
    return replace(
        item,
        classification="unverified",
        status="manual_required",
        detail=detail,
    )


def _is_traceable_https_url(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None


def _parse(raw: Mapping[str, object]) -> QueueEvidence:
    classification = _text(raw.get("classification"), "classification")
    status = _text(raw.get("status"), "status")
    if classification not in _ALLOWED_CLASSIFICATIONS:
        raise EvidenceContractError(f"unsupported classification: {classification}")
    if status not in _ALLOWED_STATUSES:
        raise EvidenceContractError(f"unsupported status: {status}")

    title = _text(raw.get("title"), "title")
    source_kind = _text(raw.get("source_kind"), "source_kind")
    normalized_title = title.casefold()
    if classification in _POSITIVE_CLASSIFICATIONS:
        if source_kind in _PROHIBITED_SOURCES:
            raise EvidenceContractError(f"prohibited positive source: {source_kind}")
        if source_kind not in _TRACEABLE_POSITIVE_SOURCES:
            raise EvidenceContractError(f"unapproved positive source: {source_kind}")
        if any(fragment in normalized_title for fragment in _PROHIBITED_TITLE_FRAGMENTS):
            raise EvidenceContractError("generic recommendation wording is prohibited")

    return QueueEvidence(
        key=_text(raw.get("key"), "key"),
        category=_text(raw.get("category"), "category"),
        classification=classification,  # type: ignore[arg-type]
        status=status,
        title=title,
        detail=_text(raw.get("detail"), "detail"),
        source_kind=source_kind,
        source_url=_optional_text(raw.get("source_url"), "source_url"),
        observed_at=_datetime(raw.get("observed_at"), "observed_at"),
        authority_verified=_boolean(raw.get("authority_verified")),
        coverage_verified=_boolean(raw.get("coverage_verified")),
        geometry_verified=_boolean(raw.get("geometry_verified")),
        comparison_basis=_optional_text(raw.get("comparison_basis"), "comparison_basis"),
        metric=_optional_text(raw.get("metric"), "metric"),
        comparison_method=_optional_text(raw.get("comparison_method"), "comparison_method"),
    )


def _apply_positive_policy(
    item: QueueEvidence, raw: Mapping[str, object]
) -> QueueEvidence:
    if item.classification not in _POSITIVE_CLASSIFICATIONS:
        return item

    if not _is_traceable_https_url(item.source_url):
        raise EvidenceContractError("positive evidence requires a traceable HTTPS source_url")

    if item.category in _REASON_CATEGORIES and not (
        item.comparison_basis and item.metric and item.comparison_method
    ):
        raise EvidenceContractError(
            "interesting reason requires comparison_basis, metric and comparison_method"
        )

    if item.category in _PLANNING_CATEGORIES or item.source_kind == "official_planning_layer":
        if not (
            item.authority_verified
            and item.coverage_verified
            and item.geometry_verified
            and item.source_url is not None
            and item.observed_at is not None
        ):
            return _manual(
                item,
                "Нужны верифицированные authority, coverage, provenance и геометрическое покрытие участка.",
            )

    if item.source_kind == "gov_kz" or item.category == "territorial":
        event_stage = _optional_text(raw.get("event_stage"), "event_stage")
        if (
            event_stage not in _TERRITORIAL_STAGES
            or not item.geometry_verified
            or item.source_url is None
            or item.observed_at is None
        ):
            return _manual(
                item,
                "Территориальному сигналу нужны дата, стадия официального проекта и геометрическая привязка.",
            )

    if item.observed_at is None:
        raise EvidenceContractError("positive evidence requires observed_at")

    if item.source_kind == "osm":
        if item.classification != "indicator":
            raise EvidenceContractError("OSM may only produce an indicator")
        if not item.geometry_verified:
            return _manual(item, "OSM-объект не привязан к участку проверенной геометрией.")

    if item.source_kind in {"official_gis", "egkn"} and not item.geometry_verified:
        return _manual(item, "Пространственный сигнал не привязан к участку проверенной геометрией.")

    if item.source_kind == "strict_verified_comparable":
        comparable_year = raw.get("comparable_year")
        target_year = raw.get("target_year")
        if (
            type(comparable_year) is not int
            or type(target_year) is not int
            or comparable_year != target_year
            or raw.get("sale_verified") is not True
            or not item.geometry_verified
            or item.category != "comparative_price"
            or type(raw.get("cohort_count")) is not int
            or int(raw["cohort_count"]) < 3
            or not isinstance(raw.get("discount_percent"), (int, float))
            or isinstance(raw.get("discount_percent"), bool)
            or float(raw["discount_percent"]) < 10.0
        ):
            return _manual(
                item,
                "Аналог должен быть завершённой проверенной продажей того же года со строгой геометрией.",
            )

    if item.category == "territorial_development":
        alternatives = raw.get("active_alternatives_count")
        if type(alternatives) is not int or alternatives < 1:
            return _manual(item, "Нет явного сравнения с активными альтернативами того же фильтра.")

    if item.category == "rare_comparative_feature":
        alternatives = raw.get("active_alternatives_count")
        official_legal_comparison = (
            item.source_kind == "official_lot"
            and raw.get("feature_kind") == "lease_term"
            and isinstance(raw.get("target_term_years"), (int, float))
            and isinstance(raw.get("next_best_term_years"), (int, float))
            and not isinstance(raw.get("target_term_years"), bool)
            and not isinstance(raw.get("next_best_term_years"), bool)
            and float(raw["target_term_years"]) > float(raw["next_best_term_years"])
        )
        if (
            type(alternatives) is not int
            or alternatives < 3
            or not (item.geometry_verified or official_legal_comparison)
        ):
            return _manual(item, "Редкий признак не доказан на активных альтернативах того же фильтра.")

    if item.category == "auction_event":
        if item.source_kind != "official_auction_history" or raw.get("event_type") not in {
            "repeat", "price_change", "deadline_change"
        }:
            return _manual(
                item,
                "Аукционное событие не подтверждено официальной историей и объяснением изменения.",
            )

    return item


def build_interesting_queue_candidate(
    *,
    lot_id: str,
    source_search_status: str | None,
    evidence: Sequence[Mapping[str, object]],
    evaluated_at: datetime,
) -> InterestingQueueCandidate:
    """Evaluate one lot without ranking it or inferring missing evidence.

    Eligibility means only: the lot is in one of the three exact current E-Qazyna
    states and has at least one positive item that survives the fail-closed policy.
    """
    bounded_lot_id = _text(lot_id, "lot_id")
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        raise EvidenceContractError(f"evidence exceeds {MAX_EVIDENCE_ITEMS} items")
    checked_at = _datetime(evaluated_at, "evaluated_at")
    assert checked_at is not None

    parsed = tuple(_apply_positive_policy(_parse(raw), raw) for raw in evidence)
    reasons = tuple(
        item
        for item in parsed
        if item.category in _REASON_CATEGORIES
        and item.classification in _POSITIVE_CLASSIFICATIONS
        and item.status in _POSITIVE_STATUSES
    )
    readiness = tuple(
        item
        for item in parsed
        if item.category not in _REASON_CATEGORIES
        and item.classification in _POSITIVE_CLASSIFICATIONS
        and item.status in _POSITIVE_STATUSES
    )
    risks = tuple(item for item in parsed if item.classification == "risk")
    unchecked = tuple(
        item
        for item in parsed
        if item.classification == "unverified" or item.status in {"manual_required", "unknown"}
    )
    actions = tuple(item for item in parsed if item.classification == "action")

    in_scope = source_search_status in ACTIVE_EQAZYNA_STATUSES
    if not in_scope:
        reasons = ()
    return InterestingQueueCandidate(
        lot_id=bounded_lot_id,
        eligible=bool(in_scope and reasons),
        reasons=reasons,
        readiness=readiness,
        risks=risks,
        unchecked=unchecked,
        actions=actions,
        evaluated_at=checked_at,
    )
