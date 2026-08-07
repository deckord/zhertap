from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.manual_genplans import (
    ManualGenplanRecord,
    manual_genplan_records,
    resolve_manual_genplan_file,
)
from app.models import GenplanLegendEntry, GenplanPipelineStatus, GenplanSourceDocument

PDF_FORMAT = "pdf"
RASTER_FORMATS = {"jpg", "jpeg", "png", "tif", "tiff"}
VECTOR_OPERATOR_RE = re.compile(rb"(\d+(?:\.\d+)?\s+){2,6}(m|l|c|re)\b")
PDF_PAGE_RE = re.compile(rb"/Type\s*/Page\b")
PDF_IMAGE_RE = re.compile(rb"/Subtype\s*/Image\b")
PDF_IMAGE_WIDTH_RE = re.compile(rb"/Width\s+(\d+)")
PDF_IMAGE_HEIGHT_RE = re.compile(rb"/Height\s+(\d+)")
PDF_RGB_FILL_RE = re.compile(
    rb"(?<![\d.])([01](?:\.\d+)?)\s+([01](?:\.\d+)?)\s+([01](?:\.\d+)?)\s+rg\b"
)
LIGHT_PIXEL_THRESHOLD = 235
LOW_SATURATION_THRESHOLD = 18
PDF_RENDER_MAX_SIDE_PX = 900
PDF_RENDER_MAX_PAGES = 50


@dataclass(frozen=True, slots=True)
class GenplanDocumentInspection:
    detected_format: str
    source_sha256: str | None
    file_size_bytes: int
    page_count: int | None = None
    pdf_route: str | None = None
    has_text_layer: bool = False
    vector_object_count: int = 0
    image_count: int = 0
    max_image_width: int | None = None
    max_image_height: int | None = None
    confidence_score: float | None = None
    pipeline_status: str = GenplanPipelineStatus.ingested.value
    next_action: str = ""
    error_message: str | None = None
    raw_metadata: dict[str, Any] | None = None


def inspect_genplan_document(path: Path, *, detected_format: str) -> GenplanDocumentInspection:
    detected = detected_format.strip(".").casefold()
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return GenplanDocumentInspection(
            detected_format=detected,
            source_sha256=None,
            file_size_bytes=0,
            pipeline_status=GenplanPipelineStatus.missing_file.value,
            next_action="upload_source_file",
            error_message=str(exc),
        )

    sha = hashlib.sha256(payload).hexdigest()
    if detected == PDF_FORMAT:
        return _inspect_pdf_payload(payload, source_sha256=sha)
    if detected in RASTER_FORMATS:
        return GenplanDocumentInspection(
            detected_format=detected,
            source_sha256=sha,
            file_size_bytes=len(payload),
            pdf_route="raster_image",
            confidence_score=0.6,
            pipeline_status=GenplanPipelineStatus.ready_for_legend_extraction.value,
            next_action="extract_legend_and_segment_colors",
            raw_metadata={"inspection": "raster_image"},
        )
    return GenplanDocumentInspection(
        detected_format=detected,
        source_sha256=sha,
        file_size_bytes=len(payload),
        confidence_score=0.2,
        pipeline_status=GenplanPipelineStatus.needs_review.value,
        next_action="unsupported_source_review",
        raw_metadata={"inspection": "unsupported_format"},
    )


def sync_manual_genplans_into_pipeline(
    session: Session,
    *,
    limit: int = 200,
    ingested_by: str | None = None,
) -> dict[str, int]:
    limit = max(1, min(limit, 1000))
    stats = {
        "seen": 0,
        "created": 0,
        "updated": 0,
        "missing": 0,
        "pdf": 0,
        "raster": 0,
        "failed": 0,
    }
    for record in manual_genplan_records()[:limit]:
        stats["seen"] += 1
        path = resolve_manual_genplan_file(record)
        if path is None:
            inspection = GenplanDocumentInspection(
                detected_format=record.extension.strip(".").casefold(),
                source_sha256=None,
                file_size_bytes=record.size_bytes,
                pipeline_status=GenplanPipelineStatus.missing_file.value,
                next_action="upload_source_file",
                error_message="source file is not available on this server",
            )
            stats["missing"] += 1
        else:
            try:
                inspection = inspect_genplan_document(
                    path,
                    detected_format=record.extension,
                )
            except Exception as exc:
                inspection = GenplanDocumentInspection(
                    detected_format=record.extension.strip(".").casefold(),
                    source_sha256=None,
                    file_size_bytes=record.size_bytes,
                    pipeline_status=GenplanPipelineStatus.failed.value,
                    next_action="inspect_error_review",
                    error_message=str(exc),
                )
                stats["failed"] += 1
        if inspection.detected_format == PDF_FORMAT:
            stats["pdf"] += 1
        elif inspection.detected_format in RASTER_FORMATS:
            stats["raster"] += 1
        created = _upsert_pipeline_document(
            session,
            record=record,
            inspection=inspection,
            ingested_by=ingested_by,
        )
        if created:
            stats["created"] += 1
        else:
            stats["updated"] += 1
    return stats


