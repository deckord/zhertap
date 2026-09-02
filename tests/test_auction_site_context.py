from __future__ import annotations

import math

from app.auction_site_context import SiteContextLimits, analyze_site_context

STAMP = "2026-08-17T10:00:00+00:00"


def evidence(source="official_gis", complete=True, confidence=0.9):
    return {
        "provenance": source,
        "observed_at": STAMP,
        "coverage_complete": complete,
        "confidence": confidence,
    }


def coverage(complete=True):
    return {
        str(radius): evidence(f"provider_{radius}", complete) for radius in (500, 1000, 3000, 5000)
    }


def test_road_near_but_inaccessible_is_physical_blocker() -> None:
    result = analyze_site_context(
        "roadside",
        physical_access={
            "connected": False,
            "road_distance_m": 8,
            "surface": "asphalt",
            "evidence": evidence(),
        },
    )
    assert result.physical_access.status == "blocked"
    assert result.physical_access.blockers
    assert result.legal_access.status == "unknown"


def test_physical_access_does_not_imply_legal_access() -> None:
    result = analyze_site_context(
        "warehouse",
        physical_access={"connected": True, "frontage_m": 40, "evidence": evidence()},
        legal_access={
            "public_road_access": None,
            "easement_confirmed": None,
            "evidence": evidence(complete=False),
        },
    )
    assert result.physical_access.status == "ready"
    assert result.legal_access.status == "unknown"
    assert "unconfirmed" in " ".join(result.legal_access.warnings)


def test_infrastructure_distance_never_implies_connection_or_capacity() -> None:
    result = analyze_site_context(
        "data_center",
        infrastructure={
            "services": {
                "electricity": {
                    "distance_m": 25,
                    "connection_status": "unknown",
                    "capacity_status": "unknown",
                    "evidence": evidence(),
                },
                "internet": {
                    "distance_m": 10,
                    "connection_status": "available",
                    "capacity_status": "unknown",
                    "evidence": evidence(),
                },
            }
        },
    )
    assert result.infrastructure.status == "attention"
    warnings = " ".join(result.infrastructure.warnings)
    assert "electricity: capacity is unknown" in warnings
    assert "internet: capacity is unknown" in warnings
    assert "water" in warnings


def test_camping_452662_profile_keeps_incomplete_context_partial() -> None:
    result = analyze_site_context(
        "camping",
        physical_access={
            "connected": True,
            "road_distance_m": 120,
            "surface": "unpaved",
            "evidence": evidence("osm_and_satellite", complete=False, confidence=0.65),
        },
        legal_access=None,
        infrastructure={"services": {}},
        environment={
            "features": [
                {"category": "settlement", "name": "Булак", "distance_m": 2000},
                {"category": "nature", "name": "Открытый ландшафт", "distance_m": 450},
            ],
            "coverage": coverage(complete=False),
        },
    )
    assert result.profile == "camping"
    assert result.physical_access.status == "attention"
    assert result.legal_access.status == "unknown"
    assert result.infrastructure.status == "unknown"
    assert result.environment.status == "attention"
    assert any("Profile-relevant" in fact for fact in result.environment.facts)


def test_environment_hazards_are_profile_sensitive_and_bounded_by_radius() -> None:
    residential = analyze_site_context(
        "residential",
        environment={
            "features": [
                {"category": "landfill", "name": "Полигон", "distance_m": 800},
                {"category": "industry", "name": "Завод", "distance_m": 900},
                {"category": "rail", "name": "ЖД", "distance_m": 450},
            ],
            "coverage": coverage(),
        },
    )
    assert residential.environment.status == "blocked"
    assert "Landfill within 1 km" in residential.environment.blockers
    assert len(residential.environment.warnings) == 2


def test_unknown_environment_coverage_is_not_zero_or_absence() -> None:
    result = analyze_site_context(
        "retail",
        environment={"features": [], "coverage": coverage(complete=False)},
    )
    assert result.environment.status == "attention"
    assert result.environment.readiness == "partial"
    assert result.environment.warnings


def test_malformed_nonfinite_and_oversized_inputs_return_explicit_errors() -> None:
    bad_access = analyze_site_context(
        "retail",
        physical_access={"connected": True, "road_distance_m": math.inf, "evidence": evidence()},
    )
    too_many = analyze_site_context(
        "retail",
        environment={
            "features": [
                {"category": "stop", "name": str(index), "distance_m": 100} for index in range(3)
            ],
            "coverage": coverage(),
        },
        limits=SiteContextLimits(max_features=2),
    )
    bad_timestamp = analyze_site_context(
        "retail",
        legal_access={
            "public_road_access": True,
            "evidence": {**evidence(), "observed_at": "not-a-date"},
        },
    )
    assert bad_access.physical_access.error_code == "invalid_number"
    assert too_many.environment.error_code == "too_many_features"
    assert bad_timestamp.legal_access.error_code == "invalid_timestamp"


def test_profile_weights_are_deterministic_but_no_universal_score_or_verdict() -> None:
    first = analyze_site_context("retail")
    second = analyze_site_context("retail")
    data_center = analyze_site_context("data_center")
    assert first == second
    assert first.profile_weights == {"access": 0.4, "infrastructure": 0.25, "environment": 0.35}
    assert data_center.profile_weights["infrastructure"] == 0.6
    assert not hasattr(first, "score")
    assert not hasattr(first, "verdict")


def test_all_required_use_profiles_have_explicit_configuration() -> None:
    profiles = (
        "retail",
        "roadside",
        "warehouse",
        "hospitality",
        "camping",
        "residential",
        "data_center",
        "agriculture",
        "other",
    )
    for profile in profiles:
        result = analyze_site_context(profile)
        assert result.profile == profile
        assert set(result.profile_weights) == {"access", "infrastructure", "environment"}


def test_empty_infrastructure_without_coverage_is_unknown_not_ready() -> None:
    result = analyze_site_context("other", infrastructure={"services": {}})
    assert result.infrastructure.status == "unknown"
    assert result.infrastructure.readiness == "unknown"
    assert result.infrastructure.provenance == ()


def test_infrastructure_complete_vs_incomplete_dimension_metadata() -> None:
    incomplete = analyze_site_context(
        "other",
        infrastructure={"services": {}, "evidence": evidence(complete=False)},
    )
    complete = analyze_site_context(
        "other",
        infrastructure={"services": {}, "evidence": evidence(complete=True)},
    )
    assert incomplete.infrastructure.status == "attention"
    assert incomplete.infrastructure.readiness == "partial"
    assert incomplete.infrastructure.provenance
    assert complete.infrastructure.status == "ready"
    assert complete.infrastructure.readiness == "ready"
    assert complete.infrastructure.provenance


def test_localized_or_unknown_environment_category_is_explicit_error() -> None:
    result = analyze_site_context(
        "residential",
        environment={
            "features": [{"category": "свалка", "name": "Полигон", "distance_m": 300}],
            "coverage": coverage(),
        },
    )
    assert result.environment.status == "error"
    assert result.environment.error_code == "unsupported_environment_category"
