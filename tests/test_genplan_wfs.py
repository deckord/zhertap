from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.genplan_wfs import BuildError, build_shymkent_release, extract_shymkent_layers


def _polygon() -> dict[str, Any]:
    return {
        "type": "MultiPolygon",
        "coordinates": [[[
            [69.5, 42.2],
            [69.6, 42.2],
            [69.6, 42.3],
            [69.5, 42.3],
            [69.5, 42.2],
        ]]],
    }


def _line() -> dict[str, Any]:
    return {
        "type": "MultiLineString",
        "coordinates": [[[69.5, 42.2], [69.6, 42.3]]],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _feature(properties: dict[str, Any], geometry: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def _source(tmp_path: Path, allowed_count: int = 1000) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    functional = {
        "type": "FeatureCollection",
        "features": [
            _feature(
                {
                    "index": "Ж-1",
                    "functional": "усадебной застройки (1-3 этажа)",
                    "name_usl_1": "Территория усадебной застройки",
                },
                _polygon(),
            )
            for _ in range(allowed_count)
        ]
        + [
            _feature(
                {"index": "Ж-5", "functional": "6-12 этажной застройки"},
                _polygon(),
            )
        ],
    }
    roads = {
        "type": "FeatureCollection",
        "features": [_feature({"name_usl": "Дороги и проезды"}, _polygon())],
    }
    red_lines = {
        "type": "FeatureCollection",
        "features": [
            _feature(
                {"number_post": "№916", "approved_date": "2023/10/17 00:00:00.000"},
                _line(),
            )
        ],
    }
    _write_json(source / "gpfunctionalzone_main.raw.geojson", functional)
    _write_json(source / "gpautotranrdc_main.raw.geojson", roads)
    _write_json(source / "gpregredlinelin.raw.geojson", red_lines)
    (source / "wfs-capabilities.xml").write_text("<WFS_Capabilities/>", encoding="utf-8")
    (source / "P2300000916.pdf").write_bytes(b"%PDF-1.7 test")
    for filename in (
        "gpfunctionalzone_main.sld",
        "gpautotranrdc_main.sld",
        "gpregredlinelin.sld",
    ):
        (source / filename).write_text("<StyledLayerDescriptor/>", encoding="utf-8")
    return source


def _review(path: Path, reviewer: str = "reviewer-a2") -> Path:
    payload = {
        "status": "VERIFIED_STRICT",
        "independent_review": True,
        "reviewer": reviewer,
        "reviewed_at_utc": "2026-07-23T10:30:00+00:00",
        "checks": {
            "wfs_schema_verified": True,
            "resolution_916_verified": True,
            "geometry_bounds_verified": True,
            "random_visual_samples_verified": True,
        },
    }
    _write_json(path, payload)
    return path


def test_extracts_only_usadba_and_keeps_roads_and_red_lines(tmp_path: Path) -> None:
    layers = extract_shymkent_layers(_source(tmp_path))

    assert len(layers["allowed"]["features"]) == 1000
    assert len(layers["prohibited"]["features"]) == 1
    assert len(layers["red_line"]["features"]) == 1
    assert {
        feature["properties"]["index"]
        for feature in layers["allowed"]["features"]
    } == {"Ж-1"}


def test_builds_release_accepted_by_safe_import_validator(tmp_path: Path) -> None:
    from tools.genplan_import import validate_release

    source = _source(tmp_path)
    review = _review(tmp_path / "independent-review.json")
    result = build_shymkent_release(source, tmp_path / "release", review)
    release = validate_release(result.manifest_path)

    assert release.release_id == "shymkent-genplan-916-wfs-v1"
    assert release.purpose == "ЛПХ"
    assert release.qa_status == "VERIFIED_STRICT"
    assert release.approved_for_search is True
    assert result.layer_counts == {"allowed": 1000, "prohibited": 1, "red_line": 1}


def test_rejects_changed_red_line_provenance(tmp_path: Path) -> None:
    source = _source(tmp_path)
    red_line_path = source / "gpregredlinelin.raw.geojson"
    payload = json.loads(red_line_path.read_text(encoding="utf-8"))
    payload["features"][0]["properties"]["number_post"] = "№915"
    _write_json(red_line_path, payload)

    with pytest.raises(BuildError, match="Resolution №916"):
        extract_shymkent_layers(source)


def test_rejects_self_review(tmp_path: Path) -> None:
    source = _source(tmp_path)
    review = _review(tmp_path / "independent-review.json", reviewer="operator-a1")

    with pytest.raises(BuildError, match="must differ"):
        build_shymkent_release(
            source,
            tmp_path / "release",
            review,
            operator="operator-a1",
        )
