from __future__ import annotations

import csv
import json
from pathlib import Path

from tools.genplan_operator_queue.cli import build_operator_queue


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_operator_queue_exports_workbench_links_and_reasons(tmp_path: Path) -> None:
    status = _write_json(
        tmp_path / "status.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "status": "pdf_page_selection_required",
                    "next_action": "select_pdf_page_then_open_workbench",
                    "region": "R",
                    "district": "D",
                    "locality": "L",
                    "title": "Plan",
                    "duplicate_of": "",
                    "queue_reasons": ["reason-a", "reason-b"],
                }
            ]
        },
    )
    workbench = _write_json(
        tmp_path / "workbench.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "pdf_contact_sheet_path": "sheets/asset-1.png",
                }
            ]
        },
    )
    bbox_audit = _write_json(
        tmp_path / "bbox.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "status": "resolved",
                    "bbox_source": "egkn",
                    "bbox_label": "Locality",
                }
            ]
        },
    )
    output = tmp_path / "queue.csv"

    rows = build_operator_queue(
        status_report=status,
        workbench_manifest=workbench,
        output=output,
        workbench_url="http://localhost:8765",
        bbox_audit_records=bbox_audit,
    )

    assert rows[0]["workbench_url"] == "http://localhost:8765/?record=asset-1"
    assert rows[0]["contact_sheet"] == "sheets/asset-1.png"
    assert rows[0]["bbox_status"] == "resolved"
    assert rows[0]["bbox_source"] == "egkn"
    assert rows[0]["source_status"] == ""
    assert rows[0]["queue_reasons"] == "reason-a | reason-b"
    with output.open("r", encoding="utf-8-sig", newline="") as handle:
        assert next(csv.DictReader(handle))["asset_id"] == "asset-1"


def test_operator_queue_exports_only_workbench_records(tmp_path: Path) -> None:
    status = _write_json(
        tmp_path / "status.json",
        {
            "records": [
                {"asset_id": "asset-1", "status": "manual_georeference_required"},
                {"asset_id": "duplicate", "status": "duplicate_manual_file"},
            ]
        },
    )
    workbench = _write_json(
        tmp_path / "workbench.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "queue_status": "manual_georeference_required",
                    "source_workflow_status": "manual_georeference_required",
                    "queue_next_action": "place_control_points_in_workbench",
                }
            ]
        },
    )

    rows = build_operator_queue(
        status_report=status,
        workbench_manifest=workbench,
        output=tmp_path / "queue.csv",
    )

    assert [row["asset_id"] for row in rows] == ["asset-1"]
    assert rows[0]["source_status"] == "manual_georeference_required"
