from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .basemap import BasemapProvider, ReferenceRaster, WebTileProvider
from .matcher import MatchThresholds, match_plan_to_reference
from .models import AutoregResult, BoundingBox
from .providers import (
    BboxResolver,
    EgknResolver,
    FallbackResolver,
    NominatimResolver,
    StaticBboxResolver,
)


@dataclass(slots=True)
class AutoregConfig:
    source: Path
    output: Path
    locality: str
    region: str = ""
    district: str = ""
    bbox: BoundingBox | None = None
    basemap: str = "arcgis"
    zoom: int = 15
    bbox_padding: float = 0.05
    resolver: BboxResolver | None = None
    basemap_provider: BasemapProvider | None = None
    thresholds: MatchThresholds | None = None


def run_autoregistration(config: AutoregConfig) -> AutoregResult:
    source = config.source.resolve()
    output = config.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = AutoregResult(
        source_path=str(source),
        source_sha256=_sha256(source),
        locality=config.locality,
    )
    try:
        bbox = config.bbox or _resolver(config).resolve(
            config.locality,
            region=config.region,
            district=config.district,
        )
        bbox = bbox.padded(config.bbox_padding)
        result.bbox = bbox
        provider = config.basemap_provider or WebTileProvider(
            source=config.basemap,
            zoom=config.zoom,
        )
        reference = provider.fetch(bbox, output)
        result.basemap_source = reference.source
        result.basemap_attribution = reference.attribution
        _save_reference(reference, output, result)

        plan = _load_plan(source)
        preview = _preview(plan)
        preview_path = output / "plan_preview.jpg"
        preview.save(preview_path, quality=88)
        result.artifacts["plan_preview"] = str(preview_path)

        outcome = match_plan_to_reference(plan, reference, config.thresholds)
        result.confidence = outcome.confidence
        result.confidence_label = outcome.confidence_label  # type: ignore[assignment]
        result.metrics = outcome.metrics
        result.proposed_gcps = outcome.gcps
        result.diagnostic_anchor_points = outcome.diagnostic_anchor_points
        result.diagnostic_anchor_guardrails = outcome.diagnostic_anchor_guardrails
        result.diagnostic_anchor_summary = outcome.diagnostic_anchor_summary
        result.reasons.extend(outcome.reasons)
        result.reasons.append("status_is_never_automatically_approved")
        if outcome.visualization is not None:
            match_path = output / "matches.jpg"
            outcome.visualization.save(match_path, quality=88)
            result.artifacts["matches"] = str(match_path)
    except Exception as exc:
        result.confidence = 0.0
        result.confidence_label = "none"
        result.reasons.extend(
            [
                f"pipeline_error:{type(exc).__name__}:{exc}",
                "manual_registration_required",
                "status_is_never_automatically_approved",
            ]
        )
    _write_result(result, output)
    return result


def _resolver(config: AutoregConfig) -> BboxResolver:
    return FallbackResolver(
        [
            EgknResolver(),
            StaticBboxResolver(),
            NominatimResolver(),
        ]
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _preview(image: Image.Image, max_dimension: int = 1800) -> Image.Image:
    preview = image.copy()
    preview.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    return preview


def _load_plan(
    source: Path,
    *,
    max_dimension: int = 4800,
    max_source_pixels: int = 500_000_000,
) -> Image.Image:
    previous_limit = Image.MAX_IMAGE_PIXELS
    # Pillow checks JPEG dimensions before draft() can request a reduced decode.
    # Keep the deterministic guard here so large non-JPEG rasters are still refused.
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(source) as image:
            width, height = image.size
            is_large_source = width * height > max_source_pixels
            if image.format == "JPEG":
                image.draft("RGB", (max_dimension, max_dimension))
            if image.size[0] * image.size[1] > max_source_pixels:
                raise ValueError(
                    f"Source raster exceeds the {max_source_pixels} pixel safety limit"
                )
            plan = image.convert("RGB")
            if is_large_source and max(plan.size) > max_dimension:
                plan.thumbnail(
                    (max_dimension, max_dimension),
                    Image.Resampling.LANCZOS,
                )
            plan.thumbnail(
                (max_dimension, max_dimension),
                Image.Resampling.LANCZOS,
            )
            return plan.copy()
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def _save_reference(
    reference: ReferenceRaster,
    output: Path,
    result: AutoregResult,
) -> None:
    path = output / "basemap.jpg"
    reference.image.save(path, quality=90)
    result.artifacts["basemap"] = str(path)


def _write_result(result: AutoregResult, output: Path) -> None:
    path = output / "result.json"
    result.artifacts["result"] = str(path)
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
