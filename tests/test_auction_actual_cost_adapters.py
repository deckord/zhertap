from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

import pytest

from app.auction_actual_cost_adapters import (
    RECORD_FIELDS,
    SCHEMA_VERSION,
    ActualCostAdapterError,
    TrustedProvider,
    TrustedReceipt,
    adapt_structured_records,
    canonical_authority_hash,
    canonical_record_sha256,
    parse_actual_cost_csv,
    parse_actual_cost_json,
    trusted_provider_registry,
)
from app.auction_actual_cost_writer import (
    STANDARD_INVESTMENT_POLICY_VERSION,
    produce_authoritative_actual_costs,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
AUTHORITY = "Лицензия или официальный орган №17"


def _record(
    cost_key: str,
    source_kind: str,
    adapter_kind: str,
    *,
    value: int = 100_000,
    record_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_kind": adapter_kind,
        "record_id": record_id or f"record-{cost_key}",
        "provider_id": f"provider-{adapter_kind}",
        "provider_authority_or_license": AUTHORITY,
        "document_sha256": (cost_key.encode().hex() + "a" * 64)[:64],
        "source_url": f"https://costs.example.kz/{adapter_kind}/{cost_key}",
        "applicability_kind": "exact_lot",
        "target_lot_id": "lot-452662",
        "scenario_key": "camping",
        "investment_policy_version": STANDARD_INVESTMENT_POLICY_VERSION,
        "holding_horizon_months": 60,
        "cost_key": cost_key,
        "source_kind": source_kind,
        "status": "found",
        "low_kzt": value,
        "base_kzt": value,
        "high_kzt": value,
        "currency": "KZT",
        "basis": {
            "tax_annual": "annual",
            "financing": "financing_horizon",
            "contingency": "one_time_reserve",
            "risk_reserve": "one_time_reserve",
        }.get(cost_key, "one_time"),
        "observed_at": "2026-08-17T12:00:00+00:00",
        "issued_at": "2026-08-16T12:00:00+00:00",
        "expires_at": "2026-09-17T12:00:00+00:00",
        "confidence": 0.9,
        "ingestion_receipt_sha256": "b" * 64,
    }


REGISTRY = trusted_provider_registry(
    [
        TrustedProvider(
            provider_id=f"provider-{adapter}",
            registry_version="registry-2026.1",
            allowed_adapter_kinds=(adapter,),
            allowed_source_kinds=tuple(
                sorted(
                    {
                        source_kind
                        for candidate_adapter, source_kind in (
                            ("official_fees", "official_fee"),
                            ("official_fees", "official_tax"),
                            ("official_fees", "official_tariff"),
                            ("utility", "connection_estimate"),
                            ("utility", "utility_quote"),
                            ("financing", "financing_quote"),
                            ("due_diligence", "contractor_quote"),
                            ("development", "contractor_quote"),
                            ("development", "cost_plan"),
                            ("risk", "risk_assessment"),
                        )
                        if candidate_adapter == adapter
                    }
                )
            ),
            allowed_https_hosts=("costs.example.kz",),
            authority_or_license_sha256=canonical_authority_hash(AUTHORITY),
        )
        for adapter in (
            "official_fees",
            "utility",
            "financing",
            "due_diligence",
            "development",
            "risk",
        )
    ]
)


def _receipts(records: list[dict[str, object]]) -> dict[str, TrustedReceipt]:
    result = {}
    for record in records:
        provider_id = str(record["provider_id"])
        record_id = str(record["record_id"])
        result[f"{provider_id}:{record_id}"] = TrustedReceipt(
            provider_id,
            record_id,
            str(record["ingestion_receipt_sha256"]),
            canonical_record_sha256(record),
            "internal_fetch",
        )
    return result


def _all_source_records() -> list[dict[str, object]]:
    return [
        _record("registration", "official_fee", "official_fees"),
        _record("tax_annual", "official_tax", "official_fees"),
        _record("connection", "connection_estimate", "utility"),
        _record("financing", "financing_quote", "financing"),
        _record("due_diligence", "contractor_quote", "due_diligence"),
        _record("development", "contractor_quote", "development"),
        _record("contingency", "cost_plan", "development"),
        _record("risk_reserve", "risk_assessment", "risk"),
    ]


def test_all_provider_neutral_contracts_emit_authoritative_facts_without_defaults() -> None:
    records = _all_source_records()
    result = adapt_structured_records(
        records,
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts(records),
    )
    assert result.issues == ()
    assert len(result.facts) == 8
    assert {fact.cost_key for fact in result.facts} == {
        "registration",
        "tax_annual",
        "connection",
        "financing",
        "due_diligence",
        "development",
        "contingency",
        "risk_reserve",
    }
    assert all(fact.currency == "KZT" for fact in result.facts)
    assert all(fact.target_lot_id == "lot-452662" for fact in result.facts)
    assert all(len(fact.source_identity) == 64 for fact in result.facts)
    assert all("provider-" in fact.source_ref for fact in result.facts)
    assert all(str(fact.generation_id) in fact.source_version for fact in result.facts)
    financing = next(fact for fact in result.facts if fact.cost_key == "financing")
    assert financing.horizon_months == 60


def test_json_and_csv_adapters_are_equivalent_and_deterministic() -> None:
    records = _all_source_records()[:2]
    json_result = parse_actual_cost_json(
        json.dumps({"schema_version": SCHEMA_VERSION, "records": records}),
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts(records),
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=RECORD_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)
    csv_record_views = [
        {key: "" if value is None else str(value) for key, value in record.items()}
        for record in records
    ]
    csv_result = parse_actual_cost_csv(
        output.getvalue(),
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts(csv_record_views),
    )
    assert json_result.facts == csv_result.facts
    assert json_result.issues == csv_result.issues == ()


def test_foreign_currency_wrong_lot_scenario_and_adapter_are_quarantined() -> None:
    foreign = _record("connection", "connection_estimate", "utility")
    foreign["currency"] = "USD"
    wrong_lot = _record("registration", "official_fee", "official_fees")
    wrong_lot["target_lot_id"] = "lot-other"
    wrong_scenario = _record("risk_reserve", "risk_assessment", "risk")
    wrong_scenario["scenario_key"] = "development"
    wrong_contract = _record("financing", "utility_quote", "utility")
    result = adapt_structured_records(
        [foreign, wrong_lot, wrong_scenario, wrong_contract],
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts([foreign, wrong_lot, wrong_scenario, wrong_contract]),
    )
    assert result.facts == ()
    assert [issue.code for issue in result.issues] == [
        "currency_or_basis_mismatch",
        "lot_applicability_mismatch",
        "scenario_policy_mismatch",
        "adapter_contract_mismatch",
    ]


def test_private_or_credentialed_urls_and_bad_document_hash_are_rejected_ssrf_free() -> None:
    private_ip = _record("connection", "connection_estimate", "utility")
    private_ip["source_url"] = "https://127.0.0.1/quote"
    credentials = _record("development", "contractor_quote", "development")
    credentials["source_url"] = "https://user:pass@costs.example.kz/quote"
    bad_hash = _record("risk_reserve", "risk_assessment", "risk")
    bad_hash["document_sha256"] = "not-a-hash"
    result = adapt_structured_records(
        [private_ip, credentials, bad_hash],
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts([private_ip, credentials, bad_hash]),
    )
    assert result.facts == ()
    assert {issue.code for issue in result.issues} == {"invalid_source_provenance"}


def test_conflict_unknown_and_expiry_are_preserved_for_authoritative_writer() -> None:
    conflict = _record("connection", "connection_estimate", "utility")
    conflict["status"] = "conflict"
    unknown = _record("registration", "official_fee", "official_fees")
    unknown["status"] = "unknown"
    unknown["low_kzt"] = unknown["base_kzt"] = unknown["high_kzt"] = None
    expired = _record("risk_reserve", "risk_assessment", "risk")
    expired["issued_at"] = "2026-07-01T00:00:00+00:00"
    expired["expires_at"] = "2026-08-01T00:00:00+00:00"
    result = adapt_structured_records(
        [conflict, unknown, expired],
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts([conflict, unknown, expired]),
    )
    assert [fact.status for fact in result.facts] == ["conflict", "found"]
    assert result.issues[0].code == "explicit_unknown_amount"
    assert result.facts[-1].expires_at < NOW


def test_official_tariff_can_have_no_expiry_but_quotes_cannot() -> None:
    official = _record("registration", "official_tariff", "official_fees")
    official["expires_at"] = None
    quote = _record("connection", "connection_estimate", "utility")
    quote["expires_at"] = None
    result = adapt_structured_records(
        [official, quote],
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts([official, quote]),
    )
    assert [fact.cost_key for fact in result.facts] == ["registration"]
    assert result.facts[0].expires_at is None
    assert result.issues[0].code == "expiry_required_for_quote"


def test_registry_blocks_forged_provider_domain_authority_and_receipt() -> None:
    forged_provider = _record("connection", "connection_estimate", "utility")
    forged_provider["provider_id"] = "attacker"
    forged_domain = _record("connection", "connection_estimate", "utility", record_id="domain")
    forged_domain["source_url"] = "https://attacker.example/quote"
    forged_authority = _record(
        "connection", "connection_estimate", "utility", record_id="authority"
    )
    forged_authority["provider_authority_or_license"] = "Самоназначенная лицензия"
    forged_receipt = _record(
        "connection", "connection_estimate", "utility", record_id="receipt"
    )
    receipts = _receipts([forged_provider, forged_domain, forged_authority, forged_receipt])
    receipts["provider-utility:receipt"] = TrustedReceipt(
        "provider-utility",
        "receipt",
        "c" * 64,
        canonical_record_sha256(forged_receipt),
        "internal_fetch",
    )
    result = adapt_structured_records(
        [forged_provider, forged_domain, forged_authority, forged_receipt],
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=receipts,
    )
    assert result.facts == ()
    assert [issue.code for issue in result.issues] == [
        "untrusted_provider",
        "provider_registry_mismatch",
        "provider_registry_mismatch",
        "trusted_receipt_mismatch",
    ]


def test_trusted_receipt_cannot_be_replayed_after_amount_or_document_mutation() -> None:
    original = _record("connection", "connection_estimate", "utility")
    trusted = _receipts([original])
    amount_mutated = dict(original)
    amount_mutated["low_kzt"] = amount_mutated["base_kzt"] = amount_mutated[
        "high_kzt"
    ] = 9_999_999
    document_mutated = dict(original)
    document_mutated["document_sha256"] = "c" * 64
    for mutated in (amount_mutated, document_mutated):
        result = adapt_structured_records(
            [mutated],
            expected_lot_id="lot-452662",
            expected_scenario_key="camping",
            registry=REGISTRY,
            trusted_receipts=trusted,
        )
        assert result.facts == ()
        assert result.issues[0].code == "trusted_receipt_mismatch"


def test_duplicate_json_keys_are_rejected_at_any_depth() -> None:
    duplicate = (
        '{"schema_version":"actual-cost-structured-source/2026.1",'
        '"records":[{"schema_version":"a","schema_version":"b"}]}'
    )
    with pytest.raises(ActualCostAdapterError, match="duplicate JSON key"):
        parse_actual_cost_json(
            duplicate,
            expected_lot_id="lot-452662",
            expected_scenario_key="camping",
            registry=REGISTRY,
            trusted_receipts={},
        )


def test_future_timestamp_passes_structure_but_is_rejected_by_authoritative_writer() -> None:
    future = _record("connection", "connection_estimate", "utility")
    future["observed_at"] = "2027-08-17T12:00:00+00:00"
    future["issued_at"] = "2027-08-16T12:00:00+00:00"
    future["expires_at"] = "2027-09-17T12:00:00+00:00"
    adapted = adapt_structured_records(
        [future],
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts([future]),
    )
    assert len(adapted.facts) == 1
    production = produce_authoritative_actual_costs(
        list(adapted.facts),
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert production.result.status == "insufficient_data"
    assert production.quarantined[0].reason == "timestamp_in_future"


def test_452662_partial_real_sources_still_leave_null_cost_completeness() -> None:
    records = _all_source_records()[:3]
    adapted = adapt_structured_records(
        records,
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts(records),
    )
    production = produce_authoritative_actual_costs(
        list(adapted.facts),
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert production.result.status == "incomplete"
    assert set(production.result.missing_keys) == {
        "financing",
        "due_diligence",
        "development",
        "contingency",
        "risk_reserve",
    }


def test_strict_envelopes_bounds_and_unknown_columns_fail_safely() -> None:
    with pytest.raises(ActualCostAdapterError):
        parse_actual_cost_json(
            "{}",
            expected_lot_id="lot-452662",
            expected_scenario_key="camping",
            registry=REGISTRY,
            trusted_receipts={},
        )
    with pytest.raises(ActualCostAdapterError):
        parse_actual_cost_json(
            b"x" * 256_001,
            expected_lot_id="lot-452662",
            expected_scenario_key="camping",
            registry=REGISTRY,
            trusted_receipts={},
        )
    extra = _record("connection", "connection_estimate", "utility")
    extra["invented_amount"] = 123
    result = adapt_structured_records(
        [extra],
        expected_lot_id="lot-452662",
        expected_scenario_key="camping",
        registry=REGISTRY,
        trusted_receipts=_receipts([extra]),
    )
    assert result.facts == ()
    assert result.issues[0].code == "schema_fields_mismatch"
