from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools.genplan_batch.cli import main
from tools.genplan_batch.dispatcher import (
    BatchConfig,
    BatchConfigurationError,
    run_batch,
)
from tools.genplan_batch.models import RunRequest


class MockRunner:
    def __init__(self, *, proposals: bool = True) -> None:
        self.proposals = proposals
        self.requests: list[RunRequest] = []

    def __call__(self, request: RunRequest) -> list[dict[str, Any]]:
        self.requests.append(request)
        attempts = []
        for basemap in request.basemaps:
            attempts.append(
                {
                    "basemap": basemap,
                    "result": {
                        "status": "needs_manual",
                        "source_sha256": request.source_sha256,
                        "proposed_gcps": (
                            [
                                {
                                    "plan_x": 10,
                                    "plan_y": 20,
                                    "longitude": 70.1,
                                    "latitude": 52.9,
                                }
                            ]
                            if self.proposals and basemap == "arcgis"
                            else []
                        ),
                        "reasons": [
                            "status_is_never_automatically_approved",
                        ],
                    },
                }
            )
        return attempts


class UnsafeRunner:
    def __call__(self, request: RunRequest) -> list[dict[str, Any]]:
        return [
            {
                "basemap": "arcgis",
                "result": {
                    "status": "STRICT",
                    "source_sha256": request.source_sha256,
                    "proposed_gcps": [],
                },
            }
        ]


class PipelineErrorRunner:
    def __call__(self, request: RunRequest) -> list[dict[str, Any]]:
        return [
            {
                "basemap": basemap,
                "result": {
                    "status": "needs_manual",
                    "source_sha256": request.source_sha256,
                    "proposed_gcps": [],
                    "reasons": [
                        "pipeline_error:BboxResolutionError:locality not resolved"
                    ],
                },
            }
            for basemap in request.basemaps
        ]


class DiagnosticAnchorRunner:
    def __call__(self, request: RunRequest) -> list[dict[str, Any]]:
        return [
            {
                "basemap": "osm",
                "result": {
                    "status": "needs_manual",
                    "source_sha256": request.source_sha256,
                    "proposed_gcps": [],
                    "diagnostic_anchor_points": [
                        {
                            "id": "diag-anchor-001",
                            "rank": 1,
                            "scope": "operator_diagnostic_only",
                            "plan_pixel": {"x": 1, "y": 2},
                            "reference_pixel": {"x": 3, "y": 4},
                            "reference_lonlat": {"longitude": 70.1, "latitude": 52.9},
                            "residual_px": 2.4,
                            "diagnostic_score": 0.4,
                            "source": "ransac_inlier",
                            "warnings": ["homography_is_ill_conditioned"],
                        }
                    ],
                    "diagnostic_anchor_guardrails": {
                        "import_eligible": False,
                        "customer_search_eligible": False,
                        "auto_apply_allowed": False,
                    },
                    "diagnostic_anchor_summary": {
                        "count": 1,
                        "quality_label": "weak_hint",
                    },
                    "reasons": [
                        "homography_is_ill_conditioned",
                        "status_is_never_automatically_approved",
                    ],
                },
            }
        ]


def _record(
    asset_id: str,
    filename: str,
    detected_format: str,
    content_sha: str,
    *,
    region: str = "Акмолинская область (01)",
    district: str = "р-н. Бурабайский (01-171)",
    locality: str = "Бурабай",
    source_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "asset_role": "plan_document",
        "sha256": content_sha,
        "extracted_path": str(source_path or (Path("C:/genplan") / filename)),
        "detected_format": detected_format,
        "egkn_region": region,
        "egkn_district": district,
        "normalized_locality": locality,
    }


