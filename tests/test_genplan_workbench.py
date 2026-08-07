from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tools.genplan_autoreg.models import BoundingBox
from tools.genplan_workbench.math import EARTH_RADIUS_M, calculate_transform
from tools.genplan_workbench.models import GCP, TransformType
from tools.genplan_workbench.server import create_app
from tools.genplan_workbench.store import ManifestStore, WorkbenchError, safe_path


def _wgs84_from_local(
    local_x: float, local_y: float, *, lon0: float = 71.0, lat0: float = 51.0
) -> tuple[float, float]:
    lon = lon0 + math.degrees(local_x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    lat = lat0 + math.degrees(local_y / EARTH_RADIUS_M)
    return lon, lat


def _point(
    point_id: str,
    pixel_x: float,
    pixel_y: float,
    *,
    role: str = "train",
) -> GCP:
    local_x = 2.5 * pixel_x + 0.4 * pixel_y + 125
    local_y = -0.2 * pixel_x + 2.2 * pixel_y - 80
    lon, lat = _wgs84_from_local(local_x, local_y)
    return GCP(
        id=point_id,
        pixel_x=pixel_x,
        pixel_y=pixel_y,
        lon=lon,
        lat=lat,
        role=role,
        reference_source="test",
    )


def _distributed_points() -> list[GCP]:
    return [
        _point("p1", 50, 50),
        _point("p2", 950, 50),
        _point("p3", 950, 750),
        _point("p4", 50, 750),
        _point("p5", 500, 100),
        _point("p6", 500, 700),
        _point("c1", 250, 400, role="checkpoint"),
        _point("c2", 750, 400, role="checkpoint"),
    ]


def _write_manifest(root: Path, source: Path) -> Path:
    manifest = root / "manifests" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "original_filename": source.name,
                        "extracted_path": str(source),
                        "detected_format": "png",
                        "width_px": 1000,
                        "height_px": 800,
                        "page_count": 1,
                        "normalized_region": "Акмолинская область",
                        "normalized_locality": "Бурабай",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest


def _write_location_manifest(
    root: Path,
    source: Path,
    *,
    region: str,
    locality: str,
    asset_id: str = "asset-1",
) -> Path:
    manifest = root / "manifests" / f"{asset_id}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": asset_id,
                        "original_filename": source.name,
                        "extracted_path": str(source),
                        "detected_format": "png",
                        "normalized_region": region,
                        "normalized_locality": locality,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest


def test_affine_transform_has_near_zero_independent_residuals() -> None:
    result = calculate_transform(
        _distributed_points(),
        TransformType.affine,
        image_width_px=1000,
        image_height_px=800,
    )

    assert result["train_rmse_m"] == pytest.approx(0, abs=0.001)
    assert result["checkpoint_rmse_m"] == pytest.approx(0, abs=0.001)
    assert result["distribution"]["status"] == "good"
    assert result["approval"]["automatic_approval"] is False
    assert result["approval"]["status"] == "qa_pending"


def test_projective_transform_fits_known_homography() -> None:
    pixels = [
        (50, 50, "train"),
        (950, 50, "train"),
        (950, 750, "train"),
        (50, 750, "train"),
        (500, 100, "train"),
        (500, 700, "train"),
        (300, 300, "checkpoint"),
        (700, 500, "checkpoint"),
    ]
    points: list[GCP] = []
    for index, (pixel_x, pixel_y, role) in enumerate(pixels):
        denominator = 1 + 0.00015 * pixel_x - 0.00008 * pixel_y
        local_x = (1.7 * pixel_x + 0.3 * pixel_y + 40) / denominator
        local_y = (-0.1 * pixel_x + 1.9 * pixel_y - 20) / denominator
        lon, lat = _wgs84_from_local(local_x, local_y)
        points.append(
            GCP(
                id=f"p{index}",
                pixel_x=pixel_x,
                pixel_y=pixel_y,
                lon=lon,
                lat=lat,
                role=role,
            )
        )

    result = calculate_transform(
        points,
        TransformType.projective,
        image_width_px=1000,
        image_height_px=800,
    )

    assert result["checkpoint_rmse_m"] == pytest.approx(0, abs=0.01)
    assert result["max_residual_m"] == pytest.approx(0, abs=0.01)


def test_safe_path_rejects_escape_and_manifest_source_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not-an-image")

    with pytest.raises(WorkbenchError, match="outside"):
        safe_path(root, outside, must_exist=True)

    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"records": [{"asset_id": "bad", "extracted_path": str(outside)}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkbenchError, match="outside"):
        ManifestStore(root, manifest)


def test_api_saves_exports_and_never_accepts_approved(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
    )
    manifest = _write_manifest(tmp_path, source)
    app = create_app(data_root=tmp_path, manifest_path=manifest)
    client = TestClient(app)

    list_payload = client.get("/api/records").json()["records"][0]
    assert list_payload["record_id"] == "asset-1"
    assert list_payload["queue_status"] == ""
    assert list_payload["has_saved_gcps"] is False
    assert list_payload["saved_point_count"] == 0
    assert client.get("/api/records/asset-1/image").status_code == 200

    points = [point.model_dump(mode="json") for point in _distributed_points()]
    request = {
        "page": 1,
        "image_width_px": 1000,
        "image_height_px": 800,
        "transform_type": "affine",
        "workflow_status": "qa_pending",
        "operator": "operator-1",
        "notes": "Ready for independent review",
        "points": points,
    }

    response = client.put("/api/records/asset-1/gcps", json=request)

    assert response.status_code == 200
    payload = response.json()
    assert payload["gcps"]["workflow_status"] == "qa_pending"
    assert payload["qa"]["qa_decision"] == "pending"
    assert payload["qa"]["guardrails"]["approved_by_workbench"] is False
    saved_list_payload = client.get("/api/records").json()["records"][0]
    assert saved_list_payload["workflow_status"] == "qa_pending"
    assert saved_list_payload["has_saved_gcps"] is True
    assert saved_list_payload["saved_point_count"] == len(points)
    assert client.get("/api/records/asset-1/export/gcps").status_code == 200
    assert client.get("/api/records/asset-1/export/qa").status_code == 200

    request["workflow_status"] = "approved"
    rejected = client.put("/api/records/asset-1/gcps", json=request)
    assert rejected.status_code == 422


def test_api_default_bbox_resolver_uses_static_city_reference(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(b"image")
    manifest = _write_location_manifest(
        tmp_path,
        source,
        region="Астана",
        locality="Астана",
    )
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))

    response = client.get("/api/records/asset-1/bbox")

    assert response.status_code == 200
    assert response.json()["source"] == "static_bbox"
    assert response.json()["label"] == "Астана"


