from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

import numpy as np
from pydantic import ValidationError

from .models import (
    CheckpointResidual,
    CheckpointSet,
    CheckStatus,
    ErrorMetrics,
    LegendEvidence,
    PointDistribution,
    Provenance,
    ProvenanceStatus,
    ReviewCheck,
    ReviewDecision,
    ReviewReason,
    ReviewResult,
)

EARTH_RADIUS_M = 6_378_137.0
EDGE_RATIO = 0.15
INTERIOR_LOW = 0.20
INTERIOR_HIGH = 0.80


class ReviewInputError(ValueError):
    """Raised when an input cannot be evaluated safely."""


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check(
    checks: list[ReviewCheck],
    code: str,
    status: CheckStatus,
    message: str,
    *,
    observed: Any = None,
    strict_requirement: Any = None,
) -> None:
    checks.append(
        ReviewCheck(
            code=code,
            status=status,
            message=message,
            observed=observed,
            strict_requirement=strict_requirement,
        )
    )


def _quadrant(pixel_x: float, pixel_y: float, width: float, height: float) -> str:
    return f"{'W' if pixel_x < width / 2 else 'E'}{'N' if pixel_y < height / 2 else 'S'}"


def _is_edge(pixel_x: float, pixel_y: float, width: float, height: float) -> bool:
    return (
        pixel_x <= width * EDGE_RATIO
        or pixel_x >= width * (1 - EDGE_RATIO)
        or pixel_y <= height * EDGE_RATIO
        or pixel_y >= height * (1 - EDGE_RATIO)
    )


def _is_interior(pixel_x: float, pixel_y: float, width: float, height: float) -> bool:
    return (
        width * INTERIOR_LOW <= pixel_x <= width * INTERIOR_HIGH
        and height * INTERIOR_LOW <= pixel_y <= height * INTERIOR_HIGH
    )


def _max_empty_radius(points: list[dict[str, Any]], width: float, height: float) -> float:
    if not points:
        return float("inf")
    normalized = np.asarray(
        [[float(point["pixel_x"]) / width, float(point["pixel_y"]) / width] for point in points]
    )
    grid_x = np.linspace(0.0, 1.0, 41)
    grid_y = np.linspace(0.0, height / width, 41)
    maximum = 0.0
    for x_value in grid_x:
        for y_value in grid_y:
            distances = np.linalg.norm(normalized - np.array([x_value, y_value]), axis=1)
            maximum = max(maximum, float(np.min(distances)))
    return maximum


def _source_tokens(source: str) -> set[str]:
    return {
        token.strip().casefold()
        for token in re.split(r"[+,|;/]", source)
        if token.strip()
    }


def _distribution(
    points: list[dict[str, Any]],
    width: float,
    height: float,
    *,
    independent: bool,
) -> PointDistribution:
    quadrants = sorted(
        {
            _quadrant(float(point["pixel_x"]), float(point["pixel_y"]), width, height)
            for point in points
        }
    )
    edge_count = sum(
        _is_edge(float(point["pixel_x"]), float(point["pixel_y"]), width, height)
        for point in points
    )
    interior_count = sum(
        _is_interior(float(point["pixel_x"]), float(point["pixel_y"]), width, height)
        for point in points
    )
    feature_count = (
        len({str(point["feature"]).strip().casefold() for point in points}) if independent else None
    )
    source_count = (
        len(set().union(*(_source_tokens(str(point["source"])) for point in points)))
        if independent
        else None
    )
    return PointDistribution(
        count=len(points),
        quadrants=quadrants,
        edge_count=edge_count,
        interior_count=interior_count,
        feature_type_count=feature_count,
        reference_source_count=source_count,
        max_empty_radius_width_ratio=(
            None if independent else round(_max_empty_radius(points, width, height), 6)
        ),
    )


def _local_target(lon: float, lat: float, lon0: float, lat0: float) -> np.ndarray:
    cos_lat = math.cos(math.radians(lat0))
    if abs(cos_lat) < 1e-9:
        raise ReviewInputError("transform origin is too close to a pole")
    return np.asarray(
        [
            EARTH_RADIUS_M * math.radians(lon - lon0) * cos_lat,
            EARTH_RADIUS_M * math.radians(lat - lat0),
        ],
        dtype=float,
    )


