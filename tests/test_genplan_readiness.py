import hashlib
import json
from pathlib import Path

from tools.genplan_readiness.cli import build_readiness_report


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _record_dir(root: Path, record_id: str) -> Path:
    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
    return root / "records" / digest


def test_readiness_report_groups_manual_pipeline_stages(tmp_path: Path) -> None:
    manifest = _write_json(
        tmp_path / "workbench.json",
        {
            "records": [
                {
                    "asset_id": "bbox-bad",
                    "original_filename": "bad.pdf",
                    "queue_status": "manual_georeference_required",
                    "bbox_status": "unresolved",
                    "bbox_reason": "region conflict",
                },
                {
                    "asset_id": "gcp-needed",
                    "original_filename": "needed.jpg",
                    "queue_status": "manual_georeference_required",
                    "bbox_status": "resolved",
                    "bbox_source": "egkn",
                },
                {
                    "asset_id": "review-needed",
                    "original_filename": "review.jpg",
                    "queue_status": "manual_georeference_required",
                    "bbox_status": "resolved",
                },
                {
                    "asset_id": "export-ready",
                    "original_filename": "ready.jpg",
                    "queue_status": "manual_georeference_required",
                    "bbox_status": "resolved",
                },
            ]
        },
    )
    workbench_output = tmp_path / "workbench_data"
    for record_id in {"review-needed", "export-ready"}:
        record_dir = _record_dir(workbench_output, record_id)
        _write_json(
            record_dir / "gcps.json",
            {
                "record_id": record_id,
                "workflow_status": "qa_pending",
                "points": [
                    {"id": "p1", "role": "train"},
                    {"id": "c1", "role": "checkpoint"},
                ],
            },
        )
        _write_json(record_dir / "qa.json", {"record_id": record_id})
    _write_json(
        _record_dir(workbench_output, "export-ready") / "review.json",
        {"record_id": "export-ready", "status": "STRICT"},
    )

    report = build_readiness_report(
        workbench_manifest=manifest,
        workbench_output=workbench_output,
        workbench_url="http://localhost:8765",
        output=tmp_path / "out",
    )

    rows = {row["record_id"]: row for row in report["records"]}
    assert rows["gcp-needed"]["workbench_url"] == (
        "http://localhost:8765/?record=gcp-needed"
    )
    assert rows["bbox-bad"]["stage"] == "bbox_review"
    assert rows["gcp-needed"]["stage"] == "gcp_needed"
    assert rows["review-needed"]["stage"] == "independent_review_needed"
    assert rows["review-needed"]["train_points"] == 1
    assert rows["review-needed"]["checkpoint_points"] == 1
    assert rows["export-ready"]["stage"] == "export_ready"
    assert report["summary"]["stage_counts"] == {
        "bbox_review": 1,
        "gcp_needed": 1,
        "independent_review_needed": 1,
        "export_ready": 1,
    }
    assert (tmp_path / "out" / "records.csv").exists()
