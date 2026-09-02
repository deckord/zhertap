from __future__ import annotations

from datetime import UTC, datetime

from app.auction_shortlist import (
    ACTIVE_SHORTLIST_STATUSES,
    AuctionEventEvidence,
    ComparativeEvidence,
    OfficialDevelopmentEvidence,
    RareFeatureEvidence,
    ReadinessEvidence,
    ShortlistLotInput,
    evaluate_shortlist_lot,
)

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _lot(**overrides: object) -> ShortlistLotInput:
    values: dict[str, object] = {
        "lot_id": "lot-1",
        "source_status": "ApplicationsAccept",
        "evaluated_at": NOW,
        "readiness": ReadinessEvidence(
            boundaries=True,
            cadastre=True,
            documents=True,
            road=True,
            water=True,
        ),
    }
    values.update(overrides)
    return ShortlistLotInput(**values)


def test_active_status_contract_is_narrow_and_explicit() -> None:
    assert ACTIVE_SHORTLIST_STATUSES == frozenset(
        {"ApplicationsAccept", "Pending", "Running"}
    )


def test_readiness_and_legacy_score_never_make_a_lot_interesting() -> None:
    result = evaluate_shortlist_lot(_lot(legacy_score=100.0))

    assert result.interesting is False
    assert result.manual_required is False
    assert result.reasons == ()
    assert result.readiness_line == "Данные достаточны для проверки"
    assert result.summary == (
        "Данных достаточно для проверки, но нет подтверждённой причины "
        "выделить его среди похожих лотов"
    )


def test_verified_same_year_price_comparison_is_auditable_and_qualifies() -> None:
    result = evaluate_shortlist_lot(
        _lot(
            comparative=ComparativeEvidence(
                source="e-Qazyna verified sales inventory",
                source_url="https://example.test/verified-sales/lot-1",
                observed_at=datetime(2026, 8, 31, tzinfo=UTC),
                target_price_per_sotka_kzt=80_000,
                cohort_median_price_per_sotka_kzt=100_000,
                verified_comparables_count=4,
                target_year=2026,
                cohort_year=2026,
                comparison_method=(
                    "strict same-year median; same right, purpose, area and geography"
                ),
            )
        )
    )

    assert result.interesting is True
    assert result.manual_required is False
    reason = result.reasons[0]
    assert reason.kind == "comparative_price"
    assert reason.classification == "confirmed"
    assert reason.metric == "80000 vs 100000 KZT/sotka; 20.0% below; n=4"
    assert reason.compared_with == "strict verified comparable cohort for 2026"
    assert reason.source_url == "https://example.test/verified-sales/lot-1"
    assert reason.source_date == "2026-08-31"
    assert "same right" in reason.comparison_method


def test_small_discount_or_wrong_year_does_not_qualify() -> None:
    small = ComparativeEvidence(
        source="verified inventory",
        source_url="https://example.test/source",
        observed_at=NOW,
        target_price_per_sotka_kzt=91_000,
        cohort_median_price_per_sotka_kzt=100_000,
        verified_comparables_count=5,
        target_year=2026,
        cohort_year=2026,
        comparison_method="strict cohort",
    )
    wrong_year = ComparativeEvidence(
        source="verified inventory",
        source_url="https://example.test/source",
        observed_at=NOW,
        target_price_per_sotka_kzt=50_000,
        cohort_median_price_per_sotka_kzt=100_000,
        verified_comparables_count=5,
        target_year=2026,
        cohort_year=2025,
        comparison_method="strict cohort",
    )

    assert evaluate_shortlist_lot(_lot(comparative=small)).interesting is False
    assert evaluate_shortlist_lot(_lot(comparative=wrong_year)).interesting is False


def test_unavailable_comparison_source_requires_manual_check_and_is_not_signal() -> None:
    result = evaluate_shortlist_lot(
        _lot(
            comparative=ComparativeEvidence(
                source="e-Qazyna",
                source_url="https://example.test/unavailable",
                observed_at=NOW,
                target_price_per_sotka_kzt=50_000,
                cohort_median_price_per_sotka_kzt=100_000,
                verified_comparables_count=10,
                target_year=2026,
                cohort_year=2026,
                comparison_method="strict cohort",
                source_available=False,
            )
        )
    )

    assert result.interesting is False
    assert result.manual_required is True
    assert result.reasons == ()
    assert result.unchecked == ("Источник сравнения недоступен; проверить вручную",)
    assert result.actions == ("Открыть официальный источник и повторить проверку сравнения",)


def test_proven_repeat_with_price_change_is_event_reason_not_investment_advice() -> None:
    result = evaluate_shortlist_lot(
        _lot(
            event=AuctionEventEvidence(
                source="official e-Qazyna lot history",
                source_url="https://example.test/auction/42",
                observed_at=datetime(2026, 8, 30, tzinfo=UTC),
                event_type="repeat_price_change",
                attempts_count=3,
                previous_price_kzt=10_000_000,
                current_price_kzt=8_000_000,
                comparison_method="same official land-object identity across publications",
                identity_confidence="high",
            )
        )
    )

    assert result.interesting is True
    reason = result.reasons[0]
    assert reason.kind == "auction_event"
    assert reason.classification == "indicator"
    assert reason.metric == "attempts=3; 10000000 -> 8000000 KZT; change=-20.0%"
    assert reason.compared_with == "previous official publication of the same land object"
    assert "не инвестиционная рекомендация" in reason.statement


