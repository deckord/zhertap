from __future__ import annotations

import json
from pathlib import Path

from tools.genplan_autoreg_diagnostics.cli import build_diagnostics_report


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_diagnostics_report_counts_errors_and_prioritizes_best_attempt(tmp_path: Path) -> None:
    output_root = tmp_path / "autoreg"
    _write_json(
        output_root / "assets" / "asset-low" / "status.json",
        {
            "asset_id": "asset-low",
            "source_path": str(tmp_path / "low.jpg"),
            "workflow_state": "completed",
            "registration_status": "needs_manual",
            "attempts": [
                {
                    "basemap": "osm",
                    "result": {
                        "status": "needs_manual",
                        "confidence": 0.15,
                        "confidence_label": "none",
                        "proposed_gcps": [],
                        "metrics": {
                            "candidate_matches": 4,
                            "inliers": 0,
                            "inlier_ratio": 0,
                            "reprojection_rmse_px": 0,
                        },
                        "reasons": [
                            "insufficient_candidate_matches",
                            "pipeline_error:ValueError:test",
                        ],
                    },
                }
            ],
        },
    )
    _write_json(
        output_root / "assets" / "asset-best" / "status.json",
        {
            "asset_id": "asset-best",
            "source_path": str(tmp_path / "best.jpg"),
            "workflow_state": "completed",
            "registration_status": "proposed",
            "attempts": [
                {
                    "basemap": "arcgis",
                    "result": {
                        "status": "needs_manual",
                        "confidence": 0.5,
                        "confidence_label": "candidate",
                        "proposed_gcps": [{"plan_x": 1}],
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
                        "metrics": {
                            "candidate_matches": 30,
                            "inliers": 20,
                            "inlier_ratio": 0.67,
                            "reprojection_rmse_px": 12.5,
                        },
                        "reasons": ["automatic_result_requires_independent_manual_review"],
                        "artifacts": {"matches": str(tmp_path / "matches.jpg")},
                    },
                }
            ],
        },
    )
    manifest = _write_json(
        tmp_path / "workbench-manifest.json",
        {
            "records": [
                {
                    "asset_id": "asset-best",
                    "original_filename": "best.jpg",
                    "egkn_region": "region",
                    "normalized_locality": "locality",
                }
            ]
        },
    )

    report = build_diagnostics_report(
        autoreg_output=output_root,
        output=tmp_path / "report",
        workbench_manifest=manifest,
    )

    assert report["summary"]["assets"] == 2
    assert report["summary"]["attempts"] == 2
    assert report["summary"]["attempts_with_proposed_gcps"] == 1
    assert report["summary"]["attempts_with_diagnostic_anchors"] == 1
    assert report["summary"]["pipeline_error_assets"] == 1
    assert report["operator_priority"][0]["asset_id"] == "asset-best"
    assert (tmp_path / "report" / "operator-priority.csv").exists()
