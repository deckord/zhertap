from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

EXTRACTOR_VERSION = "auction-legal-doc.v2"
MAX_SCAN_BLOCK_CHARS = 4_000
MAX_METADATA_TITLE_CHARS = 320
MAX_METADATA_URL_CHARS = 2_048
MAX_METADATA_ID_CHARS = 128
IMAGE_FILE_TYPES = {"jpg", "jpeg", "png"}
TEXT_FILE_TYPES = {"pdf", "docx", *IMAGE_FILE_TYPES}
ExtractionStatus = Literal[
    "ok", "unknown", "unsupported", "oversized", "encrypted", "corrupt"
]


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    document_id: int | str | None
    title: str
    source_url: str
    file_type: str | None = None
    observed_at: datetime | None = None
    lot_context: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_file_bytes: int = 8_000_000
    max_pages: int = 80
    max_text_chars: int = 500_000
    max_candidates: int = 100
    max_excerpt_chars: int = 240
    max_docx_entries: int = 500
    max_docx_uncompressed_bytes: int = 20_000_000


def _bounded_limits(limits: ExtractionLimits) -> ExtractionLimits:
    defaults = ExtractionLimits()

    def cap(value: int, hard_limit: int) -> int:
        return max(1, min(int(value), hard_limit))

    return ExtractionLimits(
        max_file_bytes=cap(limits.max_file_bytes, defaults.max_file_bytes),
        max_pages=cap(limits.max_pages, defaults.max_pages),
        max_text_chars=cap(limits.max_text_chars, defaults.max_text_chars),
        max_candidates=cap(limits.max_candidates, defaults.max_candidates),
        max_excerpt_chars=cap(limits.max_excerpt_chars, defaults.max_excerpt_chars),
        max_docx_entries=cap(limits.max_docx_entries, defaults.max_docx_entries),
        max_docx_uncompressed_bytes=cap(
            limits.max_docx_uncompressed_bytes,
            defaults.max_docx_uncompressed_bytes,
        ),
    )


@dataclass(frozen=True, slots=True)
class DocumentFactCandidate:
    """A review candidate extracted from text, never a confirmed legal fact."""

    field: str
    value: object
    document_id: int | str | None
    document_title: str
    source_url: str
    page: int | None
    section: str | None
    evidence_excerpt: str
    quote_hash: str
    content_hash: str
    extractor_version: str
    confidence: float
    observed_at: datetime | None
    extracted_at: datetime
    status: str = "preliminary"

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.isoformat() if self.observed_at else None
        payload["extracted_at"] = self.extracted_at.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class CandidateConflict:
    field: str
    values: tuple[object, ...]
    candidate_indexes: tuple[int, ...]
    # Official E-Qazyna/lot-card value is not a document candidate and therefore
    # cannot be represented by ``candidate_indexes``. Preserve it explicitly so
    # review UIs can show both sides of a card-versus-document contradiction.
    lot_context_value: object | None = None


@dataclass(frozen=True, slots=True)
class DocumentExtractionResult:
    status: ExtractionStatus
    candidates: tuple[DocumentFactCandidate, ...]
    conflicts: tuple[CandidateConflict, ...]
    content_hash: str | None
    pages_processed: int
    text_chars_processed: int
    detail: str | None = None
    extractor_version: str = EXTRACTOR_VERSION
    summary: str | None = None
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "conflicts": [asdict(conflict) for conflict in self.conflicts],
            "content_hash": self.content_hash,
            "pages_processed": self.pages_processed,
            "text_chars_processed": self.text_chars_processed,
            "detail": self.detail,
            "extractor_version": self.extractor_version,
            "summary": self.summary,
            "risks": list(self.risks),
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True, slots=True)
class DocumentTextExtractionResult:
    status: ExtractionStatus
    text: str
    content_hash: str | None
    pages_processed: int
    text_chars_processed: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class _TextBlock:
    text: str
    page: int | None
    section: str | None


