from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as main
from app.config import settings
from app.db import Base
from app.genplan_pipeline import (
    extract_next_document_legend_draft,
    inspect_genplan_document,
    legend_entry_stats,
    sync_manual_genplans_into_pipeline,
)
from app.manual_genplans import ManualGenplanRecord
from app.models import GenplanLegendEntry, GenplanPipelineStatus, GenplanSourceDocument


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_inspect_pdf_routes_single_page_vector_document(tmp_path: Path) -> None:
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(
        b"%PDF-1.7\n"
        b"1 0 obj << /Type /Page /Resources << /Font << /F1 2 0 R >> >> >> endobj\n"
        b"10 10 m 20 20 l 30 30 l 40 40 l 50 50 l 60 60 l 70 70 l 80 80 l "
        b"90 90 l 100 100 l 110 110 l 120 120 l 130 130 l 140 140 l 150 150 l "
        b"160 160 l 170 170 l 180 180 l 190 190 l 200 200 l 210 210 l\n"
        b"BT (zone) Tj ET\n"
    )

    result = inspect_genplan_document(pdf, detected_format=".pdf")

    assert result.detected_format == "pdf"
    assert result.page_count == 1
    assert result.pdf_route == "vector_pdf"
    assert result.pipeline_status == GenplanPipelineStatus.ready_for_vector_extraction.value
    assert result.source_sha256


def test_sync_manual_genplans_into_pipeline_upserts_documents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pdf = tmp_path / "talqara.pdf"
    pdf.write_bytes(
        b"%PDF-1.7\n"
        b"1 0 obj << /Type /Page /Resources << /XObject << /Im1 2 0 R >> >> >> endobj\n"
        b"2 0 obj << /Subtype /Image /Width 1200 /Height 900 >> stream image endstream\n"
    )
    record = ManualGenplanRecord(
        asset_id="asset-pdf",
        region="Акмолинская область",
        district="Аккольский район",
        locality="Талкара",
        title="Генплан/ПДП: Талкара",
        relative_path="archive/talqara.pdf",
        filename="talqara.pdf",
        extension=".pdf",
        media_type="application/pdf",
        size_bytes=pdf.stat().st_size,
        confidence="medium",
    )
    monkeypatch.setattr("app.genplan_pipeline.manual_genplan_records", lambda: (record,))
    monkeypatch.setattr("app.genplan_pipeline.resolve_manual_genplan_file", lambda _: pdf)

    with build_session() as session:
        stats = sync_manual_genplans_into_pipeline(session, ingested_by="admin")
        document = session.scalar(select(GenplanSourceDocument))
        second = sync_manual_genplans_into_pipeline(session, ingested_by="admin")
        assert document is not None
        document_asset_id = document.asset_id
        document_status = document.pipeline_status
        document_route = document.pdf_route
        document_image_count = document.image_count

    assert stats["seen"] == 1
    assert stats["created"] == 1
    assert stats["pdf"] == 1
    assert second["updated"] == 1
    assert document_asset_id == "asset-pdf"
    assert document_status == GenplanPipelineStatus.ready_for_legend_extraction.value
    assert document_route == "raster_pdf"
    assert document_image_count == 1


def test_extract_next_document_legend_draft_from_raster_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from PIL import Image, ImageDraw

    image_path = tmp_path / "akmol.png"
    image = Image.new("RGB", (240, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 119, 159), fill=(38, 166, 91))
    draw.rectangle((120, 0, 239, 79), fill=(245, 176, 65))
    draw.rectangle((120, 80, 239, 159), fill=(52, 152, 219))
    image.save(image_path)
    record = ManualGenplanRecord(
        asset_id="asset-raster",
        region="Акмолинская область",
        district="г.Акколь",
        locality="г.Акколь",
        title="Генплан: Акколь",
        relative_path="archive/akmol.png",
        filename="akmol.png",
        extension=".png",
        media_type="image/png",
        size_bytes=image_path.stat().st_size,
        confidence="medium",
    )
    monkeypatch.setattr("app.genplan_pipeline.manual_genplan_records", lambda: (record,))
    monkeypatch.setattr("app.genplan_pipeline.resolve_manual_genplan_file", lambda _: image_path)

    with build_session() as session:
        sync_manual_genplans_into_pipeline(session, ingested_by="admin")
        stats = extract_next_document_legend_draft(session, limit_colors=6)
        document = session.scalar(select(GenplanSourceDocument))
        entries = session.scalars(select(GenplanLegendEntry)).all()
        legend_stats = legend_entry_stats(session)

    assert stats["document_found"] == 1
    assert stats["colors_created"] >= 3
    assert document is not None
    assert document.pipeline_status == GenplanPipelineStatus.legend_draft_ready.value
    assert {entry.review_status for entry in entries} == {"needs_review"}
    assert {entry.target_category for entry in entries} == {"unknown"}
    assert legend_stats["total"] == len(entries)
    assert legend_stats["needs_review"] == len(entries)


