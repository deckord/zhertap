import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def use_legacy_funnel_by_default(monkeypatch):
    """Existing behavioral tests also protect the instant V1 rollback path."""
    monkeypatch.setattr(settings, "client_funnel_version", "v1")
