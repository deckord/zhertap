from app.web import _upload_content_matches_suffix


def test_due_diligence_upload_accepts_pdf_magic() -> None:
    assert _upload_content_matches_suffix(b"%PDF-1.7\nbody", ".pdf") is True


def test_due_diligence_upload_rejects_html_named_pdf() -> None:
    assert _upload_content_matches_suffix(b"<!doctype html>", ".pdf") is False


def test_due_diligence_upload_accepts_png_and_jpeg_magic() -> None:
    assert _upload_content_matches_suffix(bytes.fromhex("89504e470d0a1a0a"), ".png") is True
    assert _upload_content_matches_suffix(b"\xff\xd8\xff\xe0", ".jpg") is True
