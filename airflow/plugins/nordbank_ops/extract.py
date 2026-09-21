"""Read a watermark window from the source and write it to the lake (spec 005 sections 3, 5).

This is the phase that must not hold the `warehouse_access` pool: it reads Postgres in bulk and
writes Parquet, neither of which touches DuckDB. Everything it learns is handed back as counts
and timestamps, never as data — in particular never as cleartext, which is why the vault is
written by the register step from its own bounded re-read rather than from anything this phase
carries (spec 005 section 3).

Three properties of the write are decisions rather than mechanics:

- **The read is ordered by the primary key.** Postgres guarantees no row order without one, so
  an unordered read would produce different bytes on a retry for a reason that is not a defect,
  and criterion 5 asks for byte-identical objects.
- **`_ingested_at` is the batch's `opened_at`**, read back from the registry, so a retry
  reproduces it. See `registry.py`.
- **Parquet is written with PyArrow directly**, not through DuckDB, which would pull the pool
  into this phase for no benefit.
"""

from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Any

from .schema_drift import DriftReport, classify
from .tokenise import Tokeniser
from .validation import Rejection, project, validate

AUDIT_COLUMNS: tuple[str, ...] = ("_ingested_at", "_source_file", "_batch_id", "_source_system")


# The extract task hands these back to the register step. No data, no cleartext: counts, the
# window it actually read, and what it saw of the source's shape.
@dataclass
class ExtractReport:
    entity: str
    batch_id: str
    status: str
    rows_read: int = 0
    rows_landed: int = 0
    rows_quarantined: int = 0
    watermark_from: dt.datetime | None = None
    watermark_to: dt.datetime | None = None
    bronze_keys: list[str] = field(default_factory=list)
    quarantine_keys: list[str] = field(default_factory=list)
    # Landed rows counted by the source day their own watermark falls on, which is what
    # `ops.source_reconciliation` records. Computed here, where the rows are already in
    # memory, rather than by reading the Parquet back in the register step: the re-read cost
    # the register transaction its memory limit at the initial load and bought nothing, since
    # this phase has the same rows and has already paid for them.
    landed_by_source_date: dict[str, int] = field(default_factory=dict)
    drift: list[dict] = field(default_factory=list)
    failure_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "entity": self.entity,
            "batch_id": self.batch_id,
            "status": self.status,
            "rows_read": self.rows_read,
            "rows_landed": self.rows_landed,
            "rows_quarantined": self.rows_quarantined,
            "watermark_from": self.watermark_from.isoformat() if self.watermark_from else None,
            "watermark_to": self.watermark_to.isoformat() if self.watermark_to else None,
            "bronze_keys": self.bronze_keys,
            "quarantine_keys": self.quarantine_keys,
            "landed_by_source_date": self.landed_by_source_date,
            "drift": self.drift,
            "failure_reason": self.failure_reason,
        }


class BreakingDriftError(RuntimeError):
    """The source's shape changed in a way the contract cannot accept.

    Raised after the batch has been marked `failed`, so the task fails and the watermark stays
    where it was. Only this entity is affected: the register step runs on `all_done` and the
    entities that wrote cleanly in the same run are still registered.
    """


# --- reading the source ------------------------------------------------------------------


def live_shape(cursor: Any, schema: str, table: str) -> tuple[dict[str, str], str | None]:
    """The source's current column types and primary key, as `information_schema` spells them.

    `format_type` is what the data dictionary and therefore the contract are written against,
    so the two are directly comparable with no alias map in between.
    """
    cursor.execute(
        """
        select a.attname, format_type(a.atttypid, a.atttypmod)
          from pg_attribute a
          join pg_class c on c.oid = a.attrelid
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = %s and c.relname = %s and c.relkind = 'r'
           and a.attnum > 0 and not a.attisdropped
         order by a.attnum
        """,
        (schema, table),
    )
    types = {name: data_type for name, data_type in cursor.fetchall()}

    cursor.execute(
        """
        select a.attname
          from pg_index i
          join pg_class c on c.oid = i.indrelid
          join pg_namespace n on n.oid = c.relnamespace
          join pg_attribute a on a.attrelid = c.oid and a.attnum = any(i.indkey)
         where n.nspname = %s and c.relname = %s and i.indisprimary
        """,
        (schema, table),
    )
    keys = [row[0] for row in cursor.fetchall()]
    return types, keys[0] if len(keys) == 1 else None


