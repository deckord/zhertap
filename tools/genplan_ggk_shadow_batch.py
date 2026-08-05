from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from tools.genplan_ggk import BuildError, build_ggk_release, list_ggk_documents
from tools.genplan_ggk.builder import CATALOG_URL, PROFILE_CONFIG


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build inactive AIS GGK shadow release candidates in bulk."
    )
    parser.add_argument("--profile", required=True, choices=sorted(PROFILE_CONFIG))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--source-url",
        default=CATALOG_URL,
        help="Official URL stored in shadow release metadata.",
    )
    parser.add_argument(
        "--skip-document-id",
        action="append",
        default=[],
        help="Document id to skip; repeatable.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional limit for a pilot batch.",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Where to write JSON summary. Defaults to output-dir/summary.json.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.summary or output_dir / f"{args.profile}-summary.json"
    skip_ids = {int(item) for item in args.skip_document_id}
    rows = [
        row
        for row in list_ggk_documents()
        if int(row["id"]) not in skip_ids
    ]
    if args.limit > 0:
        rows = rows[: args.limit]

    results: list[dict[str, object]] = []
    for row in rows:
        document_id = int(row["id"])
        locality = str(row.get("locality") or "")
        release_dir = output_dir / f"{document_id}-{args.profile}"
        try:
            result = build_ggk_release(
                document_id,
                args.profile,
                release_dir,
                None,
                release_mode="shadow",
                shadow_source_url=args.source_url,
            )
        except BuildError as exc:
            results.append(
                {
                    "document_id": document_id,
                    "locality": locality,
                    "status": "blocked",
                    "reason": str(exc),
                }
            )
            continue
        results.append(
            {
                "document_id": document_id,
                "locality": locality,
                "status": "built",
                "manifest": str(result.manifest_path),
                "release_id": result.release_id,
                "scope": result.scope,
                "layer_counts": result.layer_counts,
            }
        )
        _write_json(summary_path, {"profile": args.profile, "results": results})

    payload = {"profile": args.profile, "results": results}
    _write_json(summary_path, payload)
    print(json.dumps(_totals(payload), ensure_ascii=False, indent=2))
    return 0


def _totals(payload: dict[str, object]) -> dict[str, object]:
    results = payload["results"]
    assert isinstance(results, list)
    built = sum(1 for row in results if row.get("status") == "built")
    blocked = sum(1 for row in results if row.get("status") == "blocked")
    return {
        "profile": payload["profile"],
        "built": built,
        "blocked": blocked,
        "summary": len(results),
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
