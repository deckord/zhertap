from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image

from .basemap import ReferenceRaster
from .models import DiagnosticAnchorPoint, MatchMetrics, ProposedGCP

try:
    import cv2
except ImportError:  # pragma: no cover - exercised by CLI dependency check
    cv2 = None


@dataclass(frozen=True, slots=True)
class MatchThresholds:
    min_candidate_matches: int = 24
    min_inliers: int = 12
    min_inlier_ratio: float = 0.35
    max_rmse_px: float = 8.0
    min_plan_coverage: float = 0.08
    min_reference_coverage: float = 0.08
    max_homography_condition: float = 1_000_000.0
    max_gcps: int = 12
    max_dimension: int = 2400
    diagnostic_min_inlier_ratio: float = 0.20


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    confidence: float
    confidence_label: str
    metrics: MatchMetrics
    gcps: list[ProposedGCP]
    diagnostic_anchor_points: list[DiagnosticAnchorPoint]
    diagnostic_anchor_guardrails: dict[str, bool]
    diagnostic_anchor_summary: dict[str, object]
    reasons: list[str]
    visualization: Image.Image | None = None


def match_plan_to_reference(
    plan: Image.Image,
    reference: ReferenceRaster,
    thresholds: MatchThresholds | None = None,
) -> MatchOutcome:
    if cv2 is None:
        raise RuntimeError(
            "OpenCV is required. Install tools/genplan_autoreg/requirements.txt"
        )
    thresholds = thresholds or MatchThresholds()
    plan_gray, plan_scale = _prepare(plan, thresholds.max_dimension)
    ref_gray, ref_scale = _prepare(reference.image, thresholds.max_dimension)
    detector, detector_name, norm = _detector()
    plan_points, plan_desc = detector.detectAndCompute(plan_gray, None)
    ref_points, ref_desc = detector.detectAndCompute(ref_gray, None)
    base_metrics = MatchMetrics(
        detector=detector_name,
        plan_keypoints=len(plan_points),
        reference_keypoints=len(ref_points),
    )
    if plan_desc is None or ref_desc is None or len(plan_points) < 4 or len(ref_points) < 4:
        return MatchOutcome(
            confidence=0.0,
            confidence_label="none",
            metrics=base_metrics,
            gcps=[],
            diagnostic_anchor_points=[],
            diagnostic_anchor_guardrails=_anchor_guardrails(0),
            diagnostic_anchor_summary=_anchor_summary([]),
            reasons=["insufficient_keypoints"],
        )

    matcher = cv2.BFMatcher(norm)
    pairs = matcher.knnMatch(plan_desc, ref_desc, k=2)
    good = [first for first, second in pairs if first.distance < 0.72 * second.distance]
    if len(good) < 4:
        return MatchOutcome(
            confidence=0.0,
            confidence_label="none",
            metrics=MatchMetrics(
                detector=detector_name,
                plan_keypoints=len(plan_points),
                reference_keypoints=len(ref_points),
                candidate_matches=len(good),
            ),
            gcps=[],
            diagnostic_anchor_points=[],
            diagnostic_anchor_guardrails=_anchor_guardrails(0),
            diagnostic_anchor_summary=_anchor_summary([]),
            reasons=["insufficient_candidate_matches"],
        )

    plan_xy = np.float32([plan_points[m.queryIdx].pt for m in good])
    ref_xy = np.float32([ref_points[m.trainIdx].pt for m in good])
    homography, mask = cv2.findHomography(plan_xy, ref_xy, cv2.RANSAC, 5.0)
    if homography is None or mask is None:
        return MatchOutcome(
            confidence=0.0,
            confidence_label="none",
            metrics=MatchMetrics(
                detector=detector_name,
                plan_keypoints=len(plan_points),
                reference_keypoints=len(ref_points),
                candidate_matches=len(good),
            ),
            gcps=[],
            diagnostic_anchor_points=[],
            diagnostic_anchor_guardrails=_anchor_guardrails(0),
            diagnostic_anchor_summary=_anchor_summary([]),
            reasons=["homography_not_found"],
        )

    inlier_mask = mask.ravel().astype(bool)
    inlier_plan = plan_xy[inlier_mask]
    inlier_ref = ref_xy[inlier_mask]
    projected = cv2.perspectiveTransform(inlier_plan.reshape(-1, 1, 2), homography).reshape(-1, 2)
    residuals = np.linalg.norm(projected - inlier_ref, axis=1)
    rmse = float(math.sqrt(float(np.mean(residuals**2)))) if len(residuals) else None
    plan_coverage = _coverage(inlier_plan, plan_gray.shape[1], plan_gray.shape[0])
    ref_coverage = _coverage(inlier_ref, ref_gray.shape[1], ref_gray.shape[0])
    try:
        condition = float(np.linalg.cond(homography / homography[2, 2]))
    except (np.linalg.LinAlgError, ZeroDivisionError):
        condition = math.inf
    ratio = len(inlier_plan) / len(good)
    metrics = MatchMetrics(
        detector=detector_name,
        plan_keypoints=len(plan_points),
        reference_keypoints=len(ref_points),
        candidate_matches=len(good),
        inliers=len(inlier_plan),
        inlier_ratio=round(ratio, 6),
        reprojection_rmse_px=round(rmse, 4) if rmse is not None else None,
        plan_coverage=round(plan_coverage, 6),
        reference_coverage=round(ref_coverage, 6),
        homography_condition=round(condition, 4) if math.isfinite(condition) else None,
    )
    reasons = _failure_reasons(metrics, thresholds)
    confidence = _confidence(metrics, thresholds)
    if reasons:
        confidence = min(confidence, 0.19)
    label = "medium" if confidence >= 0.60 else "low" if confidence >= 0.20 else "none"
    gcps = []
    if not reasons:
        gcps = _proposed_gcps(
            inlier_plan,
            inlier_ref,
            residuals,
            plan_scale,
            ref_scale,
            reference,
            confidence,
            thresholds.max_gcps,
            thresholds.max_rmse_px,
        )
        if len(gcps) < 4:
            reasons.append("fewer_than_four_distinct_gcps")
            confidence = min(confidence, 0.19)
            label = "none"
            gcps = []
    diagnostic_anchor_points: list[DiagnosticAnchorPoint] = []
    if reasons and _diagnostic_anchor_allowed(metrics, thresholds):
        diagnostic_anchor_points = _diagnostic_anchor_points(
            inlier_plan,
            inlier_ref,
            residuals,
            plan_scale,
            ref_scale,
            reference,
            metrics,
            reasons,
            thresholds.max_gcps,
            thresholds.max_rmse_px,
        )
    visualization = _draw_matches(
        plan_gray,
        plan_points,
        ref_gray,
        ref_points,
        good,
        inlier_mask,
    )
    return MatchOutcome(
        confidence=round(min(confidence, 0.79), 4),
        confidence_label=label,
        metrics=metrics,
        gcps=gcps,
        diagnostic_anchor_points=diagnostic_anchor_points,
        diagnostic_anchor_guardrails=_anchor_guardrails(len(diagnostic_anchor_points)),
        diagnostic_anchor_summary=_anchor_summary(diagnostic_anchor_points),
        reasons=reasons or ["automatic_result_requires_independent_manual_review"],
        visualization=visualization,
    )