def test_api_rejects_point_outside_source_image(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(b"image")
    manifest = _write_manifest(tmp_path, source)
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))
    points = [point.model_dump(mode="json") for point in _distributed_points()]
    points[0]["pixel_x"] = 1001

    response = client.put(
        "/api/records/asset-1/gcps",
        json={
            "image_width_px": 1000,
            "image_height_px": 800,
            "points": points,
        },
    )

    assert response.status_code == 400
    assert "outside" in response.json()["detail"]


def test_api_serves_autoreg_artifacts_from_manifest(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    artifact = tmp_path / "work" / "autoreg" / "assets" / "asset-1" / "matches.jpg"
    source.write_bytes(b"image")
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"matches")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "original_filename": source.name,
                        "extracted_path": str(source),
                        "detected_format": "png",
                        "autoreg_diagnostics": {
                            "attempts": [
                                {
                                    "basemap": "osm",
                                    "confidence": 0.31,
                                    "metrics": {
                                        "inliers": 8,
                                        "inlier_ratio": 0.4,
                                        "reprojection_rmse_px": 42,
                                    },
                                    "reasons": [],
                                    "artifacts": {"matches": str(artifact)},
                                }
                            ],
                            "best_attempt": {
                                "basemap": "osm",
                                "confidence": 0.31,
                                "metrics": {
                                    "inliers": 8,
                                    "inlier_ratio": 0.4,
                                    "reprojection_rmse_px": 42,
                                },
                                "artifacts": {"matches": str(artifact)},
                            },
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))

    list_payload = client.get("/api/records").json()["records"][0]
    detail = client.get("/api/records/asset-1").json()
    response = client.get("/api/records/asset-1/autoreg/osm/matches")
    rejected = client.get("/api/records/asset-1/autoreg/osm/unknown")

    assert list_payload["autoreg_has_attempts"] is True
    assert list_payload["autoreg_best_basemap"] == "osm"
    assert list_payload["autoreg_inliers"] == 8
    assert list_payload["autoreg_has_pipeline_error"] is False
    assert detail["autoreg_diagnostics"]["best_attempt"]["basemap"] == "osm"
    assert response.status_code == 200
    assert response.content == b"matches"
    assert rejected.status_code == 400