@pytest.fixture
def inventory(tmp_path: Path) -> Path:
    first_sha = hashlib.sha256(b"same-raster").hexdigest()
    source = tmp_path / "source"
    source.mkdir()
    contents = {
        "burabay.jpg": b"same-raster",
        "burabay-copy.jpg": b"same-raster",
        "talqara.pdf": b"pdf",
        "shymkent.png": b"png",
        "notes.docx": b"docx",
    }
    for filename, content in contents.items():
        (source / filename).write_bytes(content)
    records = [
        _record(
            "asset-a",
            "burabay.jpg",
            "jpeg",
            first_sha,
            source_path=source / "burabay.jpg",
        ),
        _record(
            "asset-a-copy",
            "burabay-copy.jpg",
            "jpeg",
            first_sha,
            region="Мангистауская область (13)",
            district="г. Актау (13-221)",
            source_path=source / "burabay-copy.jpg",
        ),
        _record(
            "asset-pdf",
            "talqara.pdf",
            "pdf",
            hashlib.sha256(b"pdf").hexdigest(),
            district="р-н Аккольский (01-001)",
            locality="Талкара",
            source_path=source / "talqara.pdf",
        ),
        _record(
            "asset-png",
            "shymkent.png",
            "png",
            hashlib.sha256(b"png").hexdigest(),
            region="г. Шымкент (22)",
            district="г. Шымкент (22-319)",
            locality="Шымкент",
            source_path=source / "shymkent.png",
        ),
        {
            **_record(
                "support",
                "notes.docx",
                "docx",
                hashlib.sha256(b"docx").hexdigest(),
                source_path=source / "notes.docx",
            ),
            "asset_role": "supporting_document",
        },
    ]
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": "1.0", "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _config(
    inventory: Path,
    output: Path,
    **overrides: Any,
) -> BatchConfig:
    values: dict[str, Any] = {
        "manifest": inventory,
        "output": output,
        "min_free_disk_bytes": 0,
        "max_output_bytes": 1024**3,
        "workers": 2,
        "max_tiles": 12,
    }
    values.update(overrides)
    return BatchConfig(**values)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_batch_runs_unique_rasters_and_queues_pdf_without_page(
    tmp_path: Path,
    inventory: Path,
) -> None:
    runner = MockRunner()
    output = tmp_path / "batch"

    result = run_batch(_config(inventory, output), runner=runner)

    assert result.selected == 4
    assert result.runnable == 2
    assert result.duplicate_count == 1
    assert len(runner.requests) == 2
    assert all(request.basemaps == ("arcgis", "osm") for request in runner.requests)
    assert all(request.max_tiles == 12 for request in runner.requests)

    queue = _jsonl(output / "queue.jsonl")
    pdf = next(item for item in queue if item["asset_id"] == "asset-pdf")
    duplicate = next(item for item in queue if item["asset_id"] == "asset-a-copy")
    assert pdf["action"] == "render_manual"
    assert "page" not in pdf
    assert "no_pdf_page_was_invented" in pdf["reasons"]
    assert duplicate["action"] == "duplicate"
    assert duplicate["duplicate_of"] == "asset-a"
    assert "duplicate_metadata_conflict_requires_manual_review" in duplicate["reasons"]

    first_status = json.loads(
        (output / "assets" / "asset-a" / "status.json").read_text("utf-8")
    )
    assert first_status["registration_status"] == "proposed"
    assert first_status["workflow_state"] == "completed"
    assert {attempt["basemap"] for attempt in first_status["attempts"]} == {
        "arcgis",
        "osm",
    }
    assert result.summary["safety"]["qa_or_strict_automatic"] is False
    assert result.summary["attempt_error_assets"] == 0


def test_dry_run_filters_and_never_calls_runner(
    tmp_path: Path,
    inventory: Path,
) -> None:
    runner = MockRunner()
    output = tmp_path / "dry"

    result = run_batch(
        _config(
            inventory,
            output,
            region="Акмолинская",
            district="Бурабайский",
            limit=1,
            dry_run=True,
        ),
        runner=runner,
    )

    assert result.selected == 1
    assert runner.requests == []
    queue = _jsonl(output / "queue.jsonl")
    assert queue[0]["workflow_state"] == "queued"
    assert "dry_run_not_executed" in queue[0]["reasons"]


