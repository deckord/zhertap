from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.genplan_status.cli import build_report


def _write_manifest(root: Path, asset_id: str = "asset-1") -> Path:
    source = root / "extracted" / "region" / "plan.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"image")
    manifest = root / "manual.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_root_hint": str(root / "extracted"),
                "records": [
                    {
                        "asset_id": asset_id,
                        "region": "Region",
                        "district": "District",
                        "locality": "Locality",
                        "title": "Genplan",
                        "relative_path": "region/plan.jpg",
                        "extension": ".jpg",
                        "confidence": "medium",
                        "size_bytes": 5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_status_report_marks_manual_file_only(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)

    report = build_report(manual_manifest=manifest)

    assert report["total"] == 1
    assert report["status_counts"] == {"manual_file_only": 1}
    assert report["records"][0]["next_action"] == "run_autoreg_or_open_workbench"


def test_status_report_detects_qa_pending(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    record_hash = hashlib.sha256(b"asset-1").hexdigest()
    record_dir = tmp_path / "workbench_data" / "records" / record_hash
    record_dir.mkdir(parents=True)
    (record_dir / "gcps.json").write_text("{}", encoding="utf-8")
    (record_dir / "qa.json").write_text("{}", encoding="utf-8")

    report = build_report(manual_manifest=manifest)

    assert report["status_counts"] == {"qa_pending": 1}
    assert report["records"][0]["next_action"] == "independent_georef_review"


def test_status_report_detects_geotiff_export(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    export_dir = tmp_path / "work" / "exports" / "asset-1"
    export_dir.mkdir(parents=True)
    (export_dir / "asset-1.tif").write_bytes(b"tif")

    report = build_report(manual_manifest=manifest)

    assert report["status_counts"] == {"georeferenced_export": 1}
    assert report["records"][0]["next_action"] == "configure_colors_and_vectorize"


def test_status_report_reads_batch_queue_render_manual(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    queue = tmp_path / "work" / "batch-processing-akmola" / "queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        json.dumps(
            {
                "asset_id": "asset-1",
                "action": "render_manual",
                "workflow_state": "render_manual",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_report(manual_manifest=manifest)

    assert report["status_counts"] == {"pdf_page_selection_required": 1}
    assert report["records"][0]["queue_action"] == "render_manual"


def test_status_report_keeps_duplicate_target_and_reasons(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    queue = tmp_path / "work" / "batch-processing-akmola" / "queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        json.dumps(
            {
                "asset_id": "asset-1",
                "action": "duplicate",
                "workflow_state": "duplicate",
                "duplicate_of": "asset-main",
                "reasons": ["same_sha", "metadata_conflict"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_report(manual_manifest=manifest)
    record = report["records"][0]

    assert report["status_counts"] == {"duplicate_manual_file": 1}
    assert record["duplicate_of"] == "asset-main"
    assert record["queue_reasons"] == ["same_sha", "metadata_conflict"]
