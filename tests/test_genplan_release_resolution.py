from app.models import UrbanPlanLayer
from tools.genplan_release_resolution import (
    REVIEWED_HOLD,
    SUPERSEDED,
    VERIFIED_STRICT,
    ReleaseGroup,
    _scope_key,
    classify_release,
)


def _group(*, document_id: int = 3440, first_id: int = 100) -> ReleaseGroup:
    rows = [
        UrbanPlanLayer(
            id=first_id + offset,
            region="Западно-Казахстанская область",
            district="г.Уральск",
            locality="г.Уральск",
            purpose="ЛПХ:household",
            layer_kind=kind,
            source_sha256="a" * 64,
            qa_status="WARNING",
            qa_review_json="{}",
            active=False,
        )
        for offset, kind in enumerate(("allowed", "prohibited", "red_line"))
    ]
    return ReleaseGroup(
        rows=rows,
        audit={},
        document_id=document_id,
        reviewed_points=3,
        geometry_ok=True,
        residual_ratio=0.75,
    )


def test_verified_release_requires_all_strict_evidence() -> None:
    group = _group()

    result = classify_release(
        group,
        active_scope_keys=set(),
        newest_group_ids={(3440, "ЛПХ:household"): group.first_id},
    )

    assert result.status == VERIFIED_STRICT
    assert result.activate is True


def test_release_with_active_exact_scope_is_superseded() -> None:
    group = _group()

    result = classify_release(
        group,
        active_scope_keys={_scope_key(group.scope)},
        newest_group_ids={(3440, "ЛПХ:household"): group.first_id},
    )

    assert result.status == SUPERSEDED
    assert result.activate is False


def test_release_without_published_legal_act_is_reviewed_hold() -> None:
    group = _group(document_id=3438)

    result = classify_release(
        group,
        active_scope_keys=set(),
        newest_group_ids={(3438, "ЛПХ:household"): group.first_id},
    )

    assert result.status == REVIEWED_HOLD
    assert result.reason == "official_legal_act_url_not_published"


def test_release_with_no_residual_allowed_area_stays_off() -> None:
    group = _group()
    group.residual_ratio = 0

    result = classify_release(
        group,
        active_scope_keys=set(),
        newest_group_ids={(3440, "ЛПХ:household"): group.first_id},
    )

    assert result.status == REVIEWED_HOLD
    assert result.reason == "allowed_area_fully_covered_by_restrictions"


def test_older_duplicate_shadow_release_is_superseded() -> None:
    group = _group(first_id=100)

    result = classify_release(
        group,
        active_scope_keys=set(),
        newest_group_ids={(3440, "ЛПХ:household"): 200},
    )

    assert result.status == SUPERSEDED
    assert result.reason == "duplicate_shadow_release"
