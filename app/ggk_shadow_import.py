from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UrbanPlanSource
from tools.genplan_ggk import BuildError, build_ggk_release
from tools.genplan_ggk.builder import CATALOG_URL
from tools.genplan_import import ImportConflictError, ReleaseValidationError, import_release

GGK_SHADOW_PROFILES = ("lph-household", "gardening", "lph-field")
GGK_SOURCE_PLATFORM = "ggk_wfs"
GGK_TERMINAL_IMPORT_STATUSES = {"imported", "shadow_imported", "blocked"}


def import_next_ggk_shadow_sources(
    session: Session,
    *,
    limit_sources: int = 2,
    profiles: tuple[str, ...] = GGK_SHADOW_PROFILES,
    output_root: Path | None = None,
    client: Any | None = None,
) -> dict[str, int]:
    """Build and import inactive shadow layers from the official AIS GGK WFS."""

    limit_sources = max(1, min(limit_sources, 20))
    output_root = output_root or Path("var/genplan/ggk-shadow")
    candidates = session.execute(
        select(
            UrbanPlanSource.id,
            UrbanPlanSource.external_id,
            UrbanPlanSource.locality,
        )
        .where(
            UrbanPlanSource.platform == GGK_SOURCE_PLATFORM,
            UrbanPlanSource.coverage_status == "digital_found",
            UrbanPlanSource.import_status.not_in(GGK_TERMINAL_IMPORT_STATUSES),
        )
        .order_by(
            UrbanPlanSource.last_checked_at.asc(),
            UrbanPlanSource.locality.asc(),
            UrbanPlanSource.id.asc(),
        )
        .limit(limit_sources)
    ).all()
    session.commit()

    stats = {
        "sources_checked": 0,
        "profiles_tried": 0,
        "releases_imported": 0,
        "layers_created": 0,
        "layers_existing": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
    }
    for source_id, external_id, locality in candidates:
        document_id = _document_id(external_id)
        if document_id is None:
            stats["skipped"] += 1
            _mark_source_blocked(
                session,
                source_id,
                "GGK source external_id is not a numeric document id",
            )
            continue

        stats["sources_checked"] += 1
        imported_for_source = 0
        failures: list[str] = []
        for profile in profiles:
            stats["profiles_tried"] += 1
            release_dir = output_root / profile / f"{document_id}-{profile}"
            try:
                build = build_ggk_release(
                    document_id,
                    profile,
                    release_dir,
                    None,
                    client=client,
                    release_mode="shadow",
                    shadow_source_url=CATALOG_URL,
                )
                result = import_release(session, build.manifest_path)
            except BuildError as exc:
                stats["blocked"] += 1
                failures.append(f"{profile}: {exc}")
                continue
            except (ImportConflictError, ReleaseValidationError, OSError, ValueError) as exc:
                stats["failed"] += 1
                failures.append(f"{profile}: {exc}")
                continue

            imported_for_source += 1
            stats["releases_imported"] += 1
            stats["layers_created"] += result.created_count
            stats["layers_existing"] += result.existing_count

        if imported_for_source == 0:
            label = locality or str(document_id)
            _mark_source_blocked(
                session,
                source_id,
                f"No importable GGK shadow release for {label}. " + " | ".join(failures),
            )

    return stats


def _document_id(external_id: object) -> int | None:
    try:
        return int(str(external_id).strip())
    except (TypeError, ValueError):
        return None


def _mark_source_blocked(session: Session, source_id: int, message: str) -> None:
    source = session.get(UrbanPlanSource, source_id)
    if source is None:
        return
    source.import_status = "blocked"
    source.last_error = message[:1000]
    session.commit()
