from __future__ import annotations

import importlib
import json
import math
import os
import tempfile
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from .validation import ValidatedInput, ValidationError, sha256_file, validate_inputs

EXPORTER_VERSION = "1.0"
RASTERIO_INSTALL_HINT = (
    'Rasterio is required for geospatial export. Install it with: '
    'python -m pip install "rasterio>=1.3,<1.5"'
)


class ExportError(ValueError):
    """Raised when a safe geospatial export cannot be completed."""


class RasterioDependencyError(ExportError):
    """Raised when the optional rasterio dependency is unavailable."""


@dataclass(frozen=True)
class ExportResult:
    output_path: Path
    provenance_path: Path
    output_sha256: str
    output_format: str
    record_id: str
    review_status: str


@dataclass(frozen=True)
class _RasterioModules:
    rasterio: ModuleType
    control: ModuleType
    crs: ModuleType
    enums: ModuleType
    errors: ModuleType
    shutil: ModuleType
    warp: ModuleType


def export_sheet(
    *,
    source_path: Path,
    gcps_path: Path,
    qa_path: Path,
    review_path: Path,
    output_path: Path,
    provenance_path: Path | None = None,
    overwrite: bool = False,
    max_output_pixels: int = 250_000_000,
    prefer_cog: bool = True,
) -> ExportResult:
    """Rectify one independently reviewed sheet into a WGS84 GeoTIFF."""
    try:
        validated = validate_inputs(source_path, gcps_path, qa_path, review_path)
    except ValidationError as exc:
        raise ExportError(str(exc)) from exc

    output = output_path.expanduser().resolve()
    provenance = (
        provenance_path.expanduser().resolve()
        if provenance_path is not None
        else output.parent / "provenance.json"
    )
    _validate_destinations(
        validated,
        output,
        provenance,
        overwrite=overwrite,
        max_output_pixels=max_output_pixels,
    )
    modules = _load_rasterio()
    output.parent.mkdir(parents=True, exist_ok=True)
    provenance.parent.mkdir(parents=True, exist_ok=True)

    temporary_paths: list[Path] = []
    try:
        base_tiff = _temporary_path(output.parent, ".base.tif")
        temporary_paths.append(base_tiff)
        raster_metadata = _write_warped_geotiff(
            modules,
            validated,
            base_tiff,
            max_output_pixels=max_output_pixels,
        )

        cog_available = prefer_cog and _driver_available(modules.rasterio, "COG")
        if cog_available:
            final_tiff = _temporary_path(output.parent, ".cog.tif")
            temporary_paths.append(final_tiff)
            modules.shutil.copy(
                str(base_tiff),
                str(final_tiff),
                driver="COG",
                compress="DEFLATE",
                blocksize=512,
                overview_resampling="NEAREST",
            )
            output_format = "COG"
        else:
            final_tiff = base_tiff
            output_format = "tiled_geotiff"

        output_metadata = _inspect_output(modules.rasterio, final_tiff)
        output_sha = sha256_file(final_tiff)
        provenance_payload = _build_provenance(
            validated=validated,
            output=output,
            output_sha=output_sha,
            output_format=output_format,
            raster_metadata=raster_metadata,
            output_metadata=output_metadata,
        )
        provenance_temp = _write_json_temp(provenance.parent, provenance_payload)
        temporary_paths.append(provenance_temp)

        os.replace(final_tiff, output)
        temporary_paths.remove(final_tiff)
        os.replace(provenance_temp, provenance)
        temporary_paths.remove(provenance_temp)
        return ExportResult(
            output_path=output,
            provenance_path=provenance,
            output_sha256=output_sha,
            output_format=output_format,
            record_id=validated.record_id,
            review_status=validated.review_status,
        )
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"Geospatial export failed: {exc}") from exc
    finally:
        for path in temporary_paths:
            path.unlink(missing_ok=True)


def _load_rasterio() -> _RasterioModules:
    try:
        return _RasterioModules(
            rasterio=importlib.import_module("rasterio"),
            control=importlib.import_module("rasterio.control"),
            crs=importlib.import_module("rasterio.crs"),
            enums=importlib.import_module("rasterio.enums"),
            errors=importlib.import_module("rasterio.errors"),
            shutil=importlib.import_module("rasterio.shutil"),
            warp=importlib.import_module("rasterio.warp"),
        )
    except ImportError as exc:
        raise RasterioDependencyError(RASTERIO_INSTALL_HINT) from exc


