from __future__ import annotations

from typing import TypedDict


class NeutralAuctionSummary(TypedDict):
    state: str
    status: str
    title: str
    detail: str
    price_reference_label: str
    price_reference_kzt: float | None


_NEUTRAL_PRESENTATION: dict[str, tuple[str, str, str, str]] = {
    "participate": (
        "no_critical_factors_found",
        "ready",
        "Критичные факторы не выявлены",
        "По доступным подтверждённым данным критичные факторы не выявлены; непроверенные данные требуют отдельной проверки.",
    ),
    "participate_up_to": (
        "no_critical_factors_found",
        "ready",
        "Критичные факторы не выявлены",
        "По доступным подтверждённым данным критичные факторы не выявлены; ценовой ориентир является расчётом, а не рекомендацией.",
    ),
    "requires_check": (
        "verification_required",
        "checking",
        "Требует проверки",
        "Не хватает подтверждённых юридических, пространственных или рыночных данных.",
    ),
    "high_risk": (
        "material_risks_found",
        "warning",
        "Выявлены существенные риски",
        "Подтверждённые факторы риска требуют самостоятельной оценки пользователя.",
    ),
    "do_not_participate": (
        "critical_factors_found",
        "blocked",
        "Выявлены критичные факторы",
        "Подтверждён критичный фактор или превышение расчётного ценового ориентира.",
    ),
}


def build_neutral_auction_summary(
    verdict: str | None,
    *,
    bid_ceiling_kzt: float | None = None,
) -> NeutralAuctionSummary:
    """Adapt an internal decision-engine verdict to neutral public wording.

    Internal verdict codes remain unchanged for rules, snapshots and filters. This
    adapter is deliberately presentation-only and never tells a user whether to bid.
    """

    state, status, title, detail = _NEUTRAL_PRESENTATION.get(
        verdict or "",
        _NEUTRAL_PRESENTATION["requires_check"],
    )
    return {
        "state": state,
        "status": status,
        "title": title,
        "detail": detail,
        "price_reference_label": "Расчётный ценовой ориентир",
        "price_reference_kzt": bid_ceiling_kzt,
    }