def test_api_serves_operator_diagnostic_anchors(tmp_path: Path) -> None:
    from PIL import Image

    source = tmp_path / "scan.png"
    diagnostic_source = tmp_path / "diagnostic-scan.png"
    result_path = tmp_path / "work" / "autoreg" / "assets" / "asset-1" / "result.json"
    Image.new("RGB", (500, 400), "white").save(source)
    Image.new("RGB", (1000, 800), "white").save(diagnostic_source)
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(
            {
                "source_path": str(diagnostic_source),
                "diagnostic_anchor_points": [
                    {
                        "id": "diag-anchor-001",
                        "rank": 1,
                        "scope": "operator_diagnostic_only",
                        "plan_pixel": {"x": 100.0, "y": 120.0},
                        "reference_lonlat": {
                            "longitude": 71.12345678,
                            "latitude": 51.12345678,
                        },
                    }
                ],
                "diagnostic_anchor_guardrails": {
                    "customer_search_eligible": False,
                    "import_eligible": False,
                    "auto_apply_allowed": False,
                },
                "diagnostic_anchor_summary": {
                    "count": 1,
                    "quality_label": "weak_hint",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "original_filename": source.name,
                        "extracted_path": str(source),
                        "detected_format": "png",
                        "autoreg_diagnostics": {
                            "attempts": [
                                {
                                    "basemap": "osm",
                                    "diagnostic_anchor_count": 1,
                                    "artifacts": {"result": str(result_path)},
                                }
                            ],
                            "best_attempt": {
                                "basemap": "osm",
                                "diagnostic_anchor_count": 1,
                                "artifacts": {"result": str(result_path)},
                            },
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))

    response = client.get("/api/records/asset-1/diagnostic-anchors/osm")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "operator_diagnostic_only"
    assert payload["matcher_image_width_px"] == 1000
    assert payload["matcher_image_height_px"] == 800
    assert payload["guardrails"]["customer_search_eligible"] is False
    assert payload["anchors"][0]["plan_pixel"]["x"] == 100.0


def test_api_renders_tiff_source_image(tmp_path: Path) -> None:
    from PIL import Image

    source = tmp_path / "scan.tif"
    Image.new("RGB", (2, 2), "white").save(source)
    manifest = _write_manifest(tmp_path, source)
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))

    response = client.get("/api/records/asset-1/image")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_pdf_poppler_fallback_uses_ascii_temp_source(tmp_path: Path, monkeypatch) -> None:
    import builtins

    import tools.genplan_workbench.render as render_module

    source = tmp_path / "Дамса.pdf"
    source.write_bytes(b"%PDF-1.4")
    destination = tmp_path / "rendered" / "page.png"
    commands: list[list[str]] = []
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in {"fitz", "pypdfium2"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    def fake_run(command, *, check, capture_output, timeout):
        commands.append(command)
        Path(f"{command[-1]}.png").write_bytes(b"png")

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(render_module.shutil, "which", lambda name: "pdftoppm")
    monkeypatch.setattr(render_module.subprocess, "run", fake_run)

    result = render_module.render_pdf_page(
        source,
        destination,
        page=1,
        data_root=tmp_path,
    )

    assert result == destination
    assert destination.exists()
    assert commands
    poppler_source = commands[0][-2]
    poppler_source.encode("ascii")
    assert poppler_source.endswith(".source.pdf")
    assert not Path(poppler_source).exists()


def test_api_serves_contact_sheet_when_manifest_has_one(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
    )
    contact_sheet = tmp_path / "contact.png"
    contact_sheet.write_bytes(source.read_bytes())
    manifest = tmp_path / "manifests" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "original_filename": source.name,
                        "extracted_path": str(source),
                        "pdf_contact_sheet_path": str(contact_sheet),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))

    assert client.get("/api/records/asset-1").json()["has_contact_sheet"] is True
    response = client.get("/api/records/asset-1/contact-sheet")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_api_egkn_boundary_and_parcels_return_geojson(
    tmp_path: Path, monkeypatch
) -> None:
    from shapely.geometry import Polygon

    import app.providers.egkn as egkn_module

    district_info = egkn_module.DistrictInfo(
        id=10,
        region_name="Акмолинская область",
        code="01-011",
        name="Целиноградский",
        display_name="р-н. Целиноградский (01-011)",
        srs=4326,
        ate_code="150807",
        kato="116600000",
    )
    boundary_geom = Polygon([(71.30, 50.80), (71.40, 50.80), (71.40, 50.90), (71.30, 50.90)])
    settlement_info = egkn_module.SettlementInfo(
        gid="295",
        name="Кабанбай батыр",
        kato="116665100",
        district_id=10,
        geometry=boundary_geom,
    )
    parcel_geom = Polygon([(71.31, 50.81), (71.311, 50.81), (71.311, 50.811), (71.31, 50.811)])

    class FakeProvider:
        def find_district(self, region: str, district: str) -> egkn_module.DistrictInfo:
            assert region == "Акмолинская область"
            assert district == "Целиноградский район"
            return district_info

        def find_settlement(self, district_id: int, locality: str) -> egkn_module.SettlementInfo:
            assert district_id == 10
            assert locality == "кабанбай батыра"
            return settlement_info

        def parcels(
            self,
            district: egkn_module.DistrictInfo,
            settlement: egkn_module.SettlementInfo,
        ) -> list[egkn_module.ParcelRecord]:
            return [
                egkn_module.ParcelRecord(
                    geometry=parcel_geom,
                    cadastre="01-011-123-456",
                    address="test address",
                    land_use="ЛПХ",
                    area_m2=1000.0,
                )
            ]

    monkeypatch.setattr(egkn_module, "EgknProvider", FakeProvider)

    source = tmp_path / "scan.png"
    source.write_bytes(b"image")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "asset_id": "asset-1",
                        "original_filename": source.name,
                        "extracted_path": str(source),
                        "detected_format": "png",
                        "normalized_region": "Акмолинская область",
                        "normalized_district": "Целиноградский район",
                        "normalized_locality": "кабанбай батыра",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))

    boundary_response = client.get("/api/records/asset-1/egkn/boundary")
    parcels_response = client.get("/api/records/asset-1/egkn/parcels")

    assert boundary_response.status_code == 200
    boundary_payload = boundary_response.json()
    assert boundary_payload["type"] == "Feature"
    assert boundary_payload["properties"]["kato"] == "116665100"
    assert boundary_payload["geometry"]["type"] == "Polygon"

    assert parcels_response.status_code == 200
    parcels_payload = parcels_response.json()
    assert parcels_payload["type"] == "FeatureCollection"
    assert len(parcels_payload["features"]) == 1
    assert parcels_payload["features"][0]["properties"]["cadastre"] == "01-011-123-456"


