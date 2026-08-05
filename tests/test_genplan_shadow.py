from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools.genplan_shadow.cli import main
from tools.genplan_shadow.engine import compare_candidate
from tools.genplan_shadow.models import ComparisonRequest, Decision

SHA = "a" * 64


def _polygon(
    min_x: float = 70.0,
    min_y: float = 53.0,
    max_x: float = 71.0,
    max_y: float = 54.0,
) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [min_x, min_y],
                [max_x, min_y],
                [max_x, max_y],
                [min_x, max_y],
                [min_x, min_y],
            ]
        ],
    }


def _feature(
    feature_id: str,
    category: str,
    *,
    effect: str = "block",
    geometry: dict | None = None,
) -> dict:
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": {"category": category, "effect": effect},
        "geometry": geometry or _polygon(70.4, 53.4, 70.6, 53.6),
    }


def _layer(*, features: list[dict] | None = None) -> dict:
    return {
        "layer_id": "layer-1",
        "kind": "geojson_masks",
        "coverage_geometry": _polygon(),
        "categories_checked": ["road", "water", "zone"],
        "masks": {
            "type": "FeatureCollection",
            "features": features or [],
        },
        "provenance": {
            "source_id": "official-source",
            "source_title": "Official genplan",
            "source_version": "v3",
            "source_sha256": SHA,
            "status": "verified_official",
            "identity_status": "matched",
            "official_url": "https://example.gov.kz/genplan",
            "checked_at": "2026-07-01T10:00:00+06:00",
            "valid_until": "2027-07-01T10:00:00+06:00",
            "current": True,
            "superseded_by": None,
        },
        "qa_review": {
            "decision": "VERIFIED_STRICT",
            "review_version": "qa-3",
            "reviewer_id": "reviewer-2",
            "reviewed_at": "2026-07-02T10:00:00+06:00",
            "expires_at": "2027-07-02T10:00:00+06:00",
            "independent_review": True,
            "ambiguity_resolved": True,
            "source_sha256": SHA,
        },
    }


def _request(layer: dict, *, geometry: dict | None = None) -> ComparisonRequest:
    return ComparisonRequest.model_validate(
        {
            "candidate": {
                "candidate_id": "candidate-1",
                "geometry": geometry or _polygon(70.45, 53.45, 70.55, 53.55),
            },
            "layers": [layer],
            "as_of": "2026-07-23T12:00:00+06:00",
        }
    )


def _codes(result) -> set[str]:
    return {reason.code for reason in result.reasons}


def test_verified_clear_masks_return_match() -> None:
    result = compare_candidate(_request(_layer()))

    assert result.decision == Decision.match
    assert _codes(result) == {"TRUSTED_COVERAGE_CLEAR"}
    assert result.source_versions[0].source_version == "v3"
    assert result.source_versions[0].eligibility == "eligible"


@pytest.mark.parametrize(
    ("category", "expected_code"),
    [
        ("road", "INTERSECTS_ROAD"),
        ("water", "INTERSECTS_WATER"),
        ("zone", "INTERSECTS_BLOCKED_ZONE"),
    ],
)
def test_only_verified_intersecting_masks_can_block(
    category: str, expected_code: str
) -> None:
    result = compare_candidate(
        _request(_layer(features=[_feature("mask-7", category)]))
    )

    assert result.decision == Decision.blocked
    assert _codes(result) == {expected_code}
    assert result.matched_feature_ids == ["mask-7"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda layer: layer["provenance"].update(status="secondary_source"),
         "PROVENANCE_NOT_VERIFIED"),
        (lambda layer: layer["provenance"].update(current=False),
         "STALE_SOURCE_VERSION"),
        (lambda layer: layer["provenance"].update(superseded_by="v4"),
         "STALE_SOURCE_VERSION"),
        (lambda layer: layer["qa_review"].update(decision="REJECT"),
         "QA_REJECTED"),
        (lambda layer: layer["qa_review"].update(independent_review=False),
         "QA_NOT_INDEPENDENT"),
    ],
)
def test_untrusted_layers_never_block(
    mutation,
    expected_code: str,
) -> None:
    layer = _layer(features=[_feature("road-1", "road")])
    mutation(layer)

    result = compare_candidate(_request(layer))

    assert result.decision == Decision.no_coverage
    assert result.decision != Decision.blocked
    assert expected_code in _codes(result)
    assert "NO_TRUSTED_COVERAGE" in _codes(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda layer: layer.update(ambiguous=True),
        lambda layer: layer["provenance"].update(identity_status="ambiguous"),
        lambda layer: layer["qa_review"].update(ambiguity_resolved=False),
        lambda layer: layer["qa_review"].update(source_sha256="b" * 64),
    ],
)
def test_ambiguous_or_integrity_conflicted_layer_requires_manual_review(
    mutation,
) -> None:
    layer = _layer(features=[_feature("road-1", "road")])
    mutation(layer)

    result = compare_candidate(_request(layer))

    assert result.decision == Decision.manual_review
    assert result.decision != Decision.blocked