def test_resume_requires_matching_source_and_processing_config(
    tmp_path: Path,
    inventory: Path,
) -> None:
    output = tmp_path / "resume"
    first_runner = MockRunner()
    run_batch(_config(inventory, output, region="Шымкент"), runner=first_runner)
    assert len(first_runner.requests) == 1

    resumed_runner = MockRunner()
    resumed = run_batch(
        _config(inventory, output, region="Шымкент", resume=True),
        runner=resumed_runner,
    )
    assert resumed.resumed_count == 1
    assert resumed_runner.requests == []
    status = json.loads(
        (output / "assets" / "asset-png" / "status.json").read_text("utf-8")
    )
    assert status["resumed"] is True

    changed_runner = MockRunner()
    changed = run_batch(
        _config(
            inventory,
            output,
            region="Шымкент",
            resume=True,
            max_tiles=13,
        ),
        runner=changed_runner,
    )
    assert changed.resumed_count == 0
    assert len(changed_runner.requests) == 1


def test_changed_source_invalidates_resume_and_is_not_sent_to_runner(
    tmp_path: Path,
    inventory: Path,
) -> None:
    output = tmp_path / "changed-source"
    first_runner = MockRunner()
    run_batch(_config(inventory, output, region="Шымкент"), runner=first_runner)
    assert len(first_runner.requests) == 1

    manifest = json.loads(inventory.read_text(encoding="utf-8"))
    png = next(record for record in manifest["records"] if record["asset_id"] == "asset-png")
    Path(png["extracted_path"]).write_bytes(b"changed")
    resumed_runner = MockRunner()

    result = run_batch(
        _config(inventory, output, region="Шымкент", resume=True),
        runner=resumed_runner,
    )

    assert result.resumed_count == 0
    assert resumed_runner.requests == []
    status = json.loads(
        (output / "assets" / "asset-png" / "status.json").read_text("utf-8")
    )
    assert status["workflow_state"] == "failed"
    assert status["reasons"][0].startswith("runner_error:ValueError:source SHA-256")


def test_unsafe_runner_output_is_rejected_and_never_persisted(
    tmp_path: Path,
    inventory: Path,
) -> None:
    output = tmp_path / "unsafe"

    result = run_batch(
        _config(inventory, output, region="Шымкент"),
        runner=UnsafeRunner(),
    )

    assert result.failed_count == 1
    status_path = output / "assets" / "asset-png" / "status.json"
    text = status_path.read_text(encoding="utf-8")
    status = json.loads(text)
    assert status["registration_status"] == "needs_manual"
    assert status["attempts"] == []
    assert "STRICT" not in text
    assert "runner_result_rejected:arcgis:unsafe_status" in status["reasons"]


def test_output_disk_ceiling_stops_runner_conservatively(
    tmp_path: Path,
    inventory: Path,
) -> None:
    runner = MockRunner()
    output = tmp_path / "limited"

    result = run_batch(
        _config(
            inventory,
            output,
            region="Шымкент",
            max_output_bytes=1,
        ),
        runner=runner,
    )

    assert runner.requests == []
    assert result.failed_count == 1
    status = json.loads(
        (output / "assets" / "asset-png" / "status.json").read_text("utf-8")
    )
    assert status["registration_status"] == "needs_manual"
    assert status["reasons"][0].startswith("resource_limit:")


def test_invalid_resource_settings_are_rejected(
    tmp_path: Path,
    inventory: Path,
) -> None:
    with pytest.raises(BatchConfigurationError, match="max_tiles"):
        run_batch(_config(inventory, tmp_path / "invalid", max_tiles=145))