def _prepare(image: Image.Image, max_dimension: int) -> tuple[np.ndarray, float]:
    gray = np.array(image.convert("L"))
    scale = min(1.0, max_dimension / max(gray.shape))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA,
        )
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    return gray, scale


def _detector() -> tuple[object, str, int]:
    if hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=12_000, contrastThreshold=0.025), "SIFT", cv2.NORM_L2
    return cv2.ORB_create(nfeatures=15_000, fastThreshold=8), "ORB", cv2.NORM_HAMMING


def _coverage(points: np.ndarray, width: int, height: int) -> float:
    if len(points) < 3 or width <= 0 or height <= 0:
        return 0.0
    hull = cv2.convexHull(points.astype(np.float32))
    return min(1.0, float(cv2.contourArea(hull)) / float(width * height))


def _failure_reasons(
    metrics: MatchMetrics,
    thresholds: MatchThresholds,
) -> list[str]:
    checks = [
        (
            metrics.candidate_matches < thresholds.min_candidate_matches,
            "candidate_matches_below_threshold",
        ),
        (metrics.inliers < thresholds.min_inliers, "inliers_below_threshold"),
        (metrics.inlier_ratio < thresholds.min_inlier_ratio, "inlier_ratio_below_threshold"),
        (
            metrics.reprojection_rmse_px is None
            or metrics.reprojection_rmse_px > thresholds.max_rmse_px,
            "reprojection_error_above_threshold",
        ),
        (metrics.plan_coverage < thresholds.min_plan_coverage, "plan_coverage_too_small"),
        (
            metrics.reference_coverage < thresholds.min_reference_coverage,
            "reference_coverage_too_small",
        ),
        (
            metrics.homography_condition is None
            or metrics.homography_condition > thresholds.max_homography_condition,
            "homography_is_ill_conditioned",
        ),
    ]
    return [reason for failed, reason in checks if failed]


