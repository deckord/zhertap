from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
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
PDF_LABEL_SCAN_MAX_PAGES = 50
AUTO_NOTE_PREFIX = "auto-classifier:"
PIPELINE_STATE_PRESERVED_BY_LEGEND = {
    GenplanPipelineStatus.legend_draft_ready.value,
    GenplanPipelineStatus.needs_review.value,
    GenplanPipelineStatus.failed.value,
}
LPH_KEYWORDS = (
    "lph",
    "household",
    "private subsidiary",
    "residential",
    "лпх",
    "личное подсоб",
    "приусад",
    "усадеб",
    "индивидуальн",
    "жил",
    "ж-1",
    "ж-2",
    "ж1",
    "ж2",
)
GARDENING_KEYWORDS = ("garden", "gardening", "dacha", "садовод", "садов", "дач")
RED_LINE_KEYWORDS = ("красн", "қызыл", "red line")
PROHIBITED_KEYWORDS = (
    "industrial",
    "sanitary",
    "cemetery",
    "road",
    "street",
    "reserve",
    "санитар",
    "охран",
    "водоохран",
    "кладбищ",
    "пром",
    "производ",
    "дорог",
    "улиц",
    "магистрал",
    "резерв",
)
IGNORE_KEYWORDS = (
    "background",
    "boundary",
    "label",
    "existing",
    "projected",
    "фон",
    "подпись",
    "границ",
    "существующ",
    "проектируем",
)


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
            label_ru=color.get("label_ru"),
            label_kz=color.get("label_kz"),
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
    status_counts = {
        str(status): int(count)
        for status, count in session.execute(
            select(
                GenplanSourceDocument.pipeline_status,
                func.count(GenplanSourceDocument.id),
            ).group_by(GenplanSourceDocument.pipeline_status)
        ).all()
    }
    stats = {
        "total": sum(status_counts.values()),
        "ready_vector": status_counts.get(
            GenplanPipelineStatus.ready_for_vector_extraction.value, 0
        ),
        "ready_raster": status_counts.get(
            GenplanPipelineStatus.ready_for_legend_extraction.value, 0
        ),
        "legend_draft": status_counts.get(
            GenplanPipelineStatus.legend_draft_ready.value, 0
        ),
        "needs_page": status_counts.get(
            GenplanPipelineStatus.needs_pdf_page_selection.value, 0
        ),
        "needs_review": status_counts.get(GenplanPipelineStatus.needs_review.value, 0),
        "missing": status_counts.get(GenplanPipelineStatus.missing_file.value, 0),
    }
    return stats


def legend_entry_stats(session: Session) -> dict[str, int]:
    status_counts = {
        str(status): int(count)
        for status, count in session.execute(
            select(
                GenplanLegendEntry.review_status,
                func.count(GenplanLegendEntry.id),
            ).group_by(GenplanLegendEntry.review_status)
        ).all()
    }
    auto_count = session.scalar(
        select(func.count(GenplanLegendEntry.id)).where(
            GenplanLegendEntry.notes.contains(AUTO_NOTE_PREFIX)
        )
    )
    return {
        "total": sum(status_counts.values()),
        "needs_review": status_counts.get("needs_review", 0),
        "approved": status_counts.get("approved", 0),
        "rejected": status_counts.get("rejected", 0),
        "auto_classified": int(auto_count or 0),
    }


def auto_classify_legend_entries(
    session: Session,
    *,
    limit: int = 2000,
    override_existing: bool = False,
) -> dict[str, int]:
    """Conservatively classify draft legend colors.

    Text matches can be approved because the label carries semantic meaning.
    Color-only matches are intentionally conservative: they mark likely classes
    or reject obvious non-target colors, but they do not approve LPH/gardening.
    """
    limit = max(1, min(limit, 10000))
    statement = select(GenplanLegendEntry).order_by(GenplanLegendEntry.id.asc())
    if not override_existing:
        statement = statement.where(GenplanLegendEntry.review_status == "needs_review")
    entries = session.scalars(statement.limit(limit)).all()
    stats = {
        "scanned": 0,
        "changed": 0,
        "approved": 0,
        "rejected": 0,
        "candidates": 0,
        "unchanged": 0,
    }
    for entry in entries:
        stats["scanned"] += 1
        if not override_existing and _has_operator_decision(entry):
            stats["unchanged"] += 1
            continue
        decision = _infer_legend_entry_decision(entry)
        if decision is None:
            stats["unchanged"] += 1
            continue
        target_category, layer_kind, review_status, confidence, reason = decision
        if not override_existing and _same_legend_decision(
            entry,
            target_category=target_category,
            layer_kind=layer_kind,
            review_status=review_status,
        ):
            stats["unchanged"] += 1
            continue
        entry.target_category = target_category
        entry.layer_kind = layer_kind
        entry.review_status = review_status
        entry.confidence_score = max(float(entry.confidence_score or 0), confidence)
        entry.notes = _append_auto_note(entry.notes, reason)
        stats["changed"] += 1
        if review_status == "approved":
            stats["approved"] += 1
        elif review_status == "rejected":
            stats["rejected"] += 1
        else:
            stats["candidates"] += 1
    session.commit()
    return stats


