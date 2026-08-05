from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .models import BoundingBox
from .pipeline import AutoregConfig, run_autoregistration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate conservative proposed GCPs for a raster general plan. "
            "The tool never approves a registration."
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--locality", required=True)
    parser.add_argument("--region", default="")
    parser.add_argument("--district", default="")
    parser.add_argument("--basemap", choices=("arcgis", "osm"), default="arcgis")
    parser.add_argument("--zoom", type=int, default=15)
    parser.add_argument("--bbox-padding", type=float, default=0.05)
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Skip online bbox resolution and use explicit WGS84 bounds",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bbox = (
        BoundingBox(*args.bbox, source="cli", label=args.locality)
        if args.bbox
        else None
    )
    result = run_autoregistration(
        AutoregConfig(
            source=args.source,
            output=args.output,
            locality=args.locality,
            region=args.region,
            district=args.district,
            bbox=bbox,
            basemap=args.basemap,
            zoom=args.zoom,
            bbox_padding=args.bbox_padding,
        )
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not any(reason.startswith("pipeline_error:") for reason in result.reasons) else 2


if __name__ == "__main__":
    raise SystemExit(main())