_MONEY = re.compile(
    r"(?P<amount>\d{1,3}(?:[ \u00a0\u202f]\d{3})*(?:[,.]\d{1,2})?|\d+(?:[,.]\d{1,2})?)"
    r"\s*(?:₸|тг\.?|тенге|теңге)",
    re.IGNORECASE,
)
_LEASE_TERM = re.compile(
    r"(?:срок(?:\s+(?:аренды|землепользования|права))?|аренд[аы]\s+сроком|"
    r"жалдау\s+мерзімі)"
    r"\s*[:\-]?\s*[^\n.;]{0,80}?(?P<number>\d+(?:[,.]\d+)?)\s*"
    r"(?P<unit>лет|года?|месяц(?:а|ев)?|жыл|ай)",
    re.IGNORECASE,
)
_DEADLINE = re.compile(
    r"(?P<date>\d{1,2}[./-]\d{1,2}[./-]\d{4})|"
    r"(?P<duration>(?:в\s+течение|не\s+позднее)\s+\d+\s+"
    r"(?:дн(?:я|ей)|месяц(?:а|ев)?|лет|года?))",
    re.IGNORECASE,
)

_LABEL_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "development_obligation",
        re.compile(r"осво|застро|строительств|ввод\w*\s+в\s+эксплуатац", re.IGNORECASE),
        0.82,
    ),
    (
        "termination_ground",
        re.compile(r"расторж|прекращен|основани\w*\s+(?:для\s+)?прекращ", re.IGNORECASE),
        0.84,
    ),
    (
        "renewal_condition",
        re.compile(r"продлен|продление|возобновлен|преимущественн\w*\s+прав", re.IGNORECASE),
        0.82,
    ),
    (
        "transfer_right",
        re.compile(r"субаренд|переуступ|уступк\w*\s+прав|передач\w*\s+прав", re.IGNORECASE),
        0.84,
    ),
    (
        "responsibility_penalty",
        re.compile(r"штраф|пен[яи]|неустойк|ответственност", re.IGNORECASE),
        0.84,
    ),
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _money(value: str) -> float | None:
    normalized = re.sub(r"[ \u00a0\u202f]", "", value).replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def _read_input(
    source: bytes | bytearray | Path | str,
    limit: int,
) -> tuple[bytes | None, str | None]:
    if isinstance(source, (bytes, bytearray)):
        if len(source) > limit:
            return None, "file exceeds byte limit"
        return bytes(source), None
    path = Path(source)
    try:
        if path.stat().st_size > limit:
            return None, "file exceeds byte limit"
        data = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read local file: {exc.__class__.__name__}"
    return (data, None) if len(data) <= limit else (None, "file exceeds byte limit")


def _file_kind(metadata: DocumentMetadata, source: bytes | bytearray | Path | str) -> str:
    declared = (metadata.file_type or "").strip().casefold().lstrip(".")
    if declared in {"pdf", "doc", "docx", *IMAGE_FILE_TYPES}:
        return declared
    if not isinstance(source, (bytes, bytearray)):
        suffix = Path(source).suffix.casefold().lstrip(".")
        if suffix in {"pdf", "doc", "docx", *IMAGE_FILE_TYPES}:
            return suffix
    return declared or "unknown"


def _metadata_error(metadata: DocumentMetadata, extracted_at: datetime | None) -> str | None:
    if not metadata.title or len(metadata.title) > MAX_METADATA_TITLE_CHARS:
        return "document title is empty or exceeds limit"
    if not metadata.source_url or len(metadata.source_url) > MAX_METADATA_URL_CHARS:
        return "document source URL is empty or exceeds limit"
    if metadata.document_id is not None:
        if not isinstance(metadata.document_id, (int, str)):
            return "document id has unsupported type"
        if len(str(metadata.document_id)) > MAX_METADATA_ID_CHARS:
            return "document id exceeds limit"
    if metadata.observed_at is not None and metadata.observed_at.utcoffset() is None:
        return "observed_at must be timezone-aware"
    if extracted_at is not None and extracted_at.utcoffset() is None:
        return "extracted_at must be timezone-aware"
    return None


