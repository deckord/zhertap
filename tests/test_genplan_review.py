from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from tools.genplan_review.cli import main
from tools.genplan_review.engine import EARTH_RADIUS_M, ReviewInputError, review_georeferencing

SOURCE_SHA = "a" * 64
RECORD_ID = "asset-burabay-1"
LON0 = 70.0
LAT0 = 52.0
MATRIX = [[2.0, 0.3, 100.0], [-0.2, 1.8, -50.0]]


def _wgs84(local_x: float, local_y: float) -> tuple[float, float]:
    lon = LON0 + math.degrees(local_x / (EARTH_RADIUS_M * math.cos(math.radians(LAT0))))
    lat = LAT0 + math.degrees(local_y / EARTH_RADIUS_M)
    return lon, lat


def _predicted(pixel_x: float, pixel_y: float) -> tuple[float, float]:
    return (
        MATRIX[0][0] * pixel_x + MATRIX[0][1] * pixel_y + MATRIX[0][2],
        MATRIX[1][0] * pixel_x + MATRIX[1][1] * pixel_y + MATRIX[1][2],
    )


def _a1_point(point_id: str, pixel_x: float, pixel_y: float) -> dict:
    local_x, local_y = _predicted(pixel_x, pixel_y)
    lon, lat = _wgs84(local_x, local_y)
    return {
        "id": point_id,
        "pixel_x": pixel_x,
        "pixel_y": pixel_y,
        "lon": lon,
        "lat": lat,
        "role": "train",
        "label": "",
        "reference_source": "A1 reference",
    }


def _checkpoint(
    point_id: str,
    pixel_x: float,
    pixel_y: float,
    source: str,
    feature: str,
    *,
    dx_m: float = 0.0,
    dy_m: float = 0.0,
) -> dict:
    predicted_x, predicted_y = _predicted(pixel_x, pixel_y)
    lon, lat = _wgs84(predicted_x - dx_m, predicted_y - dy_m)
    return {
        "id": point_id,
        "pixel_x": pixel_x,
        "pixel_y": pixel_y,
        "lon": lon,
        "lat": lat,
        "source": source,
        "feature": feature,
        "note": "",
    }


def _visual_samples() -> list[dict]:
    areas = ["edge"] * 4 + ["interior"] * 4 + ["boundary"] * 2 + ["critical"] * 2
    return [
        {
            "id": f"VS{index:02}",
            "area": area,
            "result": "pass",
            "source": "EGKN+satellite",
            "observation": "stable match",
        }
        for index, area in enumerate(areas, start=1)
    ]


