from __future__ import annotations

import hashlib
import io
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import fitz
import pytest
from PIL import Image

from app.auction_document_extractor import (
    EXTRACTOR_VERSION,
    DocumentMetadata,
    ExtractionLimits,
    extract_auction_document,
)

FONT_PATH = Path(r"C:\Windows\Fonts\arial.ttf")


def _metadata(file_type: str) -> DocumentMetadata:
    return DocumentMetadata(
        document_id=42,
        title="Проект договора аренды",
        source_url="https://sauda.e-qazyna.kz/documents/42",
        file_type=file_type,
        observed_at=datetime(2026, 8, 17, 8, tzinfo=UTC),
    )


def _pdf(pages: list[list[str]], *, password: str | None = None) -> bytes:
    document = fitz.open()
    for lines in pages:
        page = document.new_page()
        font_name = "helv"
        insert_kwargs: dict[str, object] = {}
        if FONT_PATH.exists():
            font_name = "auctionfont"
            insert_kwargs["fontfile"] = str(FONT_PATH)
        for index, line in enumerate(lines):
            page.insert_text(
                (48, 64 + index * 24),
                line,
                fontsize=10,
                fontname=font_name,
                **insert_kwargs,
            )
    options: dict[str, object] = {}
    if password:
        options = {
            "encryption": fitz.PDF_ENCRYPT_AES_256,
            "owner_pw": "owner-secret",
            "user_pw": password,
        }
    payload = document.tobytes(**options)
    document.close()
    return payload


def _docx(paragraphs: list[str], *, table_cells: list[str] | None = None) -> bytes:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def paragraph(text: str) -> str:
        return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'

    body = "".join(paragraph(text) for text in paragraphs)
    if table_cells:
        cells = "".join(f"<w:tc>{paragraph(text)}</w:tc>" for text in table_cells)
        body += f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>"
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml.encode("utf-8"))
    return output.getvalue()


@pytest.mark.skipif(not FONT_PATH.exists(), reason="Cyrillic test font is unavailable")
def test_pdf_extracts_all_legal_fact_types_with_page_provenance_and_conflicts() -> None:
    data = _pdf(
        [
            [
                "Срок аренды: 3 года.",
                "Единовременный дополнительный платеж: 16 200 ₸.",
                "Ежегодная арендная плата: 17 970 тенге.",
                "Гарантийный взнос: 216 250 теңге.",
                "Регистрационный сбор: 5 000 тг.",
                "Участок необходимо освоить не позднее 11.07.2029.",
            ],
            [
                "Срок аренды: 5 лет.",
                "Основанием для прекращения права является неосвоение участка.",
                "Продление договора допускается после исполнения обязательств.",
                "Передача права в субаренду допускается с согласия арендодателя.",
                "Ответственность: пеня 0,1 процента за каждый день просрочки.",
            ],
        ]
    )
    extracted_at = datetime(2026, 8, 17, 12, tzinfo=UTC)

    result = extract_auction_document(
        data,
        _metadata("pdf"),
        extracted_at=extracted_at,
    )

    assert result.status == "ok"
    assert result.pages_processed == 2
    fields = {candidate.field for candidate in result.candidates}
    assert fields == {
        "right_type",
        "lease_term_years",
        "one_time_payment_kzt",
        "annual_payment_kzt",
        "guarantee_payment_kzt",
        "other_payment_kzt",
        "development_obligation",
        "termination_ground",
        "renewal_condition",
        "transfer_right",
        "responsibility_penalty",
    }
    amounts = {candidate.field: candidate.value for candidate in result.candidates}
    assert amounts["one_time_payment_kzt"] == 16_200
    assert amounts["annual_payment_kzt"] == 17_970
    assert amounts["guarantee_payment_kzt"] == 216_250
    assert amounts["other_payment_kzt"] == 5_000
    assert any(conflict.field == "lease_term_years" for conflict in result.conflicts)
    first = result.candidates[0]
    assert first.page == 1
    assert first.source_url == _metadata("pdf").source_url
    assert first.content_hash == hashlib.sha256(data).hexdigest()
    assert first.quote_hash == hashlib.sha256(first.evidence_excerpt.encode()).hexdigest()
    assert first.extractor_version == EXTRACTOR_VERSION
    assert first.extracted_at == extracted_at
    assert len(first.evidence_excerpt) <= 240


def test_docx_extracts_paragraph_and_table_locations_deterministically() -> None:
    data = _docx(
        ["Срок аренды: 36 месяцев."],
        table_cells=["Ежегодная арендная плата: 25 000 ₸."],
    )
    timestamp = datetime(2026, 8, 17, 13, tzinfo=UTC)

    first = extract_auction_document(data, _metadata("docx"), extracted_at=timestamp)
    second = extract_auction_document(data, _metadata("docx"), extracted_at=timestamp)

    assert first.as_dict() == second.as_dict()
    assert first.status == "ok"
    lease = next(
        candidate for candidate in first.candidates if candidate.field == "lease_term_years"
    )
    annual = next(
        candidate for candidate in first.candidates if candidate.field == "annual_payment_kzt"
    )
    assert lease.value == 3
    assert lease.section == "paragraph:1"
    assert annual.section == "table:1/row:1/cell:1/paragraph:1"


