from __future__ import annotations

import csv
import json
from pathlib import Path

from tools.genplan_operator_packs.cli import build_operator_packs


def _write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_operator_packs_copy_artifacts_and_write_html(tmp_path: Path) -> None:
    diagnostics = tmp_path / "diagnostics"
    plan = tmp_path / "plan.jpg"
    matches = tmp_path / "matches.jpg"
    basemap = tmp_path / "basemap.jpg"
    for path in (plan, matches, basemap):
        path.write_bytes(path.name.encode())
    _write_csv(
        diagnostics / "operator-priority.csv",
        [
            {
                "asset_id": "asset-1",
                "filename": "plan.jpg",
                "region": "region",
                "district": "district",
                "locality": "locality",
                "best_basemap": "osm",
                "best_confidence": "0.19",
                "best_inliers": "10",
                "best_rmse_px": "4",
                "operator_score": "12.5",
                "reasons": "needs_manual",
                "best_matches_artifact": str(matches),
            }
        ],
    )
    _write_csv(
        diagnostics / "attempts.csv",
        [
            {
                "asset_id": "asset-1",
                "basemap": "osm",
                "plan_preview": str(plan),
                "basemap_artifact": str(basemap),
                "matches": str(matches),
            }
        ],
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "original_filename": "plan.jpg",
                        "egkn_region": "region",
                        "egkn_district": "district",
                        "normalized_locality": "locality",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = build_operator_packs(
        diagnostics_dir=diagnostics,
        workbench_manifest=manifest,
        output=tmp_path / "packs",
        workbench_url="http://127.0.0.1:8765",
        limit=1,
        pack_size=1,
    )

    assert summary["selected_records"] == 1
    assert (tmp_path / "packs" / "index.html").exists()
    pack_html = (tmp_path / "packs" / "pack-001.html").read_text("utf-8")
    assert "Открыть в workbench" in pack_html
    assert "asset-1" in pack_html
    assert len(list((tmp_path / "packs" / "assets").glob("*.jpg"))) == 3

