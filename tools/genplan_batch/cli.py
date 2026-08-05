from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .dispatcher import BatchConfig, BatchConfigurationError, run_batch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mass conservative preprocessing of genplan inventory. "
            "Automatic output is never QA-approved or STRICT."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclude-file", type=Path)
    parser.add_argument("--region", default="")
    parser.add_argument("--district", default="")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-tiles", type=int, default=64)
    parser.add_argument("--min-free-disk-gb", type=float, default=5.0)
    parser.add_argument("--max-output-gb", type=float, default=20.0)
    parser.add_argument(
        "--basemaps",
        nargs="+",
        choices=("arcgis", "osm"),
        default=("arcgis", "osm"),
    )
    parser.add_argument("--zoom", type=int, default=15)
    parser.add_argument("--bbox-padding", type=float, default=0.05)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_batch(
            BatchConfig(
                manifest=args.manifest,
                output=args.output,
                exclude_file=args.exclude_file,
                region=args.region,
                district=args.district,
                limit=args.limit,
                dry_run=args.dry_run,
                resume=args.resume,
                workers=args.workers,
                max_tiles=args.max_tiles,
                min_free_disk_bytes=int(args.min_free_disk_gb * 1024**3),
                max_output_bytes=int(args.max_output_gb * 1024**3),
                basemaps=tuple(args.basemaps),
                zoom=args.zoom,
                bbox_padding=args.bbox_padding,
            )
        )
    except BatchConfigurationError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result.summary, ensure_ascii=False, indent=2))
    return 2 if result.failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
