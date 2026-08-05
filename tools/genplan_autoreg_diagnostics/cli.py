from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build CSV/JSON reports from genplan autoregistration statuses."
    )
    parser.add_argument("--autoreg-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workbench-manifest", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_diagnostics_report(
        autoreg_output=args.autoreg_output,
        output=args.output,
        workbench_manifest=args.workbench_manifest,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


def build_diagnostics_report(
    *,
    autoreg_output: Path,
    output: Path,
    workbench_manifest: Path | None = None,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    manifest_records = _manifest_records(workbench_manifest)
    statuses = list(_iter_statuses(autoreg_output))
    attempt_rows: list[dict[str, Any]] = []
    asset_rows: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    pipeline_error_assets: set[str] = set()

    for status in statuses:
        asset_id = str(status.get("asset_id") or "")
        manifest = manifest_records.get(asset_id, {})
        attempts = status.get("attempts")
        safe_attempts = (
            [item for item in attempts if isinstance(item, dict)]
            if isinstance(attempts, list)
            else []
        )
        rows_for_asset = [
            _attempt_row(status, attempt, manifest)
            for attempt in safe_attempts
        ]
        attempt_rows.extend(rows_for_asset)
        for row in rows_for_asset:
            for reason in _split_reasons(row["reasons"]):
                reason_counts[reason] += 1
                if reason.startswith("pipeline_error:"):
                    pipeline_error_assets.add(asset_id)
        asset_rows.append(_asset_row(status, manifest, rows_for_asset))

    asset_rows.sort(
        key=lambda row: (
            -float(row["operator_score"]),
            int(row["has_pipeline_error"]),
            row["region"],
            row["locality"],
        )
    )
    summary = {
        "schema_version": "genplan-autoreg-diagnostics/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "autoreg_output": str(autoreg_output.resolve()),
        "workbench_manifest": str(workbench_manifest.resolve()) if workbench_manifest else "",
        "assets": len(statuses),
        "attempts": len(attempt_rows),
        "attempts_with_proposed_gcps": sum(
            1 for row in attempt_rows if int(row["proposed_gcp_count"]) > 0
        ),
        "attempts_with_diagnostic_anchors": sum(
            1 for row in attempt_rows if int(row["diagnostic_anchor_count"]) > 0
        ),
        "pipeline_error_assets": len(pipeline_error_assets),
        "pipeline_error_attempts": sum(
            1
            for row in attempt_rows
            if any(
                reason.startswith("pipeline_error:")
                for reason in _split_reasons(row["reasons"])
            )
        ),
        "workflow_counts": dict(
            Counter(str(status.get("workflow_state") or "") for status in statuses)
        ),
        "registration_counts": dict(
            Counter(str(status.get("registration_status") or "") for status in statuses)
        ),
        "reason_counts": dict(reason_counts.most_common()),
    }
    reason_rows = [
        {"reason": reason, "count": count}
        for reason, count in reason_counts.most_common()
    ]
    _write_json(output / "summary.json", summary)
    _write_csv(output / "attempts.csv", attempt_rows)
    _write_csv(output / "operator-priority.csv", asset_rows)
    _write_csv(output / "reason-counts.csv", reason_rows)
    return {
        "summary": summary,
        "attempts": attempt_rows,
        "operator_priority": asset_rows,
        "reason_counts": reason_rows,
    }


def _iter_statuses(root: Path) -> Sequence[dict[str, Any]]:
    assets_dir = root / "assets"
    if not assets_dir.exists():
        raise ValueError(f"Autoreg assets directory does not exist: {assets_dir}")
    statuses: list[dict[str, Any]] = []
    for status_path in sorted(assets_dir.glob("*/status.json")):
        payload = _read_json(status_path)
        if isinstance(payload, dict):
            statuses.append(payload)
    return statuses


def _manifest_records(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    payload = _read_json(path)
    records = payload.get("records")
    if not isinstance(records, list):
        return {}
    return {
        str(record.get("asset_id")): record
        for record in records
        if isinstance(record, dict) and record.get("asset_id")
    }


def _attempt_row(
    status: Mapping[str, Any],
    attempt: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    result = attempt.get("result")
    result = result if isinstance(result, dict) else {}
    metrics = result.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    reasons = _reasons(result.get("reasons"))
    artifacts = result.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    return {
        "asset_id": str(status.get("asset_id") or ""),
        "filename": _filename(status, manifest),
        "region": _region(status, manifest),
        "district": _district(status, manifest),
        "locality": _locality(status, manifest),
        "workflow_state": str(status.get("workflow_state") or ""),
        "registration_status": str(status.get("registration_status") or ""),
        "basemap": str(attempt.get("basemap") or ""),
        "status": str(result.get("status") or ""),
        "confidence": _number(result.get("confidence")),
        "confidence_label": str(result.get("confidence_label") or ""),
        "proposed_gcp_count": len(result.get("proposed_gcps") or []),
        "diagnostic_anchor_count": len(result.get("diagnostic_anchor_points") or []),
        "diagnostic_anchor_quality": _diagnostic_anchor_quality(result),
        "candidate_matches": _number(metrics.get("candidate_matches")),
        "inliers": _number(metrics.get("inliers")),
        "inlier_ratio": _number(metrics.get("inlier_ratio")),
        "reprojection_rmse_px": _number(metrics.get("reprojection_rmse_px")),
        "plan_coverage": _number(metrics.get("plan_coverage")),
        "reference_coverage": _number(metrics.get("reference_coverage")),
        "homography_condition": _number(metrics.get("homography_condition")),
        "reasons": ";".join(reasons),
        "plan_preview": str(artifacts.get("plan_preview") or ""),
        "basemap_artifact": str(artifacts.get("basemap") or ""),
        "matches": str(artifacts.get("matches") or ""),
        "result": str(artifacts.get("result") or ""),
    }


def _asset_row(
    status: Mapping[str, Any],
    manifest: Mapping[str, Any],
    attempt_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    best = max(attempt_rows, key=_operator_score, default={})
    reasons = sorted(
        {
            reason
            for row in attempt_rows
            for reason in _split_reasons(str(row.get("reasons") or ""))
        }
    )
    has_pipeline_error = any(reason.startswith("pipeline_error:") for reason in reasons)
    return {
        "asset_id": str(status.get("asset_id") or ""),
        "filename": _filename(status, manifest),
        "region": _region(status, manifest),
        "district": _district(status, manifest),
        "locality": _locality(status, manifest),
        "workflow_state": str(status.get("workflow_state") or ""),
        "registration_status": str(status.get("registration_status") or ""),
        "best_basemap": str(best.get("basemap") or ""),
        "best_confidence": _number(best.get("confidence")),
        "best_inliers": _number(best.get("inliers")),
        "best_rmse_px": _number(best.get("reprojection_rmse_px")),
        "best_diagnostic_anchor_count": _number(best.get("diagnostic_anchor_count")),
        "best_matches_artifact": str(best.get("matches") or ""),
        "has_pipeline_error": int(has_pipeline_error),
        "operator_score": round(_operator_score(best), 6) if best else 0,
        "reasons": ";".join(reasons),
    }


def _operator_score(row: Mapping[str, Any]) -> float:
    proposed = _float(row.get("proposed_gcp_count"))
    anchors = _float(row.get("diagnostic_anchor_count"))
    confidence = _float(row.get("confidence"))
    inliers = _float(row.get("inliers"))
    inlier_ratio = _float(row.get("inlier_ratio"))
    rmse = _float(row.get("reprojection_rmse_px"))
    penalty = min(rmse / 1000.0, 20.0) if rmse > 0 else 0.0
    return (
        proposed * 100.0
        + anchors * 20.0
        + confidence * 20.0
        + inliers
        + inlier_ratio * 10.0
        - penalty
    )


def _region(status: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    return str(
        manifest.get("egkn_region")
        or manifest.get("normalized_region")
        or status.get("region")
        or ""
    )


def _district(status: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    return str(
        manifest.get("egkn_district")
        or manifest.get("normalized_district")
        or status.get("district")
        or ""
    )


def _locality(status: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    return str(manifest.get("normalized_locality") or status.get("locality") or "")


def _diagnostic_anchor_quality(result: Mapping[str, Any]) -> str:
    summary = result.get("diagnostic_anchor_summary")
    if not isinstance(summary, dict):
        return ""
    return str(summary.get("quality_label") or "")


def _filename(status: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    return str(
        manifest.get("original_filename")
        or Path(str(status.get("source_path") or "")).name
    )


def _reasons(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _split_reasons(value: str) -> list[str]:
    return [item for item in value.split(";") if item]


def _number(value: Any) -> float | int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 6)
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return 0
    return round(parsed, 6)


def _float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    if not fieldnames:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
