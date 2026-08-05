import json

import pytest

from app.auction_geo import (
    AuctionGeoObject,
    auction_geo_metrics,
    extract_lot_point,
    haversine_m,
)
from app.auction_service import auction_lot_geo_metrics
from app.models import AuctionLot


def auction_lot_with_payload(payload: dict | None, **overrides: object) -> AuctionLot:
    values = {
        "source_lot_id": "geo-1",
        "source_url": "https://sauda.e-qazyna.kz/ru/list/geo-1",
        "title": "Auction lot",
        "raw_payload_json": json.dumps(payload) if payload is not None else None,
    }
    values.update(overrides)
    return AuctionLot(**values)


def test_haversine_calculates_distance_in_meters() -> None:
    distance = haversine_m(52.0, 71.0, 52.001, 71.0)

    assert distance == pytest.approx(111.2, abs=0.5)


def test_auction_geo_metrics_calculates_nearest_open_data_objects() -> None:
    lot = auction_lot_with_payload(
        {
            "geometry": {"type": "Point", "coordinates": [71.0, 52.0]},
            "geo_reference_objects": [
                {"kind": "road", "latitude": 52.0005, "longitude": 71.0},
                {"kind": "road", "latitude": 52.02, "longitude": 71.0},
                {"kind": "school", "latitude": 52.001, "longitude": 71.0},
                {"kind": "hospital", "latitude": 52.002, "longitude": 71.0},
                {"kind": "fuel", "latitude": 52.003, "longitude": 71.0},
                {"kind": "railway", "latitude": 52.004, "longitude": 71.0},
                {"kind": "power_line", "latitude": 52.005, "longitude": 71.0},
                {"kind": "city", "latitude": 52.006, "longitude": 71.0},
            ],
        }
    )

    metrics = auction_geo_metrics(lot)

    assert metrics.status == "ok"
    assert metrics.latitude == pytest.approx(52.0)
    assert metrics.longitude == pytest.approx(71.0)
    assert metrics.road_m == pytest.approx(55.6, abs=0.5)
    assert metrics.school_m == pytest.approx(111.2, abs=0.5)
    assert metrics.hospital_m == pytest.approx(222.4, abs=0.5)
    assert metrics.fuel_m == pytest.approx(333.6, abs=0.5)
    assert metrics.railway_m == pytest.approx(444.8, abs=0.5)
    assert metrics.power_line_m == pytest.approx(556.0, abs=0.6)
    assert metrics.distance_to_city_m == pytest.approx(667.2, abs=0.7)


def test_auction_geo_metrics_accepts_reference_objects_argument() -> None:
    lot = auction_lot_with_payload({"latitude": 52.0, "longitude": 71.0})

    metrics = auction_geo_metrics(
        lot,
        reference_objects=[
            AuctionGeoObject(kind="highway", latitude=52.0, longitude=71.001)
        ],
    )

    assert metrics.status == "ok"
    assert metrics.road_m == pytest.approx(68.5, abs=0.7)
    assert metrics.school_m is None


def test_auction_geo_metrics_returns_no_coordinates_status() -> None:
    lot = auction_lot_with_payload(
        {
            "geo_reference_objects": [
                {"kind": "road", "latitude": 52.0, "longitude": 71.0}
            ]
        },
        description="No coordinates here",
    )

    metrics = auction_lot_geo_metrics(lot)

    assert metrics.status == "no_coordinates"
    assert metrics.latitude is None
    assert metrics.longitude is None
    assert metrics.road_m is None


def test_extract_lot_point_can_read_google_maps_coordinates_from_description() -> None:
    lot = auction_lot_with_payload(
        None,
        description="Map: https://www.google.com/maps/@50.9243791,71.3612654,19z",
    )

    point = extract_lot_point(lot)

    assert point is not None
    assert point.latitude == pytest.approx(50.9243791)
    assert point.longitude == pytest.approx(71.3612654)
