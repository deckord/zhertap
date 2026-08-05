from app.legal_rules import legal_restriction_reason


def test_kokshetau_gardening_suspension_is_reported() -> None:
    reason = legal_restriction_reason(
        region="Акмолинская область",
        district="Кокшетау",
        locality="Кокшетау",
        purpose="Садоводство",
    )

    assert reason is not None
    assert "приостановлено" in reason
    assert "gov.kz" in reason


def test_kokshetau_rule_does_not_block_lph() -> None:
    assert legal_restriction_reason(
        region="Акмолинская область",
        district="Кокшетау",
        locality="Кокшетау",
        purpose="ЛПХ",
    ) is None

    assert legal_restriction_reason(
        region="Акмолинская область",
        district="Кокшетау",
        locality="Кокшетау",
        purpose="ЛПХ (новый поиск)",
    ) is None
