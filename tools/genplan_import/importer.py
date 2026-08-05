from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UrbanPlanLayer, UrbanPlanSource

from .validation import ValidatedLayer, ValidatedRelease, validate_release


class ImportConflictError(ValueError):
    """Raised when an idempotency key already exists with different content."""


@dataclass(frozen=True, slots=True)
class ImportedLayer:
    id: int
    layer_kind: str
    created: bool
    active: bool
    approved_for_search: bool


@dataclass(frozen=True, slots=True)
class ImportResult:
    release_id: str
    release_mode: str
    qa_status: str
    layers: tuple[ImportedLayer, ...]
    superseded_ids: tuple[int, ...] = ()

    @property
    def created_count(self) -> int:
        return sum(row.created for row in self.layers)

    @property
    def existing_count(self) -> int:
        return len(self.layers) - self.created_count

    @property
    def superseded_count(self) -> int:
        return len(self.superseded_ids)


def import_release(session: Session, manifest_path: Path) -> ImportResult:
    release = validate_release(Path(manifest_path))
    audit_json = _audit_json(release)
    imported: list[ImportedLayer] = []
    superseded_ids: set[int] = set()

    with session.begin():
        for vector in release.layers:
            expected = _layer_values(release, vector, audit_json)
            existing = session.scalar(
                select(UrbanPlanLayer)
                .where(
                    UrbanPlanLayer.source_sha256 == release.source_sha256,
                    UrbanPlanLayer.layer_kind == vector.layer_kind,
                    UrbanPlanLayer.purpose == release.purpose,
                    UrbanPlanLayer.region == release.region,
                    UrbanPlanLayer.district == release.district,
                    UrbanPlanLayer.locality == release.locality,
                )
                .with_for_update()
            )
            if existing is not None:
                _assert_idempotent(existing, expected)
                imported.append(
                    ImportedLayer(
                        id=existing.id,
                        layer_kind=existing.layer_kind,
                        created=False,
                        active=existing.active,
                        approved_for_search=existing.approved_for_search,
                    )
                )
                continue

            if release.active:
                stale_layers = session.scalars(
                    select(UrbanPlanLayer)
                    .where(
                        UrbanPlanLayer.source_sha256 != release.source_sha256,
                        UrbanPlanLayer.layer_kind == vector.layer_kind,
                        UrbanPlanLayer.purpose == release.purpose,
                        UrbanPlanLayer.region == release.region,
                        UrbanPlanLayer.district == release.district,
                        UrbanPlanLayer.locality == release.locality,
                        UrbanPlanLayer.title == release.title,
                        UrbanPlanLayer.active.is_(True),
                    )
                    .with_for_update()
                ).all()
                for stale in stale_layers:
                    stale.active = False
                    stale.approved_for_search = False
                    superseded_ids.add(stale.id)

            row = UrbanPlanLayer(**expected)
            session.add(row)
            session.flush()
            imported.append(
                ImportedLayer(
                    id=row.id,
                    layer_kind=row.layer_kind,
                    created=True,
                    active=row.active,
                    approved_for_search=row.approved_for_search,
                )
            )
        _update_source_registry(session, release)

    return ImportResult(
        release_id=release.release_id,
        release_mode=release.release_mode,
        qa_status=release.qa_status,
        layers=tuple(imported),
        superseded_ids=tuple(sorted(superseded_ids)),
    )


def _layer_values(
    release: ValidatedRelease,
    vector: ValidatedLayer,
    audit_json: str,
) -> dict[str, Any]:
    return {
        "region": release.region,
        "district": release.district,
        "locality": release.locality,
        "purpose": release.purpose,
        "layer_kind": vector.layer_kind,
        "zone_name": vector.zone_name,
        "title": release.title,
        "approval_document": release.approval_document,
        "approval_date": release.approval_date,
        "source_authority": release.source_authority,
        "source_url": release.source_url,
        "source_epsg": 4326,
        "source_file_name": vector.path.name,
        "source_sha256": release.source_sha256,
        "source_version": release.source_version,
        "provenance_status": release.provenance_status,
        "identity_status": release.identity_status,
        "qa_status": release.qa_status,
        "independent_review": release.independent_review,
        "qa_review_json": audit_json,
        "approved_for_search": release.approved_for_search,
        "uploaded_by": release.released_by,
        "geometry_geojson": vector.normalized_geojson,
        "active": release.active,
    }


