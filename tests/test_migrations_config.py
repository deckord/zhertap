from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_alembic_scaffold_is_present() -> None:
    assert (ROOT / "alembic.ini").exists()
    assert (ROOT / "migrations" / "env.py").exists()
    assert (ROOT / "migrations" / "script.py.mako").exists()
    assert (ROOT / "migrations" / "versions").is_dir()


def test_alembic_env_uses_application_database_url() -> None:
    env_py = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "settings.database_url" in env_py
    assert "validate_database_url()" in env_py
    assert "target_metadata = Base.metadata" in env_py
    assert "compare_type=True" in env_py


def test_baseline_migration_contains_runtime_indexes() -> None:
    migration_files = list((ROOT / "migrations" / "versions").glob("*_baseline_schema.py"))
    assert len(migration_files) == 1

    baseline = migration_files[0].read_text(encoding="utf-8")

    assert "ix_search_requests_status_updated_at" in baseline
    assert "ix_search_requests_payment_status_user" in baseline
    assert "ix_account_payments_status_account_created" in baseline
    assert "ix_web_sessions_account_active" in baseline
    assert "ix_search_requests_payment_provider_invoice_unique" in baseline
