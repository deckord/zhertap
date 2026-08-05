from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from tools.genplan_autoreg.basemap import WebTileProvider
from tools.genplan_autoreg.pipeline import AutoregConfig, run_autoregistration

from .models import RunRequest


class BatchRunner(Protocol):
    def __call__(self, request: RunRequest) -> list[dict[str, Any]]: ...


class AutoregRunner:
    """Run isolated conservative attempts against both configured basemaps."""

    def __call__(self, request: RunRequest) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for basemap in request.basemaps:
            attempt_dir = Path(request.output_dir) / "attempts" / basemap
            result = run_autoregistration(
                AutoregConfig(
                    source=Path(request.source_path),
                    output=attempt_dir,
                    locality=request.locality,
                    region=request.region,
                    district=request.district,
                    basemap=basemap,
                    zoom=request.zoom,
                    bbox_padding=request.bbox_padding,
                    basemap_provider=WebTileProvider(
                        source=basemap,
                        zoom=request.zoom,
                        max_tiles=request.max_tiles,
                    ),
                )
            )
            attempts.append(
                {
                    "basemap": basemap,
                    "result": result.to_dict(),
                }
            )
        return attempts

