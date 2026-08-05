from __future__ import annotations

import hashlib
import json
import struct
import unicodedata
import zipfile
from pathlib import Path

from tools.genplan_pipeline import PipelineConfig, run_pipeline
from tools.genplan_pipeline.cli import main
from tools.genplan_pipeline.normalize import infer_location


def png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


def test_pipeline_extracts_inventory_and_preserves_original_names(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    decomposed_district = unicodedata.normalize("NFD", "Бурабайский район")
    member = (
        "Общий каталог/Акмолинская область/"
        f"{decomposed_district}/село Бурабай.png"
    )
    payload = png_header(1200, 800)
    archive = source / "plans.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member, payload)

    result = run_pipeline(PipelineConfig(source=source, output=output))

    assert result.archive_count == 1
    assert result.asset_count == 1
    manifest = json.loads((output / "manifests" / "manifest.json").read_text("utf-8"))
    record = manifest["records"][0]
    assert record["original_member_path"] == member
    assert record["normalized_region"] == "Акмолинская область"
    assert record["region_code"] == "01"
    assert record["egkn_region"] == "Акмолинская область (01)"
    assert record["normalized_district"] == "Бурабайский район"
    assert record["normalized_locality"] == "село Бурабай"
    assert record["width_px"] == 1200
    assert record["height_px"] == 800
    assert record["sha256"] == hashlib.sha256(payload).hexdigest()
    assert record["georef_status"] == "requires_control_points"
    assert record["crs"] == ""
    assert record["control_point_count"] == 0
    assert (output / "manifests" / "manifest.csv").exists()


def test_pdf_page_count_and_repeat_run_are_stable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj <</Type /Pages /Count 2>> endobj\n"
        b"2 0 obj <</Type /Page /Parent 1 0 R>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 1 0 R>> endobj\n%%EOF"
    )
    with zipfile.ZipFile(source / "plans.zip", "w") as bundle:
        bundle.writestr(
            "Каталог/г.Астана/Генеральный план Астаны.pdf",
            pdf,
        )

    first = run_pipeline(PipelineConfig(source=source, output=output))
    second = run_pipeline(PipelineConfig(source=source, output=output))
    record = json.loads((output / "manifests" / "manifest.json").read_text("utf-8"))[
        "records"
    ][0]

    assert first.asset_count == second.asset_count == 1
    assert record["page_count"] == 2
    assert record["normalized_region"] == "г. Астана"
    assert record["normalized_locality"] == "Астаны"


def test_unsafe_zip_member_is_rejected_and_logged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    with zipfile.ZipFile(source / "unsafe.zip", "w") as bundle:
        bundle.writestr("../outside.jpg", b"\xff\xd8\xff\xd9")

    result = run_pipeline(PipelineConfig(source=source, output=output))

    assert result.asset_count == 0
    assert result.error_count == 1
    errors = [
        json.loads(line)
        for line in (output / "manifests" / "errors.jsonl").read_text("utf-8").splitlines()
    ]
    assert errors[0]["code"] == "member_not_extracted"
    assert not (tmp_path / "outside.jpg").exists()


def test_aliases_and_inventory_only_cli(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    with zipfile.ZipFile(source / "plans.zip", "w") as bundle:
        bundle.writestr("Каталог/Неизвестный регион/план.jpg", b"\xff\xd8\xff\xd9")

    exit_code = main(
        [
            "run",
            "--source",
            str(source),
            "--output",
            str(output),
            "--no-extract",
        ]
    )
    archives = json.loads((output / "manifests" / "archives.json").read_text("utf-8"))

    assert exit_code == 0
    assert len(archives["records"]) == 1
    assert json.loads((output / "manifests" / "manifest.json").read_text("utf-8"))[
        "records"
    ] == []

    info = infer_location(
        "Каталог/Моя область/Мой район/Поселок.jpg",
        {
            "regions": {
                "Моя область": {"name": "Акмолинская область", "code": "01"}
            },
            "districts": {"Мой район": "Целиноградский район"},
            "localities": {"Поселок": "Талапкер"},
        },
    )
    assert info.egkn_region == "Акмолинская область (01)"
    assert info.normalized_district == "Целиноградский район"
    assert info.normalized_locality == "Талапкер"


def test_pre_extracted_mode_uses_map_catalog_and_classifies_files(tmp_path: Path) -> None:
    root = tmp_path / "genplan"
    source = root / "extracted"
    output = root / "inventory"
    work = root / "work"
    archive_dir = source / "archive-001" / "Акмолинская область" / "Бурабайский район"
    archive_dir.mkdir(parents=True)
    work.mkdir()
    (archive_dir / "Бурабай.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (archive_dir / "Описание.docx").write_bytes(b"not-an-openxml-file")
    (archive_dir / "Примечание.txt").write_text("text", encoding="utf-8")
    (source / ".DS_Store").write_bytes(b"service")
    map_row = {
        "archive": "plans-001.zip",
        "original_member": (
            "Общий каталог/Акмолинская область/"
            "Бурабайский район/Бурабай.jpg"
        ),
        "normalized_relative_path": (
            "Акмолинская область/Бурабайский район/Бурабай.jpg"
        ),
        "size": 4,
        "is_directory": False,
        "status": "extracted",
    }
    (source / "archive-001-extraction-map.jsonl").write_text(
        json.dumps(map_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (work / "egkn_catalog.json").write_text(
        json.dumps(
            [
                {
                    "code": "01",
                    "name": "Акмолинская область",
                    "nameRu": "Акмолинская область (01)",
                    "districts": [
                        {
                            "code": "171",
                            "nameRu": "Бурабайский (01-171)",
                            "type": "р-н.",
                        }
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = run_pipeline(PipelineConfig(source=source, output=output))
    records = json.loads((output / "manifests" / "manifest.json").read_text("utf-8"))[
        "records"
    ]
    by_name = {record["original_filename"]: record for record in records}

    assert result.asset_count == 5
    plan = by_name["Бурабай.jpg"]
    assert plan["source_kind"] == "pre_extracted"
    assert plan["source_archive_name"] == "plans-001.zip"
    assert plan["asset_role"] == "plan_document"
    assert plan["district_code"] == "171"
    assert plan["egkn_district"] == "р-н. Бурабайский (01-171)"
    assert by_name["Описание.docx"]["asset_role"] == "supporting_document"
    assert by_name["Примечание.txt"]["workflow_status"] == "unsupported_file"
    assert by_name[".DS_Store"]["workflow_status"] == "service_file_ignored"
    assert by_name["archive-001-extraction-map.jsonl"]["asset_role"] == "service_file"