def extract_next_document_legend_draft(
    session: Session,
    *,
    limit_colors: int = 12,
) -> dict[str, Any]:
    limit_colors = max(3, min(limit_colors, 32))
    document = session.scalar(
        select(GenplanSourceDocument)
        .where(
            GenplanSourceDocument.pipeline_status.in_(
                (
                    GenplanPipelineStatus.ready_for_legend_extraction.value,
                    GenplanPipelineStatus.ready_for_vector_extraction.value,
                    GenplanPipelineStatus.needs_pdf_page_selection.value,
                )
            )
        )
        .where(
            ~select(GenplanLegendEntry.id)
            .where(GenplanLegendEntry.document_id == GenplanSourceDocument.id)
            .exists()
        )
        .order_by(GenplanSourceDocument.updated_at.asc(), GenplanSourceDocument.id.asc())
        .limit(1)
    )
    if document is None:
        return {
            "document_found": 0,
            "document_id": 0,
            "filename": "",
            "colors_created": 0,
            "status": "",
            "message": "no documents waiting for color extraction",
        }
    record = _record_from_document(document)
    path = resolve_manual_genplan_file(record)
    if path is None:
        document.pipeline_status = GenplanPipelineStatus.missing_file.value
        document.next_action = "upload_source_file"
        document.error_message = "source file is not available on this server"
        session.commit()
        return {
            "document_found": 1,
            "document_id": document.id,
            "filename": document.filename,
            "colors_created": 0,
            "status": document.pipeline_status,
            "message": document.error_message,
        }
    try:
        colors = _draft_colors_for_document(document, path, limit=limit_colors)
    except Exception as exc:
        document.pipeline_status = GenplanPipelineStatus.failed.value
        document.next_action = "legend_extraction_error_review"
        document.error_message = str(exc)
        session.commit()
        return {
            "document_found": 1,
            "document_id": document.id,
            "filename": document.filename,
            "colors_created": 0,
            "status": document.pipeline_status,
            "message": str(exc),
        }
    for color in colors:
        entry = GenplanLegendEntry(
            document_id=document.id,
            color_hex=color["color_hex"],
            red=color["red"],
            green=color["green"],
            blue=color["blue"],
            source=color["source"],
            target_category="unknown",
            layer_kind="unknown",
            confidence_score=color["confidence_score"],
            review_status="needs_review",
            pixel_count=color.get("pixel_count"),
            notes=color.get("notes"),
        )
        session.add(entry)
    page_numbers = sorted(
        {
            int(color["page_number"])
            for color in colors
            if color.get("page_number") is not None
        }
    )
    if page_numbers:
        raw_metadata = _load_document_metadata(document)
        raw_metadata["legend_draft_page_numbers"] = page_numbers
        document.raw_metadata_json = json.dumps(
            raw_metadata,
            ensure_ascii=False,
            sort_keys=True,
        )
    document.pipeline_status = (
        GenplanPipelineStatus.legend_draft_ready.value
        if colors
        else GenplanPipelineStatus.needs_review.value
    )
    document.next_action = (
        "review_legend_colors_and_assign_categories"
        if colors
        else "legend_colors_not_found_review_source"
    )
    document.error_message = None if colors else "no usable colors found"
    session.commit()
    return {
        "document_found": 1,
        "document_id": document.id,
        "filename": document.filename,
        "colors_created": len(colors),
        "status": document.pipeline_status,
        "message": document.next_action,
    }


def list_pipeline_documents(
    session: Session,
    *,
    limit: int = 120,
) -> list[GenplanSourceDocument]:
    return list(
        session.scalars(
            select(GenplanSourceDocument)
            .order_by(
                GenplanSourceDocument.pipeline_status.asc(),
                GenplanSourceDocument.updated_at.desc(),
                GenplanSourceDocument.id.desc(),
            )
            .limit(max(1, min(limit, 500)))
        )
    )