def enrich_pdf_legend_entry_labels(
    session: Session,
    *,
    limit_docs: int = 50,
) -> dict[str, int]:
    """Fill legend labels for existing PDF documents when vector text is available."""
    limit_docs = max(1, min(limit_docs, 500))
    documents = session.scalars(
        select(GenplanSourceDocument)
        .where(GenplanSourceDocument.detected_format == PDF_FORMAT)
        .where(
            select(GenplanLegendEntry.id)
            .where(GenplanLegendEntry.document_id == GenplanSourceDocument.id)
            .exists()
        )
        .order_by(GenplanSourceDocument.updated_at.asc(), GenplanSourceDocument.id.asc())
        .limit(limit_docs)
    ).all()
    stats = {
        "documents_scanned": 0,
        "documents_with_labels": 0,
        "labels_found": 0,
        "entries_updated": 0,
        "entries_created": 0,
        "missing_files": 0,
        "unchanged": 0,
    }
    for document in documents:
        stats["documents_scanned"] += 1
        path = resolve_manual_genplan_file(_record_from_document(document))
        if path is None:
            stats["missing_files"] += 1
            continue
        rows = [
            row
            for row in _pdf_fill_colors_from_drawings(path, limit=64)
            if row.get("label_ru") or row.get("label_kz")
        ]
        if not rows:
            stats["unchanged"] += 1
            continue
        stats["documents_with_labels"] += 1
        stats["labels_found"] += len(rows)
        existing = list_document_legend_entries(session, document.id)
        by_color: dict[str, list[GenplanLegendEntry]] = {}
        for entry in existing:
            by_color.setdefault(entry.color_hex.casefold(), []).append(entry)
        for row in rows:
            color_hex = str(row["color_hex"]).casefold()
            matches = by_color.get(color_hex) or []
            if matches:
                for entry in matches:
                    if not entry.label_ru and row.get("label_ru"):
                        entry.label_ru = str(row["label_ru"])
                    if not entry.label_kz and row.get("label_kz"):
                        entry.label_kz = str(row["label_kz"])
                    entry.confidence_score = max(
                        float(entry.confidence_score or 0),
                        float(row.get("confidence_score") or 0),
                    )
                    if row.get("notes"):
                        entry.notes = _append_note_once(entry.notes, str(row["notes"]))
                    stats["entries_updated"] += 1
                continue
            entry = GenplanLegendEntry(
                document_id=document.id,
                color_hex=row["color_hex"],
                red=row["red"],
                green=row["green"],
                blue=row["blue"],
                source=row["source"],
                label_ru=row.get("label_ru"),
                label_kz=row.get("label_kz"),
                target_category="unknown",
                layer_kind="unknown",
                confidence_score=float(row.get("confidence_score") or 0.45),
                review_status="needs_review",
                pixel_count=row.get("pixel_count"),
                notes=row.get("notes"),
            )
            session.add(entry)
            stats["entries_created"] += 1
    session.commit()
    return stats


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


