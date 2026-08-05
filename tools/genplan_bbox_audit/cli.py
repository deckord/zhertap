from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.genplan_autoreg.pipeline import _resolver
from tools.genplan_autoreg.providers import BboxResolutionError, BboxResolver


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit bbox resolution for manual genplan raster/PDF inventory."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 0:
        print(json.dumps({"error": "limit cannot be negative"}, ensure_ascii=False))
        return 2
    result = run_audit(args.manifest, args.output, limit=args.limit)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


def run_audit(
    manifest_path: Path,
    output: Path,
    *,
    limit: int | None = None,
    resolver: BboxResolver | None = None,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    records = list(manifest.get("records") or [])
    if limit is not None:
        records = records[:limit]
    output.mkdir(parents=True, exist_ok=True)
    actual_resolver = resolver or _resolver_for_audit()
    rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    for record in records:
        row = _audit_record(record, actual_resolver)
        rows.append(row)
        status_counts[row["status"]] += 1
        if row["bbox_source"]:
            source_counts[row["bbox_source"]] += 1

    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path.resolve()),
        "selected": len(records),
        "status_counts": dict(status_counts),
        "bbox_source_counts": dict(source_counts),
        "output": str(output.resolve()),
    }
    _write_json(output / "summary.json", summary)
    _write_json(output / "records.json", {"records": rows})
    _write_csv(output / "records.csv", rows)
    return {"summary": summary, "records": rows}


def _resolver_for_audit():
    class Config:
        resolver = None

    return _resolver(Config())  # type: ignore[arg-type]


def _audit_record(record: Mapping[str, Any], resolver) -> dict[str, Any]:
    locality = _locality(record)
    region = _region(record)
    district = _district(record)
    row: dict[str, Any] = {
        "asset_id": str(record.get("asset_id") or ""),
        "filename": str(record.get("original_filename") or record.get("filename") or ""),
        "region": region,
        "district": district,
        "locality": locality,
        "source_path": str(
            record.get("source_pdf_path") or record.get("extracted_path") or ""
        ),
        "status": "unresolved",
        "bbox_source": "",
        "bbox_label": "",
        "west": "",
        "south": "",
        "east": "",
        "north": "",
        "error": "",
    }
    try:
        bbox = resolver.resolve(locality, region=region, district=district)
    except BboxResolutionError as exc:
        row["error"] = str(exc)
        return row
    row.update(
        {
            "status": "resolved",
            "bbox_source": bbox.source,
            "bbox_label": bbox.label,
            "west": bbox.west,
            "south": bbox.south,
            "east": bbox.east,
            "north": bbox.north,
        }
    )
    return row


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("Manifest must contain a records array")
    return payload


def _region(record: Mapping[str, Any]) -> str:
    return str(
        record.get("egkn_region")
        or record.get("normalized_region")
        or record.get("original_region")
        or record.get("region")
        or ""
    )


def _district(record: Mapping[str, Any]) -> str:
    return str(
        record.get("egkn_district")
        or record.get("normalized_district")
        or record.get("original_district")
        or record.get("district")
        or ""
    )


def _locality(record: Mapping[str, Any]) -> str:
    return str(
        record.get("normalized_locality")
        or record.get("original_locality")
        or record.get("locality")
        or ""
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = [
        "asset_id",
        "filename",
        "region",
        "district",
        "locality",
        "status",
        "bbox_source",
        "bbox_label",
        "west",
        "south",
        "east",
        "north",
        "source_path",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


__all__ = ["main", "run_audit"]
