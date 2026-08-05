from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .metadata import inspect_file
from .models import (
    ARCHIVE_CSV_FIELDS,
    ASSET_CSV_FIELDS,
    SCHEMA_VERSION,
    ArchiveRecord,
    AssetRecord,
)
from .normalize import (
    apply_egkn_catalog,
    infer_location,
    load_aliases,
    load_egkn_catalog,
)

SUPPORTED_PLAN_FORMATS = {"pdf", "jpeg", "png", "tiff"}
SIDECAR_SUFFIXES = {".wld", ".jgw", ".jpgw", ".jpegw", ".pgw", ".tfw"}


@dataclass(slots=True)
class PipelineConfig:
    source: Path
    output: Path
    extract: bool = True
    input_mode: str = "auto"
    aliases_path: Path | None = None
    egkn_catalog_path: Path | None = None
    max_member_bytes: int = 5 * 1024**3
    max_archive_uncompressed_bytes: int = 25 * 1024**3


@dataclass(slots=True)
class PipelineResult:
    archive_count: int
    asset_count: int
    error_count: int
    output: Path


class ErrorJournal:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def add(
        self,
        *,
        stage: str,
        code: str,
        message: str,
        archive: str = "",
        member: str = "",
        path: str = "",
    ) -> None:
        self.entries.append(
            {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "stage": stage,
                "code": code,
                "message": message,
                "archive": archive,
                "member": member,
                "path": path,
            }
        )


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return value


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _safe_member_parts(name: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", name.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe ZIP member path")
    if path.parts and ":" in path.parts[0]:
        raise ValueError("drive-qualified ZIP member path")
    return path.parts


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _extract_archive(
    archive: Path,
    destination: Path,
    *,
    max_member_bytes: int,
    max_total_bytes: int,
    journal: ErrorJournal,
) -> dict[str, Path]:
    extracted: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as source:
        total = sum(info.file_size for info in source.infolist())
        if total > max_total_bytes:
            raise ValueError(
                f"archive expands to {total} bytes, limit is {max_total_bytes} bytes"
            )
        for info in source.infolist():
            try:
                parts = _safe_member_parts(info.filename)
                if _is_zip_symlink(info):
                    raise ValueError("symbolic links are not extracted")
                if info.file_size > max_member_bytes:
                    raise ValueError(
                        f"member is {info.file_size} bytes, limit is {max_member_bytes} bytes"
                    )
                target = destination.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.stat().st_size == info.file_size:
                    extracted[info.filename] = target
                    continue
                temporary = target.with_name(f"{target.name}.part")
                with source.open(info) as reader, temporary.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, length=4 * 1024 * 1024)
                os.replace(temporary, target)
                extracted[info.filename] = target
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                journal.add(
                    stage="extract",
                    code="member_not_extracted",
                    message=str(exc),
                    archive=archive.name,
                    member=info.filename,
                )
    return extracted


def _sidecars(path: Path) -> list[Path]:
    candidates: list[Path] = []
    for suffix in SIDECAR_SUFFIXES:
        candidates.extend(
            [
                path.with_suffix(suffix),
                path.with_name(path.name + suffix),
                path.with_suffix(suffix.upper()),
            ]
        )
    return sorted({candidate for candidate in candidates if candidate.exists()})


def _asset_role(path: Path, detected_format: str) -> str:
    name = path.name.casefold()
    if (
        name == ".ds_store"
        or name.endswith("-extraction-map.jsonl")
        or path.suffix.casefold() in SIDECAR_SUFFIXES
    ):
        return "service_file"
    if detected_format in SUPPORTED_PLAN_FORMATS:
        return "plan_document"
    if detected_format in {"docx", "pptx"}:
        return "supporting_document"
    return "unsupported_file"


def _workflow_for(
    detected_format: str, sidecars: list[Path], asset_role: str
) -> tuple[str, str]:
    if asset_role == "service_file":
        return "not_applicable", "service_file_ignored"
    if asset_role == "supporting_document":
        return "not_applicable", "supporting_document_requires_review"
    if asset_role == "unsupported_file":
        return "not_applicable", "unsupported_file"
    if sidecars:
        return "sidecar_detected_unverified", "awaiting_georef_validation"
    if detected_format == "tiff":
        return "metadata_requires_review", "awaiting_georef_validation"
    return "requires_control_points", "awaiting_georeference"


