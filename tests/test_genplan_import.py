from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import UrbanPlanLayer
from tools.genplan_import import (
    ImportConflictError,
    ReleaseValidationError,
    import_release,
    validate_release,
)
from tools.genplan_import.cli import main


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _polygon(offset: float = 0.0) -> dict[str, Any]:
    return {
        "type": "Polygon",
        "coordinates": [[
            [70.0 + offset, 53.0],
            [70.2 + offset, 53.0],
            [70.2 + offset, 53.2],
            [70.0 + offset, 53.2],
            [70.0 + offset, 53.0],
        ]],
    }


def _red_line() -> dict[str, Any]:
    return {
        "type": "LineString",
        "coordinates": [[70.05, 53.05], [70.15, 53.15]],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def make_release(
    root: Path,
    *,
    status: str = "VERIFIED_STRICT",
    release_mode: str = "search",
    provenance_status: str = "verified_official",
    identity_status: str = "matched",
    independent_review: bool = True,
    reviewer_role: str = "A2",
    source_url: str = "https://www.gov.kz/memleket/entities/aqmola",
    source_sha: str = "a" * 64,
    source_version: str = "2026-07-23",
    released_by: str = "release-operator",
    allow_shadow: bool | None = False,
    geometry_override: dict[str, Any] | None = None,
    mutate_review: Callable[[dict[str, Any]], None] | None = None,
    mutate_provenance: Callable[[dict[str, Any]], None] | None = None,
    mutate_manifest: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    root.mkdir()
    geometry = {
        "allowed": geometry_override or _polygon(),
        "prohibited": _polygon(0.3),
        "red_line": _red_line(),
    }
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for kind, payload in geometry.items():
        path = root / f"{kind}.geojson"
        _write_json(path, payload)
        paths[kind] = path
        hashes[kind] = _sha(path)

    review: dict[str, Any] = {
        "release_id": "burabay-2026-v1",
        "source_sha256": source_sha,
        "status": status,
        "independent_review": independent_review,
        "reviewer_role": reviewer_role,
        "reviewer": "reviewer-a2",
        "operator": "vector-operator-a1",
        "reviewed_at_utc": "2026-07-23T10:30:00+00:00",
        "allow_shadow": allow_shadow,
        "layer_sha256": hashes,
    }
    if mutate_review:
        mutate_review(review)
    review_path = root / "review.json"
    _write_json(review_path, review)

    provenance: dict[str, Any] = {
        "release_id": "burabay-2026-v1",
        "source_sha256": source_sha,
        "review_sha256": _sha(review_path),
        "provenance_status": provenance_status,
        "identity_status": identity_status,
        "official_url": source_url,
        "layers": {
            kind: {"sha256": layer_sha} for kind, layer_sha in hashes.items()
        },
    }
    if mutate_provenance:
        mutate_provenance(provenance)
    provenance_path = root / "provenance.json"
    _write_json(provenance_path, provenance)

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "release_id": "burabay-2026-v1",
        "release_mode": release_mode,
        "source_sha256": source_sha,
        "source_version": source_version,
        "source_epsg": 4326,
        "released_by": released_by,
        "purpose": "all",
        "scope": {
            "region": "Акмолинская область (01)",
            "district": "р-н. Бурабайский (01-171)",
            "locality": "Бурабай",
        },
        "document": {
            "title": "Генеральный план Бурабая",
            "approval_document": "Решение маслихата № 1",
            "approval_date": "2026-01-15",
            "source_authority": "Акимат Акмолинской области",
            "source_url": source_url,
        },
        "review": {"path": review_path.name, "sha256": _sha(review_path)},
        "provenance": {
            "path": provenance_path.name,
            "sha256": _sha(provenance_path),
        },
        "layers": {
            kind: {
                "path": paths[kind].name,
                "sha256": hashes[kind],
                "zone_name": f"{kind} zone",
            }
            for kind in ("allowed", "prohibited", "red_line")
        },
    }
    if mutate_manifest:
        mutate_manifest(manifest)
    manifest_path = root / "release-manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def build_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def row_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(UrbanPlanLayer)) or 0


@pytest.mark.parametrize("status", ["STRICT", "VERIFIED_STRICT"])
def test_strict_release_imports_all_layers_for_search(
    tmp_path: Path,
    status: str,
) -> None:
    manifest = make_release(tmp_path / "release", status=status)
    engine = build_engine()

    with Session(engine, expire_on_commit=False) as session:
        result = import_release(session, manifest)

    assert result.created_count == 3
    assert result.existing_count == 0
    with Session(engine) as session:
        layers = session.scalars(
            select(UrbanPlanLayer).order_by(UrbanPlanLayer.layer_kind)
        ).all()
    assert {row.layer_kind for row in layers} == {
        "allowed",
        "prohibited",
        "red_line",
    }
    assert all(row.provenance_status == "verified_official" for row in layers)
    assert all(row.identity_status == "matched" for row in layers)
    assert all(row.qa_status == status for row in layers)
    assert all(row.independent_review for row in layers)
    assert all(row.approved_for_search for row in layers)
    assert all(row.active for row in layers)
    assert all(row.source_epsg == 4326 for row in layers)


def test_warning_imports_only_as_inactive_shadow(tmp_path: Path) -> None:
    manifest = make_release(
        tmp_path / "release",
        status="WARNING",
        release_mode="shadow",
        allow_shadow=True,
    )
    engine = build_engine()

    with Session(engine, expire_on_commit=False) as session:
        result = import_release(session, manifest)

    assert result.qa_status == "WARNING"
    assert all(not row.active for row in result.layers)
    assert all(not row.approved_for_search for row in result.layers)
    with Session(engine) as session:
        layers = session.scalars(select(UrbanPlanLayer)).all()
    assert len(layers) == 3
    assert all(not row.active and not row.approved_for_search for row in layers)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": "PENDING"}, "review status"),
        ({"provenance_status": "unknown"}, "verified_official"),
        ({"identity_status": "ambiguous"}, "matched"),
        ({"independent_review": False}, "independent_review"),
        ({"reviewer_role": "A1"}, "A2"),
        ({"source_url": "http://www.gov.kz/example"}, "HTTPS"),
    ],
)
def test_untrusted_release_is_rejected_without_database_writes(
    tmp_path: Path,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    manifest = make_release(tmp_path / "release", **kwargs)
    engine = build_engine()

    with Session(engine) as session:
        with pytest.raises(ReleaseValidationError, match=message):
            import_release(session, manifest)
        assert row_count(session) == 0


def test_warning_cannot_activate_search(tmp_path: Path) -> None:
    manifest = make_release(
        tmp_path / "release",
        status="WARNING",
        release_mode="search",
        allow_shadow=True,
    )
    with pytest.raises(ReleaseValidationError, match="only be imported in shadow"):
        validate_release(manifest)


def test_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    manifest = make_release(tmp_path / "release")
    allowed = manifest.parent / "allowed.geojson"
    allowed.write_text(allowed.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ReleaseValidationError, match="does not match"):
        validate_release(manifest)


def test_paths_cannot_escape_release_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    _write_json(outside, _polygon())
    manifest = make_release(
        tmp_path / "release",
        mutate_manifest=lambda payload: payload["layers"]["allowed"].update(
            path="../outside.json",
            sha256=_sha(outside),
        ),
    )

    with pytest.raises(ReleaseValidationError, match="escapes"):
        validate_release(manifest)


def test_invalid_geojson_is_rejected_before_transaction(tmp_path: Path) -> None:
    manifest = make_release(
        tmp_path / "release",
        geometry_override={"type": "Point", "coordinates": [70.1, 53.1]},
    )
    engine = build_engine()

    with Session(engine) as session:
        with pytest.raises(ReleaseValidationError, match="failed normalization"):
            import_release(session, manifest)
        assert row_count(session) == 0


def test_identical_release_is_idempotent(tmp_path: Path) -> None:
    manifest = make_release(tmp_path / "release")
    engine = build_engine()

    with Session(engine, expire_on_commit=False) as session:
        first = import_release(session, manifest)
    with Session(engine, expire_on_commit=False) as session:
        second = import_release(session, manifest)

    assert first.created_count == 3
    assert second.created_count == 0
    assert second.existing_count == 3
    assert [row.id for row in first.layers] == [row.id for row in second.layers]
    with Session(engine) as session:
        assert row_count(session) == 3


def test_new_source_snapshot_supersedes_same_document_scope(tmp_path: Path) -> None:
    first = make_release(
        tmp_path / "release-v1",
        source_sha="a" * 64,
        source_version="snapshot-v1",
    )
    second = make_release(
        tmp_path / "release-v2",
        source_sha="b" * 64,
        source_version="snapshot-v2",
    )
    engine = build_engine()

    with Session(engine, expire_on_commit=False) as session:
        first_result = import_release(session, first)
    with Session(engine, expire_on_commit=False) as session:
        second_result = import_release(session, second)

    assert first_result.created_count == 3
    assert second_result.created_count == 3
    assert second_result.superseded_count == 3
    with Session(engine) as session:
        active = session.scalars(
            select(UrbanPlanLayer).where(UrbanPlanLayer.active.is_(True))
        ).all()
        inactive = session.scalars(
            select(UrbanPlanLayer).where(UrbanPlanLayer.active.is_(False))
        ).all()
    assert len(active) == 3
    assert len(inactive) == 3
    assert {row.source_sha256 for row in active} == {"b" * 64}


def test_same_idempotency_key_with_different_release_is_conflict(
    tmp_path: Path,
) -> None:
    release_dir = tmp_path / "release"
    manifest = make_release(release_dir)
    engine = build_engine()
    with Session(engine) as session:
        import_release(session, manifest)

    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["released_by"] = "different-release-operator"
    _write_json(manifest, manifest_payload)

    with Session(engine) as session:
        with pytest.raises(ImportConflictError, match="idempotency key"):
            import_release(session, manifest)
    with Session(engine) as session:
        assert row_count(session) == 3
        assert {
            row.uploaded_by for row in session.scalars(select(UrbanPlanLayer)).all()
        } == {"release-operator"}


def test_database_error_rolls_back_all_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = make_release(tmp_path / "release")
    engine = build_engine()

    with Session(engine) as session:
        original_flush = session.flush
        calls = 0

        def failing_flush(objects=None) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("simulated database failure")
            original_flush(objects)

        monkeypatch.setattr(session, "flush", failing_flush)
        with pytest.raises(RuntimeError, match="simulated database failure"):
            import_release(session, manifest)

    with Session(engine) as verification:
        assert row_count(verification) == 0


def test_cli_dry_run_does_not_require_database_table(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = make_release(tmp_path / "release")

    exit_code = main(["--manifest", str(manifest), "--dry-run"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["database_written"] is False
    assert payload["approved_for_search"] is True
    assert payload["layers"] == ["allowed", "prohibited", "red_line"]


def test_cli_initializes_database_before_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = make_release(tmp_path / "release")
    database_path = tmp_path / "import.sqlite"

    exit_code = main([
        "--manifest",
        str(manifest),
        "--database-url",
        f"sqlite:///{database_path}",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["database_written"] is True
    assert payload["created"] == 3

    verification_engine = create_engine(f"sqlite:///{database_path}")
    with Session(verification_engine) as session:
        assert row_count(session) == 3