def _infer_legend_entry_decision(
    entry: GenplanLegendEntry,
) -> tuple[str, str, str, float, str] | None:
    label = " ".join(
        value.strip().casefold()
        for value in (entry.label_ru or "", entry.label_kz or "")
        if value.strip()
    )
    if label:
        if _contains_any(label, LPH_KEYWORDS):
            return (
                "lph-household",
                "allowed",
                "approved",
                0.82,
                "label keywords indicate LPH/residential allowed zone",
            )
        if _contains_any(label, GARDENING_KEYWORDS):
            return (
                "gardening",
                "allowed",
                "approved",
                0.82,
                "label keywords indicate gardening allowed zone",
            )
        if _contains_any(label, RED_LINE_KEYWORDS):
            return (
                "red_line",
                "red_line",
                "approved",
                0.86,
                "label keywords indicate red line",
            )
        if _contains_any(label, PROHIBITED_KEYWORDS):
            return (
                "restricted",
                "prohibited",
                "approved",
                0.78,
                "label keywords indicate restricted/prohibited zone",
            )
        if _contains_any(label, IGNORE_KEYWORDS):
            return (
                "other",
                "ignore",
                "rejected",
                0.72,
                "label keywords indicate non-target map element",
            )

    hue, saturation, value = _rgb_to_hsv(entry.red, entry.green, entry.blue)
    if saturation < 0.18 or value < 0.18:
        return (
            "other",
            "ignore",
            "rejected",
            0.62,
            "low-saturation or very dark color is likely labels/background",
        )
    if 185 <= hue <= 255:
        return (
            "water_or_infrastructure",
            "ignore",
            "rejected",
            0.62,
            "blue/cyan color is normally not an LPH/gardening allowed zone",
        )
    if hue <= 12 or hue >= 345:
        return (
            "red_line_candidate",
            "red_line",
            "needs_review",
            0.58,
            "red color is likely a red line or boundary; needs confirmation",
        )
    if 18 <= hue <= 65:
        return (
            "residential_or_allowed_candidate",
            "allowed",
            "needs_review",
            0.55,
            "warm yellow/orange color is often residential; needs confirmation",
        )
    if 70 <= hue <= 165:
        return (
            "green_zone_or_garden_candidate",
            "unknown",
            "needs_review",
            0.5,
            "green color may be recreation/agriculture/gardening; needs text confirmation",
        )
    if 260 <= hue <= 335:
        return (
            "public_or_industrial_candidate",
            "unknown",
            "needs_review",
            0.48,
            "purple/pink color often requires legend confirmation",
        )
    return None


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _rgb_to_hsv(red: int, green: int, blue: int) -> tuple[float, float, float]:
    r = red / 255
    g = green / 255
    b = blue / 255
    high = max(r, g, b)
    low = min(r, g, b)
    delta = high - low
    if delta == 0:
        hue = 0.0
    elif high == r:
        hue = (60 * ((g - b) / delta) + 360) % 360
    elif high == g:
        hue = 60 * ((b - r) / delta) + 120
    else:
        hue = 60 * ((r - g) / delta) + 240
    saturation = 0.0 if high == 0 else delta / high
    return hue, saturation, high


def _has_operator_decision(entry: GenplanLegendEntry) -> bool:
    notes = entry.notes or ""
    return (
        entry.review_status in {"approved", "rejected"}
        and AUTO_NOTE_PREFIX not in notes
    )


def _same_legend_decision(
    entry: GenplanLegendEntry,
    *,
    target_category: str,
    layer_kind: str,
    review_status: str,
) -> bool:
    return (
        entry.target_category == target_category
        and entry.layer_kind == layer_kind
        and entry.review_status == review_status
    )


def _append_auto_note(notes: str | None, reason: str) -> str:
    new_note = f"{AUTO_NOTE_PREFIX} {reason}"
    return _append_note_once(notes, new_note)


def _append_note_once(notes: str | None, new_note: str) -> str:
    if not notes:
        return new_note
    if new_note in notes:
        return notes
    return f"{notes}; {new_note}"


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
    labeled_rows = _pdf_fill_colors_from_drawings(path, limit=limit)
    if labeled_rows:
        return labeled_rows

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


def _pdf_fill_colors_from_drawings(path: Path, *, limit: int) -> list[dict[str, Any]]:
    try:
        import fitz
    except ImportError:
        return []

    try:
        document = fitz.open(stream=path.read_bytes(), filetype="pdf")
    except Exception:
        return []
    counts: Counter[tuple[int, int, int]] = Counter()
    labels: dict[tuple[int, int, int], tuple[float, str]] = {}
    try:
        for page_index, page in enumerate(document):
            if page_index >= PDF_LABEL_SCAN_MAX_PAGES:
                break
            text_lines = _pdf_page_text_lines(page)
            page_rect = page.rect
            for drawing in page.get_drawings():
                fill = drawing.get("fill")
                if not fill:
                    continue
                red, green, blue = (
                    max(0, min(255, round(float(fill[index]) * 255)))
                    for index in (0, 1, 2)
                )
                if _skip_palette_color(red, green, blue):
                    continue
                color_key = (red, green, blue)
                counts[color_key] += 1
                rect = drawing.get("rect")
                if not rect or not _is_likely_legend_swatch(rect, page_rect):
                    continue
                label = _nearest_pdf_legend_label(rect, text_lines)
                if not label:
                    continue
                distance, text = label
                current = labels.get(color_key)
                if current is None or distance < current[0]:
                    labels[color_key] = (distance, text)
    except Exception:
        return []
    finally:
        document.close()

    rows = []
    for (red, green, blue), count in counts.most_common(limit):
        color_key = (red, green, blue)
        label = labels.get(color_key, (0.0, ""))[1]
        notes = "auto color draft from PDF drawing objects; requires legend review"
        if label:
            notes = f"{notes}; nearby legend text: {label}"
        rows.append(
            {
                "color_hex": _hex_color(red, green, blue),
                "red": red,
                "green": green,
                "blue": blue,
                "source": "vector_pdf_drawing",
                "label_ru": label or None,
                "confidence_score": 0.58 if label else 0.45,
                "pixel_count": int(count),
                "notes": notes,
            }
        )
    return rows