def _asset_record(
    *,
    path: Path,
    source_kind: str,
    archive_id: str,
    archive_name: str,
    member_path: str,
    aliases: dict[str, Any],
    egkn_catalog: dict[str, Any],
    journal: ErrorJournal,
) -> AssetRecord:
    content_hash = sha256_file(path)
    stable_key = "\0".join([archive_id, member_path, content_hash]).encode()
    asset_id = hashlib.sha256(stable_key).hexdigest()
    errors: list[str] = []
    try:
        metadata = inspect_file(path)
        if metadata.warning:
            errors.append("metadata_warning")
            journal.add(
                stage="metadata",
                code="metadata_warning",
                message=metadata.warning,
                archive=archive_name,
                member=member_path,
                path=str(path),
            )
    except (OSError, ValueError, EOFError) as exc:
        errors.append("metadata_failed")
        journal.add(
            stage="metadata",
            code="metadata_failed",
            message=str(exc),
            archive=archive_name,
            member=member_path,
            path=str(path),
        )
        from .metadata import FileMetadata, detect_format

        detected = detect_format(path)
        metadata = FileMetadata(detected, "application/octet-stream", method="failed")

    location = apply_egkn_catalog(infer_location(member_path, aliases), egkn_catalog)
    sidecars = _sidecars(path)
    asset_role = _asset_role(path, metadata.detected_format)
    georef_status, workflow_status = _workflow_for(
        metadata.detected_format, sidecars, asset_role
    )
    if errors and metadata.detected_format in SUPPORTED_PLAN_FORMATS:
        workflow_status = "needs_metadata_review"

    return AssetRecord(
        asset_id=asset_id,
        source_kind=source_kind,
        source_archive_id=archive_id,
        source_archive_name=archive_name,
        original_member_path=member_path,
        original_filename=PurePosixPath(member_path).name,
        extracted_path=str(path.resolve()),
        extension=path.suffix.lower(),
        detected_format=metadata.detected_format,
        media_type=metadata.media_type,
        asset_role=asset_role,
        size_bytes=path.stat().st_size,
        sha256=content_hash,
        width_px=metadata.width_px,
        height_px=metadata.height_px,
        page_count=metadata.page_count,
        metadata_method=metadata.method,
        original_region=location.original_region,
        original_district=location.original_district,
        original_locality=location.original_locality,
        normalized_region=location.normalized_region,
        region_code=location.region_code,
        egkn_region=location.egkn_region,
        normalized_district=location.normalized_district,
        district_code=location.district_code,
        egkn_district=location.egkn_district,
        normalized_locality=location.normalized_locality,
        location_confidence=location.confidence,
        normalization_notes=location.notes,
        georef_status=georef_status,
        workflow_status=workflow_status,
        sidecar_files=[str(item.resolve()) for item in sidecars],
        error_codes=errors,
    )


def _archive_record(
    archive: Path,
    sha256: str,
    destination: Path,
    journal: ErrorJournal,
) -> ArchiveRecord:
    record = ArchiveRecord(
        archive_id=sha256,
        original_path=str(archive.resolve()),
        filename=archive.name,
        size_bytes=archive.stat().st_size,
        sha256=sha256,
        extraction_directory=str(destination.resolve()),
    )
    try:
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            record.member_count = len(infos)
            record.file_count = sum(not info.is_dir() for info in infos)
            record.uncompressed_bytes = sum(info.file_size for info in infos)
            record.status = "inventoried"
    except (OSError, zipfile.BadZipFile) as exc:
        record.status = "invalid"
        record.error_count += 1
        journal.add(
            stage="archive_inventory",
            code="invalid_archive",
            message=str(exc),
            archive=archive.name,
            path=str(archive),
        )
    return record


def _loose_files(source: Path, recursive: bool) -> list[Path]:
    candidates = source.rglob("*") if recursive else source.iterdir()
    return sorted(
        path
        for path in candidates
        if path.is_file() and path.suffix.lower() != ".zip"
    )


def _detect_input_mode(source: Path, requested: str) -> str:
    if requested not in {"auto", "archives", "extracted"}:
        raise ValueError(f"Unsupported input mode: {requested}")
    if requested != "auto":
        return requested
    if source.name.casefold() == "extracted" or any(
        source.glob("*-extraction-map.jsonl")
    ):
        return "extracted"
    return "archives"


def _load_extraction_maps(
    source: Path, journal: ErrorJournal
) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for map_path in sorted(source.glob("*-extraction-map.jsonl")):
        archive_folder = map_path.name.removesuffix("-extraction-map.jsonl")
        try:
            lines = map_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            journal.add(
                stage="extraction_map",
                code="extraction_map_read_failed",
                message=str(exc),
                path=str(map_path),
            )
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                journal.add(
                    stage="extraction_map",
                    code="invalid_extraction_map_row",
                    message=f"line {line_number}: {exc}",
                    path=str(map_path),
                )
                continue
            if row.get("is_directory"):
                continue
            relative = str(row.get("normalized_relative_path", "")).replace("\\", "/")
            if relative:
                mapping[f"{archive_folder}/{relative}"] = row
    return mapping


def _auto_catalog_path(source: Path, configured: Path | None) -> Path | None:
    if configured:
        return configured
    candidate = source.parent / "work" / "egkn_catalog.json"
    return candidate if candidate.exists() else None


