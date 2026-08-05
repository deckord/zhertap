from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def build_operator_queue(
    *,
    status_report: Path,
    workbench_manifest: Path,
    output: Path,
    workbench_url: str = "http://127.0.0.1:8765",
    bbox_audit_records: Path | None = None,
) -> list[dict[str, Any]]:
    status_payload = _read_json(status_report)
    workbench_payload = _read_json(workbench_manifest)
    workbench_records = [
        record
        for record in workbench_payload.get("records", [])
        if isinstance(record, dict) and record.get("asset_id")
    ]
    status_by_id = _records_by_id(status_payload)
    bbox_by_id = (
        _records_by_id(_read_json(bbox_audit_records))
        if bbox_audit_records is not None
        else {}
    )
    rows: list[dict[str, Any]] = []
    for workbench_record in workbench_records:
        asset_id = str(workbench_record.get("asset_id") or "")
        status_record = status_by_id.get(asset_id, {})
        bbox_record = bbox_by_id.get(asset_id, {})
        rows.append(
            {
                "status": workbench_record.get("queue_status") or "",
                "source_status": workbench_record.get("source_workflow_status") or "",
                "next_action": workbench_record.get("queue_next_action") or "",
                "bbox_status": (
                    workbench_record.get("bbox_status")
                    or bbox_record.get("bbox_status")
                    or bbox_record.get("status")
                    or ""
                ),
                "bbox_source": (
                    workbench_record.get("bbox_source")
                    or bbox_record.get("bbox_source")
                    or ""
                ),
                "bbox_label": (
                    workbench_record.get("bbox_label")
                    or bbox_record.get("bbox_label")
                    or ""
                ),
                "region": (
                    workbench_record.get("egkn_region")
                    or workbench_record.get("normalized_region")
                    or status_record.get("region")
                    or ""
                ),
                "district": (
                    workbench_record.get("egkn_district")
                    or workbench_record.get("normalized_district")
                    or status_record.get("district")
                    or ""
                ),
                "locality": (
                    workbench_record.get("normalized_locality")
                    or status_record.get("locality")
                    or ""
                ),
                "title": (
                    workbench_record.get("title")
                    or workbench_record.get("original_filename")
                    or status_record.get("title")
                    or ""
                ),
                "asset_id": asset_id,
                "workbench_url": f"{workbench_url.rstrip('/')}/?record={asset_id}",
                "contact_sheet": workbench_record.get("pdf_contact_sheet_path") or "",
                "duplicate_of": status_record.get("duplicate_of") or "",
                "queue_reasons": " | ".join(status_record.get("queue_reasons") or []),
            }
        )
    rows.sort(key=_sort_key)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else []
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _sort_key(row: dict[str, Any]) -> tuple[int, str, str, str, str]:
    priorities = {
        "manual_georeference_required": 10,
        "pdf_page_selection_required": 20,
        "identity_review_required": 30,
        "duplicate_manual_file": 40,
    }
    return (
        priorities.get(str(row.get("status") or ""), 99),
        str(row.get("region") or ""),
        str(row.get("district") or ""),
        str(row.get("locality") or ""),
        str(row.get("title") or ""),
    )


def _records_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Workbench manifest must contain records list")
    return {
        str(record["asset_id"]): record
        for record in records
        if isinstance(record, dict) and record.get("asset_id")
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export operator genplan queue CSV.")
    parser.add_argument("--status-report", type=Path, required=True)
    parser.add_argument("--workbench-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workbench-url", default="http://127.0.0.1:8765")
    parser.add_argument("--bbox-audit-records", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = build_operator_queue(
        status_report=args.status_report,
        workbench_manifest=args.workbench_manifest,
        output=args.output,
        workbench_url=args.workbench_url,
        bbox_audit_records=args.bbox_audit_records,
    )
    print(json.dumps({"rows": len(rows), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
