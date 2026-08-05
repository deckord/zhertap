from __future__ import annotations

import argparse
from pathlib import Path

from .vectorize import VectorizeError, vectorize_geotiff


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Vectorize color classes from a reviewed georeferenced genplan raster."
    )
    parser.add_argument("--source", required=True, type=Path, help="Reviewed GeoTIFF/COG")
    parser.add_argument("--config", required=True, type=Path, help="Color-class JSON config")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = vectorize_geotiff(
            source_path=args.source,
            config_path=args.config,
            output_dir=args.output_dir,
            overwrite=args.overwrite,
        )
    except VectorizeError as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"manifest: {result.manifest_path}")
    for layer, count in sorted(result.feature_counts.items()):
        print(f"{layer}: {count} features")
    return 0

