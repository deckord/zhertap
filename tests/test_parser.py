import pytest

from app.parser import parse_search_text


def test_parses_burabay_area_and_cemetery_buffer() -> None:
    result = parse_search_text(
        "Акмолинская область, Боровое, ЛПХ 25 соток, кладбище не ближе 1,5 км"
    )

    assert result.region == "Акмолинская область"
    assert result.district == "Бурабайский район"
    assert result.area_ha == pytest.approx(0.10)
    assert result.cemetery_buffer_m == 0


def test_parses_hectares_and_locality() -> None:
    result = parse_search_text("Зерендинский район, Айдабол, участок 0,38 га")

    assert result.district == "Зерендинский район"
    assert result.locality == "Айдабол"
    assert result.area_ha == pytest.approx(0.10)


def test_default_area_is_ten_sotkas() -> None:
    result = parse_search_text("Акмолинская область, Зерендинский район")

    assert result.area_ha == pytest.approx(0.10)


def test_parses_explicit_locality_outside_builtin_list() -> None:
    result = parse_search_text(
        "Акмолинская область, Аршалынский район, село Жибек Жолы, ЛПХ 10 соток"
    )

    assert result.district == "Аршалынский район"
    assert result.locality == "Жибек Жолы"


def test_parses_locality_as_comma_segment_after_district() -> None:
    result = parse_search_text(
        "Акмолинская область, Зерендинский район, Кызылсая, участок 10 соток"
    )

    assert result.locality == "Кызылсая"