def _payloads() -> tuple[dict, dict, dict, dict, dict]:
    a1_pixels = [
        (50, 50),
        (950, 50),
        (950, 750),
        (50, 750),
        (300, 250),
        (700, 250),
        (300, 550),
        (700, 550),
    ]
    gcps = {
        "schema_version": "1.0",
        "record_id": RECORD_ID,
        "asset_id": RECORD_ID,
        "source_path": "scan.jpg",
        "source_sha256": SOURCE_SHA,
        "page": 1,
        "image_width_px": 1000,
        "image_height_px": 800,
        "transform_type": "affine",
        "workflow_status": "qa_pending",
        "operator": "operator-a1",
        "notes": "",
        "saved_at_utc": "2026-07-23T07:00:00Z",
        "points": [
            _a1_point(f"GCP{index:02}", x, y)
            for index, (x, y) in enumerate(a1_pixels, start=1)
        ],
    }
    calculation = {
        "transform_type": "affine",
        "pixel_to_local_m_matrix": MATRIX,
        "local_origin_wgs84": {"lon": LON0, "lat": LAT0},
        "condition_number": 10.0,
    }
    qa = {
        "schema_version": "1.0",
        "record_id": RECORD_ID,
        "source_sha256": SOURCE_SHA,
        "page": 1,
        "workflow_status": "qa_pending",
        "qa_decision": "pending",
        "generated_at_utc": "2026-07-23T07:00:00Z",
        "calculation": calculation,
        "guardrails": {
            "approved_by_workbench": False,
            "allowed_workflow_statuses": ["proposed", "qa_pending"],
            "second_reviewer_required": True,
        },
    }
    checkpoint_specs = [
        ("CP01", 100, 100, "EGKN", "road_intersection"),
        ("CP02", 900, 100, "satellite", "bridge"),
        ("CP03", 900, 700, "OSM", "block_corner"),
        ("CP04", 100, 700, "EGKN+satellite", "road_intersection"),
        ("CP05", 300, 300, "OSM", "shoreline"),
        ("CP06", 700, 500, "satellite", "block_corner"),
    ]
    checkpoints = {
        "schema_version": "genplan-checkpoints/v1",
        "record_id": RECORD_ID,
        "source_sha256": SOURCE_SHA,
        "reviewer_id": "reviewer-a2",
        "reviewed_at_utc": "2026-07-23T08:00:00Z",
        "selected_before_a1_residuals": True,
        "points": [_checkpoint(*spec) for spec in checkpoint_specs],
    }
    provenance = {
        "schema_version": "genplan-provenance/v1",
        "record_id": RECORD_ID,
        "source_sha256": SOURCE_SHA,
        "status": "verified_official",
        "document_title": "Генеральный план села Бурабай",
        "document_type": "генплан",
        "approving_authority": "Акимат",
        "approval_number": "42",
        "approval_date": "2025-01-10",
        "official_url": "https://example.gov.kz/document/42",
        "publication_reference": "",
        "source_checked_at_utc": "2026-07-23T08:00:00Z",
        "territory": "село Бурабай",
        "revision": "2025",
        "current_version_confirmed": True,
        "identity_status": "resolved",
    }
    legend = {
        "schema_version": "genplan-legend-evidence/v1",
        "record_id": RECORD_ID,
        "source_sha256": SOURCE_SHA,
        "reviewer_id": "reviewer-a2",
        "legend_status": "readable",
        "interpretation_confirmed": True,
        "scale_denominator": 10_000,
        "orientation_status": "correct",
        "anisotropy_percent": 0.5,
        "anisotropy_explained": False,
        "visual_samples": _visual_samples(),
        "layers": [
            {"name": "roads", "status": "strict", "categories": [], "note": ""},
            {"name": "zones", "status": "strict", "categories": ["Ж-1"], "note": ""},
        ],
        "notes": "",
    }
    return gcps, qa, checkpoints, provenance, legend


def _review(payloads: tuple[dict, dict, dict, dict, dict]):
    return review_georeferencing(*payloads)


def _check(result, code: str):
    return next(check for check in result.checks if check.code == code)


def test_strict_review_recomputes_independent_metric_without_refitting() -> None:
    result = _review(_payloads())

    assert result.decision.value == "STRICT"
    assert result.metrics.rmse_m == pytest.approx(0, abs=0.001)
    assert result.metrics.p95_m == pytest.approx(0, abs=0.001)
    assert result.metrics.max_m == pytest.approx(0, abs=0.001)
    assert result.checkpoint_distribution.edge_count == 4
    assert result.checkpoint_distribution.interior_count == 2
    assert result.checkpoint_distribution.quadrants == ["EN", "ES", "WN", "WS"]
    assert result.guardrails["transformation_refitted"] is False
    assert result.guardrails["a1_inputs_mutated"] is False


def test_partial_official_source_caps_result_at_warning() -> None:
    payloads = list(_payloads())
    payloads[3]["status"] = "official_copy_unverified_version"
    payloads[3]["current_version_confirmed"] = False

    result = _review(tuple(payloads))

    assert result.decision.value == "WARNING"
    assert _check(result, "OFFICIAL_PROVENANCE").status.value == "warning"


def test_unknown_source_is_rejected_and_never_promoted() -> None:
    payloads = list(_payloads())
    payloads[3]["status"] = "unknown"
    payloads[3]["official_url"] = ""
    payloads[3]["current_version_confirmed"] = False

    result = _review(tuple(payloads))

    assert result.decision.value == "REJECT"
    assert _check(result, "OFFICIAL_PROVENANCE").status.value == "fail"


def test_same_a1_and_a2_reviewer_is_rejected() -> None:
    payloads = list(_payloads())
    payloads[2]["reviewer_id"] = "operator-a1"
    payloads[4]["reviewer_id"] = "operator-a1"

    result = _review(tuple(payloads))

    assert result.decision.value == "REJECT"
    assert _check(result, "INDEPENDENT_REVIEWERS").status.value == "fail"


