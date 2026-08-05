from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SUPPORTED_RASTER_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
ALLOWED_REVIEW_STATUSES = {"STRICT", "WARNING"}
BLOCKED_REVIEW_STATUSES = {
    "PROPOSED",
    "QA_PENDING",
    "PENDING",
    "REJECT",
    "REJECTED",
}


class ValidationError(ValueError):
    """Raised when an export input fails a safety check."""


@dataclass(frozen=True)
class ValidatedInput:
    source_path: Path
    gcps_path: Path
    qa_path: Path
    review_path: Path
    gcps: dict[str, Any]
    qa: dict[str, Any]
    review: dict[str, Any]
    source_sha256: str
    gcps_sha256: str
    qa_sha256: str
    review_sha256: str
    record_id: str
    review_status: str
    publish_mode: str
    train_points: tuple[dict[str, Any], ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_inputs(
    source_path: Path,
    gcps_path: Path,
    qa_path: Path,
    review_path: Path,
) -> ValidatedInput:
    source = _existing_file(source_path, "source raster")
    gcps_file = _existing_file(gcps_path, "gcps.json")
    qa_file = _existing_file(qa_path, "qa.json")
    review_file = _existing_file(review_path, "independent review JSON")

    if source.suffix.lower() not in SUPPORTED_RASTER_SUFFIXES:
        allowed = ", ".join(sorted(SUPPORTED_RASTER_SUFFIXES))
        raise ValidationError(
            f"Unsupported source format {source.suffix!r}; use a rendered raster: {allowed}"
        )

    gcps = _load_json_object(gcps_file, "gcps.json")
    qa = _load_json_object(qa_file, "qa.json")
    review = _load_json_object(review_file, "review JSON")

    source_sha = sha256_file(source)
    gcps_sha = sha256_file(gcps_file)
    qa_sha = sha256_file(qa_file)
    review_sha = sha256_file(review_file)

    record_id = _required_text(gcps, "record_id", "gcps.json")
    _require_equal(
        _required_text(qa, "record_id", "qa.json"),
        record_id,
        "qa.json record_id",
    )
    _require_equal(
        _required_text(review, "record_id", "review JSON"),
        record_id,
        "review JSON record_id",
    )

    _require_hash(gcps, "source_sha256", source_sha, "gcps.json")
    _require_hash(qa, "source_sha256", source_sha, "qa.json")
    _require_hash(review, "source_sha256", source_sha, "review JSON")
    _require_hash(review, "gcps_sha256", gcps_sha, "review JSON")
    _require_hash(review, "qa_sha256", qa_sha, "review JSON")

    workflow_status = _required_text(gcps, "workflow_status", "gcps.json").lower()
    if workflow_status != "qa_pending":
        raise ValidationError(
            "gcps.json must be submitted as qa_pending before independent review; "
            f"got {workflow_status!r}"
        )
    qa_workflow_status = _required_text(qa, "workflow_status", "qa.json").lower()
    if qa_workflow_status != "qa_pending":
        raise ValidationError(
            "qa.json must describe the same qa_pending submission; "
            f"got {qa_workflow_status!r}"
        )
    qa_decision = _required_text(qa, "qa_decision", "qa.json").lower()
    if qa_decision != "pending":
        raise ValidationError(
            "qa.json from the workbench must remain pending; acceptance belongs only "
            "in the independent review JSON"
        )

    review_status = _review_status(review)
    if review_status in BLOCKED_REVIEW_STATUSES:
        raise ValidationError(
            f"Independent review status {review_status!r} does not permit export"
        )
    if review_status not in ALLOWED_REVIEW_STATUSES:
        raise ValidationError(
            "Independent review status must be STRICT or WARNING; "
            f"got {review_status!r}"
        )
    allow_shadow = review.get("allow_shadow")
    if review_status == "WARNING" and allow_shadow is not True:
        raise ValidationError(
            "WARNING export requires the explicit boolean flag allow_shadow=true"
        )
    if review_status == "STRICT" and allow_shadow not in (None, False):
        raise ValidationError("STRICT review must not set allow_shadow=true")

    if review.get("independent_review") is not True:
        raise ValidationError("review JSON must contain independent_review=true")
    reviewer = _required_text(review, "reviewer", "review JSON")
    operator = _required_text(gcps, "operator", "gcps.json")
    if reviewer.casefold() == operator.casefold():
        raise ValidationError("Independent reviewer must differ from the GCP operator")
    _validate_review_timestamp(_required_text(review, "reviewed_at_utc", "review JSON"))

    width = _required_positive_int(gcps, "image_width_px", "gcps.json")
    height = _required_positive_int(gcps, "image_height_px", "gcps.json")
    transform_type = _required_text(gcps, "transform_type", "gcps.json").lower()
    if transform_type not in {"affine", "projective"}:
        raise ValidationError(f"Unsupported transform_type {transform_type!r}")
    if transform_type == "projective":
        raise ValidationError(
            "Projective workbench transforms are not exported yet: Rasterio's "
            "GCP transformer is not the reviewed projective homography"
        )

    calculation = qa.get("calculation")
    if not isinstance(calculation, dict):
        raise ValidationError("qa.json calculation must be an object")
    _require_equal(
        str(calculation.get("transform_type", "")).lower(),
        transform_type,
        "qa.json transform_type",
    )

    points = gcps.get("points")
    if not isinstance(points, list):
        raise ValidationError("gcps.json points must be an array")
    validated_points = _validate_points(points, width, height)
    train_points = tuple(point for point in validated_points if point["role"] == "train")
    minimum = 3 if transform_type == "affine" else 4
    if len(train_points) < minimum:
        raise ValidationError(
            f"{transform_type} export requires at least {minimum} original train GCPs"
        )
    if calculation.get("train_count") != len(train_points):
        raise ValidationError("qa.json train_count does not match gcps.json")
    checkpoint_count = sum(point["role"] == "checkpoint" for point in validated_points)
    if calculation.get("checkpoint_count") != checkpoint_count:
        raise ValidationError("qa.json checkpoint_count does not match gcps.json")

    return ValidatedInput(
        source_path=source,
        gcps_path=gcps_file,
        qa_path=qa_file,
        review_path=review_file,
        gcps=gcps,
        qa=qa,
        review=review,
        source_sha256=source_sha,
        gcps_sha256=gcps_sha,
        qa_sha256=qa_sha,
        review_sha256=review_sha,
        record_id=record_id,
        review_status=review_status,
        publish_mode="auto_filter" if review_status == "STRICT" else "shadow",
        train_points=train_points,
    )


def _existing_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValidationError(f"{label} not found: {resolved}")
    return resolved


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"{label} must contain a JSON object")
    return payload


