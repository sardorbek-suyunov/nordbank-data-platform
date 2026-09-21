"""Register a run's batches in one warehouse transaction (spec 005 section 3).

Everything this step does is one DuckDB transaction: mark the batches that wrote as
`registered`, advance their watermarks to the observed maximum, upsert the vault, load the
quarantine index, write the reconciliation rows and record the contract versions in force. A
failure anywhere rolls all of it back, so every batch stays `written` and every watermark stays
where it was, and the next run repeats rather than skips.

Two things are outside the transaction and it matters that they are.

**Asset emission happens after the commit.** Airflow emits a task's outlets when the task
succeeds, which is after this returns. A crash in between leaves batches registered with no
asset event. Nothing is lost: the registry is authoritative and every bronze model filters on
it, so a missed asset delays a downstream run by one interval and the next run's register emits
its own. Detecting the gap belongs with the operational observability at M7.

**The vault's raw values come from a second read of the source**, not from the extract task.
Carrying token-to-raw pairs between tasks would put cleartext identifiers in the Airflow
metadata database, and spilling them to the lake would put them in the one place ADR 0005 says
must never hold one. The re-read selects only the identifier columns and the primary key, and
is bounded by both the batch's recorded `watermark_from` and its recorded `watermark_to`, so it
is deterministic and cannot pick up a row that changed after the extract task ran.
"""

from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Any

from . import registry


@dataclass
class RegisterReport:
    registered: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    vault_rows_added: int = 0
    quarantine_rows_loaded: int = 0
    reconciliation_rows: int = 0
    drift_rows: int = 0

    @property
    def any_failed(self) -> bool:
        return bool(self.failed)

    def as_dict(self) -> dict:
        return {
            "registered": self.registered,
            "failed": [{"entity": entity, "reason": reason} for entity, reason in self.failed],
            "vault_rows_added": self.vault_rows_added,
            "quarantine_rows_loaded": self.quarantine_rows_loaded,
            "reconciliation_rows": self.reconciliation_rows,
            "drift_rows": self.drift_rows,
        }


# --- the vault ----------------------------------------------------------------------------


def read_identifier_values(
    cursor: Any, contract, identifiers: tuple[str, ...], watermark_from, watermark_to
) -> list[str]:
    """Every distinct raw identifier value in the batch's own window.

    Bounded on both sides. The lower bound is the window the extract task actually read and the
    upper bound is the maximum it observed, so this sees the same rows it did and no others.
    """
    if not identifiers or watermark_to is None:
        return []

    columns = ", ".join(f'"{name}"' for name in identifiers)
    relation = f'"{contract.source_schema}"."{contract.entity}"'
    clause = f'"{contract.watermark_column}" <= %s'
    parameters: list[Any] = [watermark_to]
    if watermark_from is not None:
        clause = f'"{contract.watermark_column}" >= %s and ' + clause
        parameters.insert(0, watermark_from)

    sql = f"select {columns} from {relation} where {clause}"  # noqa: S608 - contracted identifiers
    cursor.execute(sql, tuple(parameters))

    values: list[str] = []
    seen: set[str] = set()
    for row in cursor.fetchall():
        for value in row:
            if value is None:
                continue
            rendered = value if isinstance(value, str) else str(value)
            if rendered not in seen:
                seen.add(rendered)
                values.append(rendered)
    return values


def upsert_vault(
    connection: Any,
    tokeniser,
    values: list[str],
    *,
    classification: str = "identifier",
    source_system: str,
    entity: str,
    column: str,
    batch_id: str,
    now: dt.datetime,
) -> int:
    """Insert the values this batch saw, keyed on the token, leaving existing rows alone.

    `on conflict do nothing` is what makes re-extraction idempotent and what keeps the
    first-sighting columns meaning first sighting.
    """
    if not values:
        return 0
    before = connection.execute("select count(*) from meta.pii_vault").fetchone()[0]
    connection.executemany(
        """
        insert into meta.pii_vault (
            token, raw_value, classification, first_seen_source_system, first_seen_entity,
            first_seen_column, first_seen_batch_id, created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict (token) do nothing
        """,
        [
            [
                tokeniser.token(value),
                value,
                classification,
                source_system,
                entity,
                column,
                batch_id,
                now,
            ]
            for value in values
        ],
    )
    after = connection.execute("select count(*) from meta.pii_vault").fetchone()[0]
    return int(after - before)