def pipeline_document_stats(session: Session) -> dict[str, int]:
    rows = session.scalars(select(GenplanSourceDocument)).all()
    stats = {
        "total": len(rows),
        "ready_vector": 0,
        "ready_raster": 0,
        "legend_draft": 0,
        "needs_page": 0,
        "needs_review": 0,
        "missing": 0,
    }
    for row in rows:
        if row.pipeline_status == GenplanPipelineStatus.ready_for_vector_extraction.value:
            stats["ready_vector"] += 1
        elif row.pipeline_status == GenplanPipelineStatus.ready_for_legend_extraction.value:
            stats["ready_raster"] += 1
        elif row.pipeline_status == GenplanPipelineStatus.legend_draft_ready.value:
            stats["legend_draft"] += 1
        elif row.pipeline_status == GenplanPipelineStatus.needs_pdf_page_selection.value:
            stats["needs_page"] += 1
        elif row.pipeline_status == GenplanPipelineStatus.needs_review.value:
            stats["needs_review"] += 1
        elif row.pipeline_status == GenplanPipelineStatus.missing_file.value:
            stats["missing"] += 1
    return stats


def legend_entry_stats(session: Session) -> dict[str, int]:
    rows = session.scalars(select(GenplanLegendEntry)).all()
    return {
        "total": len(rows),
        "needs_review": sum(1 for row in rows if row.review_status == "needs_review"),
        "approved": sum(1 for row in rows if row.review_status == "approved"),
    }


def list_document_legend_entries(
    session: Session,
    document_id: int,
) -> list[GenplanLegendEntry]:
    return list(
        session.scalars(
            select(GenplanLegendEntry)
            .where(GenplanLegendEntry.document_id == document_id)
            .order_by(
                GenplanLegendEntry.pixel_count.desc().nullslast(),
                GenplanLegendEntry.id.asc(),
            )
        )
    )


def set_legend_entry_classification(
    session: Session,
    entry_id: int,
    *,
    target_category: str,
    layer_kind: str,
    review_status: str,
    notes: str = "",
) -> GenplanLegendEntry:
    """Record an operator's color-class decision for one legend entry.

    This only assigns a classification; it never touches search-affecting
    data. `tools.genplan_vectorize` only segments entries this leaves as
    review_status="approved".
    """
    entry = session.get(GenplanLegendEntry, entry_id)
    if entry is None:
        raise ValueError(f"Legend entry {entry_id} not found")
    if layer_kind not in {"allowed", "prohibited", "red_line", "unknown", "ignore"}:
        raise ValueError(f"Invalid layer_kind {layer_kind!r}")
    if review_status not in {"needs_review", "approved", "rejected"}:
        raise ValueError(f"Invalid review_status {review_status!r}")
    entry.target_category = target_category.strip() or "unknown"
    entry.layer_kind = layer_kind
    entry.review_status = review_status
    entry.notes = notes.strip() or None
    session.commit()
    return entry


def build_document_legend_export(
    session: Session,
    document_id: int,
    *,
    reviewer_id: str,
) -> dict[str, Any]:
    """Export a document's reviewed `GenplanLegendEntry` rows as legend.json.

    The output validates against `tools.genplan_vectorize.models.LegendDocument`
    so it can be handed to `tools.genplan_vectorize --legend` directly,
    together with the same document's `provenance.json` from
    `tools.genplan_export` (both reference the same original
    `source_sha256`, not the exported raster's hash).
    """
    from pydantic import ValidationError

    from tools.genplan_vectorize.models import LegendDocument

    document = session.get(GenplanSourceDocument, document_id)
    if document is None:
        raise ValueError(f"Genplan document {document_id} not found")
    if not document.source_sha256:
        raise ValueError(
            "Document has no source_sha256 yet; run inventory/inspection first"
        )
    entries = session.scalars(
        select(GenplanLegendEntry)
        .where(GenplanLegendEntry.document_id == document_id)
        .order_by(GenplanLegendEntry.color_hex.asc())
    ).all()
    if not entries:
        raise ValueError("Document has no legend entries yet")

    payload: dict[str, Any] = {
        "schema_version": "genplan-legend/v1",
        "record_id": document.asset_id,
        "source_sha256": document.source_sha256,
        "source_title": document.title or document.filename,
        "reviewer_id": reviewer_id,
        "reviewed_at_utc": datetime.now(UTC).isoformat(),
        "entries": [
            {
                "color_hex": entry.color_hex,
                "red": entry.red,
                "green": entry.green,
                "blue": entry.blue,
                "source": entry.source,
                "label_ru": entry.label_ru or "",
                "label_kz": entry.label_kz or "",
                "target_category": entry.target_category,
                "layer_kind": entry.layer_kind,
                "confidence_score": entry.confidence_score,
                "review_status": entry.review_status,
                "pixel_count": entry.pixel_count,
                "notes": entry.notes or "",
            }
            for entry in entries
        ],
    }
    try:
        LegendDocument.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Legend export failed schema validation: {exc}") from exc
    return payload