def test_incomplete_classification_requires_manual_review() -> None:
    layer = _layer()
    layer["categories_checked"] = ["road", "water"]

    result = compare_candidate(_request(layer))

    assert result.decision == Decision.manual_review
    assert _codes(result) == {"INCOMPLETE_CLASSIFICATION"}


def test_unknown_intersecting_mask_requires_manual_review() -> None:
    result = compare_candidate(
        _request(_layer(features=[_feature("zone-1", "zone", effect="advisory")]))
    )

    assert result.decision == Decision.manual_review
    assert _codes(result) == {"MASK_REQUIRES_INTERPRETATION"}


def test_candidate_outside_layers_returns_no_coverage() -> None:
    result = compare_candidate(
        _request(_layer(), geometry=_polygon(72.0, 55.0, 72.1, 55.1))
    )

    assert result.decision == Decision.no_coverage
    assert _codes(result) == {"NO_LAYER_COVERAGE"}
    assert result.source_versions[0].eligibility == "outside_candidate_area"


def test_partially_covered_polygon_returns_no_coverage() -> None:
    result = compare_candidate(
        _request(_layer(), geometry=_polygon(70.9, 53.9, 71.1, 54.1))
    )

    assert result.decision == Decision.no_coverage
    assert _codes(result) == {"PARTIAL_TRUSTED_COVERAGE"}


def test_georaster_metadata_can_match_only_with_complete_classification() -> None:
    layer = _layer()
    layer["kind"] = "georaster"
    layer.pop("coverage_geometry")
    layer.pop("categories_checked")
    layer.pop("masks")
    layer["raster"] = {
        "uri": "file:///data/genplan-v3.tif",
        "crs": "EPSG:4326",
        "footprint": _polygon(),
        "classification_complete": True,
        "categories_checked": ["road", "water", "zone"],
        "masks": {"type": "FeatureCollection", "features": []},
    }

    result = compare_candidate(_request(layer))

    assert result.decision == Decision.match

    incomplete = deepcopy(layer)
    incomplete["raster"]["classification_complete"] = False
    incomplete_result = compare_candidate(_request(incomplete))
    assert incomplete_result.decision == Decision.manual_review
    assert _codes(incomplete_result) == {"INCOMPLETE_CLASSIFICATION"}


def test_point_candidate_is_supported() -> None:
    point = {"type": "Point", "coordinates": [70.5, 53.5]}
    result = compare_candidate(
        _request(
            _layer(features=[_feature("water-1", "water")]),
            geometry=point,
        )
    )

    assert result.decision == Decision.blocked
    assert result.matched_feature_ids == ["water-1"]


def test_cli_writes_auditable_json(tmp_path: Path) -> None:
    input_path = tmp_path / "request.json"
    output_path = tmp_path / "decision.json"
    request = _request(_layer())
    input_path.write_text(
        request.model_dump_json(),
        encoding="utf-8",
    )

    exit_code = main(["--input", str(input_path), "--output", str(output_path)])

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "genplan-shadow/v1"
    assert payload["decision"] == "match"
    assert payload["source_versions"][0]["source_version"] == "v3"


def test_cli_rejects_invalid_input(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "bad.json"
    input_path.write_text("{}", encoding="utf-8")

    exit_code = main(["--input", str(input_path)])

    assert exit_code == 2
    assert "genplan-shadow:" in capsys.readouterr().err
