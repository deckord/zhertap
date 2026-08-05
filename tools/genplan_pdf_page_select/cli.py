from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from multiprocessing import Process, Queue
from multiprocessing.queues import Queue as QueueType
from pathlib import Path
from typing import Any

from tools.genplan_workbench.render import render_pdf_page


def build_selected_pdf_page_manifest(
    *,
    contact_sheet_manifest: Path,
    selections: Path,
    output_dir: Path,
    data_root: Path,
    dpi: int = 180,
    max_render_seconds: int = 120,
) -> dict[str, Any]:
    contact_payload = _read_json(contact_sheet_manifest)
    selection_payload = _read_json(selections)
    contact_records = _records_by_id(contact_payload)
    selection_records = _expand_selection_records(selection_payload)
    source_selection_counts: dict[str, int] = {}
    for selection in selection_records:
        source_asset_id = str(selection.get("asset_id") or "")
        source_selection_counts[source_asset_id] = (
            source_selection_counts.get(source_asset_id, 0) + 1
        )
    rendered_dir = output_dir / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for selection in selection_records:
        asset_id = str(selection.get("asset_id") or "")
        page = int(selection.get("page") or 0)
        source_record = contact_records.get(asset_id)
        if source_record is None:
            skipped.append({"asset_id": asset_id, "reason": "missing_contact_record"})
            continue
        page_count = int(source_record.get("page_count") or 0)
        if page < 1 or page_count < page:
            skipped.append(
                {
                    "asset_id": asset_id,
                    "reason": "selected_page_out_of_range",
                    "page": page,
                    "page_count": page_count,
                }
            )
            continue
        source = Path(str(source_record.get("extracted_path") or ""))
        if source.suffix.casefold() != ".pdf":
            skipped.append({"asset_id": asset_id, "reason": "source_is_not_pdf"})
            continue
        destination = rendered_dir / f"{asset_id}-selected-page-{page:04d}.png"
        try:
            rendered = _render_pdf_page_with_timeout(
                source,
                destination,
                page=page,
                data_root=data_root,
                dpi=dpi,
                max_render_seconds=max_render_seconds,
            )
        except TimeoutError as exc:
            errors.append(
                {
                    "asset_id": asset_id,
                    "page": page,
                    "reason": "render_timeout",
                    "message": str(exc),
                }
            )
            continue
        except Exception as exc:
            errors.append(
                {
                    "asset_id": asset_id,
                    "page": page,
                    "reason": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue

        split_page = bool(selection.get("split_pages")) or (
            source_selection_counts.get(asset_id, 0) > 1
        )
        output_asset_id = f"{asset_id}-page-{page:04d}" if split_page else asset_id
        record = dict(source_record)
        record["asset_id"] = output_asset_id
        record["source_asset_id"] = asset_id
        record["source_pdf_path"] = str(source)
        record["source_pdf_sha256"] = source_record.get("sha256") or ""
        record["source_pdf_page"] = page
        record["selected_pdf_page"] = page
        record["selected_pdf_page_reason"] = str(selection.get("reason") or "")
        record["extracted_path"] = str(rendered)
        record["sha256"] = _sha256_file(rendered)
        record["detected_format"] = "png"
        record["media_type"] = "image/png"
        record["extension"] = ".png"
        record["page_count"] = 1
        record["original_filename"] = rendered.name
        record["rendered_from_selected_pdf_page"] = True
        record["split_from_multi_page_pdf"] = split_page
        records.append(record)

    manifest = {
        "schema_version": "genplan-selected-pdf-pages/v1",
        "source_contact_sheet_manifest": str(contact_sheet_manifest),
        "source_selections": str(selections),
        "output_dir": str(output_dir),
        "dpi": dpi,
        "max_render_seconds": max_render_seconds,
        "records": records,
        "skipped": skipped,
        "errors": errors,
        "summary": {
            "prepared": len(records),
            "skipped": len(skipped),
            "errors": len(errors),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _expand_selection_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Selections JSON must contain records list")
    output: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        pages = _pages_for_selection(record)
        for page in pages:
            expanded = dict(record)
            expanded.pop("pages", None)
            expanded.pop("page_range", None)
            expanded["page"] = page
            output.append(expanded)
    return output


def _pages_for_selection(record: Mapping[str, Any]) -> list[int]:
    if record.get("page") is not None:
        return [int(record["page"])]
    pages = record.get("pages")
    if isinstance(pages, list):
        return sorted({int(page) for page in pages})
    page_range = record.get("page_range")
    if isinstance(page_range, dict):
        start = int(page_range.get("start") or 0)
        end = int(page_range.get("end") or 0)
        if start < 1 or end < start:
            raise ValueError("page_range must have positive start <= end")
        return list(range(start, end + 1))
    raise ValueError("Selection record must contain page, pages or page_range")


def _records_by_id(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Manifest must contain records list")
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        if isinstance(record, dict) and record.get("asset_id"):
            output[str(record["asset_id"])] = record
    return output


def _render_pdf_page_with_timeout(
    source: Path,
    destination: Path,
    *,
    page: int,
    data_root: Path,
    dpi: int,
    max_render_seconds: int,
) -> Path:
    if destination.exists():
        return destination
    if max_render_seconds <= 0:
        raise ValueError("max_render_seconds must be positive")
    queue: QueueType = Queue()
    process = Process(
        target=_render_worker,
        args=(str(source), str(destination), str(data_root), page, dpi, queue),
    )
    process.start()
    process.join(max_render_seconds)
    if process.is_alive():
        process.terminate()
        process.join(10)
        destination.unlink(missing_ok=True)
        raise TimeoutError(f"PDF page render exceeded {max_render_seconds} seconds")
    if process.exitcode != 0:
        destination.unlink(missing_ok=True)
        message = _queue_message(queue) or f"renderer exited with {process.exitcode}"
        raise RuntimeError(message)
    message = _queue_message(queue)
    if message and message.get("status") == "error":
        destination.unlink(missing_ok=True)
        raise RuntimeError(str(message.get("message") or "render failed"))
    if not destination.exists():
        raise RuntimeError("renderer did not create destination")
    return destination


def _render_worker(
    source: str,
    destination: str,
    data_root: str,
    page: int,
    dpi: int,
    queue: QueueType,
) -> None:
    try:
        render_pdf_page(
            Path(source),
            Path(destination),
            page=page,
            data_root=Path(data_root),
            dpi=dpi,
        )
        queue.put({"status": "ok"})
    except Exception as exc:
        queue.put({"status": "error", "message": f"{type(exc).__name__}: {exc}"})
        raise


def _queue_message(queue: QueueType) -> dict[str, Any] | None:
    if queue.empty():
        return None
    message = queue.get_nowait()
    return message if isinstance(message, dict) else None


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
        description="Render operator-selected pages from multi-page PDF genplans."
    )
    parser.add_argument("--contact-sheet-manifest", type=Path, required=True)
    parser.add_argument("--selections", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--max-render-seconds", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_selected_pdf_page_manifest(
        contact_sheet_manifest=args.contact_sheet_manifest,
        selections=args.selections,
        output_dir=args.output,
        data_root=args.data_root,
        dpi=args.dpi,
        max_render_seconds=args.max_render_seconds,
    )
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    return 0


__all__ = ["build_selected_pdf_page_manifest", "main"]
