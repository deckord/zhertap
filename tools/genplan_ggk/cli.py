from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .builder import (
    PROFILE_CONFIG,
    BuildError,
    build_ggk_release,
    list_ggk_documents,
)
from .client import GgkClientError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and build official urban-plan releases from the AIS GGK WFS."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help="List public general-plan documents")
    catalog.add_argument("--city", default="", help="Optional case-insensitive locality filter")

    build = subparsers.add_parser("build", help="Build one independently reviewed release")
    build.add_argument("--document-id", required=True, type=int)
    build.add_argument("--profile", required=True, choices=sorted(PROFILE_CONFIG))
    build.add_argument("--output-dir", required=True, type=Path)
    build.add_argument("--review", type=Path)
    build.add_argument(
        "--shadow",
        action="store_true",
        help="Build an inactive WARNING release candidate without enabling search.",
    )
    build.add_argument(
        "--source-url",
        default="",
        help="Official source URL to store in a shadow release.",
    )
    build.add_argument("--operator", default="ggk-wfs-operator")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "catalog":
            rows = list_ggk_documents()
            city = args.city.strip().casefold()
            if city:
                rows = [row for row in rows if city in row["locality"].casefold()]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0

        result = build_ggk_release(
            args.document_id,
            args.profile,
            args.output_dir,
            args.review,
            operator=args.operator,
            release_mode="shadow" if args.shadow else "search",
            shadow_source_url=args.source_url,
        )
    except (BuildError, GgkClientError) as exc:
        print(f"AIS GGK release blocked: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "release_id": result.release_id,
                "document_id": result.document_id,
                "source_sha256": result.source_sha256,
                "scope": result.scope,
                "layer_counts": result.layer_counts,
                "layer_sha256": result.layer_sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
