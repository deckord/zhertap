from __future__ import annotations

import json
from pathlib import Path

from tools.genplan_workbench_queue.cli import build_workbench_manifest


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_workbench_queue_uses_prepared_pdf_png(tmp_path: Path) -> None:
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "pdf-1",
                    "extracted_path": str(tmp_path / "source.pdf"),
                    "detected_format": "pdf",
                    "original_filename": "source.pdf",
                    "asset_role": "plan_document",
                    "normalized_region": "R",
                    "normalized_district": "D",
                    "normalized_locality": "L",
                }
            ]
        },
    )
    status = _write_json(
        tmp_path / "status.json",
        {
            "records": [
                {
                    "asset_id": "pdf-1",
                    "status": "manual_georeference_required",
                    "next_action": "place_control_points_in_workbench",
                }
            ]
        },
    )
    prepared = _write_json(
        tmp_path / "prepared.json",
        {
            "records": [
                {
                    "asset_id": "pdf-1",
                    "extracted_path": str(tmp_path / "rendered.png"),
                    "detected_format": "png",
                    "original_filename": "rendered.png",
                    "asset_role": "plan_document",
                    "normalized_region": "R",
                    "normalized_district": "D",
                    "normalized_locality": "L",
                }
            ]
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        prepared_pdf_manifest=prepared,
        output=tmp_path / "out.json",
    )

    assert manifest["summary"] == {"records": 1, "skipped": 0}
    assert manifest["records"][0]["extracted_path"].endswith("rendered.png")
    assert manifest["records"][0]["queue_status"] == "manual_georeference_required"


def test_workbench_queue_promotes_rendered_single_page_pdf_to_gcp_task(
    tmp_path: Path,
) -> None:
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "single-page-pdf",
                    "extracted_path": str(tmp_path / "source.pdf"),
                    "detected_format": "pdf",
                    "original_filename": "source.pdf",
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
                    "asset_id": "single-page-pdf",
                    "status": "pdf_page_selection_required",
                    "next_action": "select_pdf_page_then_open_workbench",
                }
            ]
        },
    )
    prepared = _write_json(
        tmp_path / "prepared.json",
        {
            "records": [
                {
                    "asset_id": "single-page-pdf",
                    "extracted_path": str(tmp_path / "rendered.png"),
                    "detected_format": "png",
                    "original_filename": "rendered.png",
                    "asset_role": "plan_document",
                    "rendered_from_single_page_pdf": True,
                }
            ]
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        prepared_pdf_manifest=prepared,
        output=tmp_path / "out.json",
    )

    record = manifest["records"][0]
    assert record["queue_status"] == "manual_georeference_required"
    assert record["source_workflow_status"] == "pdf_page_selection_required"
    assert record["workflow_status"] == "manual_georeference_required"
    assert record["queue_next_action"] == "place_control_points_in_workbench"


def test_workbench_queue_promotes_selected_pdf_page_to_gcp_task(
    tmp_path: Path,
) -> None:
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "multi-page-pdf",
                    "extracted_path": str(tmp_path / "source.pdf"),
                    "detected_format": "pdf",
                    "original_filename": "source.pdf",
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
                    "next_action": "select_pdf_page_then_open_workbench",
                }
            ]
        },
    )
    selected = _write_json(
        tmp_path / "selected.json",
        {
            "records": [
                {
                    "asset_id": "multi-page-pdf",
                    "extracted_path": str(tmp_path / "page-0019.png"),
                    "detected_format": "png",
                    "original_filename": "page-0019.png",
                    "asset_role": "plan_document",
                    "rendered_from_selected_pdf_page": True,
                    "selected_pdf_page": 19,
                }
            ]
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        selected_pdf_page_manifest=selected,
        output=tmp_path / "out.json",
    )

    record = manifest["records"][0]
    assert record["extracted_path"].endswith("page-0019.png")
    assert record["selected_pdf_page"] == 19
    assert record["queue_status"] == "manual_georeference_required"
    assert record["source_workflow_status"] == "pdf_page_selection_required"


def test_workbench_queue_replaces_multi_page_pdf_with_split_pages(
    tmp_path: Path,
) -> None:
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "multi-page-pdf",
                    "extracted_path": str(tmp_path / "source.pdf"),
                    "detected_format": "pdf",
                    "original_filename": "source.pdf",
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
                    "next_action": "select_pdf_page_then_open_workbench",
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
                    "extracted_path": str(tmp_path / "page-0001.png"),
                    "detected_format": "png",
                    "original_filename": "page-0001.png",
                    "asset_role": "plan_document",
                    "rendered_from_selected_pdf_page": True,
                    "split_from_multi_page_pdf": True,
                    "selected_pdf_page": 1,
                },
                {
                    "asset_id": "multi-page-pdf-page-0002",
                    "source_asset_id": "multi-page-pdf",
                    "extracted_path": str(tmp_path / "page-0002.png"),
                    "detected_format": "png",
                    "original_filename": "page-0002.png",
                    "asset_role": "plan_document",
                    "rendered_from_selected_pdf_page": True,
                    "split_from_multi_page_pdf": True,
                    "selected_pdf_page": 2,
                },
            ]
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        selected_pdf_page_manifest=selected,
        output=tmp_path / "out.json",
    )

    assert [record["asset_id"] for record in manifest["records"]] == [
        "multi-page-pdf-page-0001",
        "multi-page-pdf-page-0002",
    ]
    assert {record["queue_status"] for record in manifest["records"]} == {
        "manual_georeference_required"
    }


def test_workbench_queue_includes_tiff(tmp_path: Path) -> None:
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "tif-1",
                    "extracted_path": str(tmp_path / "source.tif"),
                    "detected_format": "tiff",
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
                    "asset_id": "tif-1",
                    "status": "manual_georeference_required",
                }
            ]
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        output=tmp_path / "out.json",
    )

    assert manifest["summary"] == {"records": 1, "skipped": 0}
    assert manifest["records"][0]["asset_id"] == "tif-1"


def test_workbench_queue_merges_bbox_audit(tmp_path: Path) -> None:
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
                    "normalized_region": "Акмолинская область",
                    "normalized_locality": "Акколь",
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
    bbox = _write_json(
        tmp_path / "bbox.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "bbox_status": "resolved",
                    "bbox_source": "static_bbox",
                    "bbox_label": "Акколь",
                }
            ]
        },
    )

    manifest = build_workbench_manifest(
        inventory_manifest=inventory,
        status_report=status,
        bbox_audit_records=bbox,
        output=tmp_path / "out.json",
    )

    assert manifest["records"][0]["bbox_status"] == "resolved"
    assert manifest["records"][0]["bbox_source"] == "static_bbox"
    assert manifest["records"][0]["bbox_label"] == "Акколь"
