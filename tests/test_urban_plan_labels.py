from app.models import UrbanPlanStatus
from app.urban_plan_labels import (
    telegram_urban_plan_line,
    urban_plan_badge_payload,
)


def test_manual_genplan_reference_changes_web_badge_text() -> None:
    payload = urban_plan_badge_payload(
        UrbanPlanStatus.waived.value,
        language="ru",
        reference_source_kind="manual_plan_file",
    )

    assert payload["short"] == "есть карта для сверки"
    assert "карта генплана" in payload["title"].lower()
    assert payload["tone"] == "warning"


def test_manual_genplan_reference_changes_telegram_line() -> None:
    line = telegram_urban_plan_line(
        UrbanPlanStatus.unavailable.value,
        language="ru",
        reference_source_kind="manual_plan_file",
    )

    assert "есть карта для сверки" in line


def test_generic_unavailable_status_stays_generic_without_manual_file() -> None:
    payload = urban_plan_badge_payload(
        UrbanPlanStatus.waived.value,
        language="ru",
        reference_source_kind="geoportal",
    )

    assert payload["short"] != "есть карта для сверки"
