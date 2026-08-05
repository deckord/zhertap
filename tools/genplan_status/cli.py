from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MANUAL_MANIFEST = Path("app/data/manual_genplans.json")


@dataclass(frozen=True)
class AssetStatus:
    asset_id: str
    region: str
    district: str
    locality: str
    title: str
    extension: str
    confidence: str
    size_bytes: int
    status: str
    next_action: str
    queue_action: str
    queue_state: str
    duplicate_of: str
    queue_reasons: list[str]
    has_manual_file: bool
    has_autoreg_attempt: bool
    autoreg_status: str
    has_gcps: bool
    has_qa: bool
    has_review: bool
    has_geotiff: bool
    has_vector_manifest: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "region": self.region,
            "district": self.district,
            "locality": self.locality,
            "title": self.title,
            "extension": self.extension,
            "confidence": self.confidence,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "next_action": self.next_action,
            "queue_action": self.queue_action,
            "queue_state": self.queue_state,
            "duplicate_of": self.duplicate_of,
            "queue_reasons": self.queue_reasons,
            "has_manual_file": self.has_manual_file,
            "has_autoreg_attempt": self.has_autoreg_attempt,
            "autoreg_status": self.autoreg_status,
            "has_gcps": self.has_gcps,
            "has_qa": self.has_qa,
            "has_review": self.has_review,
            "has_geotiff": self.has_geotiff,
            "has_vector_manifest": self.has_vector_manifest,
        }


def build_report(
    *,
    manual_manifest: Path,
    data_root: Path | None = None,
) -> dict[str, Any]:
    payload = _read_json(manual_manifest)
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Manual genplan manifest must contain records list")

    root = _resolve_data_root(payload, data_root)
    queue = _load_queue(root)
    statuses = [
        _status_for_record(record, root, queue)
        for record in records
        if isinstance(record, dict)
    ]
    status_counts = Counter(item.status for item in statuses)
    region_counts = Counter(_nonempty(item.region) for item in statuses)
    next_action_counts = Counter(item.next_action for item in statuses)

    return {
        "schema_version": "genplan-status/v1",
        "manual_manifest": str(manual_manifest),
        "data_root": str(root) if root else "",
        "total": len(statuses),
        "status_counts": dict(sorted(status_counts.items())),
        "region_counts": dict(sorted(region_counts.items())),
        "next_action_counts": dict(sorted(next_action_counts.items())),
        "records": [item.as_dict() for item in sorted(statuses, key=_sort_key)],
    }


def _status_for_record(
    record: dict[str, Any],
    root: Path | None,
    queue: dict[str, dict[str, Any]],
) -> AssetStatus:
    asset_id = str(record.get("asset_id") or "")
    source_path = _source_path(record, root)
    record_hash = hashlib.sha256(asset_id.encode("utf-8")).hexdigest() if asset_id else ""
    gcps = _find_first(root, [f"workbench_data/records/{record_hash}/gcps.json"])
    qa = _find_first(root, [f"workbench_data/records/{record_hash}/qa.json"])
    review = _find_first(
        root,
        [
            f"reviews/{asset_id}.json",
            f"work/reviews/{asset_id}.json",
            f"workbench_data/reviews/{asset_id}.json",
            f"published/{asset_id}/review.json",
        ],
    )
    geotiff = _find_geotiff(root, asset_id)
    vector_manifest = _find_vector_manifest(root, asset_id)
    autoreg = _autoreg_status(root, asset_id)
    queue_item = queue.get(asset_id, {})
    queue_action = str(queue_item.get("action") or "")
    queue_state = str(queue_item.get("workflow_state") or "")
    duplicate_of = str(queue_item.get("duplicate_of") or "")
    raw_reasons = queue_item.get("reasons")
    queue_reasons = [
        str(reason)
        for reason in raw_reasons
        if isinstance(reason, str) and reason
    ] if isinstance(raw_reasons, list) else []

    has_gcps = gcps is not None
    has_qa = qa is not None
    has_review = review is not None
    has_geotiff = geotiff is not None
    has_vector_manifest = vector_manifest is not None

    status, next_action = _classify(
        has_manual_file=source_path is not None and source_path.exists(),
        has_autoreg_attempt=autoreg[0],
        autoreg_status=autoreg[1],
        has_gcps=has_gcps,
        has_qa=has_qa,
        has_review=has_review,
        has_geotiff=has_geotiff,
        has_vector_manifest=has_vector_manifest,
        queue_action=queue_action,
        queue_state=queue_state,
    )
    return AssetStatus(
        asset_id=asset_id,
        region=str(record.get("region") or ""),
        district=str(record.get("district") or ""),
        locality=str(record.get("locality") or ""),
        title=str(record.get("title") or ""),
        extension=str(record.get("extension") or ""),
        confidence=str(record.get("confidence") or ""),
        size_bytes=int(record.get("size_bytes") or 0),
        status=status,
        next_action=next_action,
        queue_action=queue_action,
        queue_state=queue_state,
        duplicate_of=duplicate_of,
        queue_reasons=queue_reasons,
        has_manual_file=source_path is not None and source_path.exists(),
        has_autoreg_attempt=autoreg[0],
        autoreg_status=autoreg[1],
        has_gcps=has_gcps,
        has_qa=has_qa,
        has_review=has_review,
        has_geotiff=has_geotiff,
        has_vector_manifest=has_vector_manifest,
    )


