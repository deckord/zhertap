from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.auction_v2 import (
    _normalize_downloaded_document_content,
    _validate_downloaded_document_content,
)


def _document(*, file_type: str = "pdf", source_url: str = "https://example.test/file.pdf"):
    return SimpleNamespace(file_type=file_type, source_url=source_url)


def test_pdf_download_validation_accepts_pdf_signature() -> None:
    _validate_downloaded_document_content(_document(), b"%PDF-1.7\nbody")


def test_pdf_download_validation_rejects_html_response() -> None:
    with pytest.raises(ValueError, match="not a valid PDF"):
        _validate_downloaded_document_content(
            _document(),
            b"<!DOCTYPE html><html><body>access denied</body></html>",
        )


def test_pdf_download_validation_allows_header_prefix_before_signature() -> None:
    _validate_downloaded_document_content(_document(), b"\xef\xbb\xbf\n%PDF-1.4\nbody")


def test_non_pdf_download_is_not_rejected_by_pdf_validator() -> None:
    _validate_downloaded_document_content(
        _document(file_type="docx", source_url="https://example.test/file.docx"),
        b"not-a-pdf-but-not-a-pdf-document",
    )


def test_pdf_named_jpeg_is_normalized_to_real_pdf() -> None:
    image = Image.new("RGB", (20, 10), "white")
    buffer = BytesIO()
    image.save(buffer, format="JPEG")

    normalized = _normalize_downloaded_document_content(_document(), buffer.getvalue())

    assert normalized.startswith(b"%PDF-")
    _validate_downloaded_document_content(_document(), normalized)
