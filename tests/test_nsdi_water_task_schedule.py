from pathlib import Path


def test_nsdi_water_check_is_scheduled_on_auction_worker() -> None:
    source = Path("app/tasks.py").read_text(encoding="utf-8")

    assert 'name="land_scout.check_nsdi_water_protection"' in source
    assert '"check-nsdi-water-protection"' in source
    assert (
        '"land_scout.check_nsdi_water_protection": {"queue": "auctions"}'
        in source
    )
