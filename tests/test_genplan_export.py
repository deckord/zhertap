from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from tools.genplan_export import (
    ExportError,
    RasterioDependencyError,
    export_sheet,
)
from tools.genplan_export import exporter as exporter_module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _inputs(tmp_path: Path, *, status: str = "STRICT", allow_shadow: bool = False):
    source = tmp_path / "source.png"
    image = Image.new("RGB", (32, 24), color=(240, 240, 240))
    for x in range(32):
        image.putpixel((x, x % 24), (20, 80, 160))
    image.save(source)
    source_sha = _sha256(source)

    points = [
        {
            "id": "north-west",
            "pixel_x": 0,
            "pixel_y": 0,
            "lon": 70.0,
            "lat": 52.01,
            "role": "train",
            "label": "NW",
        },
        {
            "id": "north-east",
            "pixel_x": 32,
            "pixel_y": 0,
            "lon": 70.02,
            "lat": 52.01,
            "role": "train",
            "label": "NE",
        },
        {
            "id": "south-west",
            "pixel_x": 0,
            "pixel_y": 24,
            "lon": 70.0,
            "lat": 52.0,
            "role": "train",
            "label": "SW",
        },
        {
            "id": "south-east",
            "pixel_x": 32,
            "pixel_y": 24,
            "lon": 70.02,
            "lat": 52.0,
            "role": "train",
            "label": "SE",
        },
        {
            "id": "checkpoint",
            "pixel_x": 16,
            "pixel_y": 12,
            "lon": 70.01,
            "lat": 52.005,
            "role": "checkpoint",
            "label": "independent",
        },
    ]
    gcps = tmp_path / "gcps.json"
    _write_json(
        gcps,
        {
            "schema_version": "1.0",
            "record_id": "asset-1",
            "source_sha256": source_sha,
            "image_width_px": 32,
            "image_height_px": 24,
            "transform_type": "affine",
            "workflow_status": "qa_pending",
            "operator": "operator-1",
            "points": points,
        },
    )
    qa = tmp_path / "qa.json"
    _write_json(
        qa,
        {
            "schema_version": "1.0",
            "record_id": "asset-1",
            "source_sha256": source_sha,
            "workflow_status": "qa_pending",
            "qa_decision": "pending",
            "calculation": {
                "transform_type": "affine",
                "train_count": 4,
                "checkpoint_count": 1,
            },
        },
    )
    review = tmp_path / "review.json"
    _write_json(
        review,
        {
            "schema_version": "1.0",
            "record_id": "asset-1",
            "source_sha256": source_sha,
            "gcps_sha256": _sha256(gcps),
            "qa_sha256": _sha256(qa),
            "status": status,
            "allow_shadow": allow_shadow,
            "independent_review": True,
            "reviewer": "reviewer-2",
            "reviewed_at_utc": "2026-07-23T10:30:00+00:00",
        },
    )
    return source, gcps, qa, review


@pytest.mark.parametrize(
    "status",
    ["proposed", "qa_pending", "pending", "REJECT", "rejected"],
)
def test_blocked_review_statuses_never_export(tmp_path: Path, status: str) -> None:
    source, gcps, qa, review = _inputs(tmp_path, status=status)

    with pytest.raises(ExportError, match="does not permit export"):
        export_sheet(
            source_path=source,
            gcps_path=gcps,
            qa_path=qa,
            review_path=review,
            output_path=tmp_path / "output.tif",
        )

    assert not (tmp_path / "output.tif").exists()


def test_warning_requires_explicit_shadow_flag(tmp_path: Path) -> None:
    source, gcps, qa, review = _inputs(tmp_path, status="WARNING")

    with pytest.raises(ExportError, match="allow_shadow=true"):
        export_sheet(
            source_path=source,
            gcps_path=gcps,
            qa_path=qa,
            review_path=review,
            output_path=tmp_path / "output.tif",
        )


def test_review_must_be_independent_and_bound_to_current_files(tmp_path: Path) -> None:
    source, gcps, qa, review = _inputs(tmp_path)
    payload = json.loads(review.read_text(encoding="utf-8"))
    payload["reviewer"] = "operator-1"
    _write_json(review, payload)

    with pytest.raises(ExportError, match="reviewer must differ"):
        export_sheet(
            source_path=source,
            gcps_path=gcps,
            qa_path=qa,
            review_path=review,
            output_path=tmp_path / "same-reviewer.tif",
        )

    source, gcps, qa, review = _inputs(tmp_path)
    qa_payload = json.loads(qa.read_text(encoding="utf-8"))
    qa_payload["changed_after_review"] = True
    _write_json(qa, qa_payload)
    with pytest.raises(ExportError, match="qa_sha256"):
        export_sheet(
            source_path=source,
            gcps_path=gcps,
            qa_path=qa,
            review_path=review,
            output_path=tmp_path / "stale-review.tif",
        )


