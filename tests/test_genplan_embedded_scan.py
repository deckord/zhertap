from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from tools.genplan_embedded_scan.cli import scan_manifest


def _write_inventory(path: Path, source: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "extracted_path": str(source),
                        "original_filename": source.name,
                        "detected_format": source.suffix.lstrip("."),
                        "asset_role": "plan_document",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_scan_detects_embedded_transform(tmp_path: Path) -> None:
    source = tmp_path / "plan.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(70, 51, 0.01, 0.01),
    ) as dataset:
        dataset.write(np.ones((2, 2), dtype="uint8"), 1)
    inventory = _write_inventory(tmp_path / "inventory.json", source)

    report = scan_manifest(inventory_manifest=inventory)

    assert report["summary"]["usable_embedded_georef"] == 1
    assert report["records"][0]["embedded_status"] == "embedded_transform"


def test_scan_detects_world_file_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "plan.jpg"
    source.write_bytes(b"image")
    source.with_suffix(".jgw").write_text(
        "1\n0\n0\n-1\n70\n51\n",
        encoding="utf-8",
    )
    inventory = _write_inventory(tmp_path / "inventory.json", source)

    report = scan_manifest(inventory_manifest=inventory)

    assert report["summary"]["sidecar_world_file"] == 1
    assert report["records"][0]["embedded_status"] == "sidecar_world_file"
