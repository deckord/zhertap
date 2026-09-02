from app.auction_taxonomy import (
    classify_scenario,
    classify_scenario_claims,
    select_decision_scenario,
)


def test_camping_is_distinct_from_hospitality() -> None:
    assert classify_scenario("Размещение и эксплуатация кемпинга") == "camping"
    assert classify_scenario("Строительство гостиницы и базы отдыха") == "hospitality"


def test_unknown_or_generic_purpose_is_not_assumed_resale_or_operating() -> None:
    cases = (
        (None, "PURPOSE_MISSING"),
        ("земельный участок", "PURPOSE_UNCLASSIFIED"),
    )
    for purpose, reason in cases:
        selection = select_decision_scenario(purpose)
        assert selection.status == "requires_check"
        assert selection.scenario_key is None
        assert selection.reason_codes == (reason,)


def test_claim_classifier_exposes_cross_field_purpose_conflicts() -> None:
    assert classify_scenario_claims("земельный участок | строительство магазина") == (
        "retail",
    )
    assert classify_scenario_claims("строительство магазина | производственный цех") == (
        "industrial",
        "retail",
    )