def test_source_sha_mismatch_is_rejected() -> None:
    payloads = list(_payloads())
    payloads[2]["source_sha256"] = "b" * 64

    result = _review(tuple(payloads))

    assert result.decision.value == "REJECT"
    assert _check(result, "SOURCE_SHA256_MATCH").status.value == "fail"


def test_warning_error_band_reports_recomputed_metrics() -> None:
    payloads = list(_payloads())
    for point in payloads[2]["points"]:
        replacement = _checkpoint(
            point["id"],
            point["pixel_x"],
            point["pixel_y"],
            point["source"],
            point["feature"],
            dx_m=10,
        )
        point.update(replacement)

    result = _review(tuple(payloads))

    assert result.decision.value == "WARNING"
    assert result.metrics.rmse_m == pytest.approx(10, abs=0.001)
    assert _check(result, "CHECKPOINT_RMSE").status.value == "warning"
    assert _check(result, "CHECKPOINT_MAX").status.value == "pass"


def test_error_over_reject_threshold_is_rejected() -> None:
    payloads = list(_payloads())
    first = payloads[2]["points"][0]
    first.update(
        _checkpoint(
            first["id"],
            first["pixel_x"],
            first["pixel_y"],
            first["source"],
            first["feature"],
            dx_m=35,
        )
    )

    result = _review(tuple(payloads))

    assert result.decision.value == "REJECT"
    assert result.metrics.max_m == pytest.approx(35, abs=0.001)
    assert _check(result, "CHECKPOINT_MAX").status.value == "fail"


def test_fewer_than_six_independent_points_is_rejected() -> None:
    payloads = list(_payloads())
    payloads[2]["points"] = payloads[2]["points"][:5]

    result = _review(tuple(payloads))

    assert result.decision.value == "REJECT"
    assert _check(result, "A2_CHECKPOINT_COUNT").status.value == "fail"


def test_reusing_a1_point_id_is_rejected() -> None:
    payloads = list(_payloads())
    payloads[2]["points"][0]["id"] = "GCP01"

    result = _review(tuple(payloads))

    assert result.decision.value == "REJECT"
    assert _check(result, "CHECKPOINT_ID_SEPARATION").status.value == "fail"


def test_unreadable_legend_or_rejected_layer_is_rejected() -> None:
    payloads = list(_payloads())
    payloads[4]["legend_status"] = "unreadable"
    payloads[4]["interpretation_confirmed"] = False
    payloads[4]["layers"][0]["status"] = "reject"

    result = _review(tuple(payloads))

    assert result.decision.value == "REJECT"
    assert _check(result, "LEGEND_EVIDENCE").status.value == "fail"
    assert _check(result, "THEMATIC_LAYERS").status.value == "fail"


def test_checkpoint_outside_image_is_input_error() -> None:
    payloads = list(_payloads())
    payloads[2]["points"][0]["pixel_x"] = 1001

    with pytest.raises(ReviewInputError, match="outside"):
        _review(tuple(payloads))


def test_cli_writes_separate_review_and_does_not_modify_a1(tmp_path: Path) -> None:
    payloads = _payloads()
    names = ("gcps", "qa", "checkpoints", "provenance", "legend")
    paths: dict[str, Path] = {}
    for name, payload in zip(names, payloads, strict=True):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        paths[name] = path
    gcps_before = paths["gcps"].read_bytes()
    qa_before = paths["qa"].read_bytes()
    output = tmp_path / "a2" / "review.json"

    exit_code = main(
        [
            "--gcps",
            str(paths["gcps"]),
            "--qa",
            str(paths["qa"]),
            "--checkpoints",
            str(paths["checkpoints"]),
            "--provenance",
            str(paths["provenance"]),
            "--legend",
            str(paths["legend"]),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["decision"] == "STRICT"
    assert paths["gcps"].read_bytes() == gcps_before
    assert paths["qa"].read_bytes() == qa_before


def test_inputs_are_not_mutated_by_library_call() -> None:
    payloads = _payloads()
    before = copy.deepcopy(payloads)

    _review(payloads)

    assert payloads == before
