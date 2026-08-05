from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import UrbanPlanSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register genplan portal discovery JSON as UrbanPlanSource rows."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--region", default="")
    parser.add_argument("--district", default="")
    parser.add_argument("--locality", default="")
    parser.add_argument("--authority", default="")
    parser.add_argument("--notes", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
    now = datetime.now(UTC)
    rows = _rows_from_discovery(payload)
    stats = {"seen": len(rows), "created": 0, "updated": 0}
    with SessionLocal() as session:
        for row in rows:
            source = session.scalar(
                select(UrbanPlanSource).where(
                    UrbanPlanSource.platform == row["platform"],
                    UrbanPlanSource.external_id == row["external_id"],
                )
            )
            created = source is None
            if source is None:
                source = UrbanPlanSource(
                    platform=row["platform"],
                    external_id=row["external_id"],
                    source_type="digital_vector",
                )
                session.add(source)
            source.region = args.region
            source.district = args.district
            source.locality = args.locality
            source.title = row["title"]
            source.source_authority = args.authority
            source.source_url = row["source_url"]
            source.api_base_url = row["api_base_url"]
            source.profiles_json = json.dumps(["mapping_pending"], ensure_ascii=False)
            source.collections_json = json.dumps(row["collections"], ensure_ascii=False)
            source.coverage_status = row["coverage_status"]
            source.import_status = "not_imported"
            source.layer_count = row["layer_count"]
            source.last_checked_at = now
            source.last_error = None
            source.notes = args.notes or row["notes"]
            source.raw_payload_json = json.dumps(
                row["raw"],
                ensure_ascii=False,
                sort_keys=True,
            )
            stats["created" if created else "updated"] += 1
        session.commit()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _rows_from_discovery(payload: dict[str, Any]) -> list[dict[str, Any]]:
    platform = str(payload.get("platform") or "")
    if platform == "wfs":
        return _wfs_rows(payload)
    if platform == "arcgis":
        return _arcgis_rows(payload)
    raise ValueError("Unsupported discovery platform")


def _wfs_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    base_url = str(payload.get("url") or "")
    rows = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        name = str(candidate.get("name") or "").strip()
        if not name:
            continue
        sample = candidate.get("sample") if isinstance(candidate.get("sample"), dict) else {}
        layer_count = _int_or_zero(sample.get("feature_count"))
        rows.append(
            {
                "platform": "wfs",
                "external_id": f"{base_url}:{name}",
                "title": candidate.get("title") or name,
                "source_url": base_url,
                "api_base_url": base_url,
                "collections": [name],
                "coverage_status": "geometry_found" if sample.get("ok") else "catalog_found",
                "layer_count": layer_count,
                "notes": "Discovered from WFS capabilities; mapping and QA required.",
                "raw": candidate,
            }
        )
    return rows


def _arcgis_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for service in payload.get("candidates") or []:
        if not isinstance(service, dict):
            continue
        service_url = str(service.get("url") or "")
        for layer in service.get("layers") or []:
            if not isinstance(layer, dict):
                continue
            layer_url = str(layer.get("url") or "")
            layer_name = str(layer.get("name") or "").strip()
            if not layer_url or not layer_name:
                continue
            sample = layer.get("sample") if isinstance(layer.get("sample"), dict) else {}
            rows.append(
                {
                    "platform": "arcgis_rest",
                    "external_id": layer_url,
                    "title": f"{service.get('name')}: {layer_name}",
                    "source_url": layer_url,
                    "api_base_url": service_url,
                    "collections": [layer_url],
                    "coverage_status": (
                        "geometry_found"
                        if sample.get("query_ok") and sample.get("geometry_type")
                        else "catalog_found"
                    ),
                    "layer_count": 0,
                    "notes": "Discovered from ArcGIS REST; mapping and QA required.",
                    "raw": {"service": service, "layer": layer},
                }
            )
    return rows


def _int_or_zero(value: Any) -> int:
    try:
        if value in (None, "unknown"):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