def _predict(transform_type: str, matrix: np.ndarray, pixel_x: float, pixel_y: float) -> np.ndarray:
    pixel = np.asarray([pixel_x, pixel_y, 1.0], dtype=float)
    transformed = matrix @ pixel
    if transform_type == "affine":
        if matrix.shape != (2, 3):
            raise ReviewInputError("affine transform matrix must be 2x3")
        return transformed
    if transform_type == "projective":
        if matrix.shape != (3, 3):
            raise ReviewInputError("projective transform matrix must be 3x3")
        if abs(float(transformed[2])) < 1e-12:
            raise ReviewInputError("projective transform approaches infinity")
        return transformed[:2] / transformed[2]
    raise ReviewInputError(f"unsupported saved transform type: {transform_type}")


def _residuals(
    checkpoints: CheckpointSet,
    calculation: dict[str, Any],
) -> tuple[list[CheckpointResidual], ErrorMetrics]:
    transform_type = str(calculation.get("transform_type", ""))
    try:
        matrix = np.asarray(calculation["pixel_to_local_m_matrix"], dtype=float)
        origin = calculation["local_origin_wgs84"]
        lon0 = float(origin["lon"])
        lat0 = float(origin["lat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewInputError("qa.json does not contain a usable saved transformation") from exc

    results: list[CheckpointResidual] = []
    errors: list[float] = []
    for point in checkpoints.points:
        predicted = _predict(
            transform_type,
            matrix,
            point.pixel_x,
            point.pixel_y,
        )
        target = _local_target(point.lon, point.lat, lon0, lat0)
        delta = predicted - target
        error = float(np.linalg.norm(delta))
        errors.append(error)
        results.append(
            CheckpointResidual(
                id=point.id,
                dx_m=round(float(delta[0]), 4),
                dy_m=round(float(delta[1]), 4),
                error_m=round(error, 4),
                source=point.source,
                feature=point.feature,
            )
        )
    values = np.asarray(errors, dtype=float)
    metrics = ErrorMetrics(
        count=len(errors),
        rmse_m=round(float(np.sqrt(np.mean(np.square(values)))), 4),
        p95_m=round(float(np.percentile(values, 95)), 4),
        max_m=round(float(np.max(values)), 4),
    )
    return results, metrics


def _validated_a1(gcps: dict[str, Any], qa: dict[str, Any]) -> tuple[list[dict[str, Any]], dict]:
    required_gcps = {
        "record_id",
        "source_sha256",
        "image_width_px",
        "image_height_px",
        "transform_type",
        "operator",
        "points",
    }
    missing = sorted(required_gcps - gcps.keys())
    if missing:
        raise ReviewInputError(f"gcps.json is missing required fields: {', '.join(missing)}")
    if not isinstance(gcps["points"], list):
        raise ReviewInputError("gcps.json points must be a list")
    calculation = qa.get("calculation")
    if not isinstance(calculation, dict):
        raise ReviewInputError("qa.json calculation must be an object")
    transform_type = str(gcps["transform_type"])
    if calculation.get("transform_type") != transform_type:
        raise ReviewInputError("gcps.json and qa.json transform types do not match")
    train = [
        point
        for point in gcps["points"]
        if isinstance(point, dict) and point.get("role", "train") == "train"
    ]
    for index, point in enumerate(train):
        for field in ("id", "pixel_x", "pixel_y", "lon", "lat"):
            if field not in point:
                raise ReviewInputError(f"A1 train point {index} is missing {field}")
    return train, calculation


def _sha_checks(
    gcps: dict[str, Any],
    qa: dict[str, Any],
    checkpoints: CheckpointSet,
    provenance: Provenance,
    legend: LegendEvidence,
    checks: list[ReviewCheck],
) -> str:
    values = {
        "gcps.json": str(gcps.get("source_sha256", "")).lower(),
        "qa.json": str(qa.get("source_sha256", "")).lower(),
        "checkpoints.json": checkpoints.source_sha256.lower(),
        "provenance.json": provenance.source_sha256.lower(),
        "legend.json": legend.source_sha256.lower(),
    }
    unique = set(values.values())
    valid = len(unique) == 1 and bool(re.fullmatch(r"[0-9a-f]{64}", next(iter(unique), "")))
    _check(
        checks,
        "SOURCE_SHA256_MATCH",
        CheckStatus.pass_ if valid else CheckStatus.fail,
        "All review artifacts reference the same source SHA-256."
        if valid
        else "Source SHA-256 differs between review artifacts or is invalid.",
        observed=values,
        strict_requirement="one identical valid SHA-256",
    )
    return values["gcps.json"]


def _record_checks(
    gcps: dict[str, Any],
    qa: dict[str, Any],
    checkpoints: CheckpointSet,
    provenance: Provenance,
    legend: LegendEvidence,
    checks: list[ReviewCheck],
) -> str:
    values = {
        "gcps.json": str(gcps.get("record_id", "")),
        "qa.json": str(qa.get("record_id", "")),
        "checkpoints.json": checkpoints.record_id,
        "provenance.json": provenance.record_id,
        "legend.json": legend.record_id,
    }
    valid = len(set(values.values())) == 1 and bool(values["gcps.json"])
    _check(
        checks,
        "RECORD_ID_MATCH",
        CheckStatus.pass_ if valid else CheckStatus.fail,
        "All artifacts reference the same record."
        if valid
        else "Record ID differs between review artifacts.",
        observed=values,
        strict_requirement="one identical non-empty record ID",
    )
    return values["gcps.json"]


def _operator_checks(
    gcps: dict[str, Any],
    checkpoints: CheckpointSet,
    legend: LegendEvidence,
    checks: list[ReviewCheck],
) -> tuple[str, str]:
    a1 = str(gcps.get("operator", "")).strip()
    a2 = checkpoints.reviewer_id.strip()
    separated = bool(a1 and a2 and a1.casefold() != a2.casefold())
    _check(
        checks,
        "INDEPENDENT_REVIEWERS",
        CheckStatus.pass_ if separated else CheckStatus.fail,
        "A1 operator and A2 reviewer are different."
        if separated
        else "A1 operator and A2 reviewer must be identified and different.",
        observed={"a1_operator_id": a1, "a2_reviewer_id": a2},
        strict_requirement="different non-empty IDs",
    )
    legend_same_reviewer = legend.reviewer_id.casefold() == a2.casefold()
    _check(
        checks,
        "LEGEND_REVIEWER",
        CheckStatus.pass_ if legend_same_reviewer else CheckStatus.warning,
        "Legend evidence was recorded by the A2 reviewer."
        if legend_same_reviewer
        else "Legend evidence reviewer differs from the checkpoint reviewer.",
        observed=legend.reviewer_id,
        strict_requirement=a2,
    )
    _check(
        checks,
        "CHECKPOINT_SELECTION_INDEPENDENCE",
        CheckStatus.pass_ if checkpoints.selected_before_a1_residuals else CheckStatus.fail,
        "A2 checkpoints were selected before viewing A1 residuals."
        if checkpoints.selected_before_a1_residuals
        else "A2 checkpoints were not selected independently of A1 residuals.",
        observed=checkpoints.selected_before_a1_residuals,
        strict_requirement=True,
    )
    return a1, a2


def _distribution_checks(
    a1: PointDistribution,
    a2: PointDistribution,
    checks: list[ReviewCheck],
) -> None:
    a1_count_status = (
        CheckStatus.pass_
        if a1.count >= 8
        else CheckStatus.warning
        if a1.count >= 6
        else CheckStatus.fail
    )
    _check(
        checks,
        "A1_GCP_COUNT",
        a1_count_status,
        f"A1 supplied {a1.count} training points.",
        observed=a1.count,
        strict_requirement=8,
    )
    a1_distribution_strict = (
        len(a1.quadrants) == 4
        and a1.edge_count >= 2
        and a1.interior_count >= 2
        and (a1.max_empty_radius_width_ratio or math.inf) <= 0.35
    )
    a1_distribution_warning = (
        len(a1.quadrants) >= 3 and a1.interior_count >= 1 and a1.count >= 6
    )
    _check(
        checks,
        "A1_GCP_DISTRIBUTION",
        CheckStatus.pass_
        if a1_distribution_strict
        else CheckStatus.warning
        if a1_distribution_warning
        else CheckStatus.fail,
        "A1 training-point distribution was evaluated across quarters, edges and interior.",
        observed=a1.model_dump(mode="json"),
        strict_requirement={
            "quadrants": 4,
            "edge_count": 2,
            "interior_count": 2,
            "max_empty_radius_width_ratio": 0.35,
        },
    )
    _check(
        checks,
        "A2_CHECKPOINT_COUNT",
        CheckStatus.pass_ if a2.count >= 6 else CheckStatus.fail,
        f"A2 supplied {a2.count} independent checkpoints.",
        observed=a2.count,
        strict_requirement=6,
    )
    a2_distribution_strict = (
        len(a2.quadrants) == 4
        and a2.edge_count >= 4
        and a2.interior_count >= 2
        and (a2.feature_type_count or 0) >= 3
        and (a2.reference_source_count or 0) >= 2
    )
    a2_distribution_warning = len(a2.quadrants) >= 3 and a2.count >= 6
    _check(
        checks,
        "A2_CHECKPOINT_DISTRIBUTION",
        CheckStatus.pass_
        if a2_distribution_strict
        else CheckStatus.warning
        if a2_distribution_warning
        else CheckStatus.fail,
        "A2 checkpoints were evaluated across quarters, edges, interior, features and sources.",
        observed=a2.model_dump(mode="json"),
        strict_requirement={
            "quadrants": 4,
            "edge_count": 4,
            "interior_count": 2,
            "feature_type_count": 3,
            "reference_source_count": 2,
        },
    )


def _metric_checks(metrics: ErrorMetrics, checks: list[ReviewCheck]) -> None:
    thresholds = (
        ("CHECKPOINT_RMSE", metrics.rmse_m, 5.0, 15.0),
        ("CHECKPOINT_P95", metrics.p95_m, 8.0, 25.0),
        ("CHECKPOINT_MAX", metrics.max_m, 10.0, 30.0),
    )
    for code, value, strict_limit, reject_limit in thresholds:
        status = (
            CheckStatus.pass_
            if value <= strict_limit
            else CheckStatus.warning
            if value <= reject_limit
            else CheckStatus.fail
        )
        _check(
            checks,
            code,
            status,
            f"{code.removeprefix('CHECKPOINT_')} is {value:.4f} m.",
            observed=value,
            strict_requirement=f"<= {strict_limit} m",
        )


def _provenance_checks(provenance: Provenance, checks: list[ReviewCheck]) -> None:
    required_values = {
        "document_title": provenance.document_title,
        "document_type": provenance.document_type,
        "approving_authority": provenance.approving_authority,
        "approval_number": provenance.approval_number,
        "approval_date": provenance.approval_date,
        "official_publication": provenance.official_url or provenance.publication_reference,
        "source_checked_at_utc": provenance.source_checked_at_utc,
        "territory": provenance.territory,
        "revision": provenance.revision,
    }
    complete = all(bool(value) for value in required_values.values())
    official = (
        provenance.status == ProvenanceStatus.verified_official
        and provenance.current_version_confirmed
        and provenance.identity_status == "resolved"
        and complete
    )
    if official:
        status = CheckStatus.pass_
    elif provenance.status == ProvenanceStatus.unknown or provenance.identity_status != "resolved":
        status = CheckStatus.fail
    else:
        status = CheckStatus.warning
    _check(
        checks,
        "OFFICIAL_PROVENANCE",
        status,
        "Official source, approval act and current revision are confirmed."
        if official
        else "Official source, approval act or current revision is not fully confirmed.",
        observed={
            "status": provenance.status.value,
            "current_version_confirmed": provenance.current_version_confirmed,
            "identity_status": provenance.identity_status,
            "missing_fields": [key for key, value in required_values.items() if not value],
        },
        strict_requirement="verified_official with complete act, publication and current version",
    )


def _legend_checks(legend: LegendEvidence, checks: list[ReviewCheck]) -> None:
    readable = legend.legend_status == "readable" and legend.interpretation_confirmed
    if readable:
        legend_status = CheckStatus.pass_
    elif legend.legend_status == "unreadable":
        legend_status = CheckStatus.fail
    else:
        legend_status = CheckStatus.warning
    _check(
        checks,
        "LEGEND_EVIDENCE",
        legend_status,
        "Legend is readable and its interpretation is confirmed."
        if readable
        else "Legend is absent, unreadable, or its interpretation is unconfirmed.",
        observed={
            "legend_status": legend.legend_status,
            "interpretation_confirmed": legend.interpretation_confirmed,
        },
        strict_requirement={"legend_status": "readable", "interpretation_confirmed": True},
    )

    if legend.orientation_status == "correct":
        orientation_status = CheckStatus.pass_
    elif legend.orientation_status == "explained_deviation":
        orientation_status = CheckStatus.warning
    else:
        orientation_status = CheckStatus.fail
    _check(
        checks,
        "ORIENTATION",
        orientation_status,
        f"Orientation status: {legend.orientation_status}.",
        observed=legend.orientation_status,
        strict_requirement="correct",
    )

    anisotropy = legend.anisotropy_percent
    if anisotropy is None:
        anisotropy_status = CheckStatus.warning
    elif anisotropy <= 1:
        anisotropy_status = CheckStatus.pass_
    elif anisotropy <= 3 or legend.anisotropy_explained:
        anisotropy_status = CheckStatus.warning
    else:
        anisotropy_status = CheckStatus.fail
    _check(
        checks,
        "SCALE_ANISOTROPY",
        anisotropy_status,
        "Scale anisotropy was checked.",
        observed={
            "percent": anisotropy,
            "explained": legend.anisotropy_explained,
        },
        strict_requirement="<= 1%",
    )

    scale = legend.scale_denominator
    scale_status = (
        CheckStatus.pass_
        if scale <= 10_000
        else CheckStatus.warning
        if scale <= 25_000
        else CheckStatus.fail
    )
    _check(
        checks,
        "MAP_SCALE",
        scale_status,
        f"Map scale denominator is 1:{scale}.",
        observed=scale,
        strict_requirement="<= 10000",
    )

    samples = legend.visual_samples
    sample_counts = {
        area: sum(sample.area == area for sample in samples)
        for area in ("edge", "interior", "boundary", "critical")
    }
    has_fail = any(sample.result == "fail" for sample in samples)
    strict_samples = (
        len(samples) >= 12
        and sample_counts["edge"] >= 4
        and sample_counts["interior"] >= 4
        and sample_counts["boundary"] >= 2
        and sample_counts["critical"] >= 2
        and not has_fail
    )
    if has_fail or len(samples) < 8:
        visual_status = CheckStatus.fail
    elif strict_samples:
        visual_status = CheckStatus.pass_
    else:
        visual_status = CheckStatus.warning
    _check(
        checks,
        "VISUAL_SAMPLES",
        visual_status,
        "Visual comparison samples were checked against the QA protocol.",
        observed={"total": len(samples), **sample_counts, "has_fail": has_fail},
        strict_requirement={"total": 12, "edge": 4, "interior": 4, "boundary": 2, "critical": 2},
    )

    rejected_layers = [layer.name for layer in legend.layers if layer.status == "reject"]
    warning_layers = [
        layer.name for layer in legend.layers if layer.status in {"warning", "unavailable"}
    ]
    layers_status = (
        CheckStatus.fail
        if rejected_layers
        else CheckStatus.warning
        if warning_layers
        else CheckStatus.pass_
    )
    _check(
        checks,
        "THEMATIC_LAYERS",
        layers_status,
        "Thematic-layer statuses preserve unavailable and rejected content."
        if not rejected_layers
        else "At least one thematic layer is rejected.",
        observed={"rejected": rejected_layers, "warning_or_unavailable": warning_layers},
        strict_requirement="all declared layers strict",
    )


def _decision(checks: list[ReviewCheck]) -> ReviewDecision:
    if any(check.status == CheckStatus.fail for check in checks):
        return ReviewDecision.reject
    if any(check.status == CheckStatus.warning for check in checks):
        return ReviewDecision.warning
    return ReviewDecision.strict


def review_georeferencing(
    gcps_payload: dict[str, Any],
    qa_payload: dict[str, Any],
    checkpoints_payload: dict[str, Any],
    provenance_payload: dict[str, Any],
    legend_payload: dict[str, Any],
) -> ReviewResult:
    """Evaluate A1 artifacts without fitting or mutating the saved transformation."""
    try:
        checkpoints = CheckpointSet.model_validate(checkpoints_payload)
        provenance = Provenance.model_validate(provenance_payload)
        legend = LegendEvidence.model_validate(legend_payload)
    except ValidationError as exc:
        raise ReviewInputError(str(exc)) from exc

    train, calculation = _validated_a1(gcps_payload, qa_payload)
    width = float(gcps_payload["image_width_px"])
    height = float(gcps_payload["image_height_px"])
    if width <= 0 or height <= 0:
        raise ReviewInputError("image dimensions must be positive")
    all_review_points = [point.model_dump(mode="python") for point in checkpoints.points]
    for point in [*train, *all_review_points]:
        if not 0 <= float(point["pixel_x"]) <= width or not 0 <= float(point["pixel_y"]) <= height:
            raise ReviewInputError(f"point {point['id']} is outside the source image")

    checks: list[ReviewCheck] = []
    source_sha = _sha_checks(
        gcps_payload,
        qa_payload,
        checkpoints,
        provenance,
        legend,
        checks,
    )
    record_id = _record_checks(
        gcps_payload,
        qa_payload,
        checkpoints,
        provenance,
        legend,
        checks,
    )
    a1_operator, a2_reviewer = _operator_checks(gcps_payload, checkpoints, legend, checks)

    a1_ids = {str(point["id"]) for point in train}
    overlap = sorted(a1_ids & {point.id for point in checkpoints.points})
    _check(
        checks,
        "CHECKPOINT_ID_SEPARATION",
        CheckStatus.pass_ if not overlap else CheckStatus.fail,
        "A2 checkpoint IDs do not reuse A1 GCP IDs."
        if not overlap
        else "A2 checkpoint IDs overlap A1 GCP IDs.",
        observed=overlap,
        strict_requirement=[],
    )

    residuals, metrics = _residuals(checkpoints, calculation)
    a1_distribution = _distribution(train, width, height, independent=False)
    a2_distribution = _distribution(all_review_points, width, height, independent=True)
    _distribution_checks(a1_distribution, a2_distribution, checks)
    _metric_checks(metrics, checks)
    _provenance_checks(provenance, checks)
    _legend_checks(legend, checks)

    second_reviewer_required = bool(
        qa_payload.get("guardrails", {}).get("second_reviewer_required")
    )
    _check(
        checks,
        "A1_QA_GUARDRAIL",
        CheckStatus.pass_ if second_reviewer_required else CheckStatus.warning,
        "A1 artifact explicitly requires a second reviewer."
        if second_reviewer_required
        else "A1 QA artifact does not declare the second-reviewer guardrail.",
        observed=second_reviewer_required,
        strict_requirement=True,
    )

    decision = _decision(checks)
    reasons = [
        ReviewReason(code=check.code, severity=check.status, message=check.message)
        for check in checks
        if check.status != CheckStatus.pass_
    ]
    return ReviewResult(
        record_id=record_id,
        source_sha256=source_sha,
        decision=decision,
        reviewed_at_utc=datetime.now(UTC),
        a1_operator_id=a1_operator,
        a2_reviewer_id=a2_reviewer,
        transform_type=str(calculation["transform_type"]),
        transform_sha256=_canonical_sha256(calculation),
        metrics=metrics,
        a1_distribution=a1_distribution,
        checkpoint_distribution=a2_distribution,
        checkpoint_residuals=residuals,
        provenance_status=provenance.status,
        checks=checks,
        reasons=reasons,
        guardrails={
            "a1_inputs_mutated": False,
            "transformation_refitted": False,
            "automatic_status_promotion": False,
            "strict_requires_verified_official": True,
            "review_output_is_separate": True,
        },
    )