def _pdf_blocks(data: bytes, limits: ExtractionLimits) -> tuple[list[_TextBlock], int, str | None]:
    try:
        import fitz

        document = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return [], 0, "corrupt"
    try:
        if document.needs_pass:
            return [], 0, "encrypted"
        if document.page_count > limits.max_pages:
            return [], 0, "oversized"
        blocks: list[_TextBlock] = []
        consumed = 0
        for page_index in range(document.page_count):
            try:
                text = document.load_page(page_index).get_text("text")
            except Exception:
                return [], page_index, "corrupt"
            if consumed + len(text) > limits.max_text_chars:
                return [], page_index, "oversized"
            consumed += len(text)
            for line_index, line in enumerate(text.splitlines(), start=1):
                cleaned = _clean(line)
                if cleaned:
                    blocks.append(
                        _TextBlock(cleaned, page=page_index + 1, section=f"line:{line_index}")
                    )
        if blocks:
            return blocks, document.page_count, None
        return _pdf_ocr_blocks(data, limits)
    finally:
        document.close()


def _pdf_ocr_blocks(
    data: bytes,
    limits: ExtractionLimits,
) -> tuple[list[_TextBlock], int, str | None]:
    """OCR image-only PDF pages through the existing bounded image OCR path."""
    try:
        import fitz

        document = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return [], 0, "corrupt"
    try:
        if document.needs_pass:
            return [], 0, "encrypted"
        if document.page_count > limits.max_pages:
            return [], document.page_count, "oversized"
        blocks: list[_TextBlock] = []
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            image_data = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
            page_blocks, _pages, error = _image_blocks(image_data, "png", limits)
            if error:
                if error == "ocr_unavailable":
                    return [], document.page_count, error
                continue
            blocks.extend(
                _TextBlock(item.text, page=page_index + 1, section=item.section)
                for item in page_blocks
            )
            if sum(len(item.text) for item in blocks) > limits.max_text_chars:
                return [], document.page_count, "oversized"
        return blocks, document.page_count, None
    except Exception:
        return [], document.page_count, "ocr_failed"
    finally:
        document.close()


def _docx_blocks(data: bytes, limits: ExtractionLimits) -> tuple[list[_TextBlock], str | None]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        return [], "corrupt"
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_docx_entries:
            return [], "oversized"
        if sum(info.file_size for info in infos) > limits.max_docx_uncompressed_bytes:
            return [], "oversized"
        try:
            xml_data = archive.read("word/document.xml")
        except (KeyError, RuntimeError, zipfile.BadZipFile):
            return [], "corrupt"
    if len(xml_data) > limits.max_docx_uncompressed_bytes:
        return [], "oversized"
    try:
        root = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError:
        return [], "corrupt"
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    blocks: list[_TextBlock] = []
    text_chars = 0
    paragraph_index = 0
    table_sections: dict[int, str] = {}
    for table_index, table in enumerate(root.iter(f"{namespace}tbl"), start=1):
        for row_index, row in enumerate(table.findall(f"{namespace}tr"), start=1):
            for cell_index, cell in enumerate(row.findall(f"{namespace}tc"), start=1):
                for cell_paragraph_index, paragraph in enumerate(
                    cell.iter(f"{namespace}p"), start=1
                ):
                    table_sections[id(paragraph)] = (
                        f"table:{table_index}/row:{row_index}/cell:{cell_index}/"
                        f"paragraph:{cell_paragraph_index}"
                    )
    for paragraph in root.iter(f"{namespace}p"):
        paragraph_index += 1
        text = _clean("".join(node.text or "" for node in paragraph.iter(f"{namespace}t")))
        if not text:
            continue
        text_chars += len(text)
        if text_chars > limits.max_text_chars:
            return [], "oversized"
        section = table_sections.get(id(paragraph), f"paragraph:{paragraph_index}")
        blocks.append(_TextBlock(text, page=None, section=section))
    return blocks, None