def test_docx_does_not_mix_fact_provenance_across_semantic_paragraphs() -> None:
    result = extract_auction_document(
        _docx(
            [
                "Участок передается победителю.",
                "Ежегодная арендная плата: 25 000 ₸.",
            ]
        ),
        _metadata("docx"),
        extracted_at=datetime(2026, 8, 17, 13, tzinfo=UTC),
    )

    annual = [
        candidate
        for candidate in result.candidates
        if candidate.field == "annual_payment_kzt"
    ]
    assert len(annual) == 1
    assert annual[0].section == "paragraph:2"
    assert annual[0].evidence_excerpt == "Ежегодная арендная плата: 25 000 ₸."
    assert annual[0].page is None


def test_valid_document_without_facts_is_unknown_not_negative() -> None:
    result = extract_auction_document(
        _docx(["Документ опубликован для ознакомления."]),
        _metadata("docx"),
    )

    assert result.status == "unknown"
    assert result.candidates == ()
    assert result.detail is None


def test_png_uses_optional_ocr_text_for_legal_facts(monkeypatch) -> None:
    image = Image.new("RGB", (120, 80), color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()

    monkeypatch.setattr("app.auction_document_extractor.shutil.which", lambda name: "tesseract")
    monkeypatch.setattr(
        "app.auction_document_extractor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="Срок аренды: 10 лет.\nЕжегодная арендная плата: 25 000 ₸.",
            stderr="",
        ),
    )

    result = extract_auction_document(data, _metadata("png"))

    assert result.status == "ok"
    assert result.pages_processed == 1
    assert {candidate.field for candidate in result.candidates} == {
        "right_type",
        "lease_term_years",
        "annual_payment_kzt",
    }
    lease = next(
        candidate for candidate in result.candidates if candidate.field == "lease_term_years"
    )
    assert lease.value == 10
    assert lease.section == "ocr-line:1"


def test_image_without_ocr_is_unknown_not_unsupported(monkeypatch) -> None:
    image = Image.new("RGB", (40, 40), color="white")
    output = io.BytesIO()
    image.save(output, format="JPEG")
    monkeypatch.setattr("app.auction_document_extractor.shutil.which", lambda name: None)

    result = extract_auction_document(output.getvalue(), _metadata("jpg"))

    assert result.status == "unknown"
    assert result.detail == "jpg extraction ocr_unavailable"


def test_oversized_path_stops_before_read(tmp_path: Path) -> None:
    path = tmp_path / "large.pdf"
    path.write_bytes(b"x" * 100)

    result = extract_auction_document(
        path,
        _metadata("pdf"),
        limits=ExtractionLimits(max_file_bytes=50),
    )

    assert result.status == "oversized"
    assert result.content_hash is None
    assert result.candidates == ()


def test_corrupt_pdf_returns_explicit_status() -> None:
    result = extract_auction_document(b"not-a-pdf", _metadata("pdf"))

    assert result.status == "corrupt"
    assert result.candidates == ()


def test_encrypted_pdf_returns_explicit_status() -> None:
    result = extract_auction_document(_pdf([["secret"]], password="reader"), _metadata("pdf"))

    assert result.status == "encrypted"
    assert result.candidates == ()


def test_encrypted_office_container_returns_explicit_status() -> None:
    ole_header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 100
    result = extract_auction_document(ole_header, _metadata("docx"))

    assert result.status == "encrypted"


def test_docx_uncompressed_and_text_limits_are_enforced() -> None:
    data = _docx(["Срок аренды: 3 года. " * 50])

    zip_limited = extract_auction_document(
        data,
        _metadata("docx"),
        limits=ExtractionLimits(max_docx_uncompressed_bytes=100),
    )
    text_limited = extract_auction_document(
        data,
        _metadata("docx"),
        limits=ExtractionLimits(max_text_chars=50),
    )

    assert zip_limited.status == "oversized"
    assert text_limited.status == "oversized"


def test_candidate_count_and_page_count_are_hard_bounded() -> None:
    many_candidates = extract_auction_document(
        _docx(
            [
                "Ежегодная арендная плата: 1 000 ₸.",
                "Ежегодная арендная плата: 2 000 ₸.",
            ]
        ),
        _metadata("docx"),
        limits=ExtractionLimits(max_candidates=1),
    )
    too_many_pages = extract_auction_document(
        _pdf([["page one"], ["page two"]]),
        _metadata("pdf"),
        limits=ExtractionLimits(max_pages=1),
    )

    assert len(many_candidates.candidates) == 1
    assert too_many_pages.status == "oversized"


@pytest.mark.parametrize(
    ("metadata", "timestamp"),
    [
        (
            DocumentMetadata(1, "x" * 321, "https://example.test/doc", "docx"),
            datetime(2026, 8, 17, tzinfo=UTC),
        ),
        (
            DocumentMetadata(
                1,
                "title",
                "https://example.test/doc",
                "docx",
                observed_at=datetime(2026, 8, 17),
            ),
            datetime(2026, 8, 17),
        ),
    ],
)
def test_unbounded_or_ambiguous_metadata_is_rejected(
    metadata: DocumentMetadata,
    timestamp: datetime,
) -> None:
    result = extract_auction_document(
        _docx(["Срок аренды: 3 года."]),
        metadata,
        extracted_at=timestamp,
    )

    assert result.status == "corrupt"
    assert result.candidates == ()


@pytest.mark.parametrize("file_type", ["doc", "xlsx", "unknown"])
def test_unsupported_types_never_crash(file_type: str) -> None:
    result = extract_auction_document(b"opaque", _metadata(file_type))

    assert result.status == "unsupported"
    assert result.candidates == ()