def _validate_destinations(
    validated: ValidatedInput,
    output: Path,
    provenance: Path,
    *,
    overwrite: bool,
    max_output_pixels: int,
) -> None:
    if output.suffix.lower() not in {".tif", ".tiff"}:
        raise ExportError("Output path must use the .tif or .tiff suffix")
    if output == provenance:
        raise ExportError("Output and provenance paths must differ")
    input_paths = {
        validated.source_path,
        validated.gcps_path,
        validated.qa_path,
        validated.review_path,
    }
    if output in input_paths or provenance in input_paths:
        raise ExportError("Export must not overwrite an input file")
    if not overwrite and output.exists():
        raise ExportError(f"Output already exists: {output}")
    if not overwrite and provenance.exists():
        raise ExportError(f"Provenance report already exists: {provenance}")
    if (
        isinstance(max_output_pixels, bool)
        or not isinstance(max_output_pixels, int)
        or max_output_pixels <= 0
    ):
        raise ExportError("max_output_pixels must be a positive integer")


def _write_warped_geotiff(
    modules: _RasterioModules,
    validated: ValidatedInput,
    destination: Path,
    *,
    max_output_pixels: int,
) -> dict[str, Any]:
    rasterio = modules.rasterio
    source_crs = modules.crs.CRS.from_epsg(4326)
    gcps = [
        modules.control.GroundControlPoint(
            row=point["pixel_y"],
            col=point["pixel_x"],
            x=point["lon"],
            y=point["lat"],
            z=0.0,
            id=point["id"],
            info=point["label"],
        )
        for point in validated.train_points
    ]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", modules.errors.NotGeoreferencedWarning)
        source_dataset = rasterio.open(validated.source_path)
    with source_dataset as source:
        expected_width = validated.gcps["image_width_px"]
        expected_height = validated.gcps["image_height_px"]
        if source.width != expected_width or source.height != expected_height:
            raise ExportError(
                "Source raster dimensions do not match gcps.json: "
                f"file={source.width}x{source.height}, "
                f"gcps={expected_width}x{expected_height}"
            )
        if source.width * source.height > max_output_pixels:
            raise ExportError(
                "Source raster exceeds max_output_pixels: "
                f"{source.width}x{source.height} > {max_output_pixels}"
            )
        transform, width, height = modules.warp.calculate_default_transform(
            source_crs,
            source_crs,
            source.width,
            source.height,
            gcps=gcps,
            MAX_GCP_ORDER=1,
        )
        _validate_output_grid(transform, width, height, max_output_pixels)

        profile: dict[str, Any] = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": source.count,
            "dtype": source.dtypes[0],
            "crs": source_crs,
            "transform": transform,
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
            "compress": "DEFLATE",
            "BIGTIFF": "IF_SAFER",
        }
        if source.nodata is not None:
            profile["nodata"] = source.nodata

        with rasterio.open(destination, "w", **profile) as target:
            for band_index in range(1, source.count + 1):
                modules.warp.reproject(
                    # A plain JPG/PNG has no dataset georeferencing. Passing an
                    # ndarray makes Rasterio use the reviewed GCPs explicitly
                    # instead of trying to read a transform from the source file.
                    source=source.read(band_index),
                    destination=rasterio.band(target, band_index),
                    gcps=gcps,
                    src_crs=source_crs,
                    dst_transform=transform,
                    dst_crs=source_crs,
                    src_nodata=source.nodata,
                    dst_nodata=source.nodata,
                    resampling=modules.enums.Resampling.nearest,
                    init_dest_nodata=True,
                    MAX_GCP_ORDER=1,
                )
            try:
                target.colorinterp = source.colorinterp
            except ValueError:
                pass

    return {
        "source_width": expected_width,
        "source_height": expected_height,
        "train_gcp_count": len(gcps),
        "checkpoint_gcp_count": sum(
            point.get("role") == "checkpoint"
            for point in validated.gcps.get("points", [])
        ),
        "transform_type_reviewed": validated.gcps["transform_type"],
        "warp_engine": "GDAL first-order GCP transformer via rasterio",
        "resampling": "nearest",
    }