def identifier_columns(cursor: Any, schema: str, table: str) -> tuple[str, ...]:
    """Which columns to tokenise, read from `platform.column_classifications`.

    Read from the live classification table and not from the contract, although the contract
    carries a classification too. The contract deliberately lags the source, and lagging on
    *what to protect* is the one lag that must not happen: a column newly classified as an
    identifier is tokenised on the next run rather than on the next contract bump.
    """
    cursor.execute(
        """
        select column_name from platform.column_classifications
         where schema_name = %s and table_name = %s and classification = 'identifier'
         order by column_name
        """,
        (schema, table),
    )
    return tuple(row[0] for row in cursor.fetchall())


def read_window(
    cursor: Any, contract, watermark_from: dt.datetime | None
) -> tuple[list[dict], dt.datetime | None]:
    """Every row whose watermark is at or after the window's lower bound, ordered by key.

    `>=` with an overlap rather than `>`, for the two reasons `architecture.md` gives: several
    rows can share the instant that became the watermark, and a row written in a transaction
    that started before the watermark was taken commits after it with an older stamp. A strict
    comparison loses both, permanently.
    """
    columns = ", ".join(f'"{name}"' for name in contract.column_names)
    relation = f'"{contract.source_schema}"."{contract.entity}"'
    where = "" if watermark_from is None else f' where "{contract.watermark_column}" >= %s'
    order = f' order by "{contract.primary_key}"'
    sql = f"select {columns} from {relation}{where}{order}"  # noqa: S608 - identifiers are contracted

    cursor.execute(sql, () if watermark_from is None else (watermark_from,))
    names = [description[0] for description in cursor.description]
    rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    observed = None
    for row in rows:
        stamp = row.get(contract.watermark_column)
        if stamp is not None and (observed is None or stamp > observed):
            observed = stamp
    return rows, observed


# --- writing the lake ---------------------------------------------------------------------


def _table(rows: list[dict], columns: tuple[str, ...]):
    import pyarrow as pa

    data = {name: [row.get(name) for row in rows] for name in columns}
    return pa.table(data)


def write_parquet(client: Any, bucket: str, key: str, rows: list[dict], columns: tuple[str, ...]):
    """Write one Parquet object. Returns the key, or None when there is nothing to write.

    An empty batch writes no object rather than a zero-row file: the registry records that the
    entity was asked and had nothing to say, which is the fact worth keeping, and an empty
    object would be a partition every reader has to skip.
    """
    if not rows:
        return None

    import pyarrow.parquet as pq

    buffer = io.BytesIO()
    pq.write_table(_table(rows, columns), buffer, compression="snappy")
    client.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
    return key


def decorate(rows: list[dict], *, ingested_at, source_file: str, batch_id: str, system: str):
    """Attach the four audit columns every bronze record carries."""
    for row in rows:
        row["_ingested_at"] = ingested_at
        row["_source_file"] = source_file
        row["_batch_id"] = batch_id
        row["_source_system"] = system
    return rows


