from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, select_autoescape
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.auction_legal_passport import (
    MAX_JSON_CHARS,
    PASSPORT_VERSION,
    cached_auction_legal_passport,
    get_auction_legal_passport,
)
from app.config import settings
from app.db import Base
from app.models import AuctionDocument, AuctionEvidence, AuctionLot


def _lot(**overrides: object) -> AuctionLot:
    values: dict[str, object] = {
        "source": "e-qazyna",
        "source_lot_id": "452662",
        "title": "Кемпинг",
        "source_url": "https://sauda.e-qazyna.kz/ru/list/452662",
        "source_object_url": "https://traderesources.e-qazyna.kz/source-object/42",
        "land_rights": "временное возмездное краткосрочное землепользование",
        "lease_term_years": 3,
        "purpose": "строительство кемпинга",
        "guarantee_kzt": 216_250,
        "additional_payment_kzt": 16_200,
        "annual_rent_kzt": 17_970,
        "last_seen_at": datetime(2026, 8, 17, 8, tzinfo=UTC),
    }
    values.update(overrides)
    return AuctionLot(**values)


def _session() -> tuple[Session, object]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine), engine


def test_legal_passport_452662_models_right_term_and_cost_treatment() -> None:
    session, _engine = _session()
    try:
        lot = _lot()
        session.add(lot)
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        assert passport.version == PASSPORT_VERSION
        assert passport.facts["right_type"].value == "lease"
        assert passport.facts["lease_term_years"].value == 3
        assert passport.payments["guarantee"].value == {
            "amount_kzt": 216_250.0,
            "cost_treatment": "blocked_capital",
            "frequency": "once_before_auction",
        }
        assert passport.payments["additional_payment"].value["amount_kzt"] == 16_200
        assert passport.payments["annual_rent"].value["amount_kzt"] == 17_970
        assert passport.payments["annual_rent"].value["frequency"] == "annual_during_lease"
    finally:
        session.close()


def test_legal_passport_uses_jerler_provenance_and_preserves_explicit_negative() -> None:
    session, _engine = _session()
    try:
        lot = _lot(divisible=None)
        session.add(lot)
        session.flush()
        observed_at = datetime(2026, 8, 17, 9, tzinfo=UTC)
        source_url = "https://traderesources.e-qazyna.kz/source-object/42"
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="source_object_card",
                status="found",
                title="Jerler",
                source_url=source_url,
                confidence=0.98,
                observed_at=observed_at,
                raw_payload_json=json.dumps(
                    {
                        "divisible": True,
                        "arrests_text": "не имеются",
                        "restrictions_text": "охранная зона ЛЭП",
                        "additional_payment_kzt": 16_200,
                    }
                ),
            )
        )
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        arrests = passport.facts["arrests"]
        assert arrests.status == "found"
        assert arrests.value == "не имеются"
        assert arrests.source_url == source_url
        assert arrests.observed_at == observed_at.replace(tzinfo=None)
        assert arrests.confidence == 0.98
        assert passport.payments["additional_payment"].source_url == source_url
    finally:
        session.close()


def test_missing_legal_data_stays_unknown_instead_of_becoming_no() -> None:
    session, _engine = _session()
    try:
        lot = _lot(
            land_rights=None,
            lease_term_years=None,
            purpose=None,
            guarantee_kzt=None,
            additional_payment_kzt=None,
            annual_rent_kzt=None,
        )
        session.add(lot)
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        for key in ("right_type", "land_category", "arrests", "restrictions"):
            assert passport.facts[key].status == "unknown"
            assert passport.facts[key].value is None
            assert passport.facts[key].value != "не имеются"
            assert passport.facts[key].confidence == 0
        assert passport.payments["annual_rent"].status == "unknown"
    finally:
        session.close()


def test_conflicting_sources_are_reported_without_overwriting_lot_value() -> None:
    session, _engine = _session()
    try:
        lot = _lot(land_rights="Продажа права аренды земельного участка")
        session.add(lot)
        session.flush()
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="source_object_card",
                status="conflict",
                title="Jerler",
                source_url=str(lot.source_object_url),
                confidence=0.98,
                raw_payload_json=json.dumps(
                    {
                        "land_rights": "частная собственность",
                        "conflicts": [
                            {
                                "field": "land_rights",
                                "lot_value": lot.land_rights,
                                "source_object_value": "частная собственность",
                                "resolution": "preserved_lot_value",
                            }
                        ],
                    }
                ),
            )
        )
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        fact = passport.facts["land_rights"]
        assert fact.status == "conflict"
        assert fact.value == "Продажа права аренды земельного участка"
        assert fact.confidence < 0.5
        assert max(item.confidence for item in fact.provenance) == 0.98
        assert len(fact.provenance) >= 2
        assert passport.facts["right_type"].status == "conflict"
        assert passport.facts["right_type"].value == "lease"
        assert passport.facts["right_type"].confidence < 0.5
        assert lot.land_rights == "Продажа права аренды земельного участка"
    finally:
        session.close()