def _image_blocks(
    data: bytes,
    kind: str,
    limits: ExtractionLimits,
) -> tuple[list[_TextBlock], int, str | None]:
    if shutil.which("tesseract") is None:
        return [], 1, "ocr_unavailable"
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image.verify()
    except Exception:
        return [], 0, "corrupt"
    suffix = ".jpg" if kind in {"jpg", "jpeg"} else ".png"
    try:
        with tempfile.TemporaryDirectory(prefix="auction-ocr-") as tmp:
            source = Path(tmp) / f"document{suffix}"
            source.write_bytes(data)
            command = [
                "tesseract",
                str(source),
                "stdout",
                "-l",
                "kaz+rus+eng",
                "--psm",
                "6",
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
    except subprocess.TimeoutExpired:
        return [], 1, "ocr_timeout"
    except OSError:
        return [], 1, "ocr_unavailable"
    if completed.returncode != 0:
        return [], 1, "ocr_failed"
    blocks: list[_TextBlock] = []
    consumed = 0
    for index, line in enumerate(completed.stdout.splitlines(), start=1):
        cleaned = _clean(line)
        if not cleaned:
            continue
        consumed += len(cleaned)
        if consumed > limits.max_text_chars:
            return [], 1, "oversized"
        blocks.append(_TextBlock(cleaned, page=1, section=f"ocr-line:{index}"))
    return blocks, 1, None


def _status_for_extraction_error(error: str) -> ExtractionStatus:
    if error in {"ocr_unavailable", "ocr_timeout", "ocr_failed"}:
        return "unknown"
    if error in {"unsupported", "oversized", "encrypted", "corrupt"}:
        return error
    return "corrupt"


def _excerpt(text: str, limit: int) -> str:
    """Keep a bounded excerpt, but never cut a legal sentence mid-clause when possible."""
    cleaned = _clean(text)
    if len(cleaned) <= limit:
        return cleaned
    # Extend up to one bounded clause beyond the display limit. The UI should
    # show a complete obligation, not an orphaned fragment such as "должна была".
    sentence_end = re.search(r"[.!?](?=$|\s)", cleaned[limit : min(len(cleaned), limit + 360)])
    if sentence_end is not None:
        return cleaned[: limit + sentence_end.end()].rstrip()
    return cleaned[: limit - 1].rstrip() + "…"


def _candidate(
    field: str,
    value: object,
    block: _TextBlock,
    metadata: DocumentMetadata,
    *,
    content_hash: str,
    confidence: float,
    extracted_at: datetime,
    limits: ExtractionLimits,
) -> DocumentFactCandidate:
    excerpt = _excerpt(block.text, limits.max_excerpt_chars)
    return DocumentFactCandidate(
        field=field,
        value=value,
        document_id=metadata.document_id,
        document_title=metadata.title,
        source_url=metadata.source_url,
        page=block.page,
        section=block.section,
        evidence_excerpt=excerpt,
        quote_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        content_hash=content_hash,
        extractor_version=EXTRACTOR_VERSION,
        confidence=confidence,
        observed_at=metadata.observed_at,
        extracted_at=extracted_at,
    )


def _contextual_text_block(blocks: list[_TextBlock], index: int) -> _TextBlock:
    """Join consecutive source lines until a clause reaches sentence context."""
    base = blocks[index]
    # Only PDF/OCR extraction turns one visual sentence into consecutive line
    # blocks. DOCX paragraphs and table cells are already semantic units;
    # joining them can attribute a payment or obligation to the preceding
    # paragraph and destroys the exact table-cell provenance.
    if not (base.section or "").startswith(("line:", "ocr-line:")):
        return base
    parts = [base.text]
    # PDF text extraction often breaks one legal sentence across physical lines.
    # Keep the original page/first-line provenance, but never cross a page.
    for following in blocks[index + 1 : index + 4]:
        if following.page != base.page:
            break
        if len(" ".join(parts)) >= MAX_SCAN_BLOCK_CHARS:
            break
        parts.append(following.text)
        joined = _clean(" ".join(parts))
        if re.search(r"[.!?](?:$|\s)", joined):
            break
    return _TextBlock(_clean(" ".join(parts))[:MAX_SCAN_BLOCK_CHARS], base.page, base.section)


def _extract_candidates(
    blocks: list[_TextBlock],
    metadata: DocumentMetadata,
    content_hash: str,
    limits: ExtractionLimits,
    extracted_at: datetime,
) -> list[DocumentFactCandidate]:
    result: list[DocumentFactCandidate] = []
    seen: set[tuple[str, str, int | None, str | None]] = set()

    def add(field: str, value: object, block: _TextBlock, confidence: float) -> None:
        if len(result) >= limits.max_candidates:
            return
        marker = (field, repr(value), block.page, block.section)
        if marker in seen:
            return
        seen.add(marker)
        result.append(
            _candidate(
                field,
                value,
                block,
                metadata,
                content_hash=content_hash,
                confidence=confidence,
                extracted_at=extracted_at,
                limits=limits,
            )
        )

    for index, _block in enumerate(blocks):
        scan_block = _contextual_text_block(blocks, index)
        scan_text = scan_block.text
        lowered = scan_text.casefold()
        if re.search(r"частн\w*\s+собствен|право\s+собственност", lowered):
            add("right_type", "ownership", scan_block, 0.9)
        elif re.search(r"право\s+аренд|временн\w*\s+землепольз|срок\s+аренд", lowered):
            add("right_type", "lease", scan_block, 0.9)

        lease_match = _LEASE_TERM.search(scan_text)
        if lease_match:
            amount = float(lease_match.group("number").replace(",", "."))
            unit = lease_match.group("unit").casefold()
            add(
                "lease_term_years",
                amount / 12 if "месяц" in unit or unit == "ай" else amount,
                scan_block,
                0.91,
            )

        money_match = _MONEY.search(scan_text)
        if money_match:
            amount = _money(money_match.group("amount"))
            if amount is not None:
                if re.search(r"ежегодн|годов\w*\s+аренд|жыл\s+сайын|жылдық", lowered):
                    add("annual_payment_kzt", amount, scan_block, 0.92)
                elif re.search(r"гарантийн\w*\s+взнос|кепілдік\s+жарна", lowered):
                    add("guarantee_payment_kzt", amount, scan_block, 0.92)
                elif re.search(
                    r"единоврем|разов|дополнительн\w*\s+плат|біржолғы", lowered
                ):
                    add("one_time_payment_kzt", amount, scan_block, 0.92)
                else:
                    add("other_payment_kzt", amount, scan_block, 0.78)

        for field, pattern, confidence in _LABEL_PATTERNS:
            if not pattern.search(scan_text):
                continue
            value: object = _clean(scan_text)
            if field == "development_obligation":
                deadline = _DEADLINE.search(scan_text)
                value = {
                    "obligation": _clean(scan_text),
                    "deadline": _clean(deadline.group(0)) if deadline else None,
                }
            add(field, value, scan_block, confidence)
    return result


def _context_value(
    lot_context: dict[str, object] | None,
    field: str,
) -> object | None:
    if not isinstance(lot_context, dict):
        return None
    value = lot_context.get(field)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = _clean(value)
        return value or None
    if isinstance(value, (int, float)):
        return value
    return None


def _material_value(value: object, *, field: str | None = None) -> str:
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, (int, float)):
        return f"{float(value):.6f}".rstrip("0").rstrip(".")
    cleaned = _clean(str(value)).casefold()
    if field == "cadastral_number":
        # Official cards and contracts commonly use ':' and '-' for the same
        # cadastral identity. Formatting alone must not create a red flag.
        return re.sub(r"[^0-9a-zа-яё]", "", cleaned)
    return cleaned


