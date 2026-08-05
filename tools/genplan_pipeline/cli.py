from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import PipelineConfig, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="genplan-pipeline",
        description="Inventory and safely extract scanned Kazakhstan urban plans.",
    )
    parser.add_argument(
        "command",
        choices=["run", "inventory"],
        help="Run the inventory pipeline",
    )
    parser.add_argument("--source", type=Path, required=True, help="Directory with ZIP/PDF/JPG")
    parser.add_argument("--output", type=Path, required=True, help="Pipeline workspace")
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Inventory archives and loose files without extracting ZIP members",
    )
    parser.add_argument(
        "--input-mode",
        choices=["auto", "archives", "extracted"],
        default="auto",
        help="Input layout; auto recognizes the extracted directory",
    )
    parser.add_argument("--aliases", type=Path, help="Optional normalization aliases JSON")
    parser.add_argument(
        "--egkn-catalog",
        type=Path,
        help="Live EGKN catalog JSON; auto-detected at ../work/egkn_catalog.json",
    )
    parser.add_argument(
        "--max-member-gib",
        type=float,
        default=5.0,
        help="Maximum uncompressed size of one ZIP member (default: 5 GiB)",
    )
    parser.add_argument(
        "--max-archive-gib",
        type=float,
        default=25.0,
        help="Maximum total uncompressed size of one ZIP (default: 25 GiB)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = PipelineConfig(
        source=args.source,
        output=args.output,
        extract=not args.no_extract,
        input_mode=args.input_mode,
        aliases_path=args.aliases,
        egkn_catalog_path=args.egkn_catalog,
        max_member_bytes=int(args.max_member_gib * 1024**3),
        max_archive_uncompressed_bytes=int(args.max_archive_gib * 1024**3),
    )
    try:
        result = run_pipeline(config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(f"Archives: {result.archive_count}")
    print(f"Assets: {result.asset_count}")
    print(f"Errors/warnings: {result.error_count}")
    print(f"Manifests: {result.output / 'manifests'}")
    return 1 if result.error_count else 0
