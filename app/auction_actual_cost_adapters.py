"""Network-free structured adapters for authoritative actual-cost source records.

The registry and receipt mapping are trusted worker/config inputs, never values
constructed from the provider payload itself. A payload receipt is accepted only
when it exactly matches that separately supplied trusted receipt.
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlsplit

from app.auction_actual_cost_writer import (
    BASIS_BY_COST,
    SCENARIO_HORIZON_MONTHS,
    STANDARD_INVESTMENT_POLICY_VERSION,
    ActualCostFact,
    canonical_source_identity,
)
from app.auction_price_ceiling import MAX_KZT

SCHEMA_VERSION = "actual-cost-structured-source/2026.1"
MAX_INPUT_BYTES = 256_000
MAX_RECORDS = 64
MAX_ERRORS = 32
MAX_CELL_CHARS = 2_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASCII_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,127}$")

ADAPTER_CONTRACTS = {
    "official_fees": {
        ("registration", "official_fee"),
        ("registration", "official_tariff"),
        ("tax_annual", "official_tax"),
        ("tax_annual", "official_tariff"),
    },
    "utility": {
        ("connection", "utility_quote"),
        ("connection", "connection_estimate"),
    },
    "financing": {("financing", "financing_quote")},
    "due_diligence": {("due_diligence", "contractor_quote")},
    "development": {
        ("development", "contractor_quote"),
        ("contingency", "cost_plan"),
    },
    "risk": {("risk_reserve", "risk_assessment")},
}

RECORD_FIELDS = (
    "schema_version",
    "adapter_kind",
    "record_id",
    "provider_id",
    "provider_authority_or_license",
    "document_sha256",
    "source_url",
    "applicability_kind",
    "target_lot_id",
    "scenario_key",
    "investment_policy_version",
    "holding_horizon_months",
    "cost_key",
    "source_kind",
    "status",
    "low_kzt",
    "base_kzt",
    "high_kzt",
    "currency",
    "basis",
    "observed_at",
    "issued_at",
    "expires_at",
    "confidence",
    "ingestion_receipt_sha256",
)


class ActualCostAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterIssue:
    position: int
    code: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class AdapterResult:
    facts: tuple[ActualCostFact, ...]
    issues: tuple[AdapterIssue, ...]
    truncated: bool
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class TrustedProvider:
    provider_id: str
    registry_version: str
    allowed_adapter_kinds: tuple[str, ...]
    allowed_source_kinds: tuple[str, ...]
    allowed_https_hosts: tuple[str, ...]
    authority_or_license_sha256: str
    allowed_provenance_kinds: tuple[str, ...] = ("signed_feed", "internal_fetch")


@dataclass(frozen=True, slots=True)
class TrustedReceipt:
    provider_id: str
    record_id: str
    receipt_sha256: str
    canonical_record_sha256: str
    provenance_kind: str


def canonical_record_sha256(record: Mapping[str, object]) -> str:
    if not isinstance(record, Mapping):
        raise ActualCostAdapterError("record is not canonicalizable")
    material = {
        key: value for key, value in record.items() if key != "ingestion_receipt_sha256"
    }
    try:
        rendered = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ActualCostAdapterError("record is not canonicalizable") from exc
    if len(rendered.encode("utf-8")) > MAX_CELL_CHARS * len(RECORD_FIELDS):
        raise ActualCostAdapterError("canonical record exceeds bound")
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def canonical_authority_hash(value: str) -> str:
    normalized = _bounded_text(value)
    if normalized is None:
        raise ActualCostAdapterError("authority or license is invalid")
    return hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()


def trusted_provider_registry(
    providers: tuple[TrustedProvider, ...] | list[TrustedProvider],
) -> Mapping[str, TrustedProvider]:
    if not isinstance(providers, (tuple, list)) or not 1 <= len(providers) <= 32:
        raise ActualCostAdapterError("trusted provider registry exceeds bound")
    result: dict[str, TrustedProvider] = {}
    for provider in providers:
        if (
            not isinstance(provider, TrustedProvider)
            or _bounded_text(provider.provider_id, ascii_id=True) is None
            or _bounded_text(provider.registry_version, ascii_id=True) is None
            or len(provider.registry_version) > 32
            or not _SHA256.fullmatch(provider.authority_or_license_sha256)
            or not provider.allowed_adapter_kinds
            or not provider.allowed_source_kinds
            or not provider.allowed_https_hosts
            or provider.provider_id in result
        ):
            raise ActualCostAdapterError("trusted provider registry is invalid")
        if any(kind not in ADAPTER_CONTRACTS for kind in provider.allowed_adapter_kinds):
            raise ActualCostAdapterError("trusted provider adapter kind is invalid")
        if any(
            source_kind
            not in {
                configured_source
                for contract in ADAPTER_CONTRACTS.values()
                for _, configured_source in contract
            }
            for source_kind in provider.allowed_source_kinds
        ):
            raise ActualCostAdapterError("trusted provider source kind is invalid")
        hosts = tuple(host.rstrip(".").casefold() for host in provider.allowed_https_hosts)
        if any(not _trusted_registry_host(host) for host in hosts):
            raise ActualCostAdapterError("trusted provider host is invalid")
        result[provider.provider_id] = replace_provider_hosts(provider, hosts)
    return MappingProxyType(result)


def replace_provider_hosts(
    provider: TrustedProvider,
    hosts: tuple[str, ...],
) -> TrustedProvider:
    return TrustedProvider(
        provider.provider_id,
        provider.registry_version,
        provider.allowed_adapter_kinds,
        provider.allowed_source_kinds,
        hosts,
        provider.authority_or_license_sha256,
        provider.allowed_provenance_kinds,
    )


def _trusted_registry_host(host: str) -> bool:
    if not host or "." not in host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _fingerprint(value: object) -> str:
    material = repr(value)[:4_000].encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()


def _bounded_text(value: object, *, ascii_id: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not 1 <= len(normalized) <= (128 if ascii_id else 300):
        return None
    if ascii_id and not _ASCII_ID.fullmatch(normalized):
        return None
    return normalized


def _safe_authoritative_url(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 1_000:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        return None
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return None
    if "." not in hostname:
        return None
    return value


def _url_host(value: str) -> str:
    return (urlsplit(value).hostname or "").rstrip(".").casefold()


def _aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _integer(value: object, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r"0|[1-9][0-9]*", value):
            return None
        value = int(value)
    if not isinstance(value, int) or not minimum <= value <= maximum:
        return None
    return value


def _confidence(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(normalized) or not 0.5 <= normalized <= 1:
        return None
    return normalized


def _record_fact(
    record: object,
    *,
    expected_lot_id: str,
    expected_scenario_key: str,
    registry: Mapping[str, TrustedProvider],
    receipts: Mapping[str, TrustedReceipt],
) -> tuple[ActualCostFact | None, str | None]:
    if not isinstance(record, dict) or set(record) != set(RECORD_FIELDS):
        return None, "schema_fields_mismatch"
    if record.get("schema_version") != SCHEMA_VERSION:
        return None, "schema_version_mismatch"
    adapter_kind = record.get("adapter_kind")
    cost_key = record.get("cost_key")
    source_kind = record.get("source_kind")
    if (
        adapter_kind not in ADAPTER_CONTRACTS
        or (cost_key, source_kind) not in ADAPTER_CONTRACTS[adapter_kind]
    ):
        return None, "adapter_contract_mismatch"
    if (
        record.get("applicability_kind") != "exact_lot"
        or record.get("target_lot_id") != expected_lot_id
    ):
        return None, "lot_applicability_mismatch"
    horizon = SCENARIO_HORIZON_MONTHS.get(expected_scenario_key)
    if (
        horizon is None
        or record.get("scenario_key") != expected_scenario_key
        or record.get("investment_policy_version")
        != STANDARD_INVESTMENT_POLICY_VERSION
        or _integer(record.get("holding_horizon_months"), minimum=1, maximum=360)
        != horizon
    ):
        return None, "scenario_policy_mismatch"
    if record.get("currency") != "KZT" or record.get("basis") != BASIS_BY_COST[cost_key]:
        return None, "currency_or_basis_mismatch"
    status = record.get("status")
    if status not in {"found", "unknown", "conflict"}:
        return None, "invalid_status"
    raw_amounts = (record.get("low_kzt"), record.get("base_kzt"), record.get("high_kzt"))
    if status == "unknown":
        return None, "explicit_unknown_amount"
    if status == "conflict" and all(item in {None, ""} for item in raw_amounts):
        return None, "conflict_amount_unknown"
    low = _integer(raw_amounts[0], minimum=0, maximum=MAX_KZT)
    base = _integer(raw_amounts[1], minimum=0, maximum=MAX_KZT)
    high = _integer(raw_amounts[2], minimum=0, maximum=MAX_KZT)
    if low is None or base is None or high is None or not low <= base <= high:
        return None, "invalid_money_range"
    record_id = _bounded_text(record.get("record_id"), ascii_id=True)
    provider_id = _bounded_text(record.get("provider_id"), ascii_id=True)
    authority = _bounded_text(record.get("provider_authority_or_license"))
    document_hash = record.get("document_sha256")
    source_url = _safe_authoritative_url(record.get("source_url"))
    receipt_hash = record.get("ingestion_receipt_sha256")
    if (
        record_id is None
        or provider_id is None
        or authority is None
        or not isinstance(document_hash, str)
        or not _SHA256.fullmatch(document_hash)
        or source_url is None
        or not isinstance(receipt_hash, str)
        or not _SHA256.fullmatch(receipt_hash)
    ):
        return None, "invalid_source_provenance"
    if len(provider_id) > 48:
        return None, "provider_id_exceeds_bound"
    provider = registry.get(provider_id)
    receipt_key = f"{provider_id}:{record_id}"
    receipt = receipts.get(receipt_key)
    try:
        record_hash = canonical_record_sha256(record)
    except ActualCostAdapterError:
        return None, "canonical_record_invalid"
    if provider is None:
        return None, "untrusted_provider"
    if (
        adapter_kind not in provider.allowed_adapter_kinds
        or source_kind not in provider.allowed_source_kinds
        or _url_host(source_url) not in provider.allowed_https_hosts
        or canonical_authority_hash(authority)
        != provider.authority_or_license_sha256
    ):
        return None, "provider_registry_mismatch"
    if (
        receipt is None
        or receipt.provider_id != provider_id
        or receipt.record_id != record_id
        or receipt.receipt_sha256 != receipt_hash
        or receipt.canonical_record_sha256 != record_hash
        or receipt.provenance_kind not in provider.allowed_provenance_kinds
    ):
        return None, "trusted_receipt_mismatch"
    observed = _aware_timestamp(record.get("observed_at"))
    issued = _aware_timestamp(record.get("issued_at"))
    raw_expires = record.get("expires_at")
    expires = None if raw_expires in {None, ""} else _aware_timestamp(raw_expires)
    confidence = _confidence(record.get("confidence"))
    if observed is None or issued is None or confidence is None:
        return None, "invalid_validity_or_confidence"
    no_expiry_allowed = source_kind in {"official_fee", "official_tax", "official_tariff"}
    if expires is None and not no_expiry_allowed:
        return None, "expiry_required_for_quote"
    if issued > observed or (expires is not None and expires < issued):
        return None, "invalid_validity_window"
    horizon_months = horizon if cost_key == "financing" else None
    source_identity = canonical_source_identity(source_kind, provider_id, record_id)
    return (
        ActualCostFact(
            target_lot_id=expected_lot_id,
            scenario_key=expected_scenario_key,
            investment_policy_version=STANDARD_INVESTMENT_POLICY_VERSION,
            holding_horizon_months=horizon,
            cost_key=cost_key,
            low_kzt=low,
            base_kzt=base,
            high_kzt=high,
            status=status,
            source_kind=source_kind,
            source_identity=source_identity,
            source_ref=(
                f"source_record:{provider_id}:{provider.registry_version}:"
                f"{receipt.receipt_sha256}"
            ),
            source_url=source_url,
            observed_at=observed,
            issued_at=issued,
            expires_at=expires,
            confidence=confidence,
            source_version=(
                f"registry:{provider.registry_version}:{document_hash}"
            ),
            currency="KZT",
            basis=BASIS_BY_COST[cost_key],
            horizon_months=horizon_months,
            generation_id=document_hash,
        ),
        None,
    )


def adapt_structured_records(
    records: object,
    *,
    expected_lot_id: str,
    expected_scenario_key: str,
    registry: Mapping[str, TrustedProvider],
    trusted_receipts: Mapping[str, TrustedReceipt],
) -> AdapterResult:
    if not isinstance(expected_lot_id, str) or not 1 <= len(expected_lot_id) <= 64:
        raise ActualCostAdapterError("expected lot id is invalid")
    if expected_scenario_key not in SCENARIO_HORIZON_MONTHS:
        raise ActualCostAdapterError("expected scenario key is invalid")
    if not isinstance(registry, Mapping) or not 1 <= len(registry) <= 32:
        raise ActualCostAdapterError("trusted provider registry is invalid")
    if not isinstance(trusted_receipts, Mapping) or len(trusted_receipts) > MAX_RECORDS:
        raise ActualCostAdapterError("trusted receipt collection exceeds bound")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ActualCostAdapterError("record collection exceeds bound")
    facts: list[ActualCostFact] = []
    issues: list[AdapterIssue] = []
    issue_total = 0
    for position, record in enumerate(records):
        fact, code = _record_fact(
            record,
            expected_lot_id=expected_lot_id,
            expected_scenario_key=expected_scenario_key,
            registry=registry,
            receipts=trusted_receipts,
        )
        if fact is not None:
            facts.append(fact)
            continue
        issue_total += 1
        if len(issues) < MAX_ERRORS:
            issues.append(AdapterIssue(position, code or "invalid_record", _fingerprint(record)))
    facts.sort(key=lambda item: (item.cost_key, item.source_identity, item.source_ref))
    return AdapterResult(tuple(facts), tuple(issues), issue_total > len(issues))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ActualCostAdapterError(f"duplicate JSON key: {key[:80]}")
        result[key] = value
    return result


def parse_actual_cost_json(
    payload: bytes | str,
    *,
    expected_lot_id: str,
    expected_scenario_key: str,
    registry: Mapping[str, TrustedProvider],
    trusted_receipts: Mapping[str, TrustedReceipt],
) -> AdapterResult:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
        raise ActualCostAdapterError("JSON payload exceeds bound")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ActualCostAdapterError("invalid JSON payload") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "records"}:
        raise ActualCostAdapterError("invalid JSON envelope")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ActualCostAdapterError("JSON schema version mismatch")
    return adapt_structured_records(
        value.get("records"),
        expected_lot_id=expected_lot_id,
        expected_scenario_key=expected_scenario_key,
        registry=registry,
        trusted_receipts=trusted_receipts,
    )


def parse_actual_cost_csv(
    payload: bytes | str,
    *,
    expected_lot_id: str,
    expected_scenario_key: str,
    registry: Mapping[str, TrustedProvider],
    trusted_receipts: Mapping[str, TrustedReceipt],
) -> AdapterResult:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
        raise ActualCostAdapterError("CSV payload exceeds bound")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ActualCostAdapterError("CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(RECORD_FIELDS):
        raise ActualCostAdapterError("CSV header mismatch")
    records: list[dict[str, object]] = []
    for row in reader:
        if len(records) >= MAX_RECORDS:
            raise ActualCostAdapterError("CSV record count exceeds bound")
        if None in row or any(
            value is None or len(value) > MAX_CELL_CHARS for value in row.values()
        ):
            raise ActualCostAdapterError("CSV row is malformed")
        records.append(dict(row))
    return adapt_structured_records(
        records,
        expected_lot_id=expected_lot_id,
        expected_scenario_key=expected_scenario_key,
        registry=registry,
        trusted_receipts=trusted_receipts,
    )
