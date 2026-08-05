from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .engine import ReviewInputError, review_georeferencing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="genplan-review",
        description="Independently review an A1 genplan georeferencing as A2.",
    )
    parser.add_argument("--gcps", required=True, type=Path, help="A1 gcps.json")
    parser.add_argument("--qa", required=True, type=Path, help="A1 qa.json")
    parser.add_argument(
        "--checkpoints",
        required=True,
        type=Path,
        help="Independent A2 checkpoints.json",
    )
    parser.add_argument(
        "--provenance",
        required=True,
        type=Path,
        help="Document provenance.json",
    )
    parser.add_argument(
        "--legend",
        required=True,
        type=Path,
        help="Legend and visual evidence JSON",
    )
    parser.add_argument("--output", required=True, type=Path, help="New review.json path")
    parser.add_argument("--compact", action="store_true", help="Write compact JSON")
    return parser


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReviewInputError(f"{path} must contain a JSON object")
    return payload


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(text)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = review_georeferencing(
            _load(args.gcps),
            _load(args.qa),
            _load(args.checkpoints),
            _load(args.provenance),
            _load(args.legend),
        )
        rendered = result.model_dump_json(indent=None if args.compact else 2)
        _atomic_write(args.output, rendered)
    except (OSError, json.JSONDecodeError, ReviewInputError, ValueError) as exc:
        print(f"genplan-review: {exc}", file=sys.stderr)
        return 2
    return 0