def _pdf_page_text_lines(page: Any) -> list[dict[str, Any]]:
    words = page.get_text("words")
    grouped: dict[tuple[int, int], list[Any]] = {}
    for word in words:
        if len(word) < 8:
            continue
        text = str(word[4]).strip()
        if not text:
            continue
        grouped.setdefault((int(word[5]), int(word[6])), []).append(word)

    lines = []
    for items in grouped.values():
        ordered = sorted(items, key=lambda item: (float(item[0]), float(item[1])))
        text = " ".join(str(item[4]).strip() for item in ordered if str(item[4]).strip())
        if not text:
            continue
        lines.append(
            {
                "x0": min(float(item[0]) for item in ordered),
                "y0": min(float(item[1]) for item in ordered),
                "x1": max(float(item[2]) for item in ordered),
                "y1": max(float(item[3]) for item in ordered),
                "text": text[:240],
            }
        )
    return lines


def _is_likely_legend_swatch(rect: Any, page_rect: Any) -> bool:
    width = float(rect.width)
    height = float(rect.height)
    if width < 3 or height < 3 or width > 120 or height > 120:
        return False
    area = width * height
    page_area = max(1.0, float(page_rect.width) * float(page_rect.height))
    if area > page_area * 0.015:
        return False
    aspect = width / max(height, 1.0)
    return 0.15 <= aspect <= 6.0


def _nearest_pdf_legend_label(
    rect: Any,
    text_lines: list[dict[str, Any]],
) -> tuple[float, str] | None:
    center_y = (float(rect.y0) + float(rect.y1)) / 2
    center_x = (float(rect.x0) + float(rect.x1)) / 2
    best: tuple[float, str] | None = None
    for line in text_lines:
        text = str(line["text"]).strip()
        if not text:
            continue
        line_center_y = (float(line["y0"]) + float(line["y1"])) / 2
        line_center_x = (float(line["x0"]) + float(line["x1"])) / 2
        right_gap = float(line["x0"]) - float(rect.x1)
        vertical_gap = float(line["y0"]) - float(rect.y1)
        same_row = -2 <= right_gap <= 360 and abs(line_center_y - center_y) <= 24
        below = -8 <= (float(line["x0"]) - float(rect.x0)) <= 80 and 0 <= vertical_gap <= 60
        if not same_row and not below:
            continue
        distance = abs(line_center_y - center_y) + max(0.0, right_gap)
        if below:
            distance = abs(line_center_x - center_x) + vertical_gap + 80
        if best is None or distance < best[0]:
            best = (distance, text)
    return best


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
    preserve_pipeline_state = (
        not created and _should_preserve_pipeline_document_state(session, document)
    )
    preserved_status = document.pipeline_status
    preserved_next_action = document.next_action
    preserved_error_message = document.error_message
    preserved_raw_metadata_json = document.raw_metadata_json
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
    if preserve_pipeline_state:
        document.pipeline_status = preserved_status
        document.next_action = preserved_next_action
        document.error_message = preserved_error_message
        document.raw_metadata_json = preserved_raw_metadata_json
    else:
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


def _should_preserve_pipeline_document_state(
    session: Session,
    document: GenplanSourceDocument,
) -> bool:
    if document.pipeline_status in PIPELINE_STATE_PRESERVED_BY_LEGEND:
        return True
    return session.scalar(
        select(GenplanLegendEntry.id)
        .where(GenplanLegendEntry.document_id == document.id)
        .limit(1)
    ) is not None
