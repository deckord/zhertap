from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from tools.genplan_pdf_page_select import cli


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_selected_pdf_page_manifest_renders_selected_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF test")
    contact_manifest = _write_json(
        tmp_path / "contact.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "extracted_path": str(pdf),
                    "sha256": "pdf-sha",
                    "detected_format": "pdf",
                    "media_type": "application/pdf",
                    "extension": ".pdf",
                    "page_count": 34,
                    "original_filename": "source.pdf",
                }
            ]
        },
    )
    selections = _write_json(
        tmp_path / "selections.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "page": 19,
                    "reason": "main drawing",
                }
            ]
        },
    )

    def fake_render_pdf_page_with_timeout(
        source: Path,
        destination: Path,
        *,
        page: int,
        data_root: Path,
        dpi: int,
        max_render_seconds: int,
    ) -> Path:
        assert source == pdf
        assert page == 19
        assert data_root == tmp_path
        assert dpi == 180
        assert max_render_seconds == 120
        Image.new("RGB", (8, 8), "white").save(destination)
        return destination

    monkeypatch.setattr(
        cli,
        "_render_pdf_page_with_timeout",
        fake_render_pdf_page_with_timeout,
    )

    manifest = cli.build_selected_pdf_page_manifest(
        contact_sheet_manifest=contact_manifest,
        selections=selections,
        output_dir=tmp_path / "out",
        data_root=tmp_path,
    )

    assert manifest["summary"] == {"prepared": 1, "skipped": 0, "errors": 0}
    record = manifest["records"][0]
    assert record["source_pdf_page"] == 19
    assert record["selected_pdf_page"] == 19
    assert record["selected_pdf_page_reason"] == "main drawing"
    assert record["rendered_from_selected_pdf_page"] is True
    assert record["detected_format"] == "png"
    assert Path(record["extracted_path"]).exists()


def test_selected_pdf_page_manifest_skips_out_of_range_page(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF test")
    contact_manifest = _write_json(
        tmp_path / "contact.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "extracted_path": str(pdf),
                    "page_count": 2,
                }
            ]
        },
    )
    selections = _write_json(
        tmp_path / "selections.json",
        {"records": [{"asset_id": "asset-1", "page": 3}]},
    )

    manifest = cli.build_selected_pdf_page_manifest(
        contact_sheet_manifest=contact_manifest,
        selections=selections,
        output_dir=tmp_path / "out",
        data_root=tmp_path,
    )

    assert manifest["summary"] == {"prepared": 0, "skipped": 1, "errors": 0}
    assert manifest["skipped"][0]["reason"] == "selected_page_out_of_range"


def test_selected_pdf_page_manifest_splits_page_range(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF test")
    contact_manifest = _write_json(
        tmp_path / "contact.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "extracted_path": str(pdf),
                    "page_count": 3,
                }
            ]
        },
    )
    selections = _write_json(
        tmp_path / "selections.json",
        {
            "records": [
                {
                    "asset_id": "asset-1",
                    "page_range": {"start": 1, "end": 2},
                    "split_pages": True,
                    "reason": "multi-map document",
                }
            ]
        },
    )

    def fake_render_pdf_page_with_timeout(
        source: Path,
        destination: Path,
        *,
        page: int,
        data_root: Path,
        dpi: int,
        max_render_seconds: int,
    ) -> Path:
        Image.new("RGB", (8, 8), "white").save(destination)
        return destination

    monkeypatch.setattr(
        cli,
        "_render_pdf_page_with_timeout",
        fake_render_pdf_page_with_timeout,
    )

    manifest = cli.build_selected_pdf_page_manifest(
        contact_sheet_manifest=contact_manifest,
        selections=selections,
        output_dir=tmp_path / "out",
        data_root=tmp_path,
    )

    assert manifest["summary"] == {"prepared": 2, "skipped": 0, "errors": 0}
    assert [record["asset_id"] for record in manifest["records"]] == [
        "asset-1-page-0001",
        "asset-1-page-0002",
    ]
    assert {record["source_asset_id"] for record in manifest["records"]} == {"asset-1"}
    assert all(record["split_from_multi_page_pdf"] for record in manifest["records"])