def _required_text(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must contain a non-empty {key!r}")
    return value.strip()


def _required_positive_int(payload: dict[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{label} must contain a positive integer {key!r}")
    return value


def _require_equal(actual: str, expected: str, label: str) -> None:
    if actual != expected:
        raise ValidationError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _require_hash(
    payload: dict[str, Any],
    key: str,
    expected: str,
    label: str,
) -> None:
    actual = _required_text(payload, key, label).lower()
    if len(actual) != 64 or any(char not in "0123456789abcdef" for char in actual):
        raise ValidationError(f"{label} {key} is not a valid SHA-256")
    if actual != expected:
        raise ValidationError(f"{label} {key} does not match the current input file")


def _review_status(review: dict[str, Any]) -> str:
    status = review.get("status")
    overall_status = review.get("overall_status")
    values = [
        value.strip().upper()
        for value in (status, overall_status)
        if isinstance(value, str) and value.strip()
    ]
    if not values:
        raise ValidationError("review JSON must contain status")
    if len(set(values)) != 1:
        raise ValidationError("review JSON status and overall_status conflict")
    return values[0]


def _validate_review_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("reviewed_at_utc must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("reviewed_at_utc must include a timezone")


def _validate_points(
    raw_points: list[Any],
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    point_ids: set[str] = set()
    for index, raw in enumerate(raw_points):
        if not isinstance(raw, dict):
            raise ValidationError(f"GCP at index {index} must be an object")
        point_id = _required_text(raw, "id", f"GCP at index {index}")
        if point_id in point_ids:
            raise ValidationError(f"Duplicate GCP id {point_id!r}")
        point_ids.add(point_id)
        pixel_x = _finite_number(raw.get("pixel_x"), f"GCP {point_id} pixel_x")
        pixel_y = _finite_number(raw.get("pixel_y"), f"GCP {point_id} pixel_y")
        lon = _finite_number(raw.get("lon"), f"GCP {point_id} lon")
        lat = _finite_number(raw.get("lat"), f"GCP {point_id} lat")
        role = str(raw.get("role", "")).lower()
        if role not in {"train", "checkpoint"}:
            raise ValidationError(f"GCP {point_id} has invalid role {role!r}")
        if not 0 <= pixel_x <= width or not 0 <= pixel_y <= height:
            raise ValidationError(f"GCP {point_id} lies outside the source raster")
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValidationError(f"GCP {point_id} has invalid WGS84 coordinates")
        result.append(
            {
                "id": point_id,
                "pixel_x": pixel_x,
                "pixel_y": pixel_y,
                "lon": lon,
                "lat": lat,
                "role": role,
                "label": str(raw.get("label", "")),
            }
        )
    return result


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be finite")
    return result
