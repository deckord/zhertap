from __future__ import annotations

from dataclasses import dataclass

SCENARIO_SELECTOR_VERSION = "scenario-selector/2026.2"
UNCLASSIFIED_SCENARIO = "unclassified"

SCENARIO_KEYWORDS: dict[str, tuple[str, ...]] = {
    "data_center": ("дата-центр", "центр обработки данных", "цод"),
    "camping": ("кемпинг", "кемпингов"),
    "hospitality": ("гостиниц", "баз отдыха", "турист", "объектов отдыха"),
    "roadside": ("азс", "автозаправ", "придорож", "станци техобслуж"),
    "warehouse": ("склад", "логист", "хранени"),
    "industrial": ("производств", "промышлен", "цех", "завод"),
    "retail": ("магазин", "торгов", "рынок", "супермаркет"),
    "residential": ("ижс", "жилого", "жилищ", "многоквартир", "жилой комплекс"),
    "agriculture": (
        "сельскохозяй",
        "крестьян",
        "фермер",
        "лпх",
        "пашн",
        "животновод",
    ),
    "services": ("услуг", "кафе", "ресторан", "общественного питания", "сервис"),
}


def classify_scenario(text: str | None) -> str:
    normalized = " ".join((text or "").casefold().split())
    if not normalized:
        return "unknown"
    for scenario, keywords in SCENARIO_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return scenario
    return "other"


def classify_scenario_claims(text: str | None) -> tuple[str, ...]:
    """Return every explicit supported purpose group found in source text."""

    normalized = " ".join((text or "").casefold().split())
    if not normalized:
        return ()
    return tuple(
        scenario
        for scenario, keywords in SCENARIO_KEYWORDS.items()
        if any(keyword in normalized for keyword in keywords)
    )


@dataclass(frozen=True, slots=True)
class DecisionScenarioSelection:
    """Canonical, fail-closed mapping from evidenced purpose to a decision scenario."""

    selector_version: str
    status: str
    profile: str
    scenario_key: str | None
    reason_codes: tuple[str, ...]
    purpose_confirmation_required: bool

    def as_payload(self, *, provenance_refs: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "selector_version": self.selector_version,
            "status": self.status,
            "profile": self.profile,
            "scenario_key": self.scenario_key,
            "reason_codes": list(self.reason_codes),
            "purpose_confirmation_required": self.purpose_confirmation_required,
            "provenance_refs": list(provenance_refs),
        }


def select_decision_scenario(text: str | None) -> DecisionScenarioSelection:
    """Select only when purpose itself identifies a supported investment scenario.

    A title or an unknown/other purpose is not evidence for resale or an operating
    business. Callers persist the uncertainty and keep price ceilings unavailable.
    """

    return select_decision_scenario_for_profile(classify_scenario(text))


def select_decision_scenario_for_profile(profile: str) -> DecisionScenarioSelection:
    """Canonical selector for callers that already hold the normalized profile."""

    scenario_by_profile = {
        "camping": "camping",
        "hospitality": "hospitality",
        "residential": "development",
        "retail": "operating_business",
        "roadside": "operating_business",
        "warehouse": "operating_business",
        "industrial": "operating_business",
        "data_center": "operating_business",
        "agriculture": "operating_business",
        "services": "operating_business",
    }
    scenario_key = scenario_by_profile.get(profile)
    if scenario_key is None:
        reason = "PURPOSE_MISSING" if profile == "unknown" else "PURPOSE_UNCLASSIFIED"
        return DecisionScenarioSelection(
            selector_version=SCENARIO_SELECTOR_VERSION,
            status="requires_check",
            profile=profile,
            scenario_key=None,
            reason_codes=(reason,),
            purpose_confirmation_required=True,
        )
    return DecisionScenarioSelection(
        selector_version=SCENARIO_SELECTOR_VERSION,
        status="selected",
        profile=profile,
        scenario_key=scenario_key,
        reason_codes=(),
        purpose_confirmation_required=False,
    )
