from app.auction_nsdi_checks import analyze_water_protection_intersection
from app.providers.nsdi import NsdiFeature

PARCEL = {
    "type": "Polygon",
    "coordinates": [
        [[71.61, 51.25], [71.62, 51.25], [71.62, 51.26], [71.61, 51.25]]
    ],
}
ZONE = NsdiFeature(
    feature_id="zone.1",
    source_layer="geonode:waterprotectionzone",
    geometry={
        "type": "Polygon",
        "coordinates": [
            [[71.615, 51.245], [71.625, 51.245], [71.625, 51.265], [71.615, 51.245]]
        ],
    },
    properties={"name": "Водоохранная зона"},
)


def test_water_protection_intersection_returns_warning_with_area_share() -> None:
    result = analyze_water_protection_intersection(PARCEL, (ZONE,))

    assert result.status == "intersection_found"
    assert result.feature_count == 1
    assert result.intersection_percent is not None
    assert result.intersection_percent > 0
    assert result.intersection_percent <= 100
    assert result.requires_manual_review is True


def test_empty_published_layer_is_not_treated_as_legal_clearance() -> None:
    result = analyze_water_protection_intersection(PARCEL, ())

    assert result.status == "no_intersection_in_published_layer"
    assert result.intersection_percent == 0.0
    assert result.requires_manual_review is True
