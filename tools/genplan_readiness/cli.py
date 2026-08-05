import argparse
import csv
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit manual genplan readiness from scanned source to vectors."
    )
    parser.add_argument("--workbench-manifest", type=Path, required=True)
    parser.add_argument("--workbench-output", type=Path, required=True)
    parser.add_argument("--workbench-url", default="")
    parser.add_argument("--vector-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_readiness_report(
        workbench_manifest=args.workbench_manifest,
        workbench_output=args.workbench_output,
        workbench_url=args.workbench_url,
        vector_root=args.vector_root,
        output=args.output,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


def build_readiness_report(
    *,
    workbench_manifest: Path,
    workbench_output: Path,
    output: Path,
    workbench_url: str = "",
    vector_root: Path | None = None,
) -> dict[str, Any]:
    manifest = _read_json(workbench_manifest)
    records = manifest.get("records")
    if not isinstance(records, list):
        raise ValueError("Workbench manifest must contain records list")
    output.mkdir(parents=True, exist_ok=True)
    rows = [
        _record_readiness(record, workbench_output, vector_root, workbench_url)
        for record in records
        if isinstance(record, dict)
    ]
    stage_counts = Counter(row["stage"] for row in rows)
    bbox_counts = Counter(row["bbox_status"] or "missing" for row in rows)
    summary = {
        "schema_version": "genplan-readiness/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "workbench_manifest": str(workbench_manifest.resolve()),
        "workbench_output": str(workbench_output.resolve()),
        "workbench_url": workbench_url,
        "vector_root": str(vector_root.resolve()) if vector_root else "",
        "records": len(rows),
        "stage_counts": dict(stage_counts),
        "bbox_counts": dict(bbox_counts),
        "next_action_counts": dict(Counter(row["next_action"] for row in rows)),
    }
    _write_json(output / "summary.json", summary)
    _write_json(output / "records.json", {"records": rows})
    _write_csv(output / "records.csv", rows)
    return {"summary": summary, "records": rows}


def _record_readiness(
    record: Mapping[str, Any],
    workbench_output: Path,
    vector_root: Path | None,
    workbench_url: str,
) -> dict[str, Any]:
    record_id = _record_id(record)
    record_dir = workbench_output / "records" / _sha256_text(record_id)
    gcps_path = record_dir / "gcps.json"
    qa_path = record_dir / "qa.json"
    review_path = record_dir / "review.json"
    vector_manifest = (
        _find_vector_manifest(vector_root, record_id) if vector_root is not None else None
    )
    gcps = _safe_json(gcps_path)
    qa = _safe_json(qa_path)
    review = _safe_json(review_path)
    bbox_status = str(record.get("bbox_status") or "")
    queue_status = str(record.get("queue_status") or record.get("workflow_status") or "")
    stage, next_action = _stage_and_action(
        queue_status=queue_status,
        bbox_status=bbox_status,
        gcps=gcps,
        qa=qa,
        review=review,
        vector_manifest=vector_manifest,
    )
    return {
        "record_id": record_id,
        "asset_id": str(record.get("asset_id") or ""),
        "workbench_url": _workbench_record_url(workbench_url, record),
        "filename": str(record.get("original_filename") or record.get("filename") or ""),
        "region": str(
            record.get("egkn_region")
            or record.get("normalized_region")
            or record.get("region")
            or ""
        ),
        "district": str(
            record.get("egkn_district")
            or record.get("normalized_district")
            or record.get("district")
            or ""
        ),
        "locality": str(
            record.get("normalized_locality") or record.get("locality") or ""
        ),
        "queue_status": queue_status,
        "bbox_status": bbox_status,
        "bbox_source": str(record.get("bbox_source") or ""),
        "bbox_label": str(record.get("bbox_label") or ""),
        "bbox_reason": str(record.get("bbox_reason") or ""),
        "stage": stage,
        "next_action": next_action,
        "gcps_path": str(gcps_path) if gcps_path.exists() else "",
        "qa_path": str(qa_path) if qa_path.exists() else "",
        "review_path": str(review_path) if review_path.exists() else "",
        "review_status": _review_status(review),
        "vector_manifest": str(vector_manifest) if vector_manifest else "",
        "train_points": _point_count(gcps, "train"),
        "checkpoint_points": _point_count(gcps, "checkpoint"),
    }


def _stage_and_action(
    *,
    queue_status: str,
    bbox_status: str,
    gcps: Mapping[str, Any] | None,
    qa: Mapping[str, Any] | None,
    review: Mapping[str, Any] | None,
    vector_manifest: Path | None,
) -> tuple[str, str]:
    if queue_status == "duplicate_manual_file":
        return "duplicate", "Archive duplicate and keep best source"
    if queue_status == "identity_review_required":
        return "identity_review", "Fix region/district/locality identity"
    if bbox_status and bbox_status != "resolved":
        return "bbox_review", "Resolve map-area conflict before georeferencing"
    if queue_status == "pdf_page_selection_required":
        return "page_selection", "Select correct PDF page/contact sheet"
    if not gcps:
        return "gcp_needed", "Place A1 control points in workbench"
    workflow_status = str(gcps.get("workflow_status") or "").lower()
    if workflow_status != "qa_pending":
        return "gcp_saved_not_submitted", "Submit saved GCPs to QA in workbench"
    if not qa:
        return "qa_missing", "Regenerate workbench QA JSON"
    if not review:
        return "independent_review_needed", "Run A2 independent review"
    review_status = _review_status(review)
    if review_status in {"REJECT", "REJECTED"}:
        return "review_rejected", "Fix GCP/provenance/legend and review again"
    if review_status in {"STRICT", "WARNING"} and vector_manifest:
        return "vectorized_candidate", "Run import QA before enabling search"
    if review_status in {"STRICT", "WARNING"}:
        return "export_ready", "Export GeoTIFF and vectorize candidate layers"
    return "review_pending", "Complete independent review"


def _workbench_record_url(workbench_url: str, record: Mapping[str, Any]) -> str:
    asset_id = str(record.get("asset_id") or "")
    if not workbench_url or not asset_id:
        return ""
    return f"{workbench_url.rstrip('/')}/?record={asset_id}"


def _record_id(record: Mapping[str, Any]) -> str:
    source = str(record.get("extracted_path") or record.get("source_path") or "")
    identity_source = str(
        record.get("asset_id")
        or record.get("record_id")
        or record.get("document_id")
        or source
    )
    if len(identity_source) <= 128 and all(
        char.isalnum() or char in "-_." for char in identity_source
    ):
        return identity_source
    return _sha256_text(identity_source)


def _point_count(payload: Mapping[str, Any] | None, role: str) -> int:
    if not payload:
        return 0
    points = payload.get("points")
    if not isinstance(points, list):
        return 0
    return sum(
        1
        for point in points
        if isinstance(point, dict) and str(point.get("role") or "").lower() == role
    )


def _review_status(payload: Mapping[str, Any] | None) -> str:
    if not payload:
        return ""
    status = payload.get("status") or payload.get("overall_status")
    return str(status or "").upper()


def _find_vector_manifest(root: Path | None, record_id: str) -> Path | None:
    if root is None or not root.exists():
        return None
    candidates = sorted(root.rglob("vectorize-manifest.json"))
    for candidate in candidates:
        if record_id in str(candidate):
            return candidate
    return None


def _safe_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = [
        "record_id",
        "asset_id",
        "workbench_url",
        "filename",
        "region",
        "district",
        "locality",
        "queue_status",
        "bbox_status",
        "bbox_source",
        "bbox_label",
        "bbox_reason",
        "stage",
        "next_action",
        "train_points",
        "checkpoint_points",
        "gcps_path",
        "qa_path",
        "review_path",
        "review_status",
        "vector_manifest",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["build_readiness_report", "main"]
