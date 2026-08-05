from tools.db_safety_audit import _safe_database_url


def test_safe_database_url_masks_password() -> None:
    masked = _safe_database_url("postgresql+psycopg://user:secret@example.test:5432/app")

    assert masked == "postgresql+psycopg://user:***@example.test:5432/app"
