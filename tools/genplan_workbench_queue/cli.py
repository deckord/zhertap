from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_INCLUDE_STATUSES = {
    "manual_georeference_required",
    "pdf_page_selection_required",
}
WORKBENCH_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf", ".tif", ".tiff"}


def build_workbench_manifest(
    *,
    inventory_manifest: Path,
    status_report: Path,
    output: Path,
    prepared_pdf_manifest: Path | None = None,
    selected_pdf_page_manifest: Path | None = None,
    pdf_contact_sheet_manifest: Path | None = None,
    bbox_audit_records: Path | None = None,
    autoreg_output: Path | None = None,
    include_statuses: set[str] | None = None,
) -> dict[str, Any]:
    inventory = _read_json(inventory_manifest)
    status = _read_json(status_report)
    include = include_statuses or DEFAULT_INCLUDE_STATUSES
    inventory_by_id = _records_by_id(inventory)
    prepared_by_id = (
        _records_by_id(_read_json(prepared_pdf_manifest))
        if prepared_pdf_manifest is not None and prepared_pdf_manifest.exists()
        else {}
    )
    selected_by_id = (
        _records_by_id(_read_json(selected_pdf_page_manifest))
        if selected_pdf_page_manifest is not None and selected_pdf_page_manifest.exists()
        else {}
    )
    selected_split_by_source = _split_records_by_source(selected_by_id.values())
    contact_sheet_by_id = (
        _records_by_id(_read_json(pdf_contact_sheet_manifest))
        if pdf_contact_sheet_manifest is not None and pdf_contact_sheet_manifest.exists()
        else {}
    )
    bbox_by_id = (
        _records_by_id(_read_json(bbox_audit_records))
        if bbox_audit_records is not None and bbox_audit_records.exists()
        else {}
    )
    autoreg_by_id = (
        _autoreg_by_id(autoreg_output)
        if autoreg_output is not None and autoreg_output.exists()
        else {}
    )

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for status_item in status.get("records", []):
        if not isinstance(status_item, dict):
            continue
        queue_status = str(status_item.get("status") or "")
        if queue_status not in include:
            continue
        asset_id = str(status_item.get("asset_id") or "")
        source = inventory_by_id.get(asset_id)
        if source is None:
            skipped.append(
                {
                    "asset_id": asset_id,
                    "status": queue_status,
                    "reason": "missing_inventory_record",
                }
            )
            continue
        split_records = selected_split_by_source.get(asset_id, [])
        if queue_status == "pdf_page_selection_required" and split_records:
            for split_record in split_records:
                split_asset_id = str(split_record.get("asset_id") or "")
                _append_record(
                    records=records,
                    skipped=skipped,
                    source=source,
                    workbench_record=split_record,
                    contact_sheet=contact_sheet_by_id.get(asset_id),
                    bbox_audit=bbox_by_id.get(split_asset_id) or bbox_by_id.get(asset_id),
                    autoreg=autoreg_by_id.get(split_asset_id)
                    or autoreg_by_id.get(asset_id),
                    queue_status=queue_status,
                    status_item=status_item,
                )
            continue
        record = _select_workbench_source(
            source,
            prepared_by_id.get(asset_id),
            selected_by_id.get(asset_id),
        )
        record_asset_id = str(record.get("asset_id") or "")
        _append_record(
            records=records,
            skipped=skipped,
            source=source,
            workbench_record=record,
            contact_sheet=contact_sheet_by_id.get(asset_id),
            bbox_audit=bbox_by_id.get(record_asset_id) or bbox_by_id.get(asset_id),
            autoreg=autoreg_by_id.get(record_asset_id) or autoreg_by_id.get(asset_id),
            queue_status=queue_status,
            status_item=status_item,
        )

    records.sort(key=_record_sort_key)
    manifest = {
        "schema_version": "genplan-workbench-queue/v1",
        "source_inventory": str(inventory_manifest),
        "source_status_report": str(status_report),
        "prepared_pdf_manifest": str(prepared_pdf_manifest or ""),
        "selected_pdf_page_manifest": str(selected_pdf_page_manifest or ""),
        "pdf_contact_sheet_manifest": str(pdf_contact_sheet_manifest or ""),
        "bbox_audit_records": str(bbox_audit_records or ""),
        "autoreg_output": str(autoreg_output or ""),
        "include_statuses": sorted(include),
        "records": records,
        "skipped": skipped,
        "summary": {
            "records": len(records),
            "skipped": len(skipped),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _select_workbench_source(
    inventory_record: dict[str, Any],
    prepared_record: dict[str, Any] | None,
    selected_record: dict[str, Any] | None,
) -> dict[str, Any]:
    if selected_record is not None:
        return selected_record
    if prepared_record is not None:
        return prepared_record
    return inventory_record


def _append_record(
    *,
    records: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    source: dict[str, Any],
    workbench_record: dict[str, Any],
    contact_sheet: dict[str, Any] | None,
    bbox_audit: dict[str, Any] | None,
    autoreg: dict[str, Any] | None,
    queue_status: str,
    status_item: dict[str, Any],
) -> None:
    record = dict(workbench_record)
    suffix = Path(str(record.get("extracted_path") or "")).suffix.casefold()
    if suffix not in WORKBENCH_SUFFIXES:
        skipped.append(
            {
                "asset_id": record.get("asset_id") or source.get("asset_id") or "",
                "status": queue_status,
                "reason": "unsupported_workbench_source_format",
                "suffix": suffix,
            }
        )
        return
    if contact_sheet is not None and contact_sheet.get("pdf_contact_sheet_path"):
        record["pdf_contact_sheet_path"] = contact_sheet["pdf_contact_sheet_path"]
        record["pdf_contact_sheet_sha256"] = contact_sheet.get(
            "pdf_contact_sheet_sha256", ""
        )
    if bbox_audit is not None:
        record["bbox_status"] = (
            bbox_audit.get("bbox_status") or bbox_audit.get("status") or ""
        )
        record["bbox_source"] = bbox_audit.get("bbox_source") or ""
        record["bbox_label"] = bbox_audit.get("bbox_label") or ""
        record["bbox_reason"] = (
            bbox_audit.get("bbox_reason") or bbox_audit.get("error") or ""
        )
    if autoreg is not None:
        record["autoreg_diagnostics"] = autoreg
    effective_status = _effective_queue_status(queue_status, record)
    record["queue_status"] = effective_status
    record["queue_next_action"] = _effective_next_action(
        effective_status,
        status_item.get("next_action") or "",
    )
    record["queue_action"] = status_item.get("queue_action") or ""
    record["queue_state"] = status_item.get("queue_state") or ""
    record["operator_priority"] = _priority(effective_status)
    record["source_workflow_status"] = queue_status
    record["workflow_status"] = effective_status
    records.append(record)


def _effective_queue_status(status: str, record: dict[str, Any]) -> str:
    if status == "pdf_page_selection_required" and record.get(
        "rendered_from_single_page_pdf"
    ):
        return "manual_georeference_required"
    if status == "pdf_page_selection_required" and record.get(
        "rendered_from_selected_pdf_page"
    ):
        return "manual_georeference_required"
    return status


def _effective_next_action(status: str, original: str) -> str:
    if status == "manual_georeference_required":
        return "place_control_points_in_workbench"
    return original


def _split_records_by_source(
    records: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not record.get("split_from_multi_page_pdf"):
            continue
        source_asset_id = str(record.get("source_asset_id") or "")
        if not source_asset_id:
            continue
        output.setdefault(source_asset_id, []).append(record)
    for source_records in output.values():
        source_records.sort(key=lambda item: int(item.get("selected_pdf_page") or 0))
    return output


def _records_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Manifest must contain records list")
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        if isinstance(record, dict) and record.get("asset_id"):
            output[str(record["asset_id"])] = record
    return output


def _autoreg_by_id(root: Path) -> dict[str, dict[str, Any]]:
    assets_root = root / "assets"
    if not assets_root.exists():
        return {}
    output: dict[str, dict[str, Any]] = {}
    for asset_dir in assets_root.iterdir():
        if not asset_dir.is_dir():
            continue
        status_path = asset_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            status = _read_json(status_path)
        except ValueError:
            continue
        asset_id = str(status.get("asset_id") or asset_dir.name)
        attempts = [
            _autoreg_attempt_summary(attempt)
            for attempt in status.get("attempts", [])
            if isinstance(attempt, dict)
        ]
        attempts = [attempt for attempt in attempts if attempt]
        if not attempts:
            continue
        output[asset_id] = {
            "workflow_state": status.get("workflow_state") or "",
            "registration_status": status.get("registration_status") or "",
            "reasons": status.get("reasons") or [],
            "attempts": attempts,
            "best_attempt": _best_attempt(attempts),
        }
    return output


def _autoreg_attempt_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    result = attempt.get("result")
    if not isinstance(result, dict):
        return {}
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    proposed = result.get("proposed_gcps") if isinstance(result.get("proposed_gcps"), list) else []
    anchors = (
        result.get("diagnostic_anchor_points")
        if isinstance(result.get("diagnostic_anchor_points"), list)
        else []
    )
    reasons = result.get("reasons") if isinstance(result.get("reasons"), list) else []
    return {
        "basemap": attempt.get("basemap") or "",
        "status": result.get("status") or "",
        "confidence": result.get("confidence") or 0,
        "confidence_label": result.get("confidence_label") or "",
        "proposed_gcp_count": len(proposed),
        "diagnostic_anchor_count": len(anchors),
        "diagnostic_anchor_quality": _diagnostic_anchor_quality(result),
        "reasons": reasons,
        "artifacts": {
            key: value
            for key, value in artifacts.items()
            if key in {"plan_preview", "basemap", "matches", "result"} and value
        },
        "metrics": {
            key: metrics.get(key)
            for key in (
                "candidate_matches",
                "inliers",
                "inlier_ratio",
                "reprojection_rmse_px",
                "plan_coverage",
                "reference_coverage",
                "homography_condition",
            )
            if key in metrics
        },
    }


def _best_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        return {}
    return max(
        attempts,
        key=lambda attempt: (
            float(attempt.get("confidence") or 0),
            int(attempt.get("proposed_gcp_count") or 0),
            int(attempt.get("diagnostic_anchor_count") or 0),
            int((attempt.get("metrics") or {}).get("inliers") or 0),
        ),
    )


def _diagnostic_anchor_quality(result: dict[str, Any]) -> str:
    summary = result.get("diagnostic_anchor_summary")
    if not isinstance(summary, dict):
        return ""
    return str(summary.get("quality_label") or "")


def _priority(status: str) -> int:
    return {
        "manual_georeference_required": 10,
        "pdf_page_selection_required": 20,
    }.get(status, 99)


def _record_sort_key(record: dict[str, Any]) -> tuple[int, str, str, str, str]:
    return (
        int(record.get("operator_priority") or 99),
        str(record.get("egkn_region") or record.get("normalized_region") or ""),
        str(record.get("egkn_district") or record.get("normalized_district") or ""),
        str(record.get("normalized_locality") or ""),
        str(record.get("original_filename") or record.get("asset_id") or ""),
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a focused workbench manifest.")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--status-report", type=Path, required=True)
    parser.add_argument("--prepared-pdf-manifest", type=Path)
    parser.add_argument("--selected-pdf-page-manifest", type=Path)
    parser.add_argument("--pdf-contact-sheet-manifest", type=Path)
    parser.add_argument("--bbox-audit-records", type=Path)
    parser.add_argument("--autoreg-output", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--include-status",
        action="append",
        default=[],
        help="Queue status to include. May be passed multiple times.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    include = set(args.include_status) if args.include_status else None
    manifest = build_workbench_manifest(
        inventory_manifest=args.inventory,
        status_report=args.status_report,
        prepared_pdf_manifest=args.prepared_pdf_manifest,
        selected_pdf_page_manifest=args.selected_pdf_page_manifest,
        pdf_contact_sheet_manifest=args.pdf_contact_sheet_manifest,
        bbox_audit_records=args.bbox_audit_records,
        autoreg_output=args.autoreg_output,
        output=args.output,
        include_statuses=include,
    )
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
