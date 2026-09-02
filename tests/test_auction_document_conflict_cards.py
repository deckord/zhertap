from app.web import _document_conflict_cards


def _candidate(field: str, value: object, *, page: int = 2) -> dict[str, object]:
    return {
        "field": field,
        "value": value,
        "page": page,
        "section": "Условия договора",
        "evidence_excerpt": f"Срок аренды составляет {value} лет.",
    }


def test_conflict_card_shows_official_lot_value_and_single_document_value() -> None:
    cards = _document_conflict_cards(
        [_candidate("lease_term_years", 5)],
        [
            {
                "field": "lease_term_years",
                "values": [10, 5],
                "candidate_indexes": [0],
            }
        ],
    )

    assert cards == [
        {
            "field": "lease_term_years",
            "label": "Срок аренды",
            "values": [
                {
                    "value": "10 лет",
                    "evidence": "Официальная карточка лота E-Qazyna",
                    "provenance": "карточка лота",
                },
                {
                    "value": "5 лет",
                    "evidence": "Срок аренды составляет 5 лет.",
                    "provenance": "стр. 2 · Условия договора",
                },
            ],
        }
    ]


def test_conflict_card_keeps_document_only_conflicts_backward_compatible() -> None:
    cards = _document_conflict_cards(
        [
            _candidate("lease_term_years", 3, page=1),
            _candidate("lease_term_years", 5, page=4),
        ],
        [
            {
                "field": "lease_term_years",
                "values": [3, 5],
                "candidate_indexes": [0, 1],
            }
        ],
    )

    assert [item["value"] for item in cards[0]["values"]] == ["3 лет", "5 лет"]


def test_conflict_card_does_not_render_an_uncited_single_value() -> None:
    assert _document_conflict_cards(
        [_candidate("lease_term_years", 5)],
        [
            {
                "field": "lease_term_years",
                "values": [5],
                "candidate_indexes": [0],
                "lot_context_value": None,
            }
        ],
    ) == []


def test_conflict_cards_render_official_area_and_cadastral_identity_discrepancies() -> None:
    cards = _document_conflict_cards(
        [
            _candidate("area_hectares", 1.25),
            _candidate("cadastral_number", "01-123-456-789", page=3),
        ],
        [
            {
                "field": "area_hectares",
                "values": [2.5, 1.25],
                "candidate_indexes": [0],
                "lot_context_value": 2.5,
            },
            {
                "field": "cadastral_number",
                "values": ["01:123:456:000", "01-123-456-789"],
                "candidate_indexes": [1],
                "lot_context_value": "01:123:456:000",
            },
        ],
    )

    assert [card["field"] for card in cards] == ["area_hectares", "cadastral_number"]
    assert [item["value"] for item in cards[0]["values"]] == ["2.5 га", "1.25 га"]
    assert [item["value"] for item in cards[1]["values"]] == [
        "01:123:456:000",
        "01-123-456-789",
    ]