from __future__ import annotations

import json
from pathlib import Path

from tools.genplan_workbench_queue.cli import build_workbench_manifest


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_workbench_queue_merges_split_page_bbox_audit(tmp_path: Path) -> None:
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "multi-page-pdf",
                    "extracted_path": str(tmp_path / "source.pdf"),
                    "detected_format": "pdf",
                    "asset_role": "plan_document",
                }
            ]
        },
    )
    status = _write_json(
        tmp_path / "status.json",
        {
            "records": [
                {
                    "asset_id": "multi-page-pdf",
                    "status": "pdf_page_selection_required",
                }
            ]
        },
    )
    selected = _write_json(
        tmp_path / "selected.json",
        {
            "records": [
                {
                    "asset_id": "multi-page-pdf-page-0001",
                    "source_asset_id": "multi-page-pdf",
                    "split_from_multi_page_pdf": True,
                    "selected_pdf_page": 1,
                    "extracted_path": str(tmp_path / "page-0001.png"),
                    "detected_format": "png",
                    "asset_role": "plan_document",
                    "rendered_from_selected_pdf_page": True,
                }
            ]
        },
    )
    bbox = _write_json(
        tmp_path / "bbox.json",
        {
            "records": [
                {
                    "asset_id": "multi-page-pdf-page-0001",
                    "bbox_status": "resolved",
                    "bbox_source": "egkn",
                    "bbox_label": "split page",
                }
            ]
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        selected_pdf_page_manifest=selected,
        bbox_audit_records=bbox,
        output=tmp_path / "out.json",
    )

    assert manifest["records"][0]["bbox_status"] == "resolved"
    assert manifest["records"][0]["bbox_source"] == "egkn"
    assert manifest["records"][0]["bbox_label"] == "split page"


def test_workbench_queue_merges_autoreg_diagnostics(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "extracted_path": str(source),
                    "detected_format": "png",
                    "asset_role": "plan_document",
                }
            ]
        },
    )
    status = _write_json(
        tmp_path / "status.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "status": "manual_georeference_required",
                }
            ]
        },
    )
    asset_dir = tmp_path / "autoreg" / "assets" / "asset-1"
    _write_json(
        asset_dir / "status.json",
        {
            "asset_id": "asset-1",
            "workflow_state": "completed",
            "registration_status": "needs_manual",
            "attempts": [
                {
                    "basemap": "arcgis",
                    "result": {
                        "status": "needs_manual",
                        "confidence": 0.19,
                        "confidence_label": "none",
                        "proposed_gcps": [],
                        "diagnostic_anchor_points": [
                            {
                                "id": "diag-anchor-001",
                                "scope": "operator_diagnostic_only",
                            }
                        ],
                        "diagnostic_anchor_summary": {
                            "count": 1,
                            "quality_label": "weak_hint",
                        },
                        "reasons": ["reprojection_error_above_threshold"],
                        "metrics": {
                            "inliers": 11,
                            "reprojection_rmse_px": 120.5,
                        },
                        "artifacts": {
                            "matches": str(asset_dir / "attempts" / "arcgis" / "matches.jpg"),
                            "basemap": str(asset_dir / "attempts" / "arcgis" / "basemap.jpg"),
                            "plan_preview": str(
                                asset_dir / "attempts" / "arcgis" / "plan_preview.jpg"
                            ),
                        },
                    },
                }
            ],
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        autoreg_output=tmp_path / "autoreg",
        output=tmp_path / "out.json",
    )

    diagnostics = manifest["records"][0]["autoreg_diagnostics"]
    assert diagnostics["registration_status"] == "needs_manual"
    assert diagnostics["best_attempt"]["basemap"] == "arcgis"
    assert diagnostics["best_attempt"]["metrics"]["inliers"] == 11
    assert diagnostics["best_attempt"]["diagnostic_anchor_count"] == 1
    assert "diagnostic_anchor_points" not in diagnostics["best_attempt"]
    assert diagnostics["best_attempt"]["artifacts"]["matches"].endswith("matches.jpg")