def quarantine_rows(
    rejected: list[tuple[dict, Rejection]],
    tokeniser: Tokeniser,
    identifiers: tuple[str, ...],
    *,
    batch_id: str,
    system: str,
    entity: str,
    quarantined_at,
) -> list[dict]:
    """One row per rejected record, with an identifier's offending value tokenised.

    Quarantine sits beside bronze rather than inside it, so a cleartext identifier here would
    be personal data in a place the vault does not cover. The tokenisation is not a courtesy.
    """
    out = []
    for _record, rejection in rejected:
        tokenised = rejection.column in identifiers
        value = rejection.value
        rendered = None
        if value is not None:
            rendered = tokeniser.token(value) if tokenised else str(value)
        out.append(
            {
                "batch_id": batch_id,
                "source_system": system,
                "entity": entity,
                "record_key": None if rejection.record_key is None else str(rejection.record_key),
                "column_name": rejection.column,
                "reason": rejection.reason,
                "offending_value": rendered,
                "value_is_tokenised": tokenised,
                "quarantined_at": quarantined_at,
            }
        )
    return out


QUARANTINE_COLUMNS: tuple[str, ...] = (
    "batch_id",
    "source_system",
    "entity",
    "record_key",
    "column_name",
    "reason",
    "offending_value",
    "value_is_tokenised",
    "quarantined_at",
)


# --- the phase ------------------------------------------------------------------------------


def extract_entity(
    *,
    cursor: Any,
    client: Any,
    bucket: str,
    contract,
    batch: dict,
    tokeniser: Tokeniser,
) -> ExtractReport:
    """Read one entity's window, validate it, tokenise it and write it.

    The order is deliberate: validate against the contract's declared types **before**
    tokenising, because the contract describes the value the source sent, and a token is 32
    characters whatever it replaced.
    """
    report = ExtractReport(entity=contract.entity, batch_id=batch["batch_id"], status="written")

    live_types, live_key = live_shape(cursor, contract.source_schema, contract.entity)
    drift: DriftReport = classify(contract, live_types, live_key)
    report.drift = [
        {"column": o.column, "kind": o.kind, "detail": o.detail, "action": o.action}
        for o in drift.observations
    ]
    if drift.is_breaking:
        report.status = "failed"
        report.failure_reason = drift.summary()
        return report

    rows, observed = read_window(cursor, contract, batch["watermark_from"])
    report.rows_read = len(rows)
    report.watermark_from = batch["watermark_from"]
    report.watermark_to = observed

    result = validate(rows, contract)
    report.rows_landed = result.rows_landed
    report.rows_quarantined = result.rows_quarantined

    identifiers = identifier_columns(cursor, contract.source_schema, contract.entity)
    landed = [project(row, contract.column_names) for row in result.landed]
    for row in landed:
        for column in identifiers:
            if column in row:
                row[column] = tokeniser.token(row[column])

    for row in landed:
        stamp = row.get(contract.watermark_column)
        if stamp is not None:
            day = stamp.date().isoformat()
            report.landed_by_source_date[day] = report.landed_by_source_date.get(day, 0) + 1

    decorate(
        landed,
        ingested_at=batch["opened_at"],
        source_file=contract.qualified_relation,
        batch_id=batch["batch_id"],
        system=contract.source_system,
    )

    bronze_columns = contract.column_names + AUDIT_COLUMNS
    from .registry import bronze_prefix, quarantine_prefix

    bronze_key = (
        bronze_prefix(
            contract.source_system, contract.entity, batch["ingest_date"], batch["batch_id"]
        )
        + "part-0000.parquet"
    )
    written = write_parquet(client, bucket, bronze_key, landed, bronze_columns)
    if written:
        report.bronze_keys.append(written)

    rejected = quarantine_rows(
        result.rejected,
        tokeniser,
        identifiers,
        batch_id=batch["batch_id"],
        system=contract.source_system,
        entity=contract.entity,
        quarantined_at=batch["opened_at"],
    )
    quarantine_key = (
        quarantine_prefix(
            contract.source_system, contract.entity, batch["ingest_date"], batch["batch_id"]
        )
        + "part-0000.parquet"
    )
    written = write_parquet(client, bucket, quarantine_key, rejected, QUARANTINE_COLUMNS)
    if written:
        report.quarantine_keys.append(written)

    return report
