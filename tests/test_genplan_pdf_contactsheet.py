from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

import tools.genplan_pdf_contactsheet.cli as cli


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_contact_sheet_is_created_for_pdf_page_selection(tmp_path: Path, monkeypatch) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def fake_render_pdf_page(
        source: Path,
        destination: Path,
        *,
        page: int,
        data_root: Path,
        dpi: int = 150,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (120, 80), "white" if page == 1 else "gray").save(destination)
        return destination

    monkeypatch.setattr(cli, "render_pdf_page", fake_render_pdf_page)
    inventory = _write_json(
        tmp_path / "inventory.json",
        {
            "records": [
                {
                    "asset_id": "pdf-1",
                    "asset_role": "plan_document",
                    "detected_format": "pdf",
                    "extracted_path": str(pdf),
                    "page_count": 2,
                }
            ]
        },
    )
    status = _write_json(
        tmp_path / "status.json",
        {"records": [{"asset_id": "pdf-1", "status": "pdf_page_selection_required"}]},
    )

    manifest = cli.build_contact_sheets(
        inventory_manifest=inventory,
        status_report=status,
        output_dir=tmp_path / "out",
        data_root=tmp_path,
    )

    assert manifest["summary"] == {"records": 1, "skipped": 0, "errors": 0}
    sheet = Path(manifest["records"][0]["pdf_contact_sheet_path"])
    assert sheet.exists()
    assert manifest["records"][0]["pdf_contact_sheet_sha256"]
