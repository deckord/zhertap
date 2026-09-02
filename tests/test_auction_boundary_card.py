from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "templates"
    / "site_auction_v2_detail.html"
)


def test_lot_checklist_does_not_treat_centroid_as_confirmed_boundary() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "geo_check.boundary_status == 'verified'" in source
    assert "Координаты найдены, граница не подтверждена" in source
    assert "Координаты найдены, нужна кадастровая сверка" not in source


def test_location_panel_exposes_boundary_status_and_area_provenance() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "<dt>Граница участка</dt><dd>{{ item.boundary_label }}</dd>" in source
    assert "<dt>Источник границы</dt>" in source
    assert "geo_check.boundary_area_ha" in source
    assert "geo_check.boundary_difference_percent" in source