def _inspect_pdf_payload(
    payload: bytes,
    *,
    source_sha256: str,
) -> GenplanDocumentInspection:
    page_count = max(1, len(PDF_PAGE_RE.findall(payload)))
    image_count = len(PDF_IMAGE_RE.findall(payload))
    vector_count = len(VECTOR_OPERATOR_RE.findall(payload))
    has_text = b" BT" in payload or b"\nBT" in payload or b"/Font" in payload
    widths = [int(value) for value in PDF_IMAGE_WIDTH_RE.findall(payload)[:50]]
    heights = [int(value) for value in PDF_IMAGE_HEIGHT_RE.findall(payload)[:50]]
    max_width = max(widths, default=None)
    max_height = max(heights, default=None)
    if page_count > 1:
        status = GenplanPipelineStatus.needs_pdf_page_selection.value
        route = "multi_page_pdf"
        next_action = "select_main_plan_page"
        confidence = 0.45
    elif vector_count >= 20 or has_text:
        status = GenplanPipelineStatus.ready_for_vector_extraction.value
        route = "vector_pdf"
        next_action = "extract_vector_paths_by_fill_color"
        confidence = 0.7 if vector_count >= 20 else 0.55
    else:
        status = GenplanPipelineStatus.ready_for_legend_extraction.value
        route = "raster_pdf"
        next_action = "render_pdf_extract_legend_and_segment_colors"
        confidence = 0.6 if image_count else 0.35
    return GenplanDocumentInspection(
        detected_format=PDF_FORMAT,
        source_sha256=source_sha256,
        file_size_bytes=len(payload),
        page_count=page_count,
        pdf_route=route,
        has_text_layer=has_text,
        vector_object_count=vector_count,
        image_count=image_count,
        max_image_width=max_width,
        max_image_height=max_height,
        confidence_score=confidence,
        pipeline_status=status,
        next_action=next_action,
        raw_metadata={
            "inspection": "pdf_byte_scan",
            "page_count": page_count,
            "vector_object_count": vector_count,
            "image_count": image_count,
            "has_text_layer": has_text,
        },
    )