def test_unproven_repeat_and_inactive_lot_are_excluded() -> None:
    weak_event = AuctionEventEvidence(
        source="history",
        source_url="https://example.test/history",
        observed_at=NOW,
        event_type="repeat",
        attempts_count=2,
        comparison_method="title similarity",
        identity_confidence="low",
    )
    assert evaluate_shortlist_lot(_lot(event=weak_event)).interesting is False
    assert evaluate_shortlist_lot(_lot(source_status="SuccessProtocolSigned")).eligible is False


def test_official_project_must_intersect_polygon_and_distinguish_the_lot() -> None:
    result = evaluate_shortlist_lot(
        _lot(
            development=OfficialDevelopmentEvidence(
                source="Astana open urban-planning data",
                source_url="https://example.test/official/project-17",
                observed_at=datetime(2026, 8, 29, tzinfo=UTC),
                project_name="Official access-road project 17",
                polygon_relation="intersects",
                active_alternatives_count=8,
                alternatives_with_project_count=1,
                comparison_method=(
                    "polygon intersection against the same active filter; official project layer"
                ),
            )
        )
    )

    assert result.interesting is True
    reason = result.reasons[0]
    assert reason.kind == "official_development"
    assert reason.classification == "confirmed"
    assert reason.metric == "polygon_relation=intersects; alternatives_with_project=1/8"
    assert reason.compared_with == "8 active alternatives in the same filter"
    assert reason.source_date == "2026-08-29"


def test_nearby_project_without_geometry_or_common_project_does_not_qualify() -> None:
    nearby = OfficialDevelopmentEvidence(
        source="official plan",
        source_url="https://example.test/official/project",
        observed_at=NOW,
        project_name="Road plan",
        polygon_relation="nearby_only",
        active_alternatives_count=8,
        alternatives_with_project_count=1,
        comparison_method="distance from a text geocode",
    )
    common = OfficialDevelopmentEvidence(
        source="official plan",
        source_url="https://example.test/official/project",
        observed_at=NOW,
        project_name="Road plan",
        polygon_relation="intersects",
        active_alternatives_count=8,
        alternatives_with_project_count=8,
        comparison_method="polygon intersection in the same active filter",
    )

    assert evaluate_shortlist_lot(_lot(development=nearby)).interesting is False
    assert evaluate_shortlist_lot(_lot(development=common)).interesting is False


def test_unavailable_official_project_source_is_manual_required_not_positive() -> None:
    result = evaluate_shortlist_lot(
        _lot(
            development=OfficialDevelopmentEvidence(
                source="official plan",
                source_url="https://example.test/official/unavailable",
                observed_at=NOW,
                project_name="Utility project",
                polygon_relation="intersects",
                active_alternatives_count=5,
                alternatives_with_project_count=0,
                comparison_method="polygon intersection in the same active filter",
                source_available=False,
            )
        )
    )

    assert result.interesting is False
    assert result.manual_required is True
    assert result.reasons == ()
    assert result.unchecked == (
        "Официальный источник проекта недоступен; проверить вручную",
    )


def test_rare_useful_feature_requires_explicit_same_filter_comparison() -> None:
    result = evaluate_shortlist_lot(
        _lot(
            rare_feature=RareFeatureEvidence(
                source="official cadastral rights register",
                source_url="https://example.test/official/rights/lot-1",
                observed_at=datetime(2026, 8, 31, tzinfo=UTC),
                feature_kind="right",
                feature_label="долгосрочная аренда 49 лет",
                target_metric="lease_term=49 years",
                active_alternatives_count=12,
                alternatives_with_feature_count=2,
                comparison_method="exact right and term across the same active catalog filter",
            )
        )
    )

    assert result.interesting is True
    reason = result.reasons[0]
    assert reason.kind == "rare_feature"
    assert reason.classification == "confirmed"
    assert reason.metric == (
        "target=lease_term=49 years; alternatives_with_feature=2/12; share=16.7%"
    )
    assert reason.compared_with == "12 active alternatives in the same filter"
    assert reason.source_date == "2026-08-31"


def test_common_unconfirmed_or_tiny_cohort_feature_does_not_qualify() -> None:
    base = dict(
        source="official source",
        source_url="https://example.test/official/feature",
        observed_at=NOW,
        feature_kind="access",
        feature_label="подтверждённый подъезд",
        target_metric="access=confirmed",
        comparison_method="same active filter",
    )
    common = RareFeatureEvidence(
        **base, active_alternatives_count=8, alternatives_with_feature_count=3
    )
    unconfirmed = RareFeatureEvidence(
        **base,
        active_alternatives_count=8,
        alternatives_with_feature_count=1,
        target_feature_confirmed=False,
    )
    tiny = RareFeatureEvidence(
        **base, active_alternatives_count=3, alternatives_with_feature_count=0
    )

    assert evaluate_shortlist_lot(_lot(rare_feature=common)).interesting is False
    assert evaluate_shortlist_lot(_lot(rare_feature=unconfirmed)).interesting is False
    assert evaluate_shortlist_lot(_lot(rare_feature=tiny)).interesting is False


def test_unavailable_rare_feature_source_is_manual_required_not_positive() -> None:
    result = evaluate_shortlist_lot(
        _lot(
            rare_feature=RareFeatureEvidence(
                source="official infrastructure register",
                source_url="https://example.test/official/unavailable",
                observed_at=NOW,
                feature_kind="infrastructure",
                feature_label="подключение к сети",
                target_metric="connection=confirmed",
                active_alternatives_count=10,
                alternatives_with_feature_count=1,
                comparison_method="same active filter",
                source_available=False,
            )
        )
    )

    assert result.interesting is False
    assert result.manual_required is True
    assert result.reasons == ()
    assert result.unchecked == (
        "Источник редкого признака недоступен; проверить вручную",
    )
    assert result.actions == (
        "Открыть официальный источник и повторить сравнение признака",
    )