def test_proposed_workbench_record_cannot_be_exported(tmp_path: Path) -> None:
    source, gcps, qa, review = _inputs(tmp_path)
    payload = json.loads(gcps.read_text(encoding="utf-8"))
    payload["workflow_status"] = "proposed"
    _write_json(gcps, payload)
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    review_payload["gcps_sha256"] = _sha256(gcps)
    _write_json(review, review_payload)

    with pytest.raises(ExportError, match="submitted as qa_pending"):
        export_sheet(
            source_path=source,
            gcps_path=gcps,
            qa_path=qa,
            review_path=review,
            output_path=tmp_path / "output.tif",
        )


def test_projective_transform_is_blocked_instead_of_approximated(
    tmp_path: Path,
) -> None:
    source, gcps, qa, review = _inputs(tmp_path)
    gcps_payload = json.loads(gcps.read_text(encoding="utf-8"))
    gcps_payload["transform_type"] = "projective"
    _write_json(gcps, gcps_payload)
    qa_payload = json.loads(qa.read_text(encoding="utf-8"))
    qa_payload["calculation"]["transform_type"] = "projective"
    _write_json(qa, qa_payload)
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    review_payload["gcps_sha256"] = _sha256(gcps)
    review_payload["qa_sha256"] = _sha256(qa)
    _write_json(review, review_payload)

    with pytest.raises(ExportError, match="Projective workbench transforms"):
        export_sheet(
            source_path=source,
            gcps_path=gcps,
            qa_path=qa,
            review_path=review,
            output_path=tmp_path / "output.tif",
        )


def test_missing_rasterio_has_actionable_installation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, gcps, qa, review = _inputs(tmp_path)

    def missing():
        raise RasterioDependencyError(exporter_module.RASTERIO_INSTALL_HINT)

    monkeypatch.setattr(exporter_module, "_load_rasterio", missing)
    with pytest.raises(RasterioDependencyError, match="pip install"):
        export_sheet(
            source_path=source,
            gcps_path=gcps,
            qa_path=qa,
            review_path=review,
            output_path=tmp_path / "output.tif",
        )


def test_strict_export_is_wgs84_and_preserves_all_hashes(tmp_path: Path) -> None:
    rasterio = pytest.importorskip("rasterio")
    source, gcps, qa, review = _inputs(tmp_path)
    output = tmp_path / "published" / "sheet.tif"

    result = export_sheet(
        source_path=source,
        gcps_path=gcps,
        qa_path=qa,
        review_path=review,
        output_path=output,
        prefer_cog=False,
    )

    assert result.output_format == "tiled_geotiff"
    assert result.output_sha256 == _sha256(output)
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert provenance["inputs"]["source"]["sha256"] == _sha256(source)
    assert provenance["inputs"]["gcps"]["sha256"] == _sha256(gcps)
    assert provenance["inputs"]["qa"]["sha256"] == _sha256(qa)
    assert provenance["inputs"]["review"]["sha256"] == _sha256(review)
    assert provenance["output"]["sha256"] == _sha256(output)
    assert provenance["qa_release"]["status"] == "STRICT"
    assert provenance["qa_release"]["publish_mode"] == "auto_filter"
    assert provenance["guardrails"]["generated_gcps"] is False
    assert provenance["guardrails"]["checkpoints_used_for_warp"] is False

    with rasterio.open(output) as dataset:
        assert dataset.crs.to_epsg() == 4326
        assert dataset.profile["tiled"] is True
        assert dataset.bounds.left == pytest.approx(70.0, abs=0.002)
        assert dataset.bounds.right == pytest.approx(70.02, abs=0.002)
        assert dataset.bounds.bottom == pytest.approx(52.0, abs=0.002)
        assert dataset.bounds.top == pytest.approx(52.01, abs=0.002)


def test_warning_export_is_marked_shadow(tmp_path: Path) -> None:
    pytest.importorskip("rasterio")
    source, gcps, qa, review = _inputs(
        tmp_path,
        status="WARNING",
        allow_shadow=True,
    )

    result = export_sheet(
        source_path=source,
        gcps_path=gcps,
        qa_path=qa,
        review_path=review,
        output_path=tmp_path / "warning" / "sheet.tif",
        prefer_cog=False,
    )

    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert result.review_status == "WARNING"
    assert provenance["qa_release"]["publish_mode"] == "shadow"
    assert provenance["qa_release"]["allow_shadow"] is True


def test_prefers_cog_when_driver_is_available(tmp_path: Path) -> None:
    rasterio = pytest.importorskip("rasterio")
    with rasterio.Env() as environment:
        if "COG" not in environment.drivers():
            pytest.skip("Installed GDAL build has no COG driver")
    source, gcps, qa, review = _inputs(tmp_path)

    result = export_sheet(
        source_path=source,
        gcps_path=gcps,
        qa_path=qa,
        review_path=review,
        output_path=tmp_path / "cog" / "sheet.tif",
    )

    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert result.output_format == "COG"
    assert provenance["output"]["format"] == "COG"
    assert provenance["output"]["sha256"] == _sha256(result.output_path)
