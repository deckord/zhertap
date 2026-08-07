from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .segmentation import VectorizeError, vectorize_raster


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Vectorize allowed/prohibited/red_line layers from a reviewed "
            "georeferenced genplan raster using an approved legend.json."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Reviewed GeoTIFF/COG produced by tools.genplan_export",
    )
    parser.add_argument("--legend", required=True, type=Path, help="Approved legend.json")
    parser.add_argument(
        "--provenance",
        type=Path,
        help=(
            "Optional provenance.json from tools.genplan_export. When given, "
            "legend.json is bound to the raster through the original document's "
            "SHA-256 instead of the exported raster's SHA-256 directly."
        ),
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = vectorize_raster(
            source_path=args.source,
            legend_path=args.legend,
            output_dir=args.output_dir,
            provenance_path=args.provenance,
            overwrite=args.overwrite,
        )
    except VectorizeError as exc:
        print(f"genplan-vectorize: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "workflow_status": "proposed",
                "manifest": str(result.manifest_path),
                "chain_sha256": result.chain_sha256,
                "feature_counts": result.feature_counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
