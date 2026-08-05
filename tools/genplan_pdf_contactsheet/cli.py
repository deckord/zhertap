from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from tools.genplan_workbench.render import render_pdf_page

TARGET_STATUS = "pdf_page_selection_required"


def build_contact_sheets(
    *,
    inventory_manifest: Path,
    status_report: Path,
    output_dir: Path,
    data_root: Path,
    dpi: int = 45,
    columns: int = 4,
    max_pages: int = 80,
) -> dict[str, Any]:
    inventory = _read_json(inventory_manifest)
    status = _read_json(status_report)
    inventory_by_id = _records_by_id(inventory)

    output_dir.mkdir(parents=True, exist_ok=True)
    render_dir = output_dir / "rendered-pages"
    sheets_dir = output_dir / "sheets"
    render_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for status_item in status.get("records", []):
        if not isinstance(status_item, dict):
            continue
        if status_item.get("status") != TARGET_STATUS:
            continue
        asset_id = str(status_item.get("asset_id") or "")
        source = inventory_by_id.get(asset_id)
        if source is None:
            skipped.append({"asset_id": asset_id, "reason": "missing_inventory_record"})
            continue
        if str(source.get("detected_format") or "").casefold() != "pdf":
            skipped.append({"asset_id": asset_id, "reason": "not_pdf"})
            continue
        page_count = int(source.get("page_count") or 0)
        if page_count <= 0:
            skipped.append({"asset_id": asset_id, "reason": "missing_page_count"})
            continue
        if page_count > max_pages:
            skipped.append(
                {
                    "asset_id": asset_id,
                    "reason": "too_many_pages",
                    "page_count": page_count,
                    "max_pages": max_pages,
                }
            )
            continue
        try:
            sheet = _build_one_sheet(
                asset_id=asset_id,
                source=Path(str(source.get("extracted_path") or "")),
                page_count=page_count,
                output_dir=output_dir,
                render_dir=render_dir / asset_id,
                sheets_dir=sheets_dir,
                data_root=data_root,
                dpi=dpi,
                columns=columns,
            )
        except Exception as exc:
            errors.append(
                {
                    "asset_id": asset_id,
                    "reason": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        record = dict(source)
        record["pdf_contact_sheet_path"] = str(sheet)
        record["pdf_contact_sheet_sha256"] = _sha256_file(sheet)
        record["pdf_contact_sheet_dpi"] = dpi
        record["pdf_contact_sheet_columns"] = columns
        records.append(record)

    manifest = {
        "schema_version": "genplan-pdf-contact-sheets/v1",
        "source_inventory": str(inventory_manifest),
        "source_status_report": str(status_report),
        "output_dir": str(output_dir),
        "dpi": dpi,
        "columns": columns,
        "records": records,
        "skipped": skipped,
        "errors": errors,
        "summary": {
            "records": len(records),
            "skipped": len(skipped),
            "errors": len(errors),
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _build_one_sheet(
    *,
    asset_id: str,
    source: Path,
    page_count: int,
    output_dir: Path,
    render_dir: Path,
    sheets_dir: Path,
    data_root: Path,
    dpi: int,
    columns: int,
) -> Path:
    if columns <= 0:
        raise ValueError("columns must be positive")
    thumbnails: list[tuple[int, Image.Image]] = []
    for page in range(1, page_count + 1):
        rendered = render_pdf_page(
            source,
            render_dir / f"page-{page:04d}.png",
            page=page,
            data_root=data_root,
            dpi=dpi,
        )
        with Image.open(rendered) as image:
            thumb = image.convert("RGB")
            thumb.thumbnail((360, 260), Image.Resampling.LANCZOS)
            thumbnails.append((page, thumb.copy()))

    cell_width = 400
    cell_height = 316
    rows = (len(thumbnails) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (page, thumb) in enumerate(thumbnails):
        column = index % columns
        row = index // columns
        x = column * cell_width
        y = row * cell_height
        draw.rectangle(
            [x + 12, y + 12, x + cell_width - 12, y + cell_height - 12],
            outline=(207, 216, 211),
            width=1,
        )
        label = f"Page {page}"
        draw.text((x + 20, y + 20), label, fill=(23, 33, 29), font=font)
        image_x = x + (cell_width - thumb.width) // 2
        image_y = y + 48
        sheet.paste(thumb, (image_x, image_y))

    destination = sheets_dir / f"{asset_id}-contact-sheet.png"
    temporary = output_dir / f"{asset_id}-contact-sheet.part.png"
    sheet.save(temporary)
    temporary.replace(destination)
    return destination


def _records_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Manifest must contain records list")
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        if isinstance(record, dict) and record.get("asset_id"):
            output[str(record["asset_id"])] = record
    return output


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create contact sheets for multi-page genplan PDFs."
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--status-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=45)
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=80)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_contact_sheets(
        inventory_manifest=args.inventory,
        status_report=args.status_report,
        output_dir=args.output,
        data_root=args.data_root,
        dpi=args.dpi,
        columns=args.columns,
        max_pages=args.max_pages,
    )
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
