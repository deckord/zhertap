from types import SimpleNamespace

import pytest

from app.auction_due_diligence import build_request_draft


def test_build_request_draft_prefills_authority_and_lot_context() -> None:
    lot = SimpleNamespace(
        id="lot-1",
        auction_number="A-42",
        source_lot_id="source-42",
        region="г. Астана",
        district="Алматы",
        locality="Коктал",
        cadastre_number="21-318-001-123",
        area_ha=0.12,
        purpose="Для строительства объекта торговли",
        land_rights="Аренда",
        lease_term_years=10,
    )

    draft = build_request_draft(lot, check_code="electricity")

    assert draft.check_code == "electricity"
    assert draft.authority == "Энергоснабжающая организация / акимат"
    assert "A-42" in draft.question
    assert "21-318-001-123" in draft.question
    assert "г. Астана" in draft.lot_context["region"]
    assert draft.status == "draft"


def test_build_request_draft_rejects_unknown_check_code() -> None:
    lot = SimpleNamespace(id="lot-1", auction_number="A-42")

    with pytest.raises(ValueError, match="unknown_check_code"):
        build_request_draft(lot, check_code="unknown")