def _draft_colors_for_document(
    document: GenplanSourceDocument,
    path: Path,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if document.detected_format in RASTER_FORMATS:
        return _dominant_image_colors(path, limit=limit)
    if document.detected_format == PDF_FORMAT:
        vector_colors = (
            _pdf_fill_colors(path, limit=limit)
            if document.pdf_route == "vector_pdf"
            else []
        )
        return vector_colors or _rendered_pdf_colors(path, limit=limit)
    return []


def _dominant_image_colors(path: Path, *, limit: int) -> list[dict[str, Any]]:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as image:
        image.draft("RGB", (1536, 1536))
        image = image.convert("RGB")
        rows = _dominant_image_palette(image, limit=limit)
    for row in rows:
        row["source"] = "dominant_color"
        row["notes"] = "auto color draft from raster image; requires legend review"
    return rows


def _dominant_image_palette(image: Any, *, limit: int) -> list[dict[str, Any]]:
    image.thumbnail((768, 768))
    quantized = image.quantize(colors=48).convert("RGB")
    counts: Counter[tuple[int, int, int]] = Counter(quantized.getdata())
    rows = []
    for (red, green, blue), count in counts.most_common(96):
        if _skip_palette_color(red, green, blue):
            continue
        rows.append(
            {
                "color_hex": _hex_color(red, green, blue),
                "red": red,
                "green": green,
                "blue": blue,
                "source": "",
                "confidence_score": 0.35,
                "pixel_count": int(count),
                "notes": "",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _rendered_pdf_colors(path: Path, *, limit: int) -> list[dict[str, Any]]:
    import fitz
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    candidates: list[tuple[int, int, list[dict[str, Any]]]] = []
    with fitz.open(stream=path.read_bytes(), filetype="pdf") as document:
        for page_index in range(min(len(document), PDF_RENDER_MAX_PAGES)):
            page = document.load_page(page_index)
            max_side = max(float(page.rect.width), float(page.rect.height), 1.0)
            zoom = min(2.0, PDF_RENDER_MAX_SIDE_PX / max_side)
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(zoom, zoom),
                colorspace=fitz.csRGB,
                alpha=False,
            )
            image = Image.frombytes(
                "RGB",
                (pixmap.width, pixmap.height),
                pixmap.samples,
            )
            colors = _dominant_image_palette(image, limit=limit)
            score = sum(int(color.get("pixel_count") or 0) for color in colors)
            if colors:
                candidates.append((score, page_index + 1, colors))
    if not candidates:
        return []
    _, page_number, colors = max(candidates, key=lambda item: (item[0], len(item[2])))
    for color in colors:
        color["source"] = f"rendered_pdf_page_{page_number}"
        color["page_number"] = page_number
        color["confidence_score"] = max(float(color["confidence_score"]), 0.3)
        color["notes"] = (
            f"auto color draft from rendered PDF page {page_number}; "
            "requires legend review"
        )
    return colors


def _pdf_fill_colors(path: Path, *, limit: int) -> list[dict[str, Any]]:
    payload = path.read_bytes()
    counts: Counter[tuple[int, int, int]] = Counter()
    for match in PDF_RGB_FILL_RE.finditer(payload):
        red, green, blue = (
            max(0, min(255, round(float(match.group(index)) * 255)))
            for index in (1, 2, 3)
        )
        if _skip_palette_color(red, green, blue):
            continue
        counts[(red, green, blue)] += 1
    rows = []
    for (red, green, blue), count in counts.most_common(limit):
        rows.append(
            {
                "color_hex": _hex_color(red, green, blue),
                "red": red,
                "green": green,
                "blue": blue,
                "source": "vector_pdf_fill",
                "confidence_score": 0.45,
                "pixel_count": int(count),
                "notes": "auto color draft from PDF fill operators; requires legend review",
            }
        )
    return rows


def _skip_palette_color(red: int, green: int, blue: int) -> bool:
    if (
        red >= LIGHT_PIXEL_THRESHOLD
        and green >= LIGHT_PIXEL_THRESHOLD
        and blue >= LIGHT_PIXEL_THRESHOLD
    ):
        return True
    return max(red, green, blue) - min(red, green, blue) < LOW_SATURATION_THRESHOLD


def _hex_color(red: int, green: int, blue: int) -> str:
    return f"#{red:02x}{green:02x}{blue:02x}"


def _load_document_metadata(document: GenplanSourceDocument) -> dict[str, Any]:
    if not document.raw_metadata_json:
        return {}
    try:
        value = json.loads(document.raw_metadata_json)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _record_from_document(document: GenplanSourceDocument) -> ManualGenplanRecord:
    return ManualGenplanRecord(
        asset_id=document.asset_id,
        region=document.region,
        district=document.district,
        locality=document.locality,
        title=document.title,
        relative_path=document.relative_path,
        filename=document.filename,
        extension=f".{document.detected_format}",
        media_type=document.media_type,
        size_bytes=document.file_size_bytes,
        confidence="",
    )


def _upsert_pipeline_document(
    session: Session,
    *,
    record: ManualGenplanRecord,
    inspection: GenplanDocumentInspection,
    ingested_by: str | None,
) -> bool:
    document = session.scalar(
        select(GenplanSourceDocument).where(
            GenplanSourceDocument.asset_id == record.asset_id
        )
    )
    created = document is None
    if document is None:
        document = GenplanSourceDocument(asset_id=record.asset_id)
        session.add(document)
    document.region = record.region
    document.district = record.district
    document.locality = record.locality
    document.title = record.title
    document.filename = record.filename
    document.relative_path = record.relative_path
    document.media_type = record.media_type
    document.detected_format = inspection.detected_format
    document.file_size_bytes = inspection.file_size_bytes or record.size_bytes
    document.source_sha256 = inspection.source_sha256
    document.page_count = inspection.page_count
    document.pdf_route = inspection.pdf_route
    document.has_text_layer = inspection.has_text_layer
    document.vector_object_count = inspection.vector_object_count
    document.image_count = inspection.image_count
    document.max_image_width = inspection.max_image_width
    document.max_image_height = inspection.max_image_height
    document.confidence_score = inspection.confidence_score
    document.pipeline_status = inspection.pipeline_status
    document.next_action = inspection.next_action
    document.error_message = inspection.error_message
    document.raw_metadata_json = (
        json.dumps(inspection.raw_metadata, ensure_ascii=False, sort_keys=True)
        if inspection.raw_metadata
        else None
    )
    document.ingested_by = ingested_by
    session.commit()
    return created
