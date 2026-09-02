import json
from types import SimpleNamespace

from app.auction_due_diligence import due_diligence_attachment_cards


def test_attachment_cards_expose_candidate_provenance() -> None:
    attachment = SimpleNamespace(
        id="att-1",
        extraction_status="ok",
        extraction_json=json.dumps(
            {
                "status": "ok",
                "detail": None,
                "candidates": [
                    {
                        "field": "restriction",
                        "value": "охранная зона",
                        "page": 3,
                        "section": "line:7",
                        "evidence_excerpt": "охранная зона ЛЭП",
                        "confidence": 0.84,
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )

    cards = due_diligence_attachment_cards([attachment])

    assert cards["att-1"]["status"] == "ok"
    assert cards["att-1"]["candidates"][0]["field"] == "restriction"
    assert cards["att-1"]["candidates"][0]["provenance"] == "стр. 3 · line:7"


def test_attachment_cards_keep_corrupt_status_without_facts() -> None:
    attachment = SimpleNamespace(
        id="att-2",
        extraction_status="corrupt",
        extraction_json="{not-json",
    )

    cards = due_diligence_attachment_cards([attachment])

    assert cards["att-2"]["status"] == "corrupt"
    assert cards["att-2"]["candidates"] == []
