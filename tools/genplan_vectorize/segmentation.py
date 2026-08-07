from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from .models import (
    LAYER_KINDS,
    LegendDocument,
    LegendEntry,
    VectorizeConfigError,
    load_legend_document,
)

RASTERIO_INSTALL_HINT = (
    'Rasterio is required for genplan vectorization. Install it with: '
    'python -m pip install "rasterio>=1.3,<1.5"'
)


class VectorizeError(ValueError):
    """Raised when a reviewed raster cannot be vectorized safely."""


class RasterioDependencyError(VectorizeError):
    """Raised when the optional rasterio dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class VectorizeResult:
    manifest_path: Path
    layer_paths: dict[str, Path]
    feature_counts: dict[str, int]
    chain_sha256: str


@dataclass(frozen=True, slots=True)
class _RasterModules:
    rasterio: ModuleType
    features: ModuleType
    warp: ModuleType


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def chain_sha256(hex_digests: list[str]) -> str:
    digest = hashlib.sha256()
    for value in hex_digests:
        digest.update(value.encode("ascii"))
    return digest.hexdigest()


def _check_against_provenance(
    *,
    provenance_path: Path,
    legend: LegendDocument,
    source_sha: str,
) -> None:
    """Bind legend.json to the exported raster through genplan_export's provenance.json.

    legend.json (like gcps.json/qa.json/checkpoints.json) always references
    the SHA-256 of the original raw document, not the rectified GeoTIFF. This
    lets a legend approved against `GenplanSourceDocument.source_sha256`
    bind correctly to whichever exported raster provenance.json says came
    from that same original document.
    """
    if not provenance_path.is_file():
        raise VectorizeError(f"Provenance not found: {provenance_path}")
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VectorizeError(f"Could not read provenance.json: {exc}") from exc
    if not isinstance(provenance, dict):
        raise VectorizeError("provenance.json root must be an object")

    if str(provenance.get("record_id", "")) != legend.record_id:
        raise VectorizeError(
            "provenance.json record_id does not match legend.json record_id"
        )
    original_sha = str(provenance.get("inputs", {}).get("source", {}).get("sha256", "")).lower()
    if original_sha != legend.source_sha256:
        raise VectorizeError(
            "legend.json source_sha256 does not match provenance.json's original "
            f"source hash (legend={legend.source_sha256}, provenance={original_sha})"
        )
    exported_sha = str(provenance.get("output", {}).get("sha256", "")).lower()
    if exported_sha != source_sha:
        raise VectorizeError(
            "Source raster does not match the raster provenance.json describes "
            f"as exported for this document (provenance={exported_sha}, raster={source_sha})"
        )


def vectorize_raster(
    *,
    source_path: Path,
    legend_path: Path,
    output_dir: Path,
    provenance_path: Path | None = None,
    overwrite: bool = False,
) -> VectorizeResult:
    """Segment a reviewed georeferenced raster into candidate GeoJSON layers.

    The result always carries workflow_status=proposed: this module never
    approves a release, it only produces a candidate for tools.genplan_review
    and an independent tools.genplan_import gate.
    """
    source = source_path.expanduser().resolve()
    legend_file = legend_path.expanduser().resolve()
    if not source.is_file():
        raise VectorizeError(f"Source raster not found: {source}")
    if not legend_file.is_file():
        raise VectorizeError(f"Legend not found: {legend_file}")

    try:
        legend = load_legend_document(legend_file)
    except VectorizeConfigError as exc:
        raise VectorizeError(str(exc)) from exc

    source_sha = sha256_file(source)
    if provenance_path is not None:
        _check_against_provenance(
            provenance_path=provenance_path.expanduser().resolve(),
            legend=legend,
            source_sha=source_sha,
        )
    elif source_sha != legend.source_sha256:
        raise VectorizeError(
            "legend.json source_sha256 does not match the source raster; the "
            "approved legend was reviewed for a different file "
            f"(legend={legend.source_sha256}, raster={source_sha}). Pass "
            "--provenance from tools.genplan_export if legend.json references "
            "the original document instead of the exported raster."
        )

    grouped = legend.usable_entries_by_layer_kind()
    if not any(grouped.values()):
        raise VectorizeError(
            "legend.json has no approved entries mapped to allowed/prohibited/red_line"
        )

    output = output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    legend_sha = sha256_file(legend_file)

    modules = _load_rasterio()
    layer_paths: dict[str, Path] = {}
    layer_hashes: dict[str, str] = {}
    feature_counts: dict[str, int] = {}
    with modules.rasterio.open(source) as dataset:
        if dataset.count < 3:
            raise VectorizeError("Source raster must contain at least RGB bands")
        if dataset.crs is None:
            raise VectorizeError("Source raster must be georeferenced")
        rgb = dataset.read([1, 2, 3])
        source_crs = dataset.crs.to_string()
        for layer_kind in LAYER_KINDS:
            path = output / f"{layer_kind}.geojson"
            if path.exists() and not overwrite:
                raise VectorizeError(f"Output already exists: {path}")
            collection = _collection_for_layer(
                modules,
                dataset=dataset,
                rgb=rgb,
                entries=grouped[layer_kind],
                layer_kind=layer_kind,
                source_crs=source_crs,
                legend=legend,
            )
            rendered = json.dumps(collection, ensure_ascii=False, sort_keys=True)
            path.write_text(rendered, encoding="utf-8")
            layer_paths[layer_kind] = path
            layer_hashes[layer_kind] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            feature_counts[layer_kind] = len(collection["features"])

    manifest_path = output / "manifest.json"
    if manifest_path.exists() and not overwrite:
        raise VectorizeError(f"Output already exists: {manifest_path}")

    chain = chain_sha256([source_sha, legend_sha, *(layer_hashes[kind] for kind in LAYER_KINDS)])
    manifest = {
        "schema_version": "genplan-vectorize-manifest/v1",
        "workflow_status": "proposed",
        "record_id": legend.record_id,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_raster": {"path": str(source), "sha256": source_sha},
        "legend": {
            "path": str(legend_file),
            "sha256": legend_sha,
            "record_id": legend.record_id,
            "reviewer_id": legend.reviewer_id,
            "reviewed_at_utc": legend.reviewed_at_utc.isoformat(),
        },
        "layers": {
            layer_kind: {
                "path": layer_paths[layer_kind].name,
                "sha256": layer_hashes[layer_kind],
                "feature_count": feature_counts[layer_kind],
            }
            for layer_kind in LAYER_KINDS
        },
        "chain_sha256": chain,
        "warning": (
            "Vectorized layers are proposed candidates only. They require "
            "independent review and tools.genplan_import before they can "
            "become a VERIFIED_STRICT/search release."
        ),
    }
    rendered_manifest = json.dumps(manifest, ensure_ascii=False, indent=2)
    manifest_path.write_text(rendered_manifest + "\n", encoding="utf-8")

    return VectorizeResult(
        manifest_path=manifest_path,
        layer_paths=layer_paths,
        feature_counts=feature_counts,
        chain_sha256=chain,
    )


def _load_rasterio() -> _RasterModules:
    try:
        return _RasterModules(
            rasterio=importlib.import_module("rasterio"),
            features=importlib.import_module("rasterio.features"),
            warp=importlib.import_module("rasterio.warp"),
        )
    except ImportError as exc:
        raise RasterioDependencyError(RASTERIO_INSTALL_HINT) from exc


def _collection_for_layer(
    modules: _RasterModules,
    *,
    dataset: Any,
    rgb: Any,
    entries: list[LegendEntry],
    layer_kind: str,
    source_crs: str,
    legend: LegendDocument,
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    for entry in entries:
        mask = _mask_for_entry(rgb, entry)
        if legend.min_area_px:
            mask = modules.features.sieve(mask.astype("uint8"), size=legend.min_area_px)
        else:
            mask = mask.astype("uint8")
        for geometry, value in modules.features.shapes(
            mask,
            mask=mask.astype("bool"),
            transform=dataset.transform,
        ):
            if int(value) != 1:
                continue
            geometry_4326 = modules.warp.transform_geom(
                source_crs,
                "EPSG:4326",
                geometry,
                precision=7,
            )
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "record_id": legend.record_id,
                        "layer_kind": layer_kind,
                        "target_category": entry.target_category,
                        "zone_name": entry.label_ru or entry.target_category,
                        "color_hex": entry.color_hex,
                        "workflow_status": "proposed",
                    },
                    "geometry": geometry_4326,
                }
            )
    features.sort(key=lambda feature: json.dumps(feature["geometry"], sort_keys=True))
    return {
        "type": "FeatureCollection",
        "name": f"{legend.record_id}-{layer_kind}",
        "features": features,
    }


def _mask_for_entry(rgb: Any, entry: LegendEntry) -> Any:
    import numpy as np

    return (
        (np.abs(rgb[0].astype("int16") - entry.red) <= entry.tolerance)
        & (np.abs(rgb[1].astype("int16") - entry.green) <= entry.tolerance)
        & (np.abs(rgb[2].astype("int16") - entry.blue) <= entry.tolerance)
    )