def test_cli_dry_run_writes_summary(
    tmp_path: Path,
    inventory: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "cli"

    exit_code = main(
        [
            "--manifest",
            str(inventory),
            "--output",
            str(output),
            "--region",
            "Шымкент",
            "--limit",
            "1",
            "--dry-run",
            "--min-free-disk-gb",
            "0",
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["selected"] == 1
    assert json.loads((output / "summary.json").read_text("utf-8"))["dry_run"] is True


def test_exclusion_registry_blocks_runner_and_changes_fingerprint(
    tmp_path: Path,
    inventory: Path,
) -> None:
    exclusion_file = tmp_path / "exclusions.json"
    exclusion_file.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "excluded_assets": [
                    {
                        "asset_id": "asset-png",
                        "reason": "identity_conflict:sheet_title_differs_from_filename",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runner = MockRunner()
    output = tmp_path / "excluded"
    manifest_payload = json.loads(inventory.read_text(encoding="utf-8"))
    png_record = next(
        item
        for item in manifest_payload["records"]
        if item["asset_id"] == "asset-png"
    )
    config = _config(
        inventory,
        output,
        region=png_record["egkn_region"],
        exclude_file=exclusion_file,
    )
    fingerprint = config.fingerprint()

    result = run_batch(config, runner=runner)

    assert runner.requests == []
    assert result.runnable == 0
    assert result.summary["workflow_counts"]["identity_conflict"] == 1
    status = json.loads(
        (output / "assets" / "asset-png" / "status.json").read_text("utf-8")
    )
    assert status["workflow_state"] == "identity_conflict"
    assert status["action"] == "manual_identity_review"
    assert status["registration_status"] == "needs_manual"
    assert "excluded_from_automatic_processing" in status["reasons"]

    exclusion_file.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "excluded_assets": [
                    {
                        "asset_id": "asset-png",
                        "reason": "identity_conflict:confirmed_wrong_locality",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert config.fingerprint() != fingerprint


def test_exclusion_registry_accepts_windows_utf8_bom(
    tmp_path: Path,
    inventory: Path,
) -> None:
    exclusion_file = tmp_path / "exclusions.json"
    exclusion_file.write_text(
        "\ufeff"
        + json.dumps(
            {
                "schema_version": "1.0",
                "excluded_assets": [
                    {
                        "asset_id": "asset-png",
                        "reason": "identity_conflict:manual_hold",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_batch(
        _config(inventory, tmp_path / "excluded-bom", exclude_file=exclusion_file),
        runner=MockRunner(),
    )

    assert result.summary["workflow_counts"]["identity_conflict"] == 1


def test_pipeline_attempt_errors_are_visible_in_summary(
    tmp_path: Path,
    inventory: Path,
) -> None:
    manifest_payload = json.loads(inventory.read_text(encoding="utf-8"))
    png_record = next(
        item
        for item in manifest_payload["records"]
        if item["asset_id"] == "asset-png"
    )

    result = run_batch(
        _config(
            inventory,
            tmp_path / "pipeline-error",
            region=png_record["egkn_region"],
        ),
        runner=PipelineErrorRunner(),
    )

    assert result.failed_count == 0
    assert result.summary["attempt_error_assets"] == 1
    assert result.summary["attempt_error_count"] == 1


def test_diagnostic_anchors_do_not_mark_batch_as_proposed(
    tmp_path: Path,
    inventory: Path,
) -> None:
    manifest_payload = json.loads(inventory.read_text(encoding="utf-8"))
    png_record = next(
        item
        for item in manifest_payload["records"]
        if item["asset_id"] == "asset-png"
    )

    result = run_batch(
        _config(
            inventory,
            tmp_path / "diagnostic-anchors",
            region=png_record["egkn_region"],
        ),
        runner=DiagnosticAnchorRunner(),
    )

    status = json.loads(
        (
            tmp_path
            / "diagnostic-anchors"
            / "assets"
            / "asset-png"
            / "status.json"
        ).read_text("utf-8")
    )
    assert result.summary["registration_counts"] == {"needs_manual": 1}
    assert status["registration_status"] == "needs_manual"
    assert status["attempts"][0]["result"]["proposed_gcps"] == []
    assert status["attempts"][0]["result"]["diagnostic_anchor_points"]