def _classify(
    *,
    has_manual_file: bool,
    has_autoreg_attempt: bool,
    autoreg_status: str,
    has_gcps: bool,
    has_qa: bool,
    has_review: bool,
    has_geotiff: bool,
    has_vector_manifest: bool,
    queue_action: str,
    queue_state: str,
) -> tuple[str, str]:
    if not has_manual_file:
        return "missing_source_file", "restore_or_remove_manifest_record"
    if queue_action == "render_manual" or queue_state == "render_manual":
        return "pdf_page_selection_required", "select_pdf_page_then_open_workbench"
    if queue_action == "duplicate" or queue_state == "duplicate":
        return "duplicate_manual_file", "keep_best_source_and_archive_duplicate"
    if queue_action == "manual_identity_review" or queue_state == "identity_conflict":
        return "identity_review_required", "fix_region_district_locality_identity"
    if has_vector_manifest:
        return "vectorized_candidate", "independent_vector_qa_then_import"
    if has_geotiff:
        return "georeferenced_export", "configure_colors_and_vectorize"
    if has_gcps and has_qa and has_review:
        return "reviewed_gcps", "export_geotiff"
    if has_gcps and has_qa:
        return "qa_pending", "independent_georef_review"
    if has_autoreg_attempt and autoreg_status not in {"", "needs_manual"}:
        return "autoreg_candidate", "open_workbench_and_review_points"
    if has_autoreg_attempt:
        return "manual_georeference_required", "place_control_points_in_workbench"
    return "manual_file_only", "run_autoreg_or_open_workbench"


def _source_path(record: dict[str, Any], root: Path | None) -> Path | None:
    if root is None:
        return None
    relative = str(record.get("relative_path") or "")
    if not relative:
        return None
    return root / "extracted" / Path(relative)


def _autoreg_status(root: Path | None, asset_id: str) -> tuple[bool, str]:
    if root is None or not asset_id:
        return False, ""
    result_files: list[Path] = []
    work_root = root / "work"
    if not work_root.exists():
        return False, ""
    for asset_dir in work_root.glob("*/assets/*"):
        if asset_dir.name == asset_id:
            result_files.extend(asset_dir.glob("attempts/*/result.json"))
    if not result_files:
        return False, ""
    statuses = []
    for path in result_files:
        try:
            payload = _read_json(path)
        except ValueError:
            continue
        status = str(payload.get("status") or "")
        if status:
            statuses.append(status)
    if not statuses:
        return True, ""
    if any(status not in {"needs_manual"} for status in statuses):
        return True, next(status for status in statuses if status != "needs_manual")
    return True, statuses[0]


def _load_queue(root: Path | None) -> dict[str, dict[str, Any]]:
    if root is None:
        return {}
    work_root = root / "work"
    if not work_root.exists():
        return {}
    queue: dict[str, dict[str, Any]] = {}
    for queue_path in sorted(work_root.glob("*/queue.jsonl")):
        try:
            with queue_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if isinstance(item, dict) and item.get("asset_id"):
                        item["_queue_path"] = str(queue_path)
                        queue[str(item["asset_id"])] = item
        except (OSError, json.JSONDecodeError):
            continue
    return queue


def _find_first(root: Path | None, relative_paths: list[str]) -> Path | None:
    if root is None:
        return None
    for relative in relative_paths:
        path = root / Path(relative)
        if path.exists():
            return path
    return None


def _find_geotiff(root: Path | None, asset_id: str) -> Path | None:
    if root is None or not asset_id:
        return None
    for base in [root / "published", root / "exports", root / "work" / "exports"]:
        if not base.exists():
            continue
        for path in base.rglob("*.tif"):
            if asset_id in str(path):
                return path
        for path in base.rglob("*.tiff"):
            if asset_id in str(path):
                return path
    return None


def _find_vector_manifest(root: Path | None, asset_id: str) -> Path | None:
    if root is None or not asset_id:
        return None
    for base in [root / "vectorized", root / "work" / "vectorized"]:
        if not base.exists():
            continue
        for path in base.rglob("vectorize-manifest.json"):
            if asset_id in str(path):
                return path
    return None


def _resolve_data_root(payload: dict[str, Any], data_root: Path | None) -> Path | None:
    if data_root is not None:
        return data_root.resolve()
    source_root_hint = payload.get("source_root_hint")
    if not source_root_hint:
        return None
    source_root = Path(str(source_root_hint)).expanduser()
    if source_root.name.lower() == "extracted":
        return source_root.parent.resolve()
    return source_root.resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return payload


def _sort_key(item: AssetStatus) -> tuple[str, str, str, str]:
    return (
        _nonempty(item.status),
        _nonempty(item.region),
        _nonempty(item.district),
        _nonempty(item.locality),
    )


def _nonempty(value: str) -> str:
    return value if value else "(empty)"


def _write_outputs(report: dict[str, Any], output: Path | None, csv_output: Path | None) -> None:
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if csv_output is not None:
        csv_output.parent.mkdir(parents=True, exist_ok=True)
        records = report["records"]
        fields = list(records[0].keys()) if records else []
        with csv_output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build manual genplan status report.")
    parser.add_argument("--manual-manifest", type=Path, default=DEFAULT_MANUAL_MANIFEST)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--csv-output", type=Path)
    args = parser.parse_args()

    report = build_report(
        manual_manifest=args.manual_manifest,
        data_root=args.data_root,
    )
    _write_outputs(report, args.output, args.csv_output)
    summary = {
        key: report[key]
        for key in ("total", "status_counts", "next_action_counts")
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
