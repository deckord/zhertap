from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.genplan_ggk import BuildError, build_ggk_release, list_ggk_documents
from tools.genplan_import import validate_release


def _polygon(min_x: float = 71.0, min_y: float = 51.0, size: float = 0.1) -> dict[str, Any]:
    return {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [
                    [min_x, min_y],
                    [min_x + size, min_y],
                    [min_x + size, min_y + size],
                    [min_x, min_y + size],
                    [min_x, min_y],
                ]
            ]
        ],
    }


def _line() -> dict[str, Any]:
    return {
        "type": "MultiLineString",
        "coordinates": [[[71.01, 51.01], [71.02, 51.02]]],
    }


def _feature(properties: dict[str, Any], geometry: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": geometry,
    }


class FakeClient:
    wfs_url = "https://gov.ggk.kz/geoserver/ows"

    def __init__(self, *, include_allowed: bool = True) -> None:
        functional = [
            _feature(
                {
                    "id": 101,
                    "creation_doc_id": 3607,
                    "gp_func_zone_code": "11100000",
                    "gp_func_zone_code_id": 2,
                    "deactivation_doc_id": None,
                },
                _polygon(71.03, 51.03, 0.01),
            ),
            _feature(
                {
                    "id": 102,
                    "creation_doc_id": 3607,
                    "gp_func_zone_code": "11160000",
                    "gp_func_zone_code_id": 3,
                    "deactivation_doc_id": None,
                },
                _polygon(71.0, 51.0, 0.1),
            ),
        ]
        if include_allowed:
            functional.insert(
                0,
                _feature(
                    {
                        "id": 100,
                        "creation_doc_id": 3607,
                        "gp_func_zone_code": "11010000",
                        "gp_func_zone_code_id": 1,
                        "deactivation_doc_id": None,
                    },
                    _polygon(),
                ),
            )
        self.rows = {
            "gp_documents": [
                _feature(
                    {
                        "id": 3607,
                        "gp_ggk_number": "25112024000002",
                        "doc_name": "Генеральный план г. Астана",
                        "doc_number": "№33",
                        "doc_date": "2024-04-25",
                        "kato_code_id": 10,
                        "approved_by": "Постановление Правительства РК",
                        "status_id": 1,
                        "deactivation_date": None,
                        "kato_name_ru": "г.Астана",
                    },
                    _polygon(70.8, 50.8, 0.6),
                )
            ],
            "kato_ref": [
                _feature(
                    {
                        "id": 10,
                        "kato": "710000000",
                        "name_ru": "г.Астана",
                        "parent_id": None,
                    }
                )
            ],
            "gp_func_zone_codes_ref": [
                _feature(
                    {
                        "id": 1,
                        "code": "11010000",
                        "name_ru": "Территория усадебной застройки",
                    }
                ),
                _feature(
                    {
                        "id": 2,
                        "code": "11100000",
                        "name_ru": "Территория автомобильных дорог",
                    }
                ),
                _feature(
                    {
                        "id": 3,
                        "code": "11160000",
                        "name_ru": "Зона обеспеченности энергоснабжением",
                    }
                ),
            ],
            "gp_functional_zones": functional,
            "gp_red_lines": [
                _feature(
                    {"id": 200, "creation_doc_id": 3607, "deactivation_doc_id": None},
                    _line(),
                )
            ],
        }

    def features(
        self,
        type_name: str,
        *,
        cql_filter: str | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        del cql_filter, page_size
        return json.loads(json.dumps(self.rows[type_name]))

    def one(self, type_name: str, *, cql_filter: str) -> dict[str, Any]:
        del cql_filter
        return json.loads(json.dumps(self.rows[type_name][0]))


def _review(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "status": "VERIFIED_STRICT",
                "independent_review": True,
                "reviewer": "reviewer-a2",
                "reviewed_at_utc": "2026-07-23T10:00:00+00:00",
                "checks": {
                    "document_identity_verified": True,
                    "legal_act_verified": True,
                    "kato_scope_verified": True,
                    "zone_mapping_verified": True,
                    "geometry_bounds_verified": True,
                    "random_visual_samples_verified": True,
                },
                "legal_act": {
                    "number": "№33",
                    "date": "2024-01-25",
                    "url": "https://adilet.zan.kz/rus/docs/P2400000033",
                    "status": "active",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_builds_release_accepted_by_existing_import_gate(tmp_path: Path) -> None:
    result = build_ggk_release(
        3607,
        "lph-household",
        tmp_path / "release",
        _review(tmp_path / "review-input.json"),
        client=FakeClient(),
    )
    release = validate_release(result.manifest_path)

    assert release.region == "г.Астана"
    assert release.district == "*"
    assert release.locality == "*"
    assert release.purpose == "ЛПХ:household"
    assert release.approval_date.isoformat() == "2024-01-25"
    assert release.approved_for_search is True
    assert result.layer_counts == {"allowed": 1, "prohibited": 1, "red_line": 1}

    prohibited = json.loads(
        (result.manifest_path.parent / "prohibited.geojson").read_text(encoding="utf-8")
    )
    assert {
        feature["properties"]["ggk_zone_code"]
        for feature in prohibited["features"]
    } == {"11100000"}


def test_engineering_coverage_is_not_misclassified_as_prohibited(tmp_path: Path) -> None:
    result = build_ggk_release(
        3607,
        "lph-household",
        tmp_path / "release",
        _review(tmp_path / "review-input.json"),
        client=FakeClient(),
    )
    provenance = json.loads(
        (result.manifest_path.parent / "provenance.json").read_text(encoding="utf-8")
    )

    assert provenance["zone_counts"]["11160000"] == 1
    assert result.layer_counts["prohibited"] == 1


def test_polygon_red_line_is_converted_to_boundary(tmp_path: Path) -> None:
    client = FakeClient()
    client.rows["gp_red_lines"].append(
        _feature(
            {"id": 201, "creation_doc_id": 3607, "deactivation_doc_id": None},
            _polygon(71.04, 51.04, 0.01),
        )
    )
    result = build_ggk_release(
        3607,
        "lph-household",
        tmp_path / "release",
        _review(tmp_path / "review-input.json"),
        client=client,
    )
    red_lines = json.loads(
        (result.manifest_path.parent / "red_line.geojson").read_text(encoding="utf-8")
    )
    raw_red_lines = json.loads(
        (result.manifest_path.parent / "source" / "red-line.raw.geojson").read_text(
            encoding="utf-8"
        )
    )

    assert red_lines["features"][1]["geometry"]["type"] in {
        "LineString",
        "MultiLineString",
    }
    assert raw_red_lines["features"][1]["geometry"]["type"] == "MultiPolygon"


def test_blocks_document_without_profile_allowed_zone(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="contains no allowed zones"):
        build_ggk_release(
            3607,
            "lph-household",
            tmp_path / "release",
            _review(tmp_path / "review-input.json"),
            client=FakeClient(include_allowed=False),
        )


def test_blocks_legal_act_number_mismatch(tmp_path: Path) -> None:
    review_path = _review(tmp_path / "review-input.json")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["legal_act"]["number"] = "№999"
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(BuildError, match="number does not match"):
        build_ggk_release(
            3607,
            "lph-household",
            tmp_path / "release",
            review_path,
            client=FakeClient(),
        )


def test_allows_amending_legal_act_when_base_matches_wfs_document(tmp_path: Path) -> None:
    review_path = _review(tmp_path / "review-input.json")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["legal_act"] = {
        "number": "№697",
        "date": "2026-08-04",
        "url": "https://adilet.zan.kz/rus/docs/P2600000697",
        "status": "active",
        "base_legal_act": {
            "number": "№33",
            "date": "2024-01-25",
            "url": "https://adilet.zan.kz/rus/docs/P2400000033",
            "status": "active",
        },
    }
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    result = build_ggk_release(
        3607,
        "lph-household",
        tmp_path / "release",
        review_path,
        client=FakeClient(),
    )
    release = validate_release(result.manifest_path)

    assert release.approval_date.isoformat() == "2026-08-04"
    assert release.source_url == "https://adilet.zan.kz/rus/docs/P2600000697"
    assert "№697" in release.approval_document
    assert "№33" in release.approval_document


def test_catalog_reports_document_identity() -> None:
    rows = list_ggk_documents(FakeClient())

    assert rows == [
        {
            "id": 3607,
            "locality": "г.Астана",
            "title": "Генеральный план г. Астана",
            "number": "№33",
            "date": "2024-04-25",
            "status_id": 1,
            "deactivation_date": None,
        }
    ]