def run_pipeline(config: PipelineConfig) -> PipelineResult:
    source = config.source.resolve()
    output = config.output.resolve()
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    if source == output or source in output.parents:
        raise ValueError("Output directory must not be inside source directory")

    output.mkdir(parents=True, exist_ok=True)
    extracted_root = output / "extracted"
    manifests_root = output / "manifests"
    extracted_root.mkdir(parents=True, exist_ok=True)
    manifests_root.mkdir(parents=True, exist_ok=True)

    journal = ErrorJournal()
    input_mode = _detect_input_mode(source, config.input_mode)
    aliases = load_aliases(config.aliases_path)
    egkn_catalog = load_egkn_catalog(_auto_catalog_path(source, config.egkn_catalog_path))
    archives: list[ArchiveRecord] = []
    assets: list[AssetRecord] = []

    archive_paths = (
        sorted(source.rglob("*.zip"), key=lambda item: str(item).casefold())
        if input_mode == "archives"
        else []
    )
    for index, archive in enumerate(archive_paths, start=1):
        try:
            archive_hash = sha256_file(archive)
        except OSError as exc:
            journal.add(
                stage="archive_hash",
                code="archive_hash_failed",
                message=str(exc),
                archive=archive.name,
                path=str(archive),
            )
            continue
        destination = extracted_root / f"archive_{index:03d}_{archive_hash[:12]}"
        record = _archive_record(archive, archive_hash, destination, journal)
        archives.append(record)
        if record.status == "invalid" or not config.extract:
            continue
        before_errors = len(journal.entries)
        try:
            extracted = _extract_archive(
                archive,
                destination,
                max_member_bytes=config.max_member_bytes,
                max_total_bytes=config.max_archive_uncompressed_bytes,
                journal=journal,
            )
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            record.status = "extraction_failed"
            journal.add(
                stage="extract",
                code="archive_extraction_failed",
                message=str(exc),
                archive=archive.name,
                path=str(archive),
            )
            extracted = {}
        record.error_count += len(journal.entries) - before_errors
        if extracted:
            record.status = "extracted_with_errors" if record.error_count else "extracted"
        for member, path in sorted(extracted.items(), key=lambda item: item[0].casefold()):
            assets.append(
                _asset_record(
                    path=path,
                    source_kind="zip_member",
                    archive_id=record.archive_id,
                    archive_name=record.filename,
                    member_path=member,
                    aliases=aliases,
                    egkn_catalog=egkn_catalog,
                    journal=journal,
                )
            )

    extraction_map = _load_extraction_maps(source, journal) if input_mode == "extracted" else {}
    for path in _loose_files(source, recursive=input_mode == "extracted"):
        disk_relative = path.relative_to(source).as_posix()
        map_row = extraction_map.get(disk_relative, {})
        member = str(map_row.get("original_member") or disk_relative)
        archive_name = str(map_row.get("archive") or "")
        archive_id = (
            hashlib.sha256(archive_name.encode("utf-8")).hexdigest()
            if archive_name
            else ""
        )
        try:
            assets.append(
                _asset_record(
                    path=path,
                    source_kind=(
                        "pre_extracted" if input_mode == "extracted" else "loose_file"
                    ),
                    archive_id=archive_id,
                    archive_name=archive_name,
                    member_path=member,
                    aliases=aliases,
                    egkn_catalog=egkn_catalog,
                    journal=journal,
                )
            )
        except OSError as exc:
            journal.add(
                stage="loose_file_inventory",
                code="file_inventory_failed",
                message=str(exc),
                path=str(path),
                member=member,
            )

    archives.sort(key=lambda item: item.original_path.casefold())
    assets.sort(
        key=lambda item: (
            item.source_archive_name.casefold(),
            item.original_member_path.casefold(),
        )
    )
    archive_rows = [item.to_dict() for item in archives]
    asset_rows = [item.to_dict() for item in assets]
    generated_at = datetime.now(UTC).isoformat()

    _write_json(
        manifests_root / "archives.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": generated_at,
            "source": str(source),
            "records": archive_rows,
        },
    )
    _write_csv(manifests_root / "archives.csv", ARCHIVE_CSV_FIELDS, archive_rows)
    _write_json(
        manifests_root / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": generated_at,
            "source": str(source),
            "records": asset_rows,
        },
    )
    _write_csv(manifests_root / "manifest.csv", ASSET_CSV_FIELDS, asset_rows)
    _atomic_write_text(
        manifests_root / "errors.jsonl",
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in journal.entries),
    )
    _write_json(
        manifests_root / "summary.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at_utc": generated_at,
            "source": str(source),
            "input_mode": input_mode,
            "output": str(output),
            "archive_count": len(archives),
            "asset_count": len(assets),
            "error_count": len(journal.entries),
            "georef_status_counts": _counts(item.georef_status for item in assets),
            "workflow_status_counts": _counts(item.workflow_status for item in assets),
            "format_counts": _counts(item.detected_format for item in assets),
            "asset_role_counts": _counts(item.asset_role for item in assets),
        },
    )
    _write_json(
        manifests_root / "aliases.example.json",
        {
            "regions": {
                "Акмолинская область (01)": {
                    "name": "Акмолинская область",
                    "code": "01",
                }
            },
            "districts": {"Бурабайский район": "Бурабайский район"},
            "localities": {"с. Бурабай": "Бурабай"},
        },
    )
    return PipelineResult(len(archives), len(assets), len(journal.entries), output)


def _counts(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))