def _diagnostic_anchor_allowed(
    metrics: MatchMetrics,
    thresholds: MatchThresholds,
) -> bool:
    if metrics.reprojection_rmse_px is None:
        return False
    return all(
        [
            metrics.candidate_matches >= thresholds.min_candidate_matches,
            metrics.inliers >= thresholds.min_inliers,
            metrics.inlier_ratio >= thresholds.diagnostic_min_inlier_ratio,
            metrics.reprojection_rmse_px <= thresholds.max_rmse_px,
            metrics.plan_coverage >= thresholds.min_plan_coverage,
            metrics.reference_coverage >= thresholds.min_reference_coverage,
        ]
    )


def _confidence(metrics: MatchMetrics, thresholds: MatchThresholds) -> float:
    if metrics.reprojection_rmse_px is None:
        return 0.0
    match_score = min(1.0, metrics.inliers / max(1, thresholds.min_inliers * 2))
    ratio_score = min(1.0, metrics.inlier_ratio / 0.7)
    error_score = max(0.0, 1.0 - metrics.reprojection_rmse_px / 12.0)
    coverage_score = min(
        1.0,
        math.sqrt(max(0.0, metrics.plan_coverage * metrics.reference_coverage)) / 0.35,
    )
    return 0.30 * match_score + 0.25 * ratio_score + 0.25 * error_score + 0.20 * coverage_score


def _proposed_gcps(
    plan_points: np.ndarray,
    reference_points: np.ndarray,
    residuals: np.ndarray,
    plan_scale: float,
    reference_scale: float,
    reference: ReferenceRaster,
    overall_confidence: float,
    limit: int,
    max_residual_px: float,
) -> list[ProposedGCP]:
    valid = np.flatnonzero(residuals <= max_residual_px)
    if len(valid) < 4:
        return []
    selected: list[int] = [int(valid[np.argmin(residuals[valid])])]
    while len(selected) < min(limit, len(valid)):
        remaining = [int(idx) for idx in valid if int(idx) not in selected]
        if not remaining:
            break
        candidates = [
            idx
            for idx in remaining
            if min(
                float(np.linalg.norm(reference_points[idx] - reference_points[chosen]))
                for chosen in selected
            )
            >= 20.0
        ]
        if not candidates:
            break
        next_idx = max(
            candidates,
            key=lambda idx: min(
                float(np.linalg.norm(plan_points[idx] - plan_points[chosen]))
                for chosen in selected
            ),
        )
        selected.append(next_idx)
    gcps: list[ProposedGCP] = []
    for idx in selected:
        plan_x, plan_y = plan_points[idx] / plan_scale
        ref_x, ref_y = reference_points[idx] / reference_scale
        lon, lat = reference.pixel_to_lonlat(float(ref_x), float(ref_y))
        local_confidence = overall_confidence * math.exp(-float(residuals[idx]) / 8.0)
        gcps.append(
            ProposedGCP(
                plan_x=round(float(plan_x), 3),
                plan_y=round(float(plan_y), 3),
                longitude=round(lon, 8),
                latitude=round(lat, 8),
                reference_x=round(float(ref_x), 3),
                reference_y=round(float(ref_y), 3),
                residual_px=round(float(residuals[idx]), 4),
                confidence=round(min(local_confidence, 0.79), 4),
            )
        )
    return gcps


