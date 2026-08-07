from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.genplan_vectorize import segmentation as segmentation_module
from tools.genplan_vectorize.models import (
    LegendDocument,
    LegendEntry,
    VectorizeConfigError,
    load_legend_document,
)
from tools.genplan_vectorize.segmentation import (
    RasterioDependencyError,
    VectorizeError,
    chain_sha256,
    sha256_file,
    vectorize_raster,
)

ALLOWED_RGB = (244, 211, 94)
PROHIBITED_RGB = (214, 40, 40)
RED_LINE_RGB = (255, 0, 0)
BACKGROUND_RGB = (255, 255, 255)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_source_raster(path: Path, *, size: int = 40) -> None:
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    half = size // 2
    rgb = np.zeros((3, size, size), dtype="uint8")
    for band, value in enumerate(ALLOWED_RGB):
        rgb[band, :half, :half] = value
    for band, value in enumerate(PROHIBITED_RGB):
        rgb[band, :half, half:] = value
    for band, value in enumerate(RED_LINE_RGB):
        rgb[band, half:, :half] = value
    for band, value in enumerate(BACKGROUND_RGB):
        rgb[band, half:, half:] = value

    transform = from_origin(70.0, 52.0, 0.0005, 0.0005)
    profile = {
        "driver": "GTiff",
        "width": size,
        "height": size,
        "count": 3,
        "dtype": "uint8",
        "crs": CRS.from_epsg(4326),
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(rgb)


def _legend_entry(**overrides: object) -> dict:
    base = {
        "color_hex": "#f4d35e",
        "red": 244,
        "green": 211,
        "blue": 94,
        "source": "manual",
        "label_ru": "Territory",
        "target_category": "lph-household",
        "layer_kind": "allowed",
        "confidence_score": 0.9,
        "review_status": "approved",
        "pixel_count": 400,
        "tolerance": 5,
    }
    base.update(overrides)
    return base


def _legend_payload(*, source_sha256: str, entries: list[dict] | None = None) -> dict:
    if entries is None:
        entries = [
            _legend_entry(),
            _legend_entry(
                color_hex="#d62828",
                red=214,
                green=40,
                blue=40,
                target_category="restricted",
                layer_kind="prohibited",
            ),
            _legend_entry(
                color_hex="#ff0000",
                red=255,
                green=0,
                blue=0,
                target_category="red_line",
                layer_kind="red_line",
                tolerance=20,
            ),
            _legend_entry(
                color_hex="#ffffff",
                red=255,
                green=255,
                blue=255,
                target_category="unknown",
                layer_kind="unknown",
                review_status="needs_review",
            ),
        ]
    return {
        "schema_version": "genplan-legend/v1",
        "record_id": "asset-123",
        "source_sha256": source_sha256,
        "source_title": "Test plan",
        "reviewer_id": "reviewer-a2",
        "reviewed_at_utc": "2026-08-01T09:00:00Z",
        "min_area_px": 4,
        "entries": entries,
    }


def _write_legend(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# --- model validation -------------------------------------------------------


def test_legend_entry_rejects_color_hex_rgb_mismatch() -> None:
    with pytest.raises(Exception, match="does not match red/green/blue"):
        LegendEntry.model_validate(_legend_entry(color_hex="#000000"))


def test_legend_entry_normalizes_color_hex_case() -> None:
    entry = LegendEntry.model_validate(_legend_entry(color_hex="#F4D35E"))
    assert entry.color_hex == "#f4d35e"


def test_legend_entry_is_usable_requires_approved_and_known_layer_kind() -> None:
    approved = LegendEntry.model_validate(_legend_entry())
    assert approved.is_usable is True

    unapproved = LegendEntry.model_validate(_legend_entry(review_status="needs_review"))
    assert unapproved.is_usable is False

    unmapped = LegendEntry.model_validate(_legend_entry(layer_kind="unknown"))
    assert unmapped.is_usable is False


def test_legend_document_rejects_duplicate_colors() -> None:
    payload = _legend_payload(
        source_sha256="a" * 64,
        entries=[_legend_entry(), _legend_entry()],
    )
    with pytest.raises(Exception, match="duplicate color_hex"):
        LegendDocument.model_validate(payload)


def test_legend_document_groups_usable_entries_by_layer_kind() -> None:
    payload = _legend_payload(source_sha256="a" * 64)
    document = LegendDocument.model_validate(payload)
    grouped = document.usable_entries_by_layer_kind()
    assert {kind: len(entries) for kind, entries in grouped.items()} == {
        "allowed": 1,
        "prohibited": 1,
        "red_line": 1,
    }


def test_load_legend_document_wraps_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "legend.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(VectorizeConfigError):
        load_legend_document(path)


def test_load_legend_document_wraps_schema_errors(tmp_path: Path) -> None:
    path = tmp_path / "legend.json"
    _write_legend(path, {"entries": []})
    with pytest.raises(VectorizeConfigError):
        load_legend_document(path)


# --- vectorize_raster --------------------------------------------------------


def test_vectorize_raster_writes_three_layers_and_chained_manifest(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source = tmp_path / "sheet.tif"
    _write_source_raster(source)
    source_sha = _sha256(source)

    legend_path = tmp_path / "legend.json"
    _write_legend(legend_path, _legend_payload(source_sha256=source_sha))

    output_dir = tmp_path / "out"
    result = vectorize_raster(
        source_path=source,
        legend_path=legend_path,
        output_dir=output_dir,
    )

    assert result.feature_counts == {"allowed": 1, "prohibited": 1, "red_line": 1}
    for layer_kind in ("allowed", "prohibited", "red_line"):
        path = output_dir / f"{layer_kind}.geojson"
        assert path.is_file()
        collection = json.loads(path.read_text(encoding="utf-8"))
        assert collection["type"] == "FeatureCollection"
        assert len(collection["features"]) == 1
        feature = collection["features"][0]
        assert feature["properties"]["layer_kind"] == layer_kind
        assert feature["properties"]["workflow_status"] == "proposed"

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["workflow_status"] == "proposed"
    assert manifest["record_id"] == "asset-123"
    assert manifest["source_raster"]["sha256"] == source_sha
    assert manifest["legend"]["sha256"] == _sha256(legend_path)

    expected_chain = chain_sha256(
        [
            source_sha,
            _sha256(legend_path),
            _sha256(output_dir / "allowed.geojson"),
            _sha256(output_dir / "prohibited.geojson"),
            _sha256(output_dir / "red_line.geojson"),
        ]
    )
    assert manifest["chain_sha256"] == expected_chain
    assert manifest["chain_sha256"] == result.chain_sha256
    assert sha256_file(source) == source_sha


def test_vectorize_raster_rejects_source_sha_mismatch(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source = tmp_path / "sheet.tif"
    _write_source_raster(source)

    legend_path = tmp_path / "legend.json"
    _write_legend(legend_path, _legend_payload(source_sha256="b" * 64))

    with pytest.raises(VectorizeError, match="does not match the source raster"):
        vectorize_raster(
            source_path=source,
            legend_path=legend_path,
            output_dir=tmp_path / "out",
        )


def _write_provenance(path: Path, *, record_id: str, original_sha: str, output_sha: str) -> None:
    payload = {
        "record_id": record_id,
        "inputs": {"source": {"sha256": original_sha}},
        "output": {"sha256": output_sha},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_vectorize_raster_binds_legend_through_provenance(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source = tmp_path / "sheet.tif"
    _write_source_raster(source)
    source_sha = _sha256(source)
    original_sha = "c" * 64

    legend_path = tmp_path / "legend.json"
    _write_legend(legend_path, _legend_payload(source_sha256=original_sha))

    provenance_path = tmp_path / "provenance.json"
    _write_provenance(
        provenance_path,
        record_id="asset-123",
        original_sha=original_sha,
        output_sha=source_sha,
    )

    result = vectorize_raster(
        source_path=source,
        legend_path=legend_path,
        provenance_path=provenance_path,
        output_dir=tmp_path / "out",
    )
    assert result.feature_counts == {"allowed": 1, "prohibited": 1, "red_line": 1}


def test_vectorize_raster_rejects_provenance_record_id_mismatch(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source = tmp_path / "sheet.tif"
    _write_source_raster(source)
    source_sha = _sha256(source)
    original_sha = "c" * 64

    legend_path = tmp_path / "legend.json"
    _write_legend(legend_path, _legend_payload(source_sha256=original_sha))

    provenance_path = tmp_path / "provenance.json"
    _write_provenance(
        provenance_path,
        record_id="asset-999-different",
        original_sha=original_sha,
        output_sha=source_sha,
    )

    with pytest.raises(VectorizeError, match="record_id does not match"):
        vectorize_raster(
            source_path=source,
            legend_path=legend_path,
            provenance_path=provenance_path,
            output_dir=tmp_path / "out",
        )


def test_vectorize_raster_rejects_provenance_output_sha256_mismatch(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source = tmp_path / "sheet.tif"
    _write_source_raster(source)
    original_sha = "c" * 64

    legend_path = tmp_path / "legend.json"
    _write_legend(legend_path, _legend_payload(source_sha256=original_sha))

    provenance_path = tmp_path / "provenance.json"
    _write_provenance(
        provenance_path,
        record_id="asset-123",
        original_sha=original_sha,
        output_sha="d" * 64,
    )

    with pytest.raises(VectorizeError, match="does not match the raster provenance"):
        vectorize_raster(
            source_path=source,
            legend_path=legend_path,
            provenance_path=provenance_path,
            output_dir=tmp_path / "out",
        )


def test_vectorize_raster_rejects_legend_without_usable_entries(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source = tmp_path / "sheet.tif"
    _write_source_raster(source)
    source_sha = _sha256(source)

    legend_path = tmp_path / "legend.json"
    _write_legend(
        legend_path,
        _legend_payload(
            source_sha256=source_sha,
            entries=[_legend_entry(review_status="needs_review")],
        ),
    )

    with pytest.raises(VectorizeError, match="no approved entries"):
        vectorize_raster(
            source_path=source,
            legend_path=legend_path,
            output_dir=tmp_path / "out",
        )


def test_vectorize_raster_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source = tmp_path / "sheet.tif"
    _write_source_raster(source)
    source_sha = _sha256(source)

    legend_path = tmp_path / "legend.json"
    _write_legend(legend_path, _legend_payload(source_sha256=source_sha))

    output_dir = tmp_path / "out"
    vectorize_raster(source_path=source, legend_path=legend_path, output_dir=output_dir)

    with pytest.raises(VectorizeError, match="already exists"):
        vectorize_raster(source_path=source, legend_path=legend_path, output_dir=output_dir)

    vectorize_raster(
        source_path=source,
        legend_path=legend_path,
        output_dir=output_dir,
        overwrite=True,
    )


def test_missing_rasterio_has_actionable_installation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sheet.tif"
    source.write_bytes(b"not a real raster")
    source_sha = _sha256(source)

    legend_path = tmp_path / "legend.json"
    _write_legend(legend_path, _legend_payload(source_sha256=source_sha))

    def missing():
        raise RasterioDependencyError(segmentation_module.RASTERIO_INSTALL_HINT)

    monkeypatch.setattr(segmentation_module, "_load_rasterio", missing)
    with pytest.raises(RasterioDependencyError, match="pip install"):
        vectorize_raster(
            source_path=source,
            legend_path=legend_path,
            output_dir=tmp_path / "out",
        )


def test_chain_sha256_is_order_sensitive() -> None:
    first = chain_sha256(["a" * 64, "b" * 64])
    second = chain_sha256(["b" * 64, "a" * 64])
    assert first != second
