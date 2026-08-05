from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .models import ColorRule, VectorizeConfig, load_config

RASTERIO_INSTALL_HINT = (
    'Rasterio is required for genplan vectorization. Install it with: '
    'python -m pip install "rasterio>=1.3,<1.5"'
)


class VectorizeError(ValueError):
    pass


class RasterioDependencyError(VectorizeError):
    pass


@dataclass(frozen=True, slots=True)
class VectorizeResult:
    manifest_path: Path
    layer_paths: tuple[Path, ...]
    feature_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _RasterModules:
    rasterio: ModuleType
    features: ModuleType
    warp: ModuleType


def vectorize_geotiff(
    *,
    source_path: Path,
    config_path: Path,
    output_dir: Path,
    overwrite: bool = False,
) -> VectorizeResult:
    config = load_config(config_path)
    source = source_path.expanduser().resolve()
    output = output_dir.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise VectorizeError(f"Source raster not found: {source}")
    output.mkdir(parents=True, exist_ok=True)
    modules = _load_rasterio()
    layer_paths: list[Path] = []
    feature_counts: dict[str, int] = {}
    with modules.rasterio.open(source) as dataset:
        if dataset.count < 3:
            raise VectorizeError("Source raster must contain at least RGB bands")
        if dataset.crs is None:
            raise VectorizeError("Source raster must be georeferenced")
        rgb = dataset.read([1, 2, 3])
        for rule in config.rules:
            collection = _features_for_rule(
                modules,
                dataset=dataset,
                rgb=rgb,
                rule=rule,
                config=config,
                source=source,
            )
            path = output / f"{rule.layer_kind}.geojson"
            if path.exists() and not overwrite:
                raise VectorizeError(f"Output already exists: {path}")
            path.write_text(
                json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            layer_paths.append(path)
            feature_counts[rule.layer_kind] = len(collection["features"])
    manifest_path = output / "vectorize-manifest.json"
    if manifest_path.exists() and not overwrite:
        raise VectorizeError(f"Output already exists: {manifest_path}")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "genplan-vectorize-result/v1",
                "release_id": config.release_id,
                "source_raster": str(source),
                "config": str(config_path.expanduser().resolve()),
                "layers": {
                    path.stem: {
                        "path": path.name,
                        "feature_count": feature_counts.get(path.stem, 0),
                    }
                    for path in layer_paths
                },
                "warning": (
                    "Vectorized layers are candidates only. They must pass "
                    "independent QA and tools.genplan_import before client search."
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return VectorizeResult(
        manifest_path=manifest_path,
        layer_paths=tuple(layer_paths),
        feature_counts=feature_counts,
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


def _features_for_rule(
    modules: _RasterModules,
    *,
    dataset: Any,
    rgb: Any,
    rule: ColorRule,
    config: VectorizeConfig,
    source: Path,
) -> dict[str, Any]:
    mask = _mask_for_rule(rgb, rule)
    if rule.sieve_pixels:
        mask = modules.features.sieve(mask, size=rule.sieve_pixels)
    features: list[dict[str, Any]] = []
    source_crs = dataset.crs.to_string()
    for geometry, value in modules.features.shapes(
        mask.astype("uint8"),
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
                    "release_id": config.release_id,
                    "source_title": config.source_title,
                    "source_raster": source.name,
                    "layer_kind": rule.layer_kind,
                    "zone_name": rule.zone_name,
                    "vectorize_status": "candidate_needs_qa",
                },
                "geometry": geometry_4326,
            }
        )
    return {
        "type": "FeatureCollection",
        "name": f"{config.release_id}-{rule.layer_kind}",
        "features": features,
    }


def _mask_for_rule(rgb: Any, rule: ColorRule) -> Any:
    import numpy as np

    mask = np.zeros(rgb.shape[1:], dtype=bool)
    for red, green, blue in rule.colors:
        color_match = (
            (np.abs(rgb[0].astype("int16") - red) <= rule.tolerance)
            & (np.abs(rgb[1].astype("int16") - green) <= rule.tolerance)
            & (np.abs(rgb[2].astype("int16") - blue) <= rule.tolerance)
        )
        mask |= color_match
    return mask