def _diagnostic_anchor_points(
    plan_points: np.ndarray,
    reference_points: np.ndarray,
    residuals: np.ndarray,
    plan_scale: float,
    reference_scale: float,
    reference: ReferenceRaster,
    metrics: MatchMetrics,
    warnings: list[str],
    limit: int,
    max_residual_px: float,
) -> list[DiagnosticAnchorPoint]:
    valid = np.flatnonzero(residuals <= max_residual_px)
    selected = _spatially_distributed_indices(
        plan_points,
        reference_points,
        residuals,
        valid,
        limit,
    )
    anchors: list[DiagnosticAnchorPoint] = []
    for rank, idx in enumerate(selected, start=1):
        plan_x, plan_y = plan_points[idx] / plan_scale
        ref_x, ref_y = reference_points[idx] / reference_scale
        lon, lat = reference.pixel_to_lonlat(float(ref_x), float(ref_y))
        residual = float(residuals[idx])
        diagnostic_score = _anchor_score(metrics, residual, max_residual_px)
        anchors.append(
            DiagnosticAnchorPoint(
                id=f"diag-anchor-{rank:03d}",
                rank=rank,
                scope="operator_diagnostic_only",
                plan_pixel={
                    "x": round(float(plan_x), 3),
                    "y": round(float(plan_y), 3),
                },
                reference_pixel={
                    "x": round(float(ref_x), 3),
                    "y": round(float(ref_y), 3),
                },
                reference_lonlat={
                    "longitude": round(lon, 8),
                    "latitude": round(lat, 8),
                },
                residual_px=round(residual, 4),
                diagnostic_score=round(diagnostic_score, 4),
                source="ransac_inlier",
                warnings=warnings,
            )
        )
    return anchors


def _spatially_distributed_indices(
    plan_points: np.ndarray,
    reference_points: np.ndarray,
    residuals: np.ndarray,
    valid: np.ndarray,
    limit: int,
) -> list[int]:
    if len(valid) < 4:
        return []
    selected: list[int] = [int(valid[np.argmin(residuals[valid])])]
    while len(selected) < min(limit, len(valid)):
        remaining = [int(idx) for idx in valid if int(idx) not in selected]
        if not remaining:
            break
        candidates = [
            idx
            for idx in remaining
            if min(
                float(np.linalg.norm(reference_points[idx] - reference_points[chosen]))
                for chosen in selected
            )
            >= 20.0
        ]
        if not candidates:
            break
        selected.append(
            max(
                candidates,
                key=lambda idx: min(
                    float(np.linalg.norm(plan_points[idx] - plan_points[chosen]))
                    for chosen in selected
                )
                - float(residuals[idx]),
            )
        )
    return selected if len(selected) >= 4 else []


def _anchor_score(
    metrics: MatchMetrics,
    residual_px: float,
    max_residual_px: float,
) -> float:
    ratio_score = min(1.0, metrics.inlier_ratio / 0.7)
    residual_score = max(0.0, 1.0 - residual_px / max(1.0, max_residual_px))
    coverage_score = min(
        1.0,
        math.sqrt(max(0.0, metrics.plan_coverage * metrics.reference_coverage)) / 0.35,
    )
    return 0.45 * residual_score + 0.35 * ratio_score + 0.20 * coverage_score


def _anchor_guardrails(count: int) -> dict[str, bool]:
    return {
        "has_diagnostic_anchors": count > 0,
        "import_eligible": False,
        "customer_search_eligible": False,
        "auto_apply_allowed": False,
        "requires_manual_a1_pick": True,
        "requires_a2_review": True,
    }


def _anchor_summary(anchors: Sequence[DiagnosticAnchorPoint]) -> dict[str, object]:
    return {
        "count": len(anchors),
        "quality_label": "weak_hint" if anchors else "none",
        "selection_method": (
            "low_residual_spatially_distributed_ransac_inliers"
            if anchors
            else ""
        ),
    }


def _draw_matches(
    plan_gray: np.ndarray,
    plan_points: list,
    reference_gray: np.ndarray,
    reference_points: list,
    matches: list,
    inlier_mask: np.ndarray,
) -> Image.Image:
    selected = [match for match, keep in zip(matches, inlier_mask, strict=True) if keep][:80]
    canvas = cv2.drawMatches(
        plan_gray,
        plan_points,
        reference_gray,
        reference_points,
        selected,
        None,
        flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS,
    )
    return Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
