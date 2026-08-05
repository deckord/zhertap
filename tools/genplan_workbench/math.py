from __future__ import annotations

import math
from typing import Any

import numpy as np

from .models import GCP, TransformType

EARTH_RADIUS_M = 6_378_137.0


class TransformError(ValueError):
    pass


def _local_xy(points: list[GCP]) -> tuple[np.ndarray, float, float]:
    lon0 = float(np.mean([point.lon for point in points]))
    lat0 = float(np.mean([point.lat for point in points]))
    cos_lat = math.cos(math.radians(lat0))
    if abs(cos_lat) < 1e-9:
        raise TransformError("Control points are too close to a pole")
    xy = np.array(
        [
            [
                EARTH_RADIUS_M * math.radians(point.lon - lon0) * cos_lat,
                EARTH_RADIUS_M * math.radians(point.lat - lat0),
            ]
            for point in points
        ],
        dtype=float,
    )
    return xy, lon0, lat0


def _fit_affine(pixels: np.ndarray, targets: np.ndarray) -> tuple[np.ndarray, float]:
    design = np.column_stack([pixels, np.ones(len(pixels))])
    coefficients, _, rank, singular_values = np.linalg.lstsq(design, targets, rcond=None)
    if rank < 3:
        raise TransformError("Affine control points are collinear or duplicated")
    condition = float(singular_values[0] / singular_values[-1])
    return coefficients.T, condition


def _fit_projective(pixels: np.ndarray, targets: np.ndarray) -> tuple[np.ndarray, float]:
    rows: list[list[float]] = []
    values: list[float] = []
    for (pixel_x, pixel_y), (target_x, target_y) in zip(
        pixels, targets, strict=True
    ):
        rows.append(
            [
                pixel_x,
                pixel_y,
                1.0,
                0.0,
                0.0,
                0.0,
                -target_x * pixel_x,
                -target_x * pixel_y,
            ]
        )
        values.append(target_x)
        rows.append(
            [
                0.0,
                0.0,
                0.0,
                pixel_x,
                pixel_y,
                1.0,
                -target_y * pixel_x,
                -target_y * pixel_y,
            ]
        )
        values.append(target_y)
    design = np.asarray(rows, dtype=float)
    target = np.asarray(values, dtype=float)
    coefficients, _, rank, singular_values = np.linalg.lstsq(design, target, rcond=None)
    if rank < 8:
        raise TransformError("Projective control points are degenerate or duplicated")
    matrix = np.append(coefficients, 1.0).reshape(3, 3)
    condition = float(singular_values[0] / singular_values[-1])
    return matrix, condition


