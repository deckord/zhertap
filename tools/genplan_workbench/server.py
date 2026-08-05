from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tools.genplan_autoreg.pipeline import _load_plan
from tools.genplan_autoreg.providers import (
    BboxResolutionError,
    BboxResolver,
    EgknResolver,
    FallbackResolver,
    NominatimResolver,
    StaticBboxResolver,
)

from .models import WorkbenchSave
from .render import render_pdf_page, render_tiff_page
from .store import ManifestStore, WorkbenchError, safe_path

MODULE_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = MODULE_ROOT / "static"


def create_app(
    *,
    data_root: str | Path,
    manifest_path: str | Path,
    output_path: str | Path | None = None,
    bbox_resolver: BboxResolver | None = None,
) -> FastAPI:
    try:
        store = ManifestStore(
            Path(data_root),
            Path(manifest_path),
            Path(output_path) if output_path else None,
        )
    except (OSError, WorkbenchError) as exc:
        raise RuntimeError(str(exc)) from exc

    app = FastAPI(
        title="Genplan Georeferencing Workbench",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.store = store
    app.state.bbox_resolver = bbox_resolver or FallbackResolver(
        [EgknResolver(), StaticBboxResolver(), NominatimResolver()]
    )
    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")

    def fail(exc: WorkbenchError) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    @app.get("/api/records")
    def records() -> dict[str, Any]:
        return {"records": store.list_records()}

    @app.get("/api/records/{record_id}")
    def record(record_id: str) -> dict[str, Any]:
        try:
            raw = store.get_record(record_id)
            source = store.source_path(record_id)
            saved = store.load_gcps(record_id)
        except WorkbenchError as exc:
            raise fail(exc) from exc
        return {
            "record_id": record_id,
            "filename": raw.get("original_filename") or source.name,
            "format": source.suffix.lower().lstrip("."),
            "page_count": raw.get("page_count") or 1,
            "width_px": raw.get("width_px"),
            "height_px": raw.get("height_px"),
            "region": raw.get("egkn_region") or raw.get("normalized_region") or "",
            "district": raw.get("normalized_district") or "",
            "locality": raw.get("normalized_locality") or "",
            "bbox_status": raw.get("bbox_status") or "",
            "bbox_source": raw.get("bbox_source") or "",
            "bbox_label": raw.get("bbox_label") or "",
            "bbox_reason": raw.get("bbox_reason") or "",
            "has_contact_sheet": bool(raw.get("pdf_contact_sheet_path")),
            "autoreg_diagnostics": raw.get("autoreg_diagnostics") or {},
            "saved": saved,
        }

    @app.get("/api/records/{record_id}/bbox")
    def record_bbox(record_id: str) -> dict[str, Any]:
        try:
            raw = store.get_record(record_id)
            locality = str(
                raw.get("normalized_locality")
                or raw.get("canonical_locality_name")
                or ""
            ).strip()
            region = str(
                raw.get("egkn_region")
                or raw.get("normalized_region")
                or raw.get("canonical_region_name")
                or ""
            ).strip()
            district = str(
                raw.get("egkn_district")
                or raw.get("normalized_district")
                or raw.get("canonical_district_name")
                or ""
            ).strip()
            if not locality:
                raise WorkbenchError("Locality is missing from the manifest")
            bbox = app.state.bbox_resolver.resolve(
                locality,
                region=region,
                district=district,
            )
        except (WorkbenchError, BboxResolutionError, ValueError) as exc:
            raise fail(WorkbenchError(str(exc))) from exc
        return {
            "west": bbox.west,
            "south": bbox.south,
            "east": bbox.east,
            "north": bbox.north,
            "source": bbox.source,
            "label": bbox.label,
        }

    @app.get("/api/records/{record_id}/image")
    def source_image(
        record_id: str,
        page: int = Query(default=1, ge=1, le=500),
    ) -> FileResponse:
        try:
            source = store.source_path(record_id)
            if source.suffix.lower() == ".pdf":
                record_dir = store.record_output_path(record_id)
                rendered = safe_path(
                    store.root,
                    store.render_path / record_dir.name / f"page-{page:04d}.png",
                )
                source = render_pdf_page(
                    source,
                    rendered,
                    page=page,
                    data_root=store.root,
                )
            elif source.suffix.lower() in {".tif", ".tiff"}:
                record_dir = store.record_output_path(record_id)
                rendered = safe_path(
                    store.root,
                    store.render_path / record_dir.name / f"page-{page:04d}.png",
                )
                source = render_tiff_page(
                    source,
                    rendered,
                    page=page,
                    data_root=store.root,
                )
            elif source.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                raise WorkbenchError("This source format cannot be displayed")
        except WorkbenchError as exc:
            raise fail(exc) from exc
        return FileResponse(source)

    @app.get("/api/records/{record_id}/contact-sheet")
    def contact_sheet(record_id: str) -> FileResponse:
        try:
            raw = store.get_record(record_id)
            contact_sheet_path = raw.get("pdf_contact_sheet_path")
            if not contact_sheet_path:
                raise WorkbenchError("Contact sheet is unavailable for this record")
            path = safe_path(store.root, str(contact_sheet_path), must_exist=True)
        except WorkbenchError as exc:
            raise fail(exc) from exc
        return FileResponse(path, filename=f"{record_id}-contact-sheet.png")

    @app.get("/api/records/{record_id}/autoreg/{basemap}/{artifact}")
    def autoreg_artifact(record_id: str, basemap: str, artifact: str) -> FileResponse:
        allowed_artifacts = {"plan_preview", "basemap", "matches", "result"}
        if artifact not in allowed_artifacts:
            raise fail(WorkbenchError("Autoreg artifact is not available"))
        try:
            raw = store.get_record(record_id)
            diagnostics = raw.get("autoreg_diagnostics")
            if not isinstance(diagnostics, dict):
                raise WorkbenchError("Autoreg diagnostics are unavailable")
            for attempt in diagnostics.get("attempts", []):
                if not isinstance(attempt, dict):
                    continue
                if str(attempt.get("basemap") or "") != basemap:
                    continue
                artifacts = attempt.get("artifacts")
                if not isinstance(artifacts, dict) or not artifacts.get(artifact):
                    break
                path = safe_path(store.root, str(artifacts[artifact]), must_exist=True)
                return FileResponse(path)
            raise WorkbenchError("Autoreg artifact is not available")
        except WorkbenchError as exc:
            raise fail(exc) from exc

    @app.get("/api/records/{record_id}/diagnostic-anchors/{basemap}")
    def diagnostic_anchors(record_id: str, basemap: str) -> dict[str, Any]:
        try:
            raw = store.get_record(record_id)
            source = store.source_path(record_id)
            diagnostics = raw.get("autoreg_diagnostics")
            if not isinstance(diagnostics, dict):
                raise WorkbenchError("Autoreg diagnostics are unavailable")
            result_path: Path | None = None
            for attempt in diagnostics.get("attempts", []):
                if not isinstance(attempt, dict):
                    continue
                if str(attempt.get("basemap") or "") != basemap:
                    continue
                artifacts = attempt.get("artifacts")
                if not isinstance(artifacts, dict) or not artifacts.get("result"):
                    break
                result_path = safe_path(
                    store.root,
                    str(artifacts["result"]),
                    must_exist=True,
                )
                break
            if result_path is None:
                raise WorkbenchError("Diagnostic anchors are unavailable")
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WorkbenchError(f"Cannot read diagnostic anchors: {exc}") from exc
            anchors = result.get("diagnostic_anchor_points")
            if not isinstance(anchors, list) or not anchors:
                raise WorkbenchError("Diagnostic anchors are unavailable")
            matcher_source = source
            result_source = result.get("source_path")
            if isinstance(result_source, str) and result_source.strip():
                try:
                    matcher_source = safe_path(
                        store.root,
                        result_source,
                        must_exist=True,
                    )
                except WorkbenchError:
                    matcher_source = source
            matcher_image = _load_plan(matcher_source)
            return {
                "record_id": record_id,
                "basemap": basemap,
                "source": "operator_diagnostic_only",
                "matcher_image_width_px": matcher_image.width,
                "matcher_image_height_px": matcher_image.height,
                "guardrails": result.get("diagnostic_anchor_guardrails") or {},
                "summary": result.get("diagnostic_anchor_summary") or {},
                "anchors": anchors,
            }
        except WorkbenchError as exc:
            raise fail(exc) from exc

    @app.put("/api/records/{record_id}/gcps")
    def save_gcps(record_id: str, request: WorkbenchSave) -> dict[str, Any]:
        try:
            return store.save(record_id, request)
        except WorkbenchError as exc:
            raise fail(exc) from exc

    @app.get("/api/records/{record_id}/export/gcps")
    def export_gcps(record_id: str) -> FileResponse:
        try:
            path = safe_path(
                store.root, store.record_output_path(record_id) / "gcps.json"
            )
            if not path.is_file():
                raise WorkbenchError("GCP have not been saved")
        except WorkbenchError as exc:
            raise fail(exc) from exc
        return FileResponse(path, filename=f"{record_id}-gcps.json")

    @app.get("/api/records/{record_id}/export/qa")
    def export_qa(record_id: str) -> FileResponse:
        try:
            path = safe_path(store.root, store.record_output_path(record_id) / "qa.json")
            if not path.is_file():
                raise WorkbenchError("QA report has not been generated")
        except WorkbenchError as exc:
            raise fail(exc) from exc
        return FileResponse(path, filename=f"{record_id}-qa.json")

    return app