def reconcile_document_candidates(
    candidates: tuple[DocumentFactCandidate, ...] | list[DocumentFactCandidate],
    lot_context: dict[str, object] | None,
) -> tuple[DocumentFactCandidate, ...]:
    """Mark scalar document facts that disagree with the official lot card.

    This deterministic comparison remains available when the optional LLM is
    unavailable. The document value and exact citation stay untouched; only its
    review status changes.
    """
    reconciled: list[DocumentFactCandidate] = []
    for candidate in candidates:
        context_value = _context_value(lot_context, candidate.field)
        if (
            context_value is not None
            and _material_value(context_value, field=candidate.field)
            != _material_value(candidate.value, field=candidate.field)
        ):
            candidate = replace(candidate, status="conflict")
        reconciled.append(candidate)
    return tuple(reconciled)


def _conflicts(
    candidates: list[DocumentFactCandidate],
    lot_context: dict[str, object] | None = None,
) -> tuple[CandidateConflict, ...]:
    conflict_fields = {
        "lease_term_years",
        "right_type",
        "annual_payment_kzt",
        "one_time_payment_kzt",
        "guarantee_payment_kzt",
        "other_payment_kzt",
        "area_hectares",
        "cadastral_number",
        # A deadline is scalar and materially different values require review.
        # Grounds, obligations and penalties are additive clauses: two distinct
        # clauses do not contradict each other merely because their text differs.
        "development_deadline",
        "transfer_right",
    }
    by_field: dict[str, list[tuple[int, object]]] = {}
    for index, candidate in enumerate(candidates):
        by_field.setdefault(candidate.field, []).append((index, candidate.value))
    conflicts: list[CandidateConflict] = []
    for field, values in by_field.items():
        if field not in conflict_fields:
            continue
        distinct: dict[str, object] = {}
        context_value = _context_value(lot_context, field)
        if context_value is not None:
            distinct.setdefault(_material_value(context_value, field=field), context_value)
        for _index, value in values:
            distinct.setdefault(_material_value(value, field=field), value)
        explicit_conflict = any(candidates[index].status == "conflict" for index, _ in values)
        if explicit_conflict or len(distinct) > 1:
            conflicts.append(
                CandidateConflict(
                    field=field,
                    values=tuple(distinct.values()),
                    candidate_indexes=tuple(index for index, _value in values),
                    lot_context_value=context_value,
                )
            )
    return tuple(conflicts)