def _predict(
    transform_type: TransformType, matrix: np.ndarray, pixels: np.ndarray
) -> np.ndarray:
    homogeneous = np.column_stack([pixels, np.ones(len(pixels))])
    if transform_type == TransformType.affine:
        return homogeneous @ matrix.T
    transformed = homogeneous @ matrix.T
    divisor = transformed[:, 2]
    if np.any(np.abs(divisor) < 1e-12):
        raise TransformError("Projective transform approaches infinity")
    return transformed[:, :2] / divisor[:, None]


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    unique = sorted(set(points))
    if len(unique) <= 1:
        return unique

    def cross(
        origin: tuple[float, float],
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> float:
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (
            first[1] - origin[1]
        ) * (second[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _polygon_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(
        sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(points, points[1:] + points[:1], strict=True)
        )
        / 2
    )


def distribution_metrics(
    points: list[GCP], image_width_px: int, image_height_px: int
) -> dict[str, Any]:
    train = [point for point in points if point.role.value == "train"]
    if not train:
        return {
            "status": "poor",
            "coverage_ratio": 0.0,
            "x_span_ratio": 0.0,
            "y_span_ratio": 0.0,
            "quadrants_covered": 0,
            "edge_point_count": 0,
            "issues": ["NO_TRAIN_GCP"],
        }
    normalized = [
        (point.pixel_x / image_width_px, point.pixel_y / image_height_px)
        for point in train
    ]
    x_values = [point[0] for point in normalized]
    y_values = [point[1] for point in normalized]
    coverage = _polygon_area(_convex_hull(normalized))
    x_span = max(x_values) - min(x_values)
    y_span = max(y_values) - min(y_values)
    quadrants = {
        (0 if x < 0.5 else 1, 0 if y < 0.5 else 1) for x, y in normalized
    }
    edge_count = sum(x <= 0.15 or x >= 0.85 or y <= 0.15 or y >= 0.85 for x, y in normalized)
    issues: list[str] = []
    if len(train) < 6:
        issues.append("INSUFFICIENT_DISTRIBUTED_GCP")
    if coverage < 0.35:
        issues.append("LOW_IMAGE_COVERAGE")
    if len(quadrants) < 3:
        issues.append("QUADRANTS_NOT_COVERED")
    if x_span < 0.65 or y_span < 0.65:
        issues.append("NARROW_POINT_SPAN")
    if edge_count < 3:
        issues.append("INSUFFICIENT_EDGE_GCP")

    if not issues:
        status = "good"
    elif (
        len(train) >= 4
        and coverage >= 0.15
        and len(quadrants) >= 2
        and x_span >= 0.4
        and y_span >= 0.4
    ):
        status = "warning"
    else:
        status = "poor"
    return {
        "status": status,
        "coverage_ratio": round(coverage, 6),
        "x_span_ratio": round(x_span, 6),
        "y_span_ratio": round(y_span, 6),
        "quadrants_covered": len(quadrants),
        "edge_point_count": edge_count,
        "issues": issues,
    }


def calculate_transform(
    points: list[GCP],
    transform_type: TransformType,
    image_width_px: int,
    image_height_px: int,
) -> dict[str, Any]:
    train = [point for point in points if point.role.value == "train"]
    minimum = 3 if transform_type == TransformType.affine else 4
    if len(train) < minimum:
        raise TransformError(
            f"{transform_type.value} transform requires at least {minimum} train points"
        )
    targets, lon0, lat0 = _local_xy(points)
    all_pixels = np.array([[point.pixel_x, point.pixel_y] for point in points], dtype=float)
    train_indexes = [index for index, point in enumerate(points) if point.role.value == "train"]
    train_pixels = all_pixels[train_indexes]
    train_targets = targets[train_indexes]
    if transform_type == TransformType.affine:
        matrix, condition = _fit_affine(train_pixels, train_targets)
    else:
        matrix, condition = _fit_projective(train_pixels, train_targets)
    predictions = _predict(transform_type, matrix, all_pixels)
    residual_vectors = predictions - targets
    residuals = np.linalg.norm(residual_vectors, axis=1)

    point_results = []
    for point, vector, residual in zip(
        points, residual_vectors, residuals, strict=True
    ):
        point_results.append(
            {
                "id": point.id,
                "role": point.role.value,
                "dx_m": round(float(vector[0]), 4),
                "dy_m": round(float(vector[1]), 4),
                "residual_m": round(float(residual), 4),
            }
        )

    train_residuals = np.array(
        [
            residual
            for point, residual in zip(points, residuals, strict=True)
            if point.role.value == "train"
        ]
    )
    checkpoint_residuals = np.array(
        [
            residual
            for point, residual in zip(points, residuals, strict=True)
            if point.role.value == "checkpoint"
        ]
    )

    def rmse(values: np.ndarray) -> float | None:
        return round(float(np.sqrt(np.mean(np.square(values)))), 4) if len(values) else None

    distribution = distribution_metrics(points, image_width_px, image_height_px)
    issues = list(distribution["issues"])
    if not len(checkpoint_residuals):
        issues.append("NO_INDEPENDENT_CHECKPOINTS")
    if condition > 1e12:
        issues.append("ILL_CONDITIONED_TRANSFORM")
    independent_rmse = rmse(checkpoint_residuals)
    train_rmse = rmse(train_residuals)
    evaluated_rmse = independent_rmse if independent_rmse is not None else train_rmse
    max_residual = round(float(np.max(residuals)), 4)
    if (
        independent_rmse is not None
        and independent_rmse <= 5
        and max_residual <= 10
        and distribution["status"] == "good"
    ):
        suggested_class = "high"
    elif (
        independent_rmse is not None
        and independent_rmse <= 15
        and max_residual <= 20
        and distribution["status"] != "poor"
    ):
        suggested_class = "advisory"
    else:
        suggested_class = "incomplete_or_reject"
    return {
        "transform_type": transform_type.value,
        "pixel_to_local_m_matrix": [
            [round(float(value), 12) for value in row] for row in matrix
        ],
        "local_origin_wgs84": {"lon": lon0, "lat": lat0},
        "condition_number": condition,
        "train_count": len(train_residuals),
        "checkpoint_count": len(checkpoint_residuals),
        "train_rmse_m": train_rmse,
        "checkpoint_rmse_m": independent_rmse,
        "evaluated_rmse_m": evaluated_rmse,
        "p95_residual_m": round(float(np.percentile(residuals, 95)), 4),
        "max_residual_m": max_residual,
        "point_residuals": point_results,
        "distribution": distribution,
        "issue_codes": sorted(set(issues)),
        "suggested_accuracy_class": suggested_class,
        "approval": {
            "automatic_approval": False,
            "status": "qa_pending",
            "reason": "A second reviewer must verify the source, GCP and residuals.",
        },
    }