def test_api_egkn_boundary_fails_cleanly_without_district(tmp_path: Path) -> None:
    source = tmp_path / "scan.png"
    source.write_bytes(b"image")
    manifest = _write_manifest(tmp_path, source)
    client = TestClient(create_app(data_root=tmp_path, manifest_path=manifest))

    response = client.get("/api/records/asset-1/egkn/boundary")

    assert response.status_code == 400


def test_api_resolves_bbox_for_manifest_location(tmp_path: Path) -> None:
    class Resolver:
        def resolve(
            self,
            locality: str,
            *,
            region: str = "",
            district: str = "",
        ) -> BoundingBox:
            assert locality == "Бурабай"
            assert region == "Акмолинская область"
            assert district == ""
            return BoundingBox(
                west=70.20,
                south=52.97,
                east=70.38,
                north=53.13,
                source="test",
                label="Бурабай",
            )

    source = tmp_path / "scan.png"
    source.write_bytes(b"image")
    manifest = _write_manifest(tmp_path, source)
    client = TestClient(
        create_app(
            data_root=tmp_path,
            manifest_path=manifest,
            bbox_resolver=Resolver(),
        )
    )

    response = client.get("/api/records/asset-1/bbox")

    assert response.status_code == 200
    assert response.json() == {
        "west": 70.2,
        "south": 52.97,
        "east": 70.38,
        "north": 53.13,
        "source": "test",
        "label": "Бурабай",
    }