def document_candidate_conflicts(
    candidates: tuple[DocumentFactCandidate, ...] | list[DocumentFactCandidate],
    lot_context: dict[str, object] | None = None,
) -> tuple[CandidateConflict, ...]:
    return _conflicts(list(candidates), lot_context)


def extract_auction_document_text(
    source: bytes | bytearray | Path | str,
    metadata: DocumentMetadata,
    *,
    limits: ExtractionLimits | None = None,
    extracted_at: datetime | None = None,
) -> DocumentTextExtractionResult:
    active_limits = _bounded_limits(limits or ExtractionLimits())
    metadata_error = _metadata_error(metadata, extracted_at)
    if metadata_error:
        return DocumentTextExtractionResult("corrupt", "", None, 0, 0, metadata_error)
    data, read_error = _read_input(source, active_limits.max_file_bytes)
    if data is None:
        status: ExtractionStatus = (
            "oversized" if read_error == "file exceeds byte limit" else "corrupt"
        )
        return DocumentTextExtractionResult(status, "", None, 0, 0, read_error)
    content_hash = hashlib.sha256(data).hexdigest()
    kind = _file_kind(metadata, source)
    if kind == "doc":
        return DocumentTextExtractionResult(
            "unsupported", "", content_hash, 0, 0, "legacy DOC is not parsed safely"
        )
    if kind not in TEXT_FILE_TYPES:
        return DocumentTextExtractionResult(
            "unsupported", "", content_hash, 0, 0, f"unsupported type: {kind}"
        )
    if kind == "docx" and data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return DocumentTextExtractionResult(
            "encrypted", "", content_hash, 0, 0, "encrypted Office container"
        )
    if kind == "pdf":
        blocks, pages, error = _pdf_blocks(data, active_limits)
    elif kind in IMAGE_FILE_TYPES:
        blocks, pages, error = _image_blocks(data, kind, active_limits)
    else:
        blocks, error = _docx_blocks(data, active_limits)
        pages = 0
    if error:
        return DocumentTextExtractionResult(
            _status_for_extraction_error(error),
            "",
            content_hash,
            pages,
            0,
            f"{kind} extraction {error}",
        )
    text = "\n".join(block.text for block in blocks)
    return DocumentTextExtractionResult(
        status="ok" if text else "unknown",
        text=text,
        content_hash=content_hash,
        pages_processed=pages,
        text_chars_processed=sum(len(block.text) for block in blocks),
    )


