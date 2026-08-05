from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from tools.genplan_autoreg.basemap import ReferenceRaster, WebTileProvider
from tools.genplan_autoreg.matcher import (
    _diagnostic_anchor_points,
    match_plan_to_reference,
)
from tools.genplan_autoreg.models import BoundingBox, MatchMetrics
from tools.genplan_autoreg.pipeline import (
    AutoregConfig,
    _load_plan,
    run_autoregistration,
)
from tools.genplan_autoreg.providers import EgknResolver, NominatimResolver, StaticBboxResolver

cv2 = pytest.importorskip("cv2")


def synthetic_map(size: tuple[int, int] = (900, 700)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    rng = np.random.default_rng(42)
    for index in range(70):
        x = int(rng.integers(30, size[0] - 80))
        y = int(rng.integers(30, size[1] - 80))
        width = int(rng.integers(15, 70))
        height = int(rng.integers(15, 70))
        color = tuple(int(value) for value in rng.integers(20, 220, size=3))
        draw.rectangle((x, y, x + width, y + height), outline=color, width=3)
        draw.text((x + 2, y + 2), str(index), fill=(0, 0, 0))
    for offset in range(80, 850, 110):
        draw.line((offset, 0, offset - 120, 700), fill=(20, 20, 20), width=5)
    for offset in range(70, 650, 95):
        draw.line((0, offset, 900, offset + 60), fill=(40, 40, 40), width=4)
    return image


def test_nominatim_bbox_resolution_uses_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["countrycodes"] == "kz"
        return httpx.Response(
            200,
            json=[
                {
                    "name": "Бурабай",
                    "display_name": "Бурабай, Бурабайский район, Казахстан",
                    "boundingbox": ["52.98", "53.10", "70.20", "70.36"],
                }
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bbox = NominatimResolver(client=client).resolve(
            "Бурабай",
            region="Акмолинская область",
            district="Бурабайский район",
        )

    assert bbox.source == "nominatim"
    assert bbox.west == 70.20
    assert bbox.north == 53.10


def test_egkn_bbox_resolution_transforms_projected_geometry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/map/districts"):
            return httpx.Response(
                200,
                json=[
                    {
                        "nameRu": "Акмолинская область",
                        "districts": [
                            {
                                "id": 171,
                                "nameRu": "Бурабайский район",
                                "srs": 3857,
                            }
                        ],
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "name": "Бурабай",
                    "geom": (
                        "POLYGON ((7812364 6982997, 7834628 6982997, "
                        "7834628 7019900, 7812364 7019900, 7812364 6982997))"
                    ),
                }
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bbox = EgknResolver(client=client).resolve(
            "Бурабай",
            region="Акмолинская область",
            district="Бурабайский район",
        )

    assert bbox.source == "egkn"
    assert 70.1 < bbox.west < bbox.east < 70.5
    assert 52.9 < bbox.south < bbox.north < 53.2


def test_egkn_name_matching_handles_district_suffixes_spaces_and_kazakh_letters() -> None:
    rows = [
        {"name": "Гуляйполе", "geom": "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"},
        {"name": "Бозайғыр", "geom": "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"},
        {"name": "Жукей", "geom": "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"},
        {"name": "96 разъезд", "geom": "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"},
        {"name": "им. Хаджимукана", "geom": "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"},
    ]

    assert EgknResolver._find_locality(rows, "Гуляй поле")["name"] == "Гуляйполе"
    assert EgknResolver._find_locality(rows, "Бозайгыр")["name"] == "Бозайғыр"
    assert EgknResolver._find_locality(rows, "Жокей")["name"] == "Жукей"
    assert EgknResolver._find_locality(rows, "96")["name"] == "96 разъезд"
    assert EgknResolver._find_locality(rows, "кажымукан")["name"] == "им. Хаджимукана"
    district = EgknResolver._find_district(
        [
            {
                "nameRu": "Акмолинская область",
                "districts": [{"nameRu": "Шортандинский (01-012)"}],
            }
        ],
        "Акмолинская область (01)",
        "Шортанды район",
    )
    assert district["nameRu"] == "Шортандинский (01-012)"


def test_static_bbox_resolver_handles_city_plan_documents() -> None:
    bbox = StaticBboxResolver().resolve(
        "Генплан",
        region="Акмолинская область (01)",
        district="г. Кокшетау (01-174)",
    )

    assert bbox.source == "static_bbox"
    assert bbox.label == "Кокшетау"
    assert 69.0 < bbox.west < bbox.east < 69.8


def test_static_bbox_resolver_handles_old_region_folder_for_city_documents() -> None:
    bbox = StaticBboxResolver().resolve(
        "г.Талдыкорган",
        region="Алматинская область (03)",
        district="г.Талдыкорган",
    )

    assert bbox.source == "static_bbox"
    assert bbox.label == "Талдыкорган"
    assert 78.0 < bbox.west < bbox.east < 78.8


def test_static_bbox_resolver_handles_almaty_city_document() -> None:
    bbox = StaticBboxResolver().resolve(
        "г.Алматы",
        region="г. Алматы",
        district="",
    )

    assert bbox.source == "static_bbox"
    assert bbox.label == "Алматы"
    assert 76.6 < bbox.west < bbox.east < 77.2


def test_tile_provider_is_network_free_with_mock_transport(tmp_path: Path) -> None:
    tile = Image.new("RGB", (256, 256), (20, 100, 180))
    buffer = io.BytesIO()
    tile.save(buffer, format="PNG")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=buffer.getvalue())

    bbox = BoundingBox(70.20, 53.00, 70.205, 53.005, "test")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reference = WebTileProvider(
            source="osm",
            zoom=14,
            max_tiles=16,
            client=client,
        ).fetch(bbox, tmp_path)

    assert reference.image.width > 1
    assert reference.attribution == "© OpenStreetMap contributors"
    lon, lat = reference.pixel_to_lonlat(
        reference.image.width / 2,
        reference.image.height / 2,
    )
    assert bbox.west < lon < bbox.east
    assert bbox.south < lat < bbox.north


def test_known_homography_produces_proposals_but_never_approval() -> None:
    reference_image = synthetic_map()
    source = np.array(reference_image)
    transform = np.float32([[1.0, 0.025, 35.0], [-0.015, 1.0, 28.0], [0.00002, 0.00001, 1.0]])
    plan_array = cv2.warpPerspective(source, transform, reference_image.size)
    plan = Image.fromarray(plan_array)
    reference = ReferenceRaster(
        image=reference_image,
        bbox=BoundingBox(70.20, 53.00, 70.40, 53.20, "test"),
        source="offline-test",
        attribution="synthetic",
    )

    result = match_plan_to_reference(plan, reference)

    assert result.metrics.inliers >= 12
    assert len(result.gcps) >= 4
    assert result.confidence <= 0.79
    assert "automatic_result_requires_independent_manual_review" in result.reasons
    for gcp in result.gcps:
        assert 70.20 <= gcp.longitude <= 70.40
        assert 53.00 <= gcp.latitude <= 53.20


def test_blank_images_fail_conservatively() -> None:
    image = Image.new("RGB", (600, 400), "white")
    reference = ReferenceRaster(
        image=image,
        bbox=BoundingBox(70.20, 53.00, 70.40, 53.20, "test"),
        source="offline-test",
        attribution="synthetic",
    )

    result = match_plan_to_reference(image, reference)

    assert result.confidence == 0
    assert result.gcps == []
    assert "insufficient_keypoints" in result.reasons


def test_degenerate_homography_never_leaks_proposed_gcps() -> None:
    reference_image = synthetic_map()
    plan = reference_image.resize((150, 700)).resize(reference_image.size)
    reference = ReferenceRaster(
        image=reference_image,
        bbox=BoundingBox(70.20, 53.00, 70.40, 53.20, "test"),
        source="offline-test",
        attribution="synthetic",
    )

    result = match_plan_to_reference(plan, reference)

    if result.reasons != ["automatic_result_requires_independent_manual_review"]:
        assert result.gcps == []
        assert result.confidence < 0.20


def test_diagnostic_anchor_points_are_operator_only_not_proposed_gcps() -> None:
    plan_points = np.float32(
        [
            [10, 10],
            [240, 20],
            [230, 220],
            [20, 230],
            [120, 120],
        ]
    )
    reference_points = np.float32(
        [
            [20, 20],
            [250, 25],
            [245, 225],
            [25, 240],
            [130, 130],
        ]
    )
    residuals = np.float32([1.0, 2.0, 2.5, 3.0, 1.5])
    reference = ReferenceRaster(
        image=Image.new("RGB", (300, 260), "white"),
        bbox=BoundingBox(70.20, 53.00, 70.40, 53.20, "test"),
        source="offline-test",
        attribution="synthetic",
    )

    anchors = _diagnostic_anchor_points(
        plan_points,
        reference_points,
        residuals,
        plan_scale=1.0,
        reference_scale=1.0,
        reference=reference,
        metrics=MatchMetrics(
            candidate_matches=30,
            inliers=20,
            inlier_ratio=0.5,
            reprojection_rmse_px=3.0,
            plan_coverage=0.2,
            reference_coverage=0.2,
            homography_condition=999999999,
        ),
        warnings=["homography_is_ill_conditioned"],
        limit=12,
        max_residual_px=8.0,
    )

    assert len(anchors) >= 4
    assert anchors[0].scope == "operator_diagnostic_only"
    assert anchors[0].warnings == ["homography_is_ill_conditioned"]


class StaticBasemap:
    def __init__(self, image: Image.Image) -> None:
        self.image = image

    def fetch(self, bbox: BoundingBox, output_dir: Path) -> ReferenceRaster:
        return ReferenceRaster(
            image=self.image,
            bbox=bbox,
            source="offline-test",
            attribution="synthetic",
        )


def test_pipeline_writes_needs_manual_result_without_network(tmp_path: Path) -> None:
    source = tmp_path / "plan.png"
    synthetic_map().save(source)
    bbox = BoundingBox(70.20, 53.00, 70.40, 53.20, "test")

    result = run_autoregistration(
        AutoregConfig(
            source=source,
            output=tmp_path / "output",
            locality="Бурабай",
            bbox=bbox,
            bbox_padding=0,
            basemap_provider=StaticBasemap(synthetic_map()),
        )
    )

    payload = json.loads((tmp_path / "output" / "result.json").read_text("utf-8"))
    assert result.status == "needs_manual"
    assert payload["status"] == "needs_manual"
    assert payload["confidence"] <= 0.79
    assert "result" in payload["artifacts"]
    assert all(value != "approved" for value in payload.values())


def test_large_known_local_jpeg_is_decoded_under_explicit_safety_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "large-by-test-limit.jpg"
    Image.new("RGB", (40, 30), "white").save(source)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    loaded = _load_plan(
        source,
        max_dimension=20,
        max_source_pixels=2_000,
    )

    assert max(loaded.size) <= 20
    assert Image.MAX_IMAGE_PIXELS == 100


def test_jpeg_above_explicit_pixel_limit_is_safely_downsampled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "too-large.jpg"
    Image.new("RGB", (4_000, 3_000), "white").save(source)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    loaded = _load_plan(source, max_dimension=500, max_source_pixels=1_000_000)

    assert max(loaded.size) <= 500
    assert Image.MAX_IMAGE_PIXELS == 100


def test_non_jpeg_source_above_explicit_pixel_limit_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "too-large.png"
    Image.new("RGB", (40, 30), "white").save(source)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

    with pytest.raises(ValueError, match="pixel safety limit"):
        _load_plan(source, max_source_pixels=1_000)

    assert Image.MAX_IMAGE_PIXELS == 100
