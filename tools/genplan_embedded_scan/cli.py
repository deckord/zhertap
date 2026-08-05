from __future__ import annotations

import argparse
import json
import warnings
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

RASTER_SUFFIXES = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}
WORLD_FILE_SUFFIXES = {
    ".tfw",
    ".tifw",
    ".tiffw",
    ".jgw",
    ".jpgw",
    ".jpegw",
    ".pgw",
    ".pngw",
    ".wld",
}


def scan_manifest(
    *,
    inventory_manifest: Path,
    output: Path | None = None,
) -> dict[str, Any]:
    inventory = _read_json(inventory_manifest)
    records = inventory.get("records")
    if not isinstance(records, list):
        raise ValueError("Inventory manifest must contain records list")

    scanned: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        source = Path(str(record.get("extracted_path") or ""))
        if source.suffix.casefold() not in RASTER_SUFFIXES:
            continue
        scanned.append(_scan_record(record, source))

    status_counts = Counter(item["embedded_status"] for item in scanned)
    manifest = {
        "schema_version": "genplan-embedded-georef-scan/v1",
        "source_inventory": str(inventory_manifest),
        "records": scanned,
        "summary": {
            "records": len(scanned),
            "status_counts": dict(sorted(status_counts.items())),
            "usable_embedded_georef": sum(
                item["embedded_status"] in {"embedded_transform", "embedded_gcps"}
                for item in scanned
            ),
            "sidecar_world_file": sum(
                item["embedded_status"] == "sidecar_world_file"
                for item in scanned
            ),
        },
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return manifest


def _scan_record(record: dict[str, Any], source: Path) -> dict[str, Any]:
    base = {
        "asset_id": str(record.get("asset_id") or ""),
        "source": str(source),
        "filename": str(record.get("original_filename") or source.name),
        "region": str(record.get("egkn_region") or record.get("normalized_region") or ""),
        "district": str(
            record.get("egkn_district") or record.get("normalized_district") or ""
        ),
        "locality": str(record.get("normalized_locality") or ""),
        "suffix": source.suffix.casefold(),
        "exists": source.exists(),
    }
    if not source.exists():
        return {
            **base,
            "embedded_status": "missing_source_file",
            "reason": "source path does not exist",
        }

    world_file = _find_world_file(source)
    if world_file is not None:
        return {
            **base,
            "embedded_status": "sidecar_world_file",
            "world_file": str(world_file),
            "reason": "world file found; CRS still needs confirmation",
        }

    try:
        import rasterio
        from rasterio.errors import NotGeoreferencedWarning
    except ImportError:
        return {
            **base,
            "embedded_status": "scanner_dependency_missing",
            "reason": "rasterio is not installed",
        }

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(source) as dataset:
                gcps, gcp_crs = dataset.gcps
                crs = dataset.crs
                transform = dataset.transform
                has_gcps = bool(gcps and gcp_crs)
                has_transform = (
                    crs is not None
                    and not transform.is_identity
                    and transform.a != 0
                    and transform.e != 0
                )
                if has_gcps:
                    status = "embedded_gcps"
                    reason = "dataset contains GCPs and CRS"
                elif has_transform:
                    status = "embedded_transform"
                    reason = "dataset contains CRS and non-identity transform"
                else:
                    status = "no_embedded_georef"
                    reason = "no CRS/GCP/geotransform found"
                return {
                    **base,
                    "embedded_status": status,
                    "reason": reason,
                    "driver": dataset.driver,
                    "width": dataset.width,
                    "height": dataset.height,
                    "crs": str(crs) if crs else "",
                    "gcp_crs": str(gcp_crs) if gcp_crs else "",
                    "gcp_count": len(gcps),
                    "transform": list(transform)[:6],
                }
    except Exception as exc:
        return {
            **base,
            "embedded_status": "read_error",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _find_world_file(source: Path) -> Path | None:
    candidates = [
        source.with_suffix(suffix)
        for suffix in WORLD_FILE_SUFFIXES
    ]
    stem = source.with_suffix("")
    candidates.extend(stem.with_name(stem.name + suffix) for suffix in WORLD_FILE_SUFFIXES)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan genplan raster files for embedded georeferencing."
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = scan_manifest(inventory_manifest=args.inventory, output=args.output)
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