# --- the quarantine index --------------------------------------------------------------------


def load_quarantine(connection: Any, client: Any, bucket: str, keys: list[str]) -> int:
    """Read the quarantine Parquet the extract task wrote and index it in `dq`.

    The lake object is the durable evidence and this table is the rebuildable index, so the
    direction is object first, table second, and never the other way round.
    """
    if not keys:
        return 0

    import pyarrow.parquet as pq

    loaded = 0
    for key in keys:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        for row in pq.read_table(io.BytesIO(body)).to_pylist():
            connection.execute(
                """
                insert into dq.quarantine_log (
                    batch_id, source_system, entity, record_key, column_name, reason,
                    offending_value, value_is_tokenised, quarantined_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["batch_id"],
                    row["source_system"],
                    row["entity"],
                    row["record_key"],
                    row["column_name"],
                    row["reason"],
                    row["offending_value"],
                    row["value_is_tokenised"],
                    row["quarantined_at"],
                ],
            )
            loaded += 1
    return loaded


# --- reconciliation ---------------------------------------------------------------------------


def claimed_by_source(cursor: Any, entity: str, source_date: dt.date) -> int | None:
    """What the source says it changed on a day, from its own control total.

    `platform.tick_log` is the simulated source system's batch trailer: a real core banking
    system publishes a control total and this one does the same. The platform reads it and does
    not land it, which is the line between reading a source's claim and ingesting its
    bookkeeping.

    None means no tick wrote that day — the initial load of the historical book, or a date the
    simulation never reached — and the reconciliation row records a null rather than a zero,
    because "nothing changed" and "nothing claimed" are different facts.
    """
    cursor.execute(
        """
        select coalesce(sum(c.rows_inserted + c.rows_updated), 0)
          from platform.tick_log l
          join platform.tick_table_counts c on c.tick_log_id = l.tick_log_id
         where l.simulated_date = %s and c.table_name = %s
        """,
        (source_date, entity),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    cursor.execute(
        "select count(*) from platform.tick_log where simulated_date = %s", (source_date,)
    )
    if cursor.fetchone()[0] == 0:
        return None
    return int(row[0])


def landed_on_day(
    connection: Any,
    client: Any,
    bucket: str,
    keys: list[str],
    watermark_column: str,
    source_date: dt.date,
) -> int:
    """How many landed rows carry a watermark on that source day.

    Scoped by the row's own `updated_at` and not by the batch, because the batch spans the
    overlap window and a source day does not. The registry's `rows_landed` is the other number
    and neither substitutes for the other.
    """
    if not keys:
        return 0

    import pyarrow.parquet as pq

    landed = 0
    for key in keys:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        for row in pq.read_table(io.BytesIO(body)).to_pylist():
            stamp = row.get(watermark_column)
            if stamp is not None and stamp.date() == source_date:
                landed += 1
    return landed


def write_reconciliation(
    connection: Any,
    *,
    source_system: str,
    entity: str,
    source_date: dt.date,
    rows_claimed: int | None,
    rows_landed: int,
    rows_quarantined: int,
    batch_id: str,
    now: dt.datetime,
) -> None:
    difference = None if rows_claimed is None else rows_landed + rows_quarantined - rows_claimed
    connection.execute(
        """
        insert into ops.source_reconciliation (
            source_system, entity, source_date, rows_claimed, rows_landed, rows_quarantined,
            difference, batch_id, recorded_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict (source_system, entity, source_date) do update set
            rows_claimed = excluded.rows_claimed,
            rows_landed = excluded.rows_landed,
            rows_quarantined = excluded.rows_quarantined,
            difference = excluded.difference,
            batch_id = excluded.batch_id,
            recorded_at = excluded.recorded_at
        """,
        [
            source_system,
            entity,
            source_date,
            rows_claimed,
            rows_landed,
            rows_quarantined,
            difference,
            batch_id,
            now,
        ],
    )


# --- drift and contract versions -----------------------------------------------------------


def record_drift(connection: Any, batch: dict, observations: list[dict], now: dt.datetime) -> int:
    for observation in observations:
        connection.execute(
            """
            insert into meta.schema_drift_log (
                batch_id, source_system, entity, column_name, drift_kind, contract_version,
                action_taken, detail, observed_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict (batch_id, column_name, drift_kind) do nothing
            """,
            [
                batch["batch_id"],
                batch["source_system"],
                batch["entity"],
                observation["column"],
                observation["kind"],
                batch["contract_version"],
                observation["action"],
                observation["detail"],
                now,
            ],
        )
    return len(observations)


def record_contract_version(connection: Any, contract, now: dt.datetime) -> None:
    connection.execute(
        """
        insert into meta.contract_version (
            source_system, entity, contract_version, dictionary_revision, contract_fingerprint,
            in_force_from
        ) values (?, ?, ?, ?, ?, ?)
        on conflict (source_system, entity, contract_version) do nothing
        """,
        [
            contract.source_system,
            contract.entity,
            contract.contract_version,
            contract.dictionary_revision,
            contract.fingerprint,
            now,
        ],
    )


# --- the phase --------------------------------------------------------------------------------


def register_run(
    *,
    connection: Any,
    cursor: Any,
    client: Any,
    bucket: str,
    contracts: dict,
    reports: list[dict],
    tokeniser,
    source_date: dt.date,
    now: dt.datetime,
) -> RegisterReport:
    """One transaction over every batch of one run.

    `reports` are the extract phase's own reports, one per entity, carrying counts and
    timestamps and no data. A report whose status is `failed` marks its batch failed and moves
    the watermark nowhere; the rest register. That is the entity-grain reading of "no partial
    load": one entity's breaking drift does not stop the other forty-four.
    """
    report = RegisterReport()
    connection.execute("begin transaction")
    try:
        for entry in reports:
            entity = entry["entity"]
            contract = contracts[entity]
            batch = registry.batch(connection, entry["batch_id"])
            if batch is None:
                raise registry.RegistryError(
                    f"batch {entry['batch_id']} vanished from the registry"
                )

            record_contract_version(connection, contract, now)
            report.drift_rows += record_drift(connection, batch, entry.get("drift", []), now)

            if entry["status"] == "failed":
                registry.mark_failed(connection, batch["batch_id"], entry["failure_reason"], now)
                report.failed.append((entity, entry["failure_reason"]))
                continue

            watermark_to = _timestamp(entry["watermark_to"])
            registry.mark_written(
                connection,
                batch["batch_id"],
                rows_read=entry["rows_read"],
                rows_landed=entry["rows_landed"],
                rows_quarantined=entry["rows_quarantined"],
                watermark_to=watermark_to,
                written_at=now,
            )
            registry.mark_registered(connection, batch["batch_id"], now)
            registry.advance_watermark(
                connection,
                batch["source_system"],
                entity,
                watermark_to,
                batch["batch_id"],
                now,
            )
            report.registered.append(entity)

            identifiers = tuple(contract.identifier_columns())
            for column in identifiers:
                values = read_identifier_values(
                    cursor, contract, (column,), _timestamp(entry["watermark_from"]), watermark_to
                )
                report.vault_rows_added += upsert_vault(
                    connection,
                    tokeniser,
                    values,
                    source_system=batch["source_system"],
                    entity=entity,
                    column=column,
                    batch_id=batch["batch_id"],
                    now=now,
                )

            report.quarantine_rows_loaded += load_quarantine(
                connection, client, bucket, entry.get("quarantine_keys", [])
            )

            write_reconciliation(
                connection,
                source_system=batch["source_system"],
                entity=entity,
                source_date=source_date,
                rows_claimed=claimed_by_source(cursor, entity, source_date),
                rows_landed=landed_on_day(
                    connection,
                    client,
                    bucket,
                    entry.get("bronze_keys", []),
                    contract.watermark_column,
                    source_date,
                ),
                rows_quarantined=entry["rows_quarantined"],
                batch_id=batch["batch_id"],
                now=now,
            )
            report.reconciliation_rows += 1

        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise
    return report


def _timestamp(value) -> dt.datetime | None:
    if value is None or isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(value)
