from __future__ import annotations

import json
from pathlib import Path

from tools.genplan_autoreg.providers import StaticBboxResolver
from tools.genplan_bbox_audit.cli import run_audit


def test_bbox_audit_writes_summary_and_rows(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "egkn_region": "Акмолинская область (01)",
                        "egkn_district": "г. Кокшетау (01-174)",
                        "normalized_locality": "Генплан",
                        "extracted_path": "plan-1.jpg",
                    },
                    {
                        "asset_id": "asset-2",
                        "egkn_region": "Западно-Казахстанская область (08)",
                        "normalized_locality": "Актау",
                        "extracted_path": "plan-2.jpg",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_audit(manifest, tmp_path / "out", resolver=StaticBboxResolver())

    assert result["summary"]["status_counts"] == {"resolved": 1, "unresolved": 1}
    assert result["records"][0]["bbox_source"] == "static_bbox"
    assert (tmp_path / "out" / "summary.json").is_file()
    assert (tmp_path / "out" / "records.csv").is_file()
