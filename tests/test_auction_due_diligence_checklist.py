from types import SimpleNamespace

from app.auction_due_diligence import build_due_diligence_checklist


def test_checklist_adds_lease_and_scenario_checks_and_counts_blockers() -> None:
    lot = SimpleNamespace(
        land_rights="Аренда земельного участка",
        lease_term_years=5,
        purpose="Для строительства магазина",
        cadastre_number=None,
    )
    requests = [SimpleNamespace(check_code="electricity", status="waiting")]
    manual_checks = {"access": {"status": "done"}}

    checklist = build_due_diligence_checklist(
        lot,
        requests=requests,
        manual_checks=manual_checks,
        documents_count=0,
        planning_status="unknown",
    )

    codes = {item["code"] for item in checklist["items"]}
    assert {"lease_terms", "retail_purpose", "electricity", "access"} <= codes
    assert checklist["critical_open"] >= 1
    assert checklist["completion_percent"] < 100
    assert checklist["items_by_code"]["electricity"]["status"] == "waiting"


def test_checklist_keeps_unknown_as_open_not_pass() -> None:
    lot = SimpleNamespace(land_rights="", lease_term_years=None, purpose="", cadastre_number="")

    checklist = build_due_diligence_checklist(
        lot,
        requests=[],
        manual_checks={},
        documents_count=3,
        planning_status="clear",
    )

    item = checklist["items_by_code"]["cadastre"]
    assert item["status"] == "unknown"
    assert item["critical"] is True
