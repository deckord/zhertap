from __future__ import annotations

import hashlib
import json
from pathlib import Path

import tools.genplan_pdf_prepare.cli as cli


def _sha(value: bytes = b"pdf") -> str:
    return hashlib.sha256(value).hexdigest()


def test_prepare_renders_only_single_page_pdfs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "extracted" / "plan.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    multi = tmp_path / "extracted" / "multi.pdf"
    multi.write_bytes(b"multi")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "one",
                        "asset_role": "plan_document",
                        "detected_format": "pdf",
                        "page_count": 1,
                        "sha256": _sha(),
                        "extracted_path": str(source),
                    },
                    {
                        "asset_id": "multi",
                        "asset_role": "plan_document",
                        "detected_format": "pdf",
                        "page_count": 3,
                        "sha256": _sha(b"multi"),
                        "extracted_path": str(multi),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_render(source_path, destination, *, data_root, dpi, max_render_seconds):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"png")
        return destination

    monkeypatch.setattr(cli, "_render_pdf_page_with_timeout", fake_render)

    result = cli.build_single_page_pdf_manifest(
        inventory_manifest=manifest,
        output_dir=tmp_path / "out",
        data_root=tmp_path,
    )

    assert result["summary"] == {"prepared": 1, "skipped": 1, "render_errors": 0}
    assert result["records"][0]["detected_format"] == "png"
    assert result["records"][0]["rendered_from_single_page_pdf"] is True
    assert result["skipped"][0]["asset_id"] == "multi"
