from app.web import _document_decision_brief


def test_document_brief_uses_grounded_llm_meaning_instead_of_generic_counter() -> None:
    candidates = [
        {
            "field": "right_type",
            "is_llm": True,
            "is_grounded": True,
            "value": "ownership",
        }
    ]

    brief = _document_decision_brief(
        candidates,
        summary=(
            "В договоре указано право собственности, что расходится "
            "с карточкой аренды."
        ),
        risks=["Сверить вид приобретаемого права до участия."],
        unknowns=["Срок аренды в документе не подтверждён."],
    )

    assert brief["summary"] == (
        "В договоре указано право собственности, что расходится с карточкой аренды."
    )
    assert brief["risks"] == ["Сверить вид приобретаемого права до участия."]
    assert brief["unknowns"] == ["Срок аренды в документе не подтверждён."]