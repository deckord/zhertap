from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .math import TransformError, calculate_transform
from .models import WorkbenchSave

SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
SUPPORTED_TIFF_SUFFIXES = {".tif", ".tiff"}
SUPPORTED_SOURCE_SUFFIXES = SUPPORTED_IMAGE_SUFFIXES | SUPPORTED_TIFF_SUFFIXES | {".pdf"}


class WorkbenchError(ValueError):
    pass


def safe_path(root: Path, candidate: str | Path, *, must_exist: bool = False) -> Path:
    root = root.resolve()
    requested = Path(candidate)
    if not requested.is_absolute():
        requested = root / requested
    resolved = requested.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkbenchError("Path is outside the configured data root") from exc
    return resolved


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


class ManifestStore:
    def __init__(self, root: Path, manifest_path: Path, output_path: Path | None = None):
        self.root = root.resolve(strict=True)
        self.manifest_path = safe_path(self.root, manifest_path, must_exist=True)
        default_output = self.root / "workbench_data"
        self.output_path = safe_path(self.root, output_path or default_output)
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.render_path = safe_path(self.root, self.output_path / "rendered")
        self.records_path = safe_path(self.root, self.output_path / "records")
        self.render_path.mkdir(parents=True, exist_ok=True)
        self.records_path.mkdir(parents=True, exist_ok=True)
        self._records = self._load_manifest()

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(f"Cannot read manifest: {exc}") from exc
        records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise WorkbenchError("Manifest must be a list or contain a records list")
        indexed: dict[str, dict[str, Any]] = {}
        for raw in records:
            if not isinstance(raw, dict):
                continue
            source = raw.get("extracted_path") or raw.get("source_path")
            if not source:
                continue
            identity_source = str(
                raw.get("asset_id")
                or raw.get("record_id")
                or raw.get("document_id")
                or source
            )
            record_id = (
                identity_source
                if len(identity_source) <= 128
                and all(char.isalnum() or char in "-_." for char in identity_source)
                else hashlib.sha256(identity_source.encode("utf-8")).hexdigest()
            )
            if record_id in indexed:
                raise WorkbenchError(f"Duplicate manifest record ID: {record_id}")
            record = dict(raw)
            record["_record_id"] = record_id
            record["_source_path"] = str(safe_path(self.root, source, must_exist=True))
            suffix = Path(record["_source_path"]).suffix.lower()
            if suffix not in SUPPORTED_SOURCE_SUFFIXES:
                continue
            indexed[record_id] = record
        return indexed

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record_id, record in sorted(
            self._records.items(),
            key=lambda item: (
                str(item[1].get("normalized_region", "")).casefold(),
                str(item[1].get("normalized_locality", "")).casefold(),
                str(item[1].get("original_filename", "")).casefold(),
            ),
        ):
            saved = self.load_gcps(record_id)
            autoreg = _autoreg_summary(record)
            records.append(
                {
                    "record_id": record_id,
                    "filename": record.get("original_filename")
                    or Path(record["_source_path"]).name,
                    "format": Path(record["_source_path"]).suffix.lower().lstrip("."),
                    "region": record.get("egkn_region")
                    or record.get("normalized_region")
                    or record.get("canonical_region_name")
                    or "",
                    "district": record.get("normalized_district")
                    or record.get("canonical_district_name")
                    or "",
                    "locality": record.get("normalized_locality")
                    or record.get("canonical_locality_name")
                    or "",
                    "page_count": record.get("page_count") or 1,
                    "workflow_status": saved.get("workflow_status", "proposed"),
                    "source_workflow_status": record.get("workflow_status") or "",
                    "queue_status": record.get("queue_status")
                    or record.get("source_workflow_status")
                    or record.get("workflow_status")
                    or "",
                    "queue_next_action": record.get("queue_next_action") or "",
                    "bbox_status": record.get("bbox_status") or "",
                    "bbox_source": record.get("bbox_source") or "",
                    "bbox_label": record.get("bbox_label") or "",
                    "bbox_reason": record.get("bbox_reason") or "",
                    "has_saved_gcps": bool(saved.get("points")),
                    "saved_point_count": len(saved.get("points") or []),
                    **autoreg,
                }
            )
        return records

    def get_record(self, record_id: str) -> dict[str, Any]:
        try:
            return self._records[record_id]
        except KeyError as exc:
            raise WorkbenchError("Manifest record not found") from exc

    def source_path(self, record_id: str) -> Path:
        record = self.get_record(record_id)
        return safe_path(self.root, record["_source_path"], must_exist=True)

    def record_output_path(self, record_id: str) -> Path:
        digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
        return safe_path(self.root, self.records_path / digest)

    def load_gcps(self, record_id: str) -> dict[str, Any]:
        self.get_record(record_id)
        path = safe_path(self.root, self.record_output_path(record_id) / "gcps.json")
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(f"Cannot read saved GCP: {exc}") from exc

    def save(self, record_id: str, request: WorkbenchSave) -> dict[str, Any]:
        record = self.get_record(record_id)
        for point in request.points:
            if point.pixel_x > request.image_width_px or point.pixel_y > request.image_height_px:
                raise WorkbenchError(f"Point {point.id} is outside the source image")
        try:
            calculation = calculate_transform(
                request.points,
                request.transform_type,
                request.image_width_px,
                request.image_height_px,
            )
        except TransformError as exc:
            raise WorkbenchError(str(exc)) from exc
        timestamp = datetime.now(UTC).isoformat()
        output = self.record_output_path(record_id)
        source = self.source_path(record_id)
        source_sha = record.get("sha256") or _sha256(source)
        gcps_payload = {
            "schema_version": "1.0",
            "record_id": record_id,
            "asset_id": record.get("asset_id") or "",
            "source_path": str(source.relative_to(self.root)),
            "source_sha256": source_sha,
            "page": request.page,
            "image_width_px": request.image_width_px,
            "image_height_px": request.image_height_px,
            "transform_type": request.transform_type.value,
            "workflow_status": request.workflow_status.value,
            "operator": request.operator,
            "notes": request.notes,
            "saved_at_utc": timestamp,
            "points": [point.model_dump(mode="json") for point in request.points],
        }
        qa_payload = {
            "schema_version": "1.0",
            "record_id": record_id,
            "source_sha256": source_sha,
            "page": request.page,
            "workflow_status": request.workflow_status.value,
            "qa_decision": "pending",
            "generated_at_utc": timestamp,
            "calculation": calculation,
            "guardrails": {
                "approved_by_workbench": False,
                "allowed_workflow_statuses": ["proposed", "qa_pending"],
                "second_reviewer_required": True,
            },
        }
        _atomic_json(safe_path(self.root, output / "gcps.json"), gcps_payload)
        _atomic_json(safe_path(self.root, output / "qa.json"), qa_payload)
        return {"gcps": gcps_payload, "qa": qa_payload}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _autoreg_summary(record: dict[str, Any]) -> dict[str, Any]:
    diagnostics = record.get("autoreg_diagnostics")
    if not isinstance(diagnostics, dict):
        return {
            "autoreg_has_attempts": False,
            "autoreg_best_basemap": "",
            "autoreg_confidence": 0,
            "autoreg_inliers": 0,
            "autoreg_rmse_px": 0,
            "autoreg_diagnostic_anchor_count": 0,
            "autoreg_diagnostic_anchor_quality": "",
            "autoreg_operator_score": 0,
            "autoreg_has_pipeline_error": False,
        }
    attempts = diagnostics.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    best = diagnostics.get("best_attempt")
    if not isinstance(best, dict):
        best = max(
            (attempt for attempt in attempts if isinstance(attempt, dict)),
            key=_autoreg_score,
            default={},
        )
    metrics = best.get("metrics") if isinstance(best, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}
    reasons = [
        str(reason)
        for attempt in attempts
        if isinstance(attempt, dict)
        for reason in attempt.get("reasons", [])
    ]
    return {
        "autoreg_has_attempts": bool(attempts),
        "autoreg_best_basemap": str(best.get("basemap") or ""),
        "autoreg_confidence": _number(best.get("confidence")),
        "autoreg_inliers": _number(metrics.get("inliers")),
        "autoreg_rmse_px": _number(metrics.get("reprojection_rmse_px")),
        "autoreg_diagnostic_anchor_count": _number(best.get("diagnostic_anchor_count")),
        "autoreg_diagnostic_anchor_quality": str(
            best.get("diagnostic_anchor_quality") or ""
        ),
        "autoreg_operator_score": round(_autoreg_score(best), 6) if best else 0,
        "autoreg_has_pipeline_error": any(
            reason.startswith("pipeline_error:") for reason in reasons
        ),
    }


def _autoreg_score(attempt: dict[str, Any]) -> float:
    metrics = attempt.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    proposed = _number(attempt.get("proposed_gcp_count"))
    anchors = _number(attempt.get("diagnostic_anchor_count"))
    confidence = _number(attempt.get("confidence"))
    inliers = _number(metrics.get("inliers"))
    inlier_ratio = _number(metrics.get("inlier_ratio"))
    rmse = _number(metrics.get("reprojection_rmse_px"))
    penalty = min(rmse / 1000.0, 20.0) if rmse > 0 else 0.0
    return (
        proposed * 100.0
        + anchors * 20.0
        + confidence * 20.0
        + inliers
        + inlier_ratio * 10.0
        - penalty
    )


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