def test_document_extractions_feed_legal_passport_and_preserve_card_on_conflict() -> None:
    session, _engine = _session()
    try:
        lot = _lot()
        session.add(lot)
        session.flush()
        observed_at = datetime(2026, 8, 17, 11, tzinfo=UTC)
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="document_extraction",
                status="found",
                title="Извлечение проекта договора",
                source_url="https://example.test/contract.pdf",
                confidence=1.0,
                observed_at=observed_at,
                raw_payload_json=json.dumps(
                    {
                        "result": {
                            "candidates": [
                                {
                                    "field": "right_type",
                                    "value": "ownership",
                                    "confidence": 0.94,
                                    "evidence_excerpt": (
                                        "участок передается в частную собственность"
                                    ),
                                    "extractor_version": "rules+llm",
                                },
                                {
                                    "field": "lease_term_years",
                                    "value": 5,
                                    "confidence": 0.92,
                                    "evidence_excerpt": "срок аренды составляет 5 лет",
                                    "extractor_version": "rules+llm",
                                },
                                {
                                    "field": "termination_ground",
                                    "value": "неосвоение участка в установленный срок",
                                    "confidence": 0.91,
                                    "evidence_excerpt": "неосвоение участка в установленный срок",
                                    "document_id": 71,
                                    "document_title": "Проект договора аренды",
                                    "page": 8,
                                    "section": "Ответственность сторон",
                                    "quote_hash": "quote-71",
                                    "content_hash": "content-71",
                                    "extractor_version": "rules+llm",
                                },
                            ]
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        assert passport.facts["right_type"].status == "conflict"
        assert passport.facts["right_type"].value == "lease"
        assert passport.facts["lease_term_years"].status == "conflict"
        assert passport.facts["lease_term_years"].value == 3
        termination = passport.facts["termination_ground"]
        assert termination.status == "found"
        assert termination.value == "неосвоение участка в установленный срок"
        assert termination.source_url == "https://example.test/contract.pdf"
        assert termination.confidence == 0.91
        assert termination.provenance[0].document_id == 71
        assert termination.provenance[0].document_title == "Проект договора аренды"
        assert termination.provenance[0].page == 8
        assert termination.provenance[0].section == "Ответственность сторон"
        assert termination.provenance[0].evidence_excerpt == (
            "неосвоение участка в установленный срок"
        )
        assert termination.provenance[0].quote_hash == "quote-71"
        assert termination.provenance[0].content_hash == "content-71"
        assert termination.as_dict()["provenance"][0]["page"] == 8
    finally:
        session.close()


def test_document_free_text_lease_right_is_normalized_before_conflict_check() -> None:
    session, _engine = _session()
    try:
        lot = _lot(land_rights="Продажа права аренды земельного участка")
        session.add(lot)
        session.flush()
        excerpt = "Вид права: временное возмездное землепользование сроком на 5 лет"
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="document_extraction",
                status="found",
                title="Извлечение схемы участка",
                source_url="https://example.test/scheme.pdf",
                confidence=1.0,
                observed_at=datetime(2026, 8, 17, 11, tzinfo=UTC),
                raw_payload_json=json.dumps(
                    {
                        "result": {
                            "candidates": [
                                {
                                    "field": "right_type",
                                    "value": "временное возмездное землепользование",
                                    "confidence": 0.93,
                                    "evidence_excerpt": excerpt,
                                    "extractor_version": "auction-legal-doc.v1+llm",
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        fact = passport.facts["right_type"]
        assert fact.status == "found"
        assert fact.value == "lease"
        assert any(item.evidence_excerpt == excerpt for item in fact.provenance)
    finally:
        session.close()


def test_document_candidate_explicit_conflict_is_not_downgraded_to_found() -> None:
    session, _engine = _session()
    try:
        lot = _lot()
        session.add(lot)
        session.flush()
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="document_extraction",
                status="found",
                title="Извлечение проекта договора",
                source_url="https://example.test/contract.pdf",
                confidence=1.0,
                observed_at=datetime(2026, 8, 17, 11, tzinfo=UTC),
                raw_payload_json=json.dumps(
                    {
                        "result": {
                            "candidates": [
                                {
                                    "field": "transfer_right",
                                    "value": "передача права требует согласия арендодателя",
                                    "status": "conflict",
                                    "confidence": 0.86,
                                    "evidence_excerpt": (
                                        "Передача права требует согласия арендодателя, "
                                        "однако пункт 8.4 допускает уступку без согласия."
                                    ),
                                    "page": 7,
                                    "extractor_version": "auction-legal-doc.v1+llm",
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        fact = passport.facts["transfer_right"]
        assert fact.status == "conflict"
        assert fact.confidence < 0.5
        assert fact.provenance[0].page == 7
    finally:
        session.close()


def test_additional_document_conditions_remain_found_with_all_citations() -> None:
    session, _engine = _session()
    try:
        lot = _lot()
        session.add(lot)
        session.flush()
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="document_extraction",
                status="found",
                title="Извлечение проекта договора",
                source_url="https://example.test/contract.pdf",
                confidence=1.0,
                observed_at=datetime(2026, 8, 17, 11, tzinfo=UTC),
                raw_payload_json=json.dumps(
                    {
                        "result": {
                            "candidates": [
                                {
                                    "field": "termination_ground",
                                    "value": "расторжение при неосвоении",
                                    "confidence": 0.91,
                                    "evidence_excerpt": (
                                        "Договор расторгается при неосвоении участка."
                                    ),
                                    "page": 8,
                                    "extractor_version": "rules+llm",
                                },
                                {
                                    "field": "termination_ground",
                                    "value": "расторжение при неуплате",
                                    "confidence": 0.9,
                                    "evidence_excerpt": "Договор расторгается при неуплате аренды.",
                                    "page": 9,
                                    "extractor_version": "rules+llm",
                                },
                            ]
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        condition = passport.facts["termination_ground"]
        assert condition.status == "found"
        assert [item.page for item in condition.provenance] == [8, 9]
        assert [item.evidence_excerpt for item in condition.provenance] == [
            "Договор расторгается при неосвоении участка.",
            "Договор расторгается при неуплате аренды.",
        ]
    finally:
        session.close()


def test_builder_is_read_only_and_caps_evidence_and_documents() -> None:
    session, engine = _session()
    try:
        lot = _lot()
        session.add(lot)
        session.flush()
        for index in range(25):
            session.add(
                AuctionDocument(
                    lot_id=lot.id,
                    title=f"Документ {index}",
                    source_url=f"https://example.test/{index}.pdf",
                    storage_status="linked",
                )
            )
        session.commit()
        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(statement.strip().casefold())

        event.listen(engine, "before_cursor_execute", capture)
        passport = get_auction_legal_passport(
            session,
            lot.id,
            max_evidence=10_000,
            max_documents=10_000,
        )
        event.remove(engine, "before_cursor_execute", capture)

        assert passport is not None
        assert len(passport.documents) == 20
        assert not session.new
        assert not session.dirty
        assert statements
        assert all(statement.startswith("select") for statement in statements)
        assert all(" limit " in f" {statement} " for statement in statements[1:])
    finally:
        session.close()


def test_oversized_evidence_json_is_skipped_without_hiding_valid_row() -> None:
    session, _engine = _session()
    try:
        lot = _lot()
        session.add(lot)
        session.flush()
        session.add_all(
            [
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="source_object_card",
                    status="found",
                    title="oversized",
                    confidence=0.99,
                    observed_at=datetime(2026, 8, 17, 10, tzinfo=UTC),
                    raw_payload_json=json.dumps({"restrictions_text": "x" * MAX_JSON_CHARS}),
                ),
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="source_object_card",
                    status="found",
                    title="valid",
                    confidence=0.98,
                    observed_at=datetime(2026, 8, 17, 9, tzinfo=UTC),
                    raw_payload_json=json.dumps({"restrictions_text": "охранная зона ЛЭП"}),
                ),
            ]
        )
        session.commit()

        passport = get_auction_legal_passport(session, lot.id)

        assert passport is not None
        assert passport.facts["restrictions"].value == "охранная зона ЛЭП"
        assert passport.facts["restrictions"].status == "found"
    finally:
        session.close()


def test_cached_passport_returns_api_safe_payload_from_shared_cache(monkeypatch) -> None:
    session, _engine = _session()
    try:
        monkeypatch.setattr(settings, "auction_cache_enabled", True)
        monkeypatch.setattr(settings, "app_env", "test")
        from app.shared_cache import shared_json_cache

        monkeypatch.setattr(shared_json_cache, "_client", None)
        monkeypatch.setattr(shared_json_cache, "_redis_retry_after", float("inf"))
        shared_json_cache.clear_local()
        lot = _lot()
        document = AuctionDocument(
            title="Проект договора",
            source_url="https://example.test/contract.pdf",
            storage_status="linked",
        )
        lot.documents.append(document)
        session.add(lot)
        session.commit()
        lot_id = lot.id
        document_id = document.id

        first = cached_auction_legal_passport(session, lot_id)
        session.expunge_all()
        second = cached_auction_legal_passport(session, lot_id)

        assert first == second
        assert first is not None
        assert first["version"] == PASSPORT_VERSION
        assert first["payments"]["guarantee"]["value"]["amount_kzt"] == 216_250
        stored_document = session.get(AuctionDocument, document_id)
        assert stored_document is not None
        stored_document.storage_status = "downloaded"
        session.commit()

        refreshed = cached_auction_legal_passport(session, lot_id)

        assert refreshed is not None
        assert refreshed["documents"][0]["storage_status"] == "downloaded"
        assert refreshed != second
    finally:
        shared_json_cache.clear_local()
        session.close()


def test_lot_card_uses_real_legal_passport_statuses_for_restrictions() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "site_auction_v2_detail.html"
    ).read_text(encoding="utf-8")

    assert "legal.restrictions.status == 'confirmed'" not in template
    assert "legal.restrictions.status == 'found'" in template
    assert "legal.restrictions.status == 'conflict'" in template


def test_lot_card_surfaces_material_document_conditions_from_legal_passport() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "site_auction_v2_detail.html"
    ).read_text(encoding="utf-8")

    for key in (
        "development_obligation",
        "development_deadline",
        "termination_ground",
        "renewal_condition",
        "transfer_right",
        "responsibility_penalty",
    ):
        assert f"legal.{key}.value" in template
        assert f"legal.{key})" in template
    assert "legal_condition.status == 'conflict'" in template
    assert "Требует сверки: в официальных материалах найдены разные условия" in template
    assert "Официальные документы: существенные условия" in template
    assert "for citation in legal_condition.provenance[:5]" in template
    assert "citation.page" in template
    assert "citation.evidence_excerpt" in template
    assert "Страница" in template


def test_legal_condition_macro_renders_bounded_document_citation() -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "site_auction_v2_detail.html"
    )
    macro_source = template_path.read_text(encoding="utf-8").split("{% block title %}", 1)[0]
    macro_source = macro_source.replace('{% extends "site_base.html" %}', "", 1)
    environment = Environment(
        autoescape=select_autoescape(("html",)),
    )
    macro = environment.from_string(macro_source).module.render_legal_condition

    rendered = macro(
        "Основание расторжения",
        {
            "value": "Неосвоение участка",
            "status": "conflict",
            "source_url": "https://example.test/contract.pdf",
            "provenance": [
                {
                    "evidence_excerpt": "Участок должен быть освоен за 3 года",
                    "page": 8,
                    "section": "Ответственность сторон",
                }
            ],
        },
    )

    assert "Неосвоение участка" in rendered
    assert "Требует сверки" in rendered
    assert "Страница 8" in rendered
    assert "Ответственность сторон" in rendered
    assert "Участок должен быть освоен за 3 года" in rendered
    assert 'href="https://example.test/contract.pdf"' in rendered


def test_legal_condition_macro_renders_every_distinct_official_clause() -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "site_auction_v2_detail.html"
    )
    macro_source = template_path.read_text(encoding="utf-8").split("{% block title %}", 1)[0]
    macro_source = macro_source.replace('{% extends "site_base.html" %}', "", 1)
    macro = (
        Environment(autoescape=select_autoescape(("html",)))
        .from_string(macro_source)
        .module.render_legal_condition
    )

    rendered = macro(
        "Основания расторжения",
        {
            "value": "расторжение при неосвоении",
            "status": "found",
            "source_url": "https://example.test/contract.pdf",
            "provenance": [
                {
                    "evidence_excerpt": "Расторжение при неосвоении участка.",
                    "page": 8,
                    "section": "Освоение",
                },
                {
                    "evidence_excerpt": "Расторжение при неуплате аренды.",
                    "page": 9,
                    "section": "Платежи",
                },
            ],
        },
    )

    assert "Расторжение при неосвоении участка." in rendered
    assert "Расторжение при неуплате аренды." in rendered
    assert "Страница 8" in rendered
    assert "Страница 9" in rendered


def test_lot_detail_summary_uses_current_payload_contract() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "site_auction_v2_detail.html"
    ).read_text(encoding="utf-8")

    assert "item.transparent_conclusion" not in template
    assert "item.decision_summary.facts" in template
    assert "item.risk_flags" in template
    assert "item.next_actions" in template