def extract_auction_document(
    source: bytes | bytearray | Path | str,
    metadata: DocumentMetadata,
    *,
    limits: ExtractionLimits | None = None,
    extracted_at: datetime | None = None,
) -> DocumentExtractionResult:
    """Return review candidates from a local document, never confirmed legal facts."""
    active_limits = _bounded_limits(limits or ExtractionLimits())
    metadata_error = _metadata_error(metadata, extracted_at)
    if metadata_error:
        return DocumentExtractionResult("corrupt", (), (), None, 0, 0, metadata_error)
    data, read_error = _read_input(source, active_limits.max_file_bytes)
    if data is None:
        status: ExtractionStatus = (
            "oversized" if read_error == "file exceeds byte limit" else "corrupt"
        )
        return DocumentExtractionResult(status, (), (), None, 0, 0, read_error)
    content_hash = hashlib.sha256(data).hexdigest()
    kind = _file_kind(metadata, source)
    if kind == "doc":
        return DocumentExtractionResult(
            "unsupported", (), (), content_hash, 0, 0, "legacy DOC is not parsed safely"
        )
    if kind not in TEXT_FILE_TYPES:
        return DocumentExtractionResult(
            "unsupported", (), (), content_hash, 0, 0, f"unsupported type: {kind}"
        )
    if kind == "docx" and data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return DocumentExtractionResult(
            "encrypted", (), (), content_hash, 0, 0, "encrypted Office container"
        )
    if kind == "pdf":
        blocks, pages, error = _pdf_blocks(data, active_limits)
    elif kind in IMAGE_FILE_TYPES:
        blocks, pages, error = _image_blocks(data, kind, active_limits)
    else:
        blocks, error = _docx_blocks(data, active_limits)
        pages = 0
    if error:
        return DocumentExtractionResult(
            _status_for_extraction_error(error),
            (),
            (),
            content_hash,
            pages,
            0,
            f"{kind} extraction {error}",
        )
    text_chars = sum(len(block.text) for block in blocks)
    timestamp = extracted_at or datetime.now(UTC)
    candidates = list(
        reconcile_document_candidates(
            _extract_candidates(blocks, metadata, content_hash, active_limits, timestamp),
            metadata.lot_context,
        )
    )
    status: ExtractionStatus = "ok" if candidates else "unknown"
    return DocumentExtractionResult(
        status=status,
        candidates=tuple(candidates),
        conflicts=_conflicts(candidates, metadata.lot_context),
        content_hash=content_hash,
        pages_processed=pages,
        text_chars_processed=text_chars,
    )
