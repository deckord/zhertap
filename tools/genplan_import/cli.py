from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.db import init_db

from .importer import ImportConflictError, import_release
from .validation import ReleaseValidationError, validate_release


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely import an independently reviewed genplan vector release."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Path to release-manifest.json",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="SQLAlchemy database URL (default: application DATABASE_URL)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the complete release without writing to the database",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dry_run:
            release = validate_release(args.manifest)
            payload = {
                "release_id": release.release_id,
                "release_mode": release.release_mode,
                "qa_status": release.qa_status,
                "approved_for_search": release.approved_for_search,
                "layers": [row.layer_kind for row in release.layers],
                "database_written": False,
            }
        else:
            connect_args = (
                {"check_same_thread": False}
                if args.database_url.startswith("sqlite")
                else {}
            )
            engine = create_engine(
                args.database_url,
                pool_pre_ping=True,
                connect_args=connect_args,
            )
            init_db(bind=engine)
            with Session(engine, expire_on_commit=False) as session:
                result = import_release(session, args.manifest)
            payload = {
                "release_id": result.release_id,
                "release_mode": result.release_mode,
                "qa_status": result.qa_status,
                "created": result.created_count,
                "existing": result.existing_count,
                "superseded": result.superseded_count,
                "superseded_ids": list(result.superseded_ids),
                "layers": [
                    {
                        "id": row.id,
                        "layer_kind": row.layer_kind,
                        "created": row.created,
                        "active": row.active,
                        "approved_for_search": row.approved_for_search,
                    }
                    for row in result.layers
                ],
                "database_written": True,
            }
    except (ReleaseValidationError, ImportConflictError) as exc:
        print(f"Import blocked: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Import failed; transaction rolled back: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0
