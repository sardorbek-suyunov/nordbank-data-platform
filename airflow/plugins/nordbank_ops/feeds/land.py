"""Land one feed batch: bronze rows with their payload, quarantine rows with theirs.

The same writers specification 005 built, with one addition every API and file record carries:
`_raw_payload`, the record as received, structurally faithful, with every identifier field
replaced by its token (spec 006 section 4, ADR 0005). The parsed columns are a convenience and
the payload is the evidence, so a quarantined record keeps its payload as well: the quarantine
Parquet is the durable record of what was refused and why.

The feed module that parsed the delivery builds each payload, because only it knows the format
well enough to replace an identifier without disturbing anything else. This module refuses a
payload that still contains a cleartext identifier the record carried, rather than trusting
that every feed got that right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nordbank_ops.extract import (
    AUDIT_COLUMNS,
    QUARANTINE_COLUMNS,
    ExtractReport,
    decorate,
    quarantine_rows,
    write_parquet,
)
from nordbank_ops.registry import bronze_prefix, quarantine_prefix
from nordbank_ops.validation import Rejection, project, validate

PAYLOAD = "_raw_payload"


@dataclass
class Parsed:
    """What a feed module hands this one: typed records, refusals, and what it saw."""

    records: list[dict] = field(default_factory=list)
    payloads: list[str] = field(default_factory=list)
    refused: list[tuple[dict, Rejection, str]] = field(default_factory=list)
    identifiers_seen: list[str] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)
    breaking: str | None = None


class CleartextInPayloadError(RuntimeError):
    """A payload still carries an identifier the record held in the clear."""


def _guard(payloads: list[str], identifiers: list[str]) -> None:
    needles = [value for value in set(identifiers) if value and len(value) >= 4]
    for payload in payloads:
        for needle in needles:
            if needle in payload:
                raise CleartextInPayloadError(
                    "a payload carries a cleartext identifier; the feed's payload builder did "
                    "not replace it with its token"
                )


def land(
    *,
    client: Any,
    bucket: str,
    contract,
    batch: dict,
    parsed: Parsed,
    tokeniser,
    source_file: str,
) -> ExtractReport:
    report = ExtractReport(entity=contract.entity, batch_id=batch["batch_id"], status="written")
    report.drift = list(parsed.drift)
    identifiers = contract.identifier_columns()

    if parsed.breaking is not None:
        # The whole delivery is refused: every record goes to quarantine with the breaking
        # reason, nothing reaches bronze, and the batch fails. No partial load, at entity grain.
        refused = [
            (record, Rejection("*", f"breaking drift: {parsed.breaking}", None, None), payload)
            for record, payload in zip(parsed.records, parsed.payloads, strict=True)
        ] + parsed.refused
        report.status = "failed"
        report.failure_reason = f"breaking drift: {parsed.breaking}"
        records, payloads = [], []
    else:
        refused = list(parsed.refused)
        records, payloads = parsed.records, parsed.payloads

    _guard(payloads + [p for _r, _j, p in refused], parsed.identifiers_seen)

    result = validate(records, contract)
    by_id = {id(record): payload for record, payload in zip(records, payloads, strict=True)}
    for record, rejection in result.rejected:
        refused.append((record, rejection, by_id[id(record)]))

    landed = []
    for record in result.landed:
        row = project(record, contract.column_names)
        for column in identifiers:
            row[column] = tokeniser.token(row[column])
        row[PAYLOAD] = by_id[id(record)]
        landed.append(row)

    report.rows_read = len(parsed.records) + len(parsed.refused)
    report.rows_landed = len(landed)
    report.rows_quarantined = len(refused)
    if report.rows_landed + report.rows_quarantined != report.rows_read:
        raise RuntimeError(
            f"{contract.entity}: landed {report.rows_landed} plus quarantined "
            f"{report.rows_quarantined} is not the {report.rows_read} read"
        )

    decorate(
        landed,
        ingested_at=batch["opened_at"],
        source_file=source_file,
        batch_id=batch["batch_id"],
        system=contract.source_system,
    )
    columns = contract.column_names + (PAYLOAD,) + AUDIT_COLUMNS
    key = (
        bronze_prefix(
            contract.source_system, contract.entity, batch["ingest_date"], batch["batch_id"]
        )
        + "part-0000.parquet"
    )
    if report.status != "failed":
        written = write_parquet(client, bucket, key, landed, columns)
        if written:
            report.bronze_keys.append(written)

    rows = quarantine_rows(
        [(record, rejection) for record, rejection, _p in refused],
        tokeniser,
        identifiers,
        batch_id=batch["batch_id"],
        system=contract.source_system,
        entity=contract.entity,
        quarantined_at=batch["opened_at"],
    )
    for row, (_record, _rejection, payload) in zip(rows, refused, strict=True):
        row[PAYLOAD] = payload
    qkey = (
        quarantine_prefix(
            contract.source_system, contract.entity, batch["ingest_date"], batch["batch_id"]
        )
        + "part-0000.parquet"
    )
    written = write_parquet(client, bucket, qkey, rows, QUARANTINE_COLUMNS + (PAYLOAD,))
    if written:
        report.quarantine_keys.append(written)
    return report


def failed(contract, batch: dict, reason: str) -> ExtractReport:
    """A report for a batch that could not be fetched or read at all."""
    report = ExtractReport(entity=contract.entity, batch_id=batch["batch_id"], status="failed")
    report.failure_reason = reason
    return report
