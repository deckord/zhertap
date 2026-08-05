from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from multiprocessing import Process, Queue
from multiprocessing.queues import Queue as QueueType
from pathlib import Path
from typing import Any


def build_single_page_pdf_manifest(
    *,
    inventory_manifest: Path,
    output_dir: Path,
    data_root: Path,
    dpi: int = 150,
    max_render_seconds: int = 120,
) -> dict[str, Any]:
    inventory = _read_json(inventory_manifest)
    records = inventory.get("records")
    if not isinstance(records, list):
        raise ValueError("Inventory manifest must contain records list")

    rendered_dir = output_dir / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    render_errors: list[dict[str, Any]] = []

    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("asset_role") != "plan_document":
            continue
        if str(record.get("detected_format") or "").casefold() != "pdf":
            continue
        asset_id = str(record.get("asset_id") or "")
        page_count = int(record.get("page_count") or 0)
        if not asset_id or page_count != 1:
            skipped.append(
                {
                    "asset_id": asset_id,
                    "reason": "not_single_page_pdf",
                    "page_count": page_count,
                }
            )
            continue
        source = Path(str(record.get("extracted_path") or ""))
        destination = rendered_dir / f"{asset_id}-page-0001.png"
        try:
            rendered = _render_pdf_page_with_timeout(
                source,
                destination,
                data_root=data_root,
                dpi=dpi,
                max_render_seconds=max_render_seconds,
            )
        except TimeoutError as exc:
            render_errors.append(
                {
                    "asset_id": asset_id,
                    "source": str(source),
                    "reason": "render_timeout",
                    "message": str(exc),
                }
            )
            continue
        except Exception as exc:
            render_errors.append(
                {
                    "asset_id": asset_id,
                    "source": str(source),
                    "reason": f"{type(exc).__name__}",
                    "message": str(exc),
                }
            )
            continue
        prepared_record = dict(record)
        prepared_record["source_pdf_path"] = str(source)
        prepared_record["source_pdf_sha256"] = record.get("sha256")
        prepared_record["source_pdf_page"] = 1
        prepared_record["extracted_path"] = str(rendered)
        prepared_record["sha256"] = _sha256_file(rendered)
        prepared_record["detected_format"] = "png"
        prepared_record["media_type"] = "image/png"
        prepared_record["extension"] = ".png"
        prepared_record["original_filename"] = rendered.name
        prepared_record["rendered_from_single_page_pdf"] = True
        prepared.append(prepared_record)

    manifest = {
        "schema_version": "genplan-single-page-pdf-renders/v1",
        "source_manifest": str(inventory_manifest),
        "output_dir": str(output_dir),
        "dpi": dpi,
        "records": prepared,
        "skipped": skipped,
        "render_errors": render_errors,
        "summary": {
            "prepared": len(prepared),
            "skipped": len(skipped),
            "render_errors": len(render_errors),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _render_pdf_page_with_timeout(
    source: Path,
    destination: Path,
    *,
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
        args=(str(source), str(destination), str(data_root), dpi, queue),
    )
    process.start()
    process.join(max_render_seconds)
    if process.is_alive():
        process.terminate()
        process.join(10)
        destination.unlink(missing_ok=True)
        raise TimeoutError(f"PDF render exceeded {max_render_seconds} seconds")
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
    dpi: int,
    queue: QueueType,
) -> None:
    try:
        from tools.genplan_workbench.render import render_pdf_page

        render_pdf_page(
            Path(source),
            Path(destination),
            page=1,
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
        description="Render single-page PDF genplans into PNG batch input."
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--max-render-seconds", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_single_page_pdf_manifest(
        inventory_manifest=args.inventory,
        output_dir=args.output,
        data_root=args.data_root,
        dpi=args.dpi,
        max_render_seconds=args.max_render_seconds,
    )
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
