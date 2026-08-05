from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .builder import count_collection, export_collection, summarize_collection
from .client import SmartGeoHubClientError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a Smart GeoHub urban-plan collection as a review candidate."
    )
    parser.add_argument("--base-url", required=True, help="Portal base URL")
    parser.add_argument("--collection", required=True, help="Smart GeoHub collection id")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--context-admterr-id", default="kz")
    parser.add_argument("--max-features", default=10_000, type=int)
    parser.add_argument(
        "--geometry-workers",
        default=1,
        type=int,
        help="Parallel geometry fetch workers for full export",
    )
    parser.add_argument("--operator", default="smart-geohub-exporter")
    parser.add_argument(
        "--search-field",
        action="append",
        default=[],
        help="Smart GeoHub list search field. Repeat with --search-text.",
    )
    parser.add_argument(
        "--search-text",
        action="append",
        default=[],
        help="Smart GeoHub list search text. Repeat with --search-field.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only scan list properties and write summary-manifest.json",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Only read the API total and write count-manifest.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.search_field) != len(args.search_text):
        print("--search-field and --search-text must be provided in pairs", file=sys.stderr)
        return 2
    search = dict(zip(args.search_field, args.search_text, strict=True))
    if args.summary_only and args.count_only:
        print("--summary-only and --count-only cannot be used together", file=sys.stderr)
        return 2
    try:
        if args.count_only:
            count = count_collection(
                base_url=args.base_url,
                collection=args.collection,
                output_dir=args.output_dir,
                context_admterr_id=args.context_admterr_id,
                operator=args.operator,
                search=search,
            )
            print(
                json.dumps(
                    {
                        "manifest": str(count.manifest_path),
                        "collection": count.collection,
                        "feature_count": count.feature_count,
                        "database_written": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.summary_only:
            summary = summarize_collection(
                base_url=args.base_url,
                collection=args.collection,
                output_dir=args.output_dir,
                context_admterr_id=args.context_admterr_id,
                max_features=args.max_features,
                operator=args.operator,
                search=search,
            )
            print(
                json.dumps(
                    {
                        "manifest": str(summary.manifest_path),
                        "collection": summary.collection,
                        "feature_count": summary.feature_count,
                        "truncated_by_limit": summary.truncated_by_limit,
                        "database_written": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        result = export_collection(
            base_url=args.base_url,
            collection=args.collection,
            output_dir=args.output_dir,
            context_admterr_id=args.context_admterr_id,
            max_features=args.max_features,
            operator=args.operator,
            search=search,
            geometry_workers=args.geometry_workers,
        )
    except SmartGeoHubClientError as exc:
        print(f"Smart GeoHub export blocked: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Smart GeoHub export failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "geojson": str(result.geojson_path),
                "collection": result.collection,
                "feature_count": result.feature_count,
                "source_sha256": result.source_sha256,
                "geometry_types": result.geometry_types,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