def test_extract_next_document_legend_draft_from_rendered_pdf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import fitz

    pdf_path = tmp_path / "rendered.pdf"
    document = fitz.open()
    page = document.new_page(width=240, height=160)
    page.draw_rect(fitz.Rect(0, 0, 120, 160), color=None, fill=(0.15, 0.65, 0.35))
    page.draw_rect(fitz.Rect(120, 0, 240, 80), color=None, fill=(0.95, 0.55, 0.1))
    page.draw_rect(fitz.Rect(120, 80, 240, 160), color=None, fill=(0.1, 0.35, 0.85))
    document.save(pdf_path)
    document.close()
    record = ManualGenplanRecord(
        asset_id="asset-rendered-pdf",
        region="Акмолинская область",
        district="г.Акколь",
        locality="г.Акколь",
        title="PDF генплан: Акколь",
        relative_path="archive/rendered.pdf",
        filename="rendered.pdf",
        extension=".pdf",
        media_type="application/pdf",
        size_bytes=pdf_path.stat().st_size,
        confidence="medium",
    )
    monkeypatch.setattr("app.genplan_pipeline.manual_genplan_records", lambda: (record,))
    monkeypatch.setattr("app.genplan_pipeline.resolve_manual_genplan_file", lambda _: pdf_path)

    with build_session() as session:
        sync_manual_genplans_into_pipeline(session, ingested_by="admin")
        stats = extract_next_document_legend_draft(session, limit_colors=6)
        result = session.scalar(select(GenplanSourceDocument))
        entries = session.scalars(select(GenplanLegendEntry)).all()

    assert stats["document_found"] == 1
    assert stats["colors_created"] >= 3
    assert result is not None
    assert result.pipeline_status == GenplanPipelineStatus.legend_draft_ready.value
    assert all(entry.source.startswith("rendered_pdf_page_") for entry in entries)


def test_api_genplan_catalog_returns_pipeline_documents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image_path = tmp_path / "akmol.png"
    image_path.write_bytes(b"not-used")
    record = ManualGenplanRecord(
        asset_id="asset-api",
        region="Акмолинская область",
        district="г.Акколь",
        locality="г.Акколь",
        title="Генплан: Акколь",
        relative_path="archive/akmol.png",
        filename="akmol.png",
        extension=".png",
        media_type="image/png",
        size_bytes=9,
        confidence="medium",
    )
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "internal_api_key", "site-secret")
    monkeypatch.setattr("app.genplan_pipeline.manual_genplan_records", lambda: (record,))
    monkeypatch.setattr("app.genplan_pipeline.resolve_manual_genplan_file", lambda _: image_path)
    session = build_session()
    try:
        sync_manual_genplans_into_pipeline(session, ingested_by="admin")
        document = session.scalar(select(GenplanSourceDocument))
        assert document is not None
        session.add(
            GenplanLegendEntry(
                document_id=document.id,
                color_hex="#26a65b",
                red=38,
                green=166,
                blue=91,
                source="dominant_color",
                target_category="unknown",
                layer_kind="unknown",
                confidence_score=0.35,
                review_status="needs_review",
            )
        )
        session.commit()

        def override_db():
            yield session

        main.app.dependency_overrides[main.get_db] = override_db
        response = TestClient(main.app).get(
            "/api/genplans/catalog?region=Акмолинская область",
            headers={"X-API-Key": "site-secret"},
        )
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["asset_id"] == "asset-api"
    assert payload["items"][0]["legend_entry_count"] == 1
