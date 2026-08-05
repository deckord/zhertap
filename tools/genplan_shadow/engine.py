from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .models import (
    ComparisonRequest,
    ComparisonResult,
    Decision,
    Layer,
    LayerKind,
    ProvenanceStatus,
    Reason,
    SourceEvaluation,
)

SUPPORTED_CANDIDATE_TYPES = {"Point", "Polygon", "MultiPolygon"}
BLOCKING_CATEGORIES = {"road", "water"}


@dataclass(frozen=True)
class LayerAssessment:
    layer: Layer
    coverage: BaseGeometry
    masks: dict[str, Any] | None
    categories_checked: frozenset[str]
    eligibility: str
    reasons: tuple[Reason, ...]


def _reason(
    code: str,
    message: str,
    layer: Layer | None = None,
    feature_ids: list[str] | None = None,
) -> Reason:
    return Reason(
        code=code,
        message=message,
        layer_id=layer.layer_id if layer else None,
        source_version=layer.provenance.source_version if layer else None,
        feature_ids=feature_ids or [],
    )


def _geometry(value: dict[str, Any], *, label: str) -> BaseGeometry:
    try:
        geometry = shape(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid GeoJSON geometry: {exc}") from exc
    if geometry.is_empty:
        raise ValueError(f"{label} is empty")
    if not geometry.is_valid:
        raise ValueError(f"{label} is topologically invalid")
    return geometry


def _layer_spatial_payload(
    layer: Layer,
) -> tuple[BaseGeometry, dict[str, Any] | None, frozenset[str]]:
    if layer.kind == LayerKind.georaster:
        assert layer.raster is not None
        coverage = _geometry(layer.raster.footprint, label=f"{layer.layer_id} footprint")
        masks = layer.raster.masks
        categories = frozenset(
            category.casefold() for category in layer.raster.categories_checked
        )
        if not layer.raster.classification_complete:
            categories = frozenset()
        return coverage, masks, categories
    assert layer.coverage_geometry is not None
    coverage = _geometry(
        layer.coverage_geometry,
        label=f"{layer.layer_id} coverage_geometry",
    )
    return (
        coverage,
        layer.masks,
        frozenset(category.casefold() for category in layer.categories_checked),
    )


def _trust_assessment(request: ComparisonRequest, layer: Layer) -> tuple[str, list[Reason]]:
    provenance = layer.provenance
    review = layer.qa_review
    reasons: list[Reason] = []
    manual = False

    if layer.ambiguous or provenance.identity_status.casefold() != "matched":
        manual = True
        reasons.append(
            _reason(
                "AMBIGUOUS_LAYER",
                "Layer identity or jurisdiction is ambiguous.",
                layer,
            )
        )
    if not review.ambiguity_resolved:
        manual = True
        reasons.append(
            _reason(
                "QA_AMBIGUITY_UNRESOLVED",
                "The independent review did not resolve source ambiguity.",
                layer,
            )
        )
    if provenance.source_sha256.casefold() != review.source_sha256.casefold():
        manual = True
        reasons.append(
            _reason(
                "SOURCE_HASH_MISMATCH",
                "QA review refers to a different source checksum.",
                layer,
            )
        )

    accepted = {decision.casefold() for decision in request.policy.accepted_qa_decisions}
    qa_decision = review.decision.casefold()
    if qa_decision == "reject":
        reasons.append(_reason("QA_REJECTED", "The layer was rejected by QA.", layer))
    elif qa_decision not in accepted:
        reasons.append(
            _reason(
                "QA_NOT_ACCEPTED",
                "The layer does not have an accepted QA decision.",
                layer,
            )
        )
    if not review.independent_review:
        reasons.append(
            _reason(
                "QA_NOT_INDEPENDENT",
                "The layer has no independent second-person review.",
                layer,
            )
        )
    if provenance.status != ProvenanceStatus.verified_official:
        reasons.append(
            _reason(
                "PROVENANCE_NOT_VERIFIED",
                "The layer is not tied to a verified official source.",
                layer,
            )
        )

    stale = (
        not provenance.current
        or provenance.superseded_by is not None
        or (
            provenance.valid_until is not None
            and provenance.valid_until < request.as_of
        )
        or (
            review.expires_at is not None
            and review.expires_at < request.as_of
        )
        or request.as_of - provenance.checked_at
        > timedelta(days=request.policy.max_source_age_days)
    )
    if stale:
        reasons.append(
            _reason(
                "STALE_SOURCE_VERSION",
                "The source version or its verification is outdated.",
                layer,
            )
        )

    if manual:
        return "manual_review", reasons
    if reasons:
        return "ineligible", reasons
    return "eligible", reasons


def _assess_layer(request: ComparisonRequest, layer: Layer) -> LayerAssessment:
    coverage, masks, categories = _layer_spatial_payload(layer)
    eligibility, reasons = _trust_assessment(request, layer)
    return LayerAssessment(
        layer=layer,
        coverage=coverage,
        masks=masks,
        categories_checked=categories,
        eligibility=eligibility,
        reasons=tuple(reasons),
    )


def _source_evaluation(
    assessment: LayerAssessment,
    eligibility: str | None = None,
) -> SourceEvaluation:
    layer = assessment.layer
    return SourceEvaluation(
        layer_id=layer.layer_id,
        source_id=layer.provenance.source_id,
        source_version=layer.provenance.source_version,
        source_sha256=layer.provenance.source_sha256.casefold(),
        qa_decision=layer.qa_review.decision,
        eligibility=eligibility or assessment.eligibility,
    )


def _feature_id(feature: dict[str, Any], index: int) -> str:
    raw_id = feature.get("id")
    if raw_id is None:
        raw_id = feature.get("properties", {}).get("id")
    return str(raw_id) if raw_id is not None else f"feature-{index}"


def _intersecting_masks(
    candidate: BaseGeometry,
    assessment: LayerAssessment,
) -> tuple[list[Reason], list[Reason], list[str]]:
    if assessment.masks is None:
        return [], [], []
    if assessment.masks.get("type") != "FeatureCollection":
        return (
            [],
            [
                _reason(
                    "INVALID_MASK_COLLECTION",
                    "Mask payload is not a GeoJSON FeatureCollection.",
                    assessment.layer,
                )
            ],
            [],
        )

    blocking: list[Reason] = []
    manual: list[Reason] = []
    matched_ids: list[str] = []
    features = assessment.masks.get("features")
    if not isinstance(features, list):
        return (
            [],
            [
                _reason(
                    "INVALID_MASK_COLLECTION",
                    "Mask FeatureCollection has no feature list.",
                    assessment.layer,
                )
            ],
            [],
        )

    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            manual.append(
                _reason(
                    "INVALID_MASK_FEATURE",
                    "Mask collection contains a malformed feature.",
                    assessment.layer,
                )
            )
            continue
        feature_id = _feature_id(feature, index)
        try:
            geometry = _geometry(
                feature.get("geometry", {}),
                label=f"{assessment.layer.layer_id}/{feature_id}",
            )
        except ValueError:
            manual.append(
                _reason(
                    "INVALID_MASK_GEOMETRY",
                    "A mask has invalid geometry and requires review.",
                    assessment.layer,
                    [feature_id],
                )
            )
            continue
        if not geometry.intersects(candidate):
            continue

        matched_ids.append(feature_id)
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        category = str(properties.get("category", "")).casefold()
        effect = str(properties.get("effect", "")).casefold()
        if category in BLOCKING_CATEGORIES and effect in {"", "block"}:
            blocking.append(
                _reason(
                    f"INTERSECTS_{category.upper()}",
                    f"Candidate intersects a reviewed {category} mask.",
                    assessment.layer,
                    [feature_id],
                )
            )
        elif category == "zone" and effect == "block":
            blocking.append(
                _reason(
                    "INTERSECTS_BLOCKED_ZONE",
                    "Candidate intersects a reviewed zone marked as blocked.",
                    assessment.layer,
                    [feature_id],
                )
            )
        elif category == "zone" and effect == "allow":
            continue
        else:
            manual.append(
                _reason(
                    "MASK_REQUIRES_INTERPRETATION",
                    "An intersecting mask has an unknown category or non-final effect.",
                    assessment.layer,
                    [feature_id],
                )
            )
    return blocking, manual, matched_ids


def _result(
    request: ComparisonRequest,
    decision: Decision,
    reasons: list[Reason],
    assessments: list[LayerAssessment],
    matched_ids: list[str] | None = None,
    eligibility_overrides: dict[str, str] | None = None,
) -> ComparisonResult:
    overrides = eligibility_overrides or {}
    return ComparisonResult(
        candidate_id=request.candidate.candidate_id,
        decision=decision,
        as_of=request.as_of,
        reasons=reasons,
        source_versions=[
            _source_evaluation(item, overrides.get(item.layer.layer_id))
            for item in assessments
        ],
        matched_feature_ids=sorted(set(matched_ids or [])),
    )


def compare_candidate(request: ComparisonRequest) -> ComparisonResult:
    """Compare a candidate with reviewed layers without mutating any external state."""

    candidate = _geometry(request.candidate.geometry, label="candidate geometry")
    if candidate.geom_type not in SUPPORTED_CANDIDATE_TYPES:
        raise ValueError(
            "candidate geometry must be a Point, Polygon or MultiPolygon"
        )

    assessments = [_assess_layer(request, layer) for layer in request.layers]
    relevant = [item for item in assessments if item.coverage.intersects(candidate)]
    eligibility_overrides = {
        item.layer.layer_id: "outside_candidate_area"
        for item in assessments
        if item not in relevant
    }
    if not relevant:
        return _result(
            request,
            Decision.no_coverage,
            [_reason("NO_LAYER_COVERAGE", "No supplied layer intersects the candidate.")],
            assessments,
            eligibility_overrides=eligibility_overrides,
        )

    manual_sources = [item for item in relevant if item.eligibility == "manual_review"]
    if manual_sources:
        reasons = [reason for item in manual_sources for reason in item.reasons]
        return _result(
            request,
            Decision.manual_review,
            reasons,
            assessments,
            eligibility_overrides=eligibility_overrides,
        )

    eligible = [item for item in relevant if item.eligibility == "eligible"]
    if not eligible:
        reasons = [reason for item in relevant for reason in item.reasons]
        reasons.append(
            _reason(
                "NO_TRUSTED_COVERAGE",
                "No current, verified and independently approved layer covers the candidate.",
            )
        )
        return _result(
            request,
            Decision.no_coverage,
            reasons,
            assessments,
            eligibility_overrides=eligibility_overrides,
        )

    trusted_coverage = unary_union([item.coverage for item in eligible])
    if not trusted_coverage.covers(candidate):
        return _result(
            request,
            Decision.no_coverage,
            [
                _reason(
                    "PARTIAL_TRUSTED_COVERAGE",
                    "Trusted layers do not fully cover the candidate geometry.",
                )
            ],
            assessments,
            eligibility_overrides=eligibility_overrides,
        )

    missing_categories: list[str] = []
    for category in (item.casefold() for item in request.policy.required_categories):
        category_coverages = [
            item.coverage for item in eligible if category in item.categories_checked
        ]
        if not category_coverages or not unary_union(category_coverages).covers(candidate):
            missing_categories.append(category)
    if missing_categories:
        return _result(
            request,
            Decision.manual_review,
            [
                _reason(
                    "INCOMPLETE_CLASSIFICATION",
                    "Trusted coverage is missing complete checks for: "
                    + ", ".join(sorted(missing_categories)),
                )
            ],
            assessments,
            eligibility_overrides=eligibility_overrides,
        )

    blocking_reasons: list[Reason] = []
    manual_reasons: list[Reason] = []
    matched_ids: list[str] = []
    for assessment in eligible:
        blocking, manual, ids = _intersecting_masks(candidate, assessment)
        blocking_reasons.extend(blocking)
        manual_reasons.extend(manual)
        matched_ids.extend(ids)
    if manual_reasons:
        return _result(
            request,
            Decision.manual_review,
            manual_reasons,
            assessments,
            matched_ids,
            eligibility_overrides,
        )
    if blocking_reasons:
        return _result(
            request,
            Decision.blocked,
            blocking_reasons,
            assessments,
            matched_ids,
            eligibility_overrides,
        )
    return _result(
        request,
        Decision.match,
        [
            _reason(
                "TRUSTED_COVERAGE_CLEAR",
                "Candidate is fully covered and no reviewed blocking mask intersects it.",
            )
        ],
        assessments,
        matched_ids,
        eligibility_overrides,
    )
