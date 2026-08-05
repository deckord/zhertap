from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from .engine import compare_candidate
from .models import ComparisonRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shadow-compare a land candidate with reviewed genplan layers."
    )
    parser.add_argument("--input", required=True, type=Path, help="Request JSON path.")
    parser.add_argument("--output", type=Path, help="Write decision JSON to this path.")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of indented JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        request = ComparisonRequest.model_validate(payload)
        result = compare_candidate(request)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        print(f"genplan-shadow: {exc}", file=sys.stderr)
        return 2

    indent = None if args.compact else 2
    rendered = result.model_dump_json(indent=indent)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0