def _validate_output_grid(
    transform: Any,
    width: int,
    height: int,
    max_output_pixels: int,
) -> None:
    if width <= 0 or height <= 0:
        raise ExportError("Calculated output grid is empty")
    if width > 200_000 or height > 200_000:
        raise ExportError(f"Calculated output dimensions are unsafe: {width}x{height}")
    if width * height > max_output_pixels:
        raise ExportError(
            "Calculated output exceeds max_output_pixels: "
            f"{width}x{height} > {max_output_pixels}"
        )
    coefficients = tuple(transform)[:6]
    if not all(math.isfinite(float(value)) for value in coefficients):
        raise ExportError("Calculated output transform contains non-finite values")
    if abs(float(transform.a)) < 1e-15 or abs(float(transform.e)) < 1e-15:
        raise ExportError("Calculated output pixel size is invalid")


def _driver_available(rasterio: ModuleType, driver: str) -> bool:
    with rasterio.Env() as environment:
        return driver in environment.drivers()


def _inspect_output(rasterio: ModuleType, path: Path) -> dict[str, Any]:
    with rasterio.open(path) as dataset:
        if dataset.crs is None or dataset.crs.to_epsg() != 4326:
            raise ExportError("Output raster is not tagged as WGS84 (EPSG:4326)")
        bounds = dataset.bounds
        values = (bounds.left, bounds.bottom, bounds.right, bounds.top)
        if not all(math.isfinite(float(value)) for value in values):
            raise ExportError("Output raster has invalid bounds")
        return {
            "driver": dataset.driver,
            "crs": dataset.crs.to_string(),
            "width": dataset.width,
            "height": dataset.height,
            "band_count": dataset.count,
            "dtype": dataset.dtypes[0],
            "is_tiled": bool(dataset.profile.get("tiled", False)),
            "bounds_wgs84": {
                "west": bounds.left,
                "south": bounds.bottom,
                "east": bounds.right,
                "north": bounds.top,
            },
            "transform": list(dataset.transform)[:6],
        }


def _build_provenance(
    *,
    validated: ValidatedInput,
    output: Path,
    output_sha: str,
    output_format: str,
    raster_metadata: dict[str, Any],
    output_metadata: dict[str, Any],
) -> dict[str, Any]:
    review = validated.review
    return {
        "schema_version": "1.0",
        "exporter": {
            "name": "tools.genplan_export",
            "version": EXPORTER_VERSION,
        },
        "exported_at_utc": datetime.now(UTC).isoformat(),
        "record_id": validated.record_id,
        "qa_release": {
            "status": validated.review_status,
            "publish_mode": validated.publish_mode,
            "allow_shadow": review.get("allow_shadow") is True,
            "independent_review": True,
            "reviewer": review["reviewer"],
            "reviewed_at_utc": review["reviewed_at_utc"],
        },
        "inputs": {
            "source": {
                "path": str(validated.source_path),
                "sha256": validated.source_sha256,
            },
            "gcps": {
                "path": str(validated.gcps_path),
                "sha256": validated.gcps_sha256,
            },
            "qa": {
                "path": str(validated.qa_path),
                "sha256": validated.qa_sha256,
            },
            "review": {
                "path": str(validated.review_path),
                "sha256": validated.review_sha256,
            },
        },
        "georeferencing": raster_metadata,
        "output": {
            "path": str(output),
            "sha256": output_sha,
            "format": output_format,
            **output_metadata,
        },
        "guardrails": {
            "generated_gcps": False,
            "used_gcp_roles": ["train"],
            "checkpoints_used_for_warp": False,
            "legal_status_inference": False,
        },
    }


def _temporary_path(directory: Path, suffix: str) -> Path:
    handle, value = tempfile.mkstemp(prefix=".genplan-export-", suffix=suffix, dir=directory)
    os.close(handle)
    path = Path(value)
    path.unlink()
    return path


def _write_json_temp(directory: Path, payload: dict[str, Any]) -> Path:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix=".genplan-export-",
        suffix=".json",
        dir=directory,
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        return Path(handle.name)
