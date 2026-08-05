from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    ALLOWED_REGISTRATION_STATUSES,
    SCHEMA_VERSION,
    AssetStatus,
    BatchResult,
    RunRequest,
)
from .runner import AutoregRunner, BatchRunner

RASTER_FORMATS = {"jpeg", "png", "tiff"}
PDF_FORMAT = "pdf"
FORBIDDEN_RESULT_VALUES = {
    "approved",
    "qa",
    "strict",
    "verified",
    "verified_strict",
}
COMPLETED_WORKFLOWS = {
    "completed",
    "render_manual",
    "duplicate",
    "identity_conflict",
    "unsupported_manual",
}


class BatchConfigurationError(ValueError):
    pass


class ResourceLimitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BatchConfig:
    manifest: Path
    output: Path
    exclude_file: Path | None = None
    region: str = ""
    district: str = ""
    limit: int | None = None
    dry_run: bool = False
    resume: bool = False
    workers: int = 2
    max_tiles: int = 64
    min_free_disk_bytes: int = 5 * 1024**3
    max_output_bytes: int = 20 * 1024**3
    basemaps: tuple[str, ...] = ("arcgis", "osm")
    zoom: int = 15
    bbox_padding: float = 0.05

    def validate(self) -> None:
        if self.limit is not None and self.limit < 0:
            raise BatchConfigurationError("limit cannot be negative")
        if not 1 <= self.workers <= 16:
            raise BatchConfigurationError("workers must be between 1 and 16")
        if not 1 <= self.max_tiles <= 144:
            raise BatchConfigurationError("max_tiles must be between 1 and 144")
        if not self.basemaps or any(item not in {"arcgis", "osm"} for item in self.basemaps):
            raise BatchConfigurationError("basemaps must contain arcgis and/or osm")
        if len(set(self.basemaps)) != len(self.basemaps):
            raise BatchConfigurationError("basemaps cannot contain duplicates")
        if not 0 <= self.zoom <= 19:
            raise BatchConfigurationError("zoom must be between 0 and 19")
        if self.bbox_padding < 0:
            raise BatchConfigurationError("bbox_padding cannot be negative")
        if self.min_free_disk_bytes < 0 or self.max_output_bytes <= 0:
            raise BatchConfigurationError("disk limits are invalid")
        if self.exclude_file is not None:
            _load_exclusions(self.exclude_file)

    def processing_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "runner": "tools.genplan_autoreg",
            "bbox_resolver_version": "egkn-static-nominatim-2026-08-03",
            "basemaps": list(self.basemaps),
            "zoom": self.zoom,
            "bbox_padding": self.bbox_padding,
            "max_tiles": self.max_tiles,
            "exclusions_sha256": (
                _sha256_file(self.exclude_file)
                if self.exclude_file is not None
                else None
            ),
        }

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.processing_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ResourceGuard:
    def __init__(self, config: BatchConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._active = 0
        self._reserve_per_worker = (
            config.max_tiles * 256 * 256 * 4 * len(config.basemaps)
            + 128 * 1024**2
        )

    def acquire(self) -> None:
        with self._lock:
            output_size = _directory_size(self.config.output)
            if output_size >= self.config.max_output_bytes:
                raise ResourceLimitError("maximum batch output size reached")
            free = shutil.disk_usage(self.config.output).free
            required = (
                self.config.min_free_disk_bytes
                + (self._active + 1) * self._reserve_per_worker
            )
            if free < required:
                raise ResourceLimitError(
                    f"insufficient free disk: {free} bytes available, {required} required"
                )
            self._active += 1

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def run_batch(
    config: BatchConfig,
    *,
    runner: BatchRunner | None = None,
) -> BatchResult:
    config.validate()
    manifest = _load_manifest(config.manifest)
    exclusions = (
        _load_exclusions(config.exclude_file)
        if config.exclude_file is not None
        else {}
    )
    config.output.mkdir(parents=True, exist_ok=True)
    config_sha = config.fingerprint()
    selected = _select_records(manifest["records"], config)
    queue, statuses, runnable = _build_queue(selected, config_sha, exclusions)
    resumed_count = 0

    if config.resume:
        pending: list[dict[str, Any]] = []
        for record in runnable:
            previous = _load_resumable_status(config.output, record, config_sha)
            if previous is None:
                pending.append(record)
                continue
            previous.resumed = True
            statuses[record["asset_id"]] = previous
            resumed_count += 1
        runnable = pending

    _write_all_statuses(config.output, statuses)
    _write_queue(config.output / "queue.jsonl", queue, statuses)

    failed_count = 0
    if not config.dry_run and runnable:
        actual_runner = runner or AutoregRunner()
        guard = ResourceGuard(config)
        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            futures = {
                executor.submit(
                    _process_raster,
                    record,
                    config,
                    config_sha,
                    actual_runner,
                    guard,
                ): record
                for record in runnable
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    status = future.result()
                except Exception as exc:  # defensive isolation between assets
                    status = _base_status(
                        record,
                        config_sha,
                        workflow_state="failed",
                        action="autoreg",
                        registration_status="needs_manual",
                        reasons=[f"dispatcher_error:{type(exc).__name__}:{exc}"],
                    )
                statuses[record["asset_id"]] = status
                if status.workflow_state == "failed":
                    failed_count += 1
                _write_status(config.output, status)

    if config.dry_run:
        for record in runnable:
            status = statuses[record["asset_id"]]
            status.workflow_state = "queued"
            status.reasons = _unique([*status.reasons, "dry_run_not_executed"])
            _write_status(config.output, status)

    _write_queue(config.output / "queue.jsonl", queue, statuses)
    summary = _make_summary(
        config=config,
        selected=selected,
        queue=queue,
        statuses=statuses,
        runnable_total=sum(1 for item in queue if item["action"] == "autoreg"),
        resumed_count=resumed_count,
        failed_count=failed_count,
    )
    _write_json(config.output / "summary.json", summary)
    return BatchResult(
        selected=len(selected),
        runnable=summary["runnable"],
        duplicate_count=summary["duplicates"],
        resumed_count=resumed_count,
        failed_count=failed_count,
        output_dir=str(config.output.resolve()),
        summary=summary,
    )


def _process_raster(
    record: dict[str, Any],
    config: BatchConfig,
    config_sha: str,
    runner: BatchRunner,
    guard: ResourceGuard,
) -> AssetStatus:
    asset_output = _asset_dir(config.output, record["asset_id"])
    acquired = False
    try:
        source = Path(record["extracted_path"])
        if not source.is_file():
            raise FileNotFoundError(f"source asset does not exist: {source}")
        actual_sha = _sha256_file(source)
        if actual_sha != record["sha256"]:
            raise ValueError(
                f"source SHA-256 differs from inventory: {actual_sha}"
            )
        guard.acquire()
        acquired = True
        attempts = runner(
            RunRequest(
                asset_id=record["asset_id"],
                source_path=record["extracted_path"],
                source_sha256=record["sha256"],
                locality=_locality(record),
                region=_region(record),
                district=_district(record),
                output_dir=str(asset_output),
                basemaps=config.basemaps,
                zoom=config.zoom,
                bbox_padding=config.bbox_padding,
                max_tiles=config.max_tiles,
            )
        )
        safe_attempts, rejected_reasons = _sanitize_attempts(attempts, record["sha256"])
        has_proposals = any(
            bool(attempt.get("result", {}).get("proposed_gcps"))
            for attempt in safe_attempts
        )
        runner_errors = [
            reason
            for attempt in safe_attempts
            for reason in attempt.get("result", {}).get("reasons", [])
            if str(reason).startswith("pipeline_error:")
        ]
        status = _base_status(
            record,
            config_sha,
            workflow_state="completed" if safe_attempts else "failed",
            action="autoreg",
            registration_status="proposed" if has_proposals else "needs_manual",
            reasons=_unique(
                [
                    *rejected_reasons,
                    *runner_errors,
                    "automatic_result_requires_independent_manual_review",
                ]
            ),
        )
        status.attempts = safe_attempts
        return status
    except ResourceLimitError as exc:
        return _base_status(
            record,
            config_sha,
            workflow_state="failed",
            action="autoreg",
            registration_status="needs_manual",
            reasons=[f"resource_limit:{exc}"],
        )
    except Exception as exc:
        return _base_status(
            record,
            config_sha,
            workflow_state="failed",
            action="autoreg",
            registration_status="needs_manual",
            reasons=[f"runner_error:{type(exc).__name__}:{exc}"],
        )
    finally:
        if acquired:
            guard.release()


def _sanitize_attempts(
    attempts: Sequence[Mapping[str, Any]] | Any,
    expected_sha: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        return [], ["runner_result_rejected:not_a_sequence"]
    safe: list[dict[str, Any]] = []
    rejected: list[str] = []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping):
            rejected.append(f"runner_result_rejected:attempt_{index}_not_object")
            continue
        basemap = str(attempt.get("basemap", ""))
        result = attempt.get("result")
        if basemap not in {"arcgis", "osm"} or not isinstance(result, Mapping):
            rejected.append(f"runner_result_rejected:attempt_{index}_invalid_shape")
            continue
        if _contains_forbidden_value(result):
            rejected.append(f"runner_result_rejected:{basemap}:unsafe_status")
            continue
        if str(result.get("source_sha256", "")) != expected_sha:
            rejected.append(f"runner_result_rejected:{basemap}:sha_mismatch")
            continue
        status = str(result.get("status", "needs_manual")).casefold()
        if status not in {"needs_manual", "proposed"}:
            rejected.append(f"runner_result_rejected:{basemap}:invalid_status")
            continue
        safe_result = dict(result)
        safe_result["status"] = "needs_manual"
        safe.append({"basemap": basemap, "result": safe_result})
    return safe, rejected


def _contains_forbidden_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_forbidden_value(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return any(_contains_forbidden_value(item) for item in value)
    return isinstance(value, str) and value.strip().casefold() in FORBIDDEN_RESULT_VALUES


def _build_queue(
    records: list[dict[str, Any]],
    config_sha: str,
    exclusions: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, AssetStatus], list[dict[str, Any]]]:
    exclusions = exclusions or {}
    canonical_by_sha: dict[str, dict[str, Any]] = {}
    queue: list[dict[str, Any]] = []
    statuses: dict[str, AssetStatus] = {}
    runnable: list[dict[str, Any]] = []
    for sequence, record in enumerate(records, start=1):
        content_sha = str(record.get("sha256", ""))
        exclusion_reason = exclusions.get(str(record["asset_id"]))
        if exclusion_reason is not None:
            status = _base_status(
                record,
                config_sha,
                workflow_state="identity_conflict",
                action="manual_identity_review",
                registration_status="needs_manual",
                reasons=[
                    "excluded_from_automatic_processing",
                    exclusion_reason,
                ],
            )
            statuses[record["asset_id"]] = status
            queue.append(
                {
                    "sequence": sequence,
                    "asset_id": record["asset_id"],
                    "source_sha256": content_sha,
                    "detected_format": record.get("detected_format", ""),
                    "region": _region(record),
                    "district": _district(record),
                    "locality": _locality(record),
                    "action": status.action,
                    "duplicate_of": None,
                }
            )
            continue
        canonical = canonical_by_sha.get(content_sha)
        if canonical is not None:
            conflict = any(
                _location_value(record, key) != _location_value(canonical, key)
                for key in ("region", "district", "locality")
            )
            reasons = ["duplicate_sha_not_reprocessed"]
            if conflict:
                reasons.append("duplicate_metadata_conflict_requires_manual_review")
            status = _base_status(
                record,
                config_sha,
                workflow_state="duplicate",
                action="duplicate",
                registration_status="needs_manual",
                reasons=reasons,
            )
            status.duplicate_of = canonical["asset_id"]
        else:
            canonical_by_sha[content_sha] = record
            detected_format = str(record.get("detected_format", "")).casefold()
            if detected_format in RASTER_FORMATS:
                status = _base_status(
                    record,
                    config_sha,
                    workflow_state="queued",
                    action="autoreg",
                    registration_status="needs_manual",
                    reasons=["awaiting_conservative_autoregistration"],
                )
                runnable.append(record)
            elif detected_format == PDF_FORMAT:
                status = _base_status(
                    record,
                    config_sha,
                    workflow_state="render_manual",
                    action="render_manual",
                    registration_status="needs_manual",
                    reasons=[
                        "pdf_requires_explicit_page_selection_and_rendering",
                        "no_pdf_page_was_invented",
                    ],
                )
            else:
                status = _base_status(
                    record,
                    config_sha,
                    workflow_state="unsupported_manual",
                    action="manual",
                    registration_status="needs_manual",
                    reasons=["format_not_supported_by_autoregistration"],
                )
        statuses[record["asset_id"]] = status
        queue.append(
            {
                "sequence": sequence,
                "asset_id": record["asset_id"],
                "source_sha256": content_sha,
                "detected_format": record.get("detected_format", ""),
                "region": _region(record),
                "district": _district(record),
                "locality": _locality(record),
                "action": status.action,
                "duplicate_of": status.duplicate_of,
            }
        )
    return queue, statuses, runnable


def _base_status(
    record: Mapping[str, Any],
    config_sha: str,
    *,
    workflow_state: str,
    action: str,
    registration_status: str,
    reasons: list[str],
) -> AssetStatus:
    if registration_status not in ALLOWED_REGISTRATION_STATUSES:
        raise ValueError("unsafe automatic registration status")
    return AssetStatus(
        asset_id=str(record["asset_id"]),
        source_sha256=str(record["sha256"]),
        config_sha256=config_sha,
        source_path=str(record.get("extracted_path", "")),
        detected_format=str(record.get("detected_format", "")),
        region=_region(record),
        district=_district(record),
        locality=_locality(record),
        workflow_state=workflow_state,
        registration_status=registration_status,  # type: ignore[arg-type]
        action=action,
        reasons=reasons,
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchConfigurationError(f"cannot read inventory manifest: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise BatchConfigurationError("inventory manifest must contain a records list")
    required = {"asset_id", "sha256", "extracted_path", "detected_format"}
    for index, record in enumerate(payload["records"]):
        if not isinstance(record, dict) or not required.issubset(record):
            raise BatchConfigurationError(f"invalid manifest record at index {index}")
    return payload


def _load_exclusions(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchConfigurationError(f"cannot read exclusion registry: {exc}") from exc
    entries = payload.get("excluded_assets") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise BatchConfigurationError(
            "exclusion registry must contain an excluded_assets list"
        )
    exclusions: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BatchConfigurationError(
                f"invalid exclusion registry entry at index {index}"
            )
        asset_id = str(entry.get("asset_id", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        if not asset_id or not reason:
            raise BatchConfigurationError(
                f"exclusion entry {index} requires asset_id and reason"
            )
        if asset_id in exclusions:
            raise BatchConfigurationError(
                f"duplicate exclusion registry asset_id: {asset_id}"
            )
        exclusions[asset_id] = reason
    return exclusions


def _select_records(
    records: list[dict[str, Any]],
    config: BatchConfig,
) -> list[dict[str, Any]]:
    selected = [
        record
        for record in records
        if record.get("asset_role") == "plan_document"
        and _matches_filter(record, "region", config.region)
        and _matches_filter(record, "district", config.district)
    ]
    if config.limit is not None:
        return selected[: config.limit]
    return selected


def _matches_filter(record: Mapping[str, Any], field: str, query: str) -> bool:
    if not query:
        return True
    needle = _search_text(query)
    keys = {
        "region": ("original_region", "normalized_region", "egkn_region", "region_code"),
        "district": (
            "original_district",
            "normalized_district",
            "egkn_district",
            "district_code",
        ),
    }[field]
    return any(needle in _search_text(str(record.get(key, ""))) for key in keys)


def _search_text(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def _region(record: Mapping[str, Any]) -> str:
    return str(
        record.get("egkn_region")
        or record.get("normalized_region")
        or record.get("original_region")
        or ""
    )


def _district(record: Mapping[str, Any]) -> str:
    return str(
        record.get("egkn_district")
        or record.get("normalized_district")
        or record.get("original_district")
        or ""
    )


def _locality(record: Mapping[str, Any]) -> str:
    return str(record.get("normalized_locality") or record.get("original_locality") or "")


def _location_value(record: Mapping[str, Any], field: str) -> str:
    getter = {"region": _region, "district": _district, "locality": _locality}[field]
    return _search_text(getter(record))


def _load_resumable_status(
    output: Path,
    record: Mapping[str, Any],
    config_sha: str,
) -> AssetStatus | None:
    path = _asset_dir(output, str(record["asset_id"])) / "status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        payload.get("source_sha256") != record["sha256"]
        or payload.get("config_sha256") != config_sha
        or payload.get("workflow_state") not in COMPLETED_WORKFLOWS
        or payload.get("registration_status") not in ALLOWED_REGISTRATION_STATUSES
    ):
        return None
    source = Path(str(record.get("extracted_path", "")))
    if not source.is_file() or _sha256_file(source) != record["sha256"]:
        return None
    allowed_fields = set(AssetStatus.__dataclass_fields__)
    try:
        values = {
            key: value for key, value in payload.items() if key in allowed_fields
        }
        return AssetStatus(**values)
    except (TypeError, ValueError):
        return None


def _make_summary(
    *,
    config: BatchConfig,
    selected: list[dict[str, Any]],
    queue: list[dict[str, Any]],
    statuses: dict[str, AssetStatus],
    runnable_total: int,
    resumed_count: int,
    failed_count: int,
) -> dict[str, Any]:
    workflow_counts = Counter(status.workflow_state for status in statuses.values())
    registration_counts = Counter(
        status.registration_status
        for status in statuses.values()
        if status.registration_status is not None
    )
    attempt_error_assets = sum(
        any(str(reason).startswith("pipeline_error:") for reason in status.reasons)
        for status in statuses.values()
    )
    attempt_error_count = sum(
        str(reason).startswith("pipeline_error:")
        for status in statuses.values()
        for reason in status.reasons
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "manifest": str(config.manifest.resolve()),
        "exclude_file": (
            str(config.exclude_file.resolve())
            if config.exclude_file is not None
            else None
        ),
        "output": str(config.output.resolve()),
        "config_sha256": config.fingerprint(),
        "processing_config": config.processing_payload(),
        "filters": {
            "region": config.region,
            "district": config.district,
            "limit": config.limit,
        },
        "dry_run": config.dry_run,
        "resume": config.resume,
        "selected": len(selected),
        "queue_entries": len(queue),
        "runnable": runnable_total,
        "unique_sha256": len({record["sha256"] for record in selected}),
        "duplicates": workflow_counts["duplicate"],
        "pdf_render_manual": workflow_counts["render_manual"],
        "resumed": resumed_count,
        "failed": failed_count,
        "attempt_error_assets": attempt_error_assets,
        "attempt_error_count": attempt_error_count,
        "workflow_counts": dict(sorted(workflow_counts.items())),
        "registration_counts": dict(sorted(registration_counts.items())),
        "output_bytes": _directory_size(config.output),
        "safety": {
            "automatic_statuses": sorted(ALLOWED_REGISTRATION_STATUSES),
            "qa_or_strict_automatic": False,
        },
    }


def _write_all_statuses(output: Path, statuses: Mapping[str, AssetStatus]) -> None:
    for status in statuses.values():
        _write_status(output, status)


def _write_status(output: Path, status: AssetStatus) -> None:
    _write_json(_asset_dir(output, status.asset_id) / "status.json", status.to_dict())


def _write_queue(
    path: Path,
    queue: list[dict[str, Any]],
    statuses: Mapping[str, AssetStatus],
) -> None:
    rows = []
    for item in queue:
        status = statuses[item["asset_id"]]
        rows.append(
            {
                **item,
                "workflow_state": status.workflow_state,
                "registration_status": status.registration_status,
                "resumed": status.resumed,
                "reasons": status.reasons,
            }
        )
    content = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    _atomic_write_text(path, content)


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _asset_dir(output: Path, asset_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", asset_id)
    return output / "assets" / safe_id


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values))
