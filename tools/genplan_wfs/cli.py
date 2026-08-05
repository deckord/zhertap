from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .builder import BuildError, build_shymkent_release


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build an independently reviewed Shymkent genplan release from WFS snapshots."
    )
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--operator", default="genplan-wfs-operator")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_shymkent_release(
            args.source_dir,
            args.output_dir,
            args.review,
            operator=args.operator,
        )
    except BuildError as exc:
        print(f"WFS release blocked: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "source_sha256": result.source_sha256,
                "layer_counts": result.layer_counts,
                "layer_sha256": result.layer_sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
