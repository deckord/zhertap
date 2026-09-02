import io
import zipfile
from dataclasses import replace
from datetime import UTC, datetime

from app.auction_document_extractor import (
    DocumentFactCandidate,
    DocumentMetadata,
    document_candidate_conflicts,
    extract_auction_document,
)


def _docx(paragraphs: list[str]) -> bytes:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml.encode("utf-8"))
    return output.getvalue()


def _candidate(field: str, value: object) -> DocumentFactCandidate:
    return DocumentFactCandidate(
        field=field,
        value=value,
        document_id=1,
        document_title="Договор",
        source_url="https://example.test/doc",
        page=1,
        section=None,
        evidence_excerpt="условие",
        quote_hash="q",
        content_hash="h",
        extractor_version="test",
        confidence=0.8,
        observed_at=None,
        extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_document_conflicts_do_not_treat_additional_termination_grounds_as_contradictions() -> None:
    conflicts = document_candidate_conflicts(
        [
            _candidate("termination_ground", "расторжение при неосвоении"),
            _candidate("termination_ground", "расторжение при неуплате"),
        ]
    )

    assert conflicts == ()


def test_document_conflicts_keep_mutually_exclusive_lease_terms() -> None:
    conflicts = document_candidate_conflicts(
        [
            _candidate("lease_term_years", 3),
            _candidate("lease_term_years", 5),
        ]
    )

    assert [(item.field, item.values) for item in conflicts] == [
        ("lease_term_years", (3, 5))
    ]


def test_document_conflicts_preserve_explicit_single_candidate_conflict() -> None:
    candidate = replace(_candidate("right_type", "ownership"), status="conflict")

    conflicts = document_candidate_conflicts([candidate])

    assert [(item.field, item.values, item.candidate_indexes) for item in conflicts] == [
        ("right_type", ("ownership",), (0,))
    ]


def test_rule_extraction_marks_document_term_that_contradicts_official_lot_card() -> None:
    result = extract_auction_document(
        _docx(["Право аренды земельного участка сроком на 5 лет."]),
        DocumentMetadata(
            document_id=7,
            title="Договор.docx",
            source_url="https://example.test/contract.docx",
            file_type="docx",
            lot_context={"right_type": "lease", "lease_term_years": 10},
        ),
        extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    lease_term = next(item for item in result.candidates if item.field == "lease_term_years")
    assert lease_term.status == "conflict"
    assert [(item.field, item.values) for item in result.conflicts] == [
        ("lease_term_years", (10, 5.0))
    ]
    assert result.conflicts[0].lot_context_value == 10
    assert result.as_dict()["conflicts"][0]["lot_context_value"] == 10


def test_rule_extraction_does_not_flag_matching_official_lot_context() -> None:
    result = extract_auction_document(
        _docx(["Право аренды земельного участка сроком на 10 лет."]),
        DocumentMetadata(
            document_id=8,
            title="Договор.docx",
            source_url="https://example.test/contract.docx",
            file_type="docx",
            lot_context={"right_type": "lease", "lease_term_years": 10},
        ),
        extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result.conflicts == ()
    assert all(item.status != "conflict" for item in result.candidates)


def test_document_conflicts_include_area_and_cadastral_identity_against_lot_card() -> None:
    conflicts = document_candidate_conflicts(
        [
            _candidate("area_hectares", 1.25),
            _candidate("cadastral_number", "01-123-456-789"),
        ],
        {
            "area_hectares": 2.5,
            "cadastral_number": "01:123:456:000",
        },
    )

    assert [(item.field, item.lot_context_value) for item in conflicts] == [
        ("area_hectares", 2.5),
        ("cadastral_number", "01:123:456:000"),
    ]


def test_cadastral_identity_formatting_alone_is_not_a_conflict() -> None:
    conflicts = document_candidate_conflicts(
        [_candidate("cadastral_number", "01-123-456-789")],
        {"cadastral_number": "01:123:456:789"},
    )

    assert conflicts == ()