def _audit_json(release: ValidatedRelease) -> str:
    payload = {
        "schema_version": "1.0",
        "release_id": release.release_id,
        "release_mode": release.release_mode,
        "manifest_sha256": release.manifest_sha256,
        "review_sha256": release.review_sha256,
        "provenance_sha256": release.provenance_sha256,
        "review": release.review,
        "provenance": release.provenance,
        "vector_sha256": {
            layer.layer_kind: layer.sha256 for layer in release.layers
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_idempotent(
    existing: UrbanPlanLayer,
    expected: dict[str, Any],
) -> None:
    compared_fields = (
        "zone_name",
        "title",
        "approval_document",
        "approval_date",
        "source_authority",
        "source_url",
        "source_epsg",
        "source_file_name",
        "source_version",
        "provenance_status",
        "identity_status",
        "qa_status",
        "independent_review",
        "qa_review_json",
        "approved_for_search",
        "uploaded_by",
        "geometry_geojson",
        "active",
    )
    different = [
        field for field in compared_fields if getattr(existing, field) != expected[field]
    ]
    if different:
        key = (
            existing.source_sha256,
            existing.layer_kind,
            existing.purpose,
            existing.region,
            existing.district,
            existing.locality,
        )
        raise ImportConflictError(
            "Existing UrbanPlanLayer differs for idempotency key "
            f"{key!r}; conflicting fields: {', '.join(different)}"
        )


def _update_source_registry(session: Session, release: ValidatedRelease) -> None:
    source_manifest = _load_source_manifest(release)
    if not source_manifest:
        return
    schema = str(source_manifest.get("schema_version") or "")
    if schema != "smart-geohub-source/v1":
        return
    base_url = str(source_manifest.get("base_url") or "").rstrip("/") + "/"
    collections = source_manifest.get("collections")
    if not base_url or not isinstance(collections, list):
        return
    import_status = "imported" if release.approved_for_search else "shadow_imported"
    updated_collections: set[str] = set()
    for row in collections:
        if not isinstance(row, dict):
            continue
        collection = str(row.get("collection") or "").strip()
        if not collection or collection in updated_collections:
            continue
        updated_collections.add(collection)
        source = session.scalar(
            select(UrbanPlanSource).where(
                UrbanPlanSource.platform == "smart_geohub",
                UrbanPlanSource.source_url == base_url,
                UrbanPlanSource.collections_json == json.dumps(
                    [collection],
                    ensure_ascii=False,
                ),
            )
        )
        if source is None:
            continue
        source.import_status = import_status
        if release.approved_for_search:
            source.coverage_status = "imported"
        if isinstance(row.get("feature_count"), int):
            source.layer_count = row["feature_count"]
        source.last_error = None
        source.notes = _source_registry_note(release, source.notes)


def _load_source_manifest(release: ValidatedRelease) -> dict[str, Any] | None:
    spec = release.provenance.get("source_manifest")
    if not isinstance(spec, dict):
        return None
    relative = spec.get("path")
    if not isinstance(relative, str) or not relative.strip():
        return None
    path = (release.manifest_path.parent / relative).resolve()
    if not path.is_file() or not path.is_relative_to(release.manifest_path.parent):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _source_registry_note(release: ValidatedRelease, current: str | None) -> str:
    marker = (
        f"Release {release.release_id} imported as {release.release_mode}/"
        f"{release.qa_status}; approved_for_search={release.approved_for_search}."
    )
    if not current:
        return marker
    if marker in current:
        return current
    return f"{current} {marker}"
