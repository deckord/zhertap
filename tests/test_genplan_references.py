from types import SimpleNamespace

import app.services as services
from app.genplan_references import GENPLAN_REFERENCES, GGK_REFERENCE, genplan_reference_payload
from app.manual_genplans import manual_genplan_records
from app.models import SearchRequest


def _scope(
    *,
    region: str,
    district: str,
    locality: str | None = None,
    language: str = "ru",
) -> SimpleNamespace:
    return SimpleNamespace(
        region=region,
        region_label=None,
        district=district,
        district_label=None,
        locality=locality,
        locality_label=None,
        language=language,
    )


def _reference_by_url(url: str):
    return next(reference for reference in GENPLAN_REFERENCES if reference.url == url)


def test_legal_city_reference_is_not_shown_to_clients() -> None:
    astana = _reference_by_url("https://adilet.zan.kz/rus/docs/P2400000033")

    payload = genplan_reference_payload(
        _scope(region=astana.region, district="client district", locality="client locality"),
        manual_files_root="Z:/missing-genplan-root",
    )

    assert payload["url"] == GGK_REFERENCE.url
    assert payload["source_kind"] == "geoportal"
    assert "adilet.zan.kz" not in payload["url"]


def test_region_reference_is_used_when_city_document_is_unknown() -> None:
    akmola_map = _reference_by_url("https://map.iaqmola.kz/")

    payload = genplan_reference_payload(
        _scope(region=akmola_map.region, district="client district", locality="client locality"),
        manual_files_root="Z:/missing-genplan-root",
    )

    assert payload["url"] == akmola_map.url
    assert payload["source_kind"] == "geoportal"


def test_shymkent_reference_points_to_map_not_legal_article() -> None:
    shymkent = _reference_by_url("https://geo-shym.kz/map/?access_token=&lang=ru")

    payload = genplan_reference_payload(
        _scope(region=shymkent.region, district="client district", locality="client locality"),
        manual_files_root="Z:/missing-genplan-root",
    )

    assert payload["url"] == shymkent.url
    assert payload["source_kind"] == "interactive_map"


def test_city_legal_reference_falls_back_to_general_geoportal() -> None:
    aktobe = _reference_by_url("https://adilet.zan.kz/rus/docs/P2400000461")

    payload = genplan_reference_payload(
        _scope(region=aktobe.region, district=aktobe.locality, locality=None),
        manual_files_root="Z:/missing-genplan-root",
    )

    assert payload["url"] == GGK_REFERENCE.url
    assert payload["source_kind"] == "geoportal"
    assert "adilet.zan.kz" not in payload["url"]


def test_manual_genplan_file_wins_over_reference_fallback(tmp_path) -> None:
    record = manual_genplan_records()[0]
    scope = _scope(
        region=record.region,
        district=record.district or "client district",
        locality=record.locality or None,
    )
    target = tmp_path / record.relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fake image")

    payload = genplan_reference_payload(
        scope,
        base_url="https://zhertap.kz",
        manual_files_root=str(tmp_path),
    )

    assert payload["url"].startswith("https://zhertap.kz/manual-genplans/")
    assert payload["source_kind"] == "manual_plan_file"


def test_telegram_genplan_button_uses_client_language_without_adilet(monkeypatch) -> None:
    monkeypatch.setattr(services.settings, "manual_genplan_files_root", "Z:/missing-genplan-root")
    petropavlovsk = _reference_by_url("https://adilet.zan.kz/rus/docs/P2200000722")
    request = SearchRequest(
        region=petropavlovsk.region,
        district=petropavlovsk.locality,
        locality=petropavlovsk.locality,
        language="kz",
    )

    keyboard = services.telegram_genplan_reply_markup(request)

    assert keyboard["inline_keyboard"][0][0]["url"] == GGK_REFERENCE.url
    assert "adilet.zan.kz" not in keyboard["inline_keyboard"][0][0]["url"]
