from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .exporter import ExportError, export_sheet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export an independently reviewed genplan raster as WGS84 COG/GeoTIFF."
        )
    )
    parser.add_argument("--source", required=True, type=Path, help="JPG/PNG/TIFF raster")
    parser.add_argument("--gcps", required=True, type=Path, help="Workbench gcps.json")
    parser.add_argument("--qa", required=True, type=Path, help="Workbench qa.json")
    parser.add_argument(
        "--review",
        required=True,
        type=Path,
        help="Separate independent review JSON",
    )
    parser.add_argument("--output", required=True, type=Path, help="Output .tif path")
    parser.add_argument(
        "--provenance",
        type=Path,
        help="Provenance JSON path (default: provenance.json next to output)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output and provenance report",
    )
    parser.add_argument(
        "--max-output-pixels",
        type=int,
        default=250_000_000,
        help="Safety limit for the rectified raster (default: 250000000)",
    )
    parser.add_argument(
        "--no-cog",
        action="store_true",
        help="Always create a tiled GeoTIFF instead of attempting COG",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = export_sheet(
            source_path=args.source,
            gcps_path=args.gcps,
            qa_path=args.qa,
            review_path=args.review,
            output_path=args.output,
            provenance_path=args.provenance,
            overwrite=args.overwrite,
            max_output_pixels=args.max_output_pixels,
            prefer_cog=not args.no_cog,
        )
    except ExportError as exc:
        print(f"Export blocked: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "record_id": result.record_id,
                "review_status": result.review_status,
                "format": result.output_format,
                "output": str(result.output_path),
                "output_sha256": result.output_sha256,
                "provenance": str(result.provenance_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
