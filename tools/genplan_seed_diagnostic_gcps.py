from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tools.genplan_autoreg.pipeline import _load_plan
from tools.genplan_workbench.models import GCP, WorkbenchSave
from tools.genplan_workbench.store import ManifestStore, WorkbenchError, safe_path


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description=(
            "Seed draft workbench GCP files from operator-only diagnostic anchors."
        )
    )
    parser.add_argument("--root", type=Path, required=True, help="Allowed data root")
    parser.add_argument("--manifest", type=Path, required=True, help="Workbench manifest")
    parser.add_argument("--output", type=Path, help="Workbench output directory")
    parser.add_argument(
        "--operator",
        default="diagnostic-anchor-seed",
        help="Operator label stored in draft GCP files",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing draft GCP files for records with diagnostic anchors",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="Optional JSON summary path below --root",
    )
    arguments = parser.parse_args()

    summary = seed_diagnostic_gcps(
        root=arguments.root,
        manifest=arguments.manifest,
        output=arguments.output,
        operator=arguments.operator,
        overwrite=arguments.overwrite,
    )
    if arguments.summary:
        summary_path = safe_path(arguments.root, arguments.summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def seed_diagnostic_gcps(
    *,
    root: Path,
    manifest: Path,
    output: Path | None,
    operator: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    store = ManifestStore(root, manifest, output)
    records = store.list_records()
    summary: dict[str, Any] = {
        "records_seen": len(records),
        "with_diagnostic_anchors": 0,
        "seeded": 0,
        "skipped_existing": 0,
        "skipped_insufficient_points": 0,
        "errors": [],
        "seeded_records": [],
    }
    for listed in records:
        record_id = str(listed["record_id"])
        raw = store.get_record(record_id)
        best = (raw.get("autoreg_diagnostics") or {}).get("best_attempt") or {}
        count = int(best.get("diagnostic_anchor_count") or 0)
        if count <= 0:
            continue
        summary["with_diagnostic_anchors"] += 1
        if store.load_gcps(record_id).get("points") and not overwrite:
            summary["skipped_existing"] += 1
            continue
        try:
            request = _request_from_best_attempt(
                store=store,
                record_id=record_id,
                best=best,
                operator=operator,
            )
            if len(request.points) < 3:
                summary["skipped_insufficient_points"] += 1
                continue
            store.save(record_id, request)
        except (OSError, json.JSONDecodeError, WorkbenchError, ValueError) as exc:
            summary["errors"].append({"record_id": record_id, "error": str(exc)})
            continue
        summary["seeded"] += 1
        summary["seeded_records"].append(
            {
                "record_id": record_id,
                "filename": listed.get("filename") or "",
                "basemap": best.get("basemap") or "",
                "points": len(request.points),
            }
        )
    return summary


def _request_from_best_attempt(
    *,
    store: ManifestStore,
    record_id: str,
    best: dict[str, Any],
    operator: str,
) -> WorkbenchSave:
    result_path = _best_result_path(store, best)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    anchors = result.get("diagnostic_anchor_points")
    if not isinstance(anchors, list) or not anchors:
        raise WorkbenchError("Diagnostic anchors are unavailable")
    source = store.source_path(record_id)
    matcher_source = source
    result_source = result.get("source_path")
    if isinstance(result_source, str) and result_source.strip():
        try:
            matcher_source = safe_path(store.root, result_source, must_exist=True)
        except WorkbenchError:
            matcher_source = source
    source_image = _load_plan(source)
    matcher_image = _load_plan(matcher_source)
    scale_x = source_image.width / matcher_image.width
    scale_y = source_image.height / matcher_image.height

    basemap = str(best.get("basemap") or "")
    points: list[GCP] = []
    for index, anchor in enumerate(anchors, start=1):
        plan_pixel = anchor.get("plan_pixel") if isinstance(anchor, dict) else None
        reference = anchor.get("reference_lonlat") if isinstance(anchor, dict) else None
        if not isinstance(plan_pixel, dict) or not isinstance(reference, dict):
            continue
        points.append(
            GCP(
                id=f"diagnostic-{index:03d}",
                pixel_x=float(plan_pixel["x"]) * scale_x,
                pixel_y=float(plan_pixel["y"]) * scale_y,
                lon=float(reference["longitude"]),
                lat=float(reference["latitude"]),
                role="train",
                label=f"diagnostic anchor {index}; verify manually",
                reference_source=f"{basemap} diagnostic only",
            )
        )

    return WorkbenchSave(
        image_width_px=source_image.width,
        image_height_px=source_image.height,
        transform_type="affine",
        workflow_status="proposed",
        operator=operator,
        notes=(
            f"Draft seeded from {basemap} diagnostic anchors. "
            "Operator must verify or move every point before A2 QA. "
            "Not customer-search eligible until approved import."
        ),
        points=points,
    )


def _best_result_path(store: ManifestStore, best: dict[str, Any]) -> Path:
    artifacts = best.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts.get("result"):
        raise WorkbenchError("Best attempt result artifact is missing")
    return safe_path(store.root, str(artifacts["result"]), must_exist=True)


if __name__ == "__main__":
    main()
