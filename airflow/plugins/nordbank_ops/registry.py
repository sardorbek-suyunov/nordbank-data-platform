"""Batch identity, sequencing and the operational registry (spec 005 sections 1 and 4).

The batch id is `{entity}-{interval_start:%Y%m%dT%H%M%S}-{seq:02d}` and the sequence is chosen
from the registry's own status and from nothing else. That is measured rather than stylistic.
In Airflow 3.3.2:

- a retry increments `try_number` and **changes the task instance's UUID**, so task identity is
  not stable across a try;
- a cleared task does the same;
- a backfill re-run of an already-completed logical date **reuses the same `run_id`** and
  clears its task instances, so it is indistinguishable from a retry by run identity.

The registry's status survives all three, so it is the discriminator. `triggering_run_id` is
recorded as provenance and is never read back to decide anything.

Two further things live here because they are decisions rather than plumbing.

`opened_at` is the batch's clock reading, and every record of the batch carries it in
`_ingested_at`. A retry reads it back rather than taking a new one, because criterion 5 asks
for byte-identical objects on retry and a fresh reading per record would make them differ.

`rows_landed` on a batch counts the whole batch, overlap window included.
`ops.source_reconciliation` counts a source day. They are different numbers and conflating them
would make criteria 14 and 15 contradict each other.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

OPEN = "open"
WRITTEN = "written"
REGISTERED = "registered"
FAILED = "failed"

UNREGISTERED: frozenset[str] = frozenset({OPEN, WRITTEN})

BRONZE_PREFIX = "bronze"
QUARANTINE_PREFIX = "quarantine"


class RegistryError(RuntimeError):
    """An illegal transition. Raised rather than logged: the registry is load-bearing."""


@dataclass(frozen=True)
class BatchKey:
    source_system: str
    entity: str
    interval_start: dt.datetime


@dataclass(frozen=True)
class Allocation:
    batch_id: str
    sequence: int
    reused: bool


def format_batch_id(entity: str, interval_start: dt.datetime, sequence: int) -> str:
    return f"{entity}-{interval_start:%Y%m%dT%H%M%S}-{sequence:02d}"


def choose_sequence(existing: list[tuple[int, str]]) -> tuple[int, bool]:
    """The sequence to use, and whether it reuses an existing batch.

    `existing` is every (sequence, status) already recorded for this entity and interval.

    - An `open` or `written` batch is reused, which is the retry, the cleared task and the
      backfill reprocess, all three of which look identical from the registry.
    - Otherwise, if anything is recorded, the next sequence, so a `registered` partition is
      never modified and a `failed` one is never overwritten.
    - Otherwise 01.
    """
    if not existing:
        return 1, False
    unregistered = [sequence for sequence, status in existing if status in UNREGISTERED]
    if unregistered:
        return max(unregistered), True
    return max(sequence for sequence, _ in existing) + 1, False


def object_prefix(prefix: str, source_system: str, entity: str, ingest_date, batch_id: str) -> str:
    day = ingest_date.isoformat() if hasattr(ingest_date, "isoformat") else str(ingest_date)
    return f"{prefix}/{source_system}/{entity}/ingest_date={day}/batch_id={batch_id}/"


def bronze_prefix(source_system: str, entity: str, ingest_date, batch_id: str) -> str:
    return object_prefix(BRONZE_PREFIX, source_system, entity, ingest_date, batch_id)


def quarantine_prefix(source_system: str, entity: str, ingest_date, batch_id: str) -> str:
    return object_prefix(QUARANTINE_PREFIX, source_system, entity, ingest_date, batch_id)


# --- registry access -------------------------------------------------------------------
#
# Every function here takes a live DuckDB connection and does no transaction control of its
# own. The caller owns the transaction, because the open step and the register step are each
# specified to be one.


def existing_batches(connection: Any, key: BatchKey) -> list[tuple[int, str]]:
    rows = connection.execute(
        """
        select batch_sequence, status
          from ops.batch_registry
         where source_system = ? and entity = ? and interval_start = ?
         order by batch_sequence
        """,
        [key.source_system, key.entity, key.interval_start],
    ).fetchall()
    return [(int(sequence), status) for sequence, status in rows]


def watermark(connection: Any, source_system: str, entity: str) -> dt.datetime | None:
    row = connection.execute(
        "select watermark_at from ops.extract_watermark where source_system = ? and entity = ?",
        [source_system, entity],
    ).fetchone()
    return row[0] if row else None


def allocate(
    connection: Any,
    key: BatchKey,
    *,
    source_schema: str,
    ingest_date: dt.date,
    interval_end: dt.datetime,
    contract_version: int,
    watermark_from: dt.datetime | None,
    opened_at: dt.datetime,
    triggering_run_id: str,
) -> Allocation:
    """Reserve a batch id, inserting an `open` row unless an unregistered batch is reused."""
    sequence, reused = choose_sequence(existing_batches(connection, key))
    batch_id = format_batch_id(key.entity, key.interval_start, sequence)
    prefix = bronze_prefix(key.source_system, key.entity, ingest_date, batch_id)

    if reused:
        # The reused batch keeps its own `opened_at`, so `_ingested_at` is reproduced rather
        # than moved. Its counts are reset, because the retry is about to write them again.
        connection.execute(
            """
            update ops.batch_registry
               set status = ?, rows_read = 0, rows_landed = 0, rows_quarantined = 0,
                   watermark_from = ?, watermark_to = null, failure_reason = null,
                   written_at = null, ended_at = null, triggering_run_id = ?
             where batch_id = ?
            """,
            [OPEN, watermark_from, triggering_run_id, batch_id],
        )
        return Allocation(batch_id=batch_id, sequence=sequence, reused=True)

    connection.execute(
        """
        insert into ops.batch_registry (
            batch_id, source_system, source_schema, entity, ingest_date,
            interval_start, interval_end, batch_sequence, contract_version,
            watermark_from, object_prefix, status, opened_at, triggering_run_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            batch_id,
            key.source_system,
            source_schema,
            key.entity,
            ingest_date,
            key.interval_start,
            interval_end,
            sequence,
            contract_version,
            watermark_from,
            prefix,
            OPEN,
            opened_at,
            triggering_run_id,
        ],
    )
    return Allocation(batch_id=batch_id, sequence=sequence, reused=False)


# The columns `batch` selects and the names it hands back, kept together so the query and the
# mapping cannot drift apart.
_BATCH_COLUMNS: tuple[str, ...] = (
    "batch_id",
    "source_system",
    "source_schema",
    "entity",
    "ingest_date",
    "interval_start",
    "interval_end",
    "batch_sequence",
    "contract_version",
    "watermark_from",
    "watermark_to",
    "rows_read",
    "rows_landed",
    "rows_quarantined",
    "object_prefix",
    "status",
    "failure_reason",
    "opened_at",
)


def batch(connection: Any, batch_id: str) -> dict | None:
    row = connection.execute(
        f"select {', '.join(_BATCH_COLUMNS)} from ops.batch_registry where batch_id = ?",  # noqa: S608
        [batch_id],
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_BATCH_COLUMNS, row, strict=True))


def mark_written(
    connection: Any,
    batch_id: str,
    *,
    rows_read: int,
    rows_landed: int,
    rows_quarantined: int,
    watermark_to: dt.datetime | None,
    written_at: dt.datetime,
) -> None:
    connection.execute(
        """
        update ops.batch_registry
           set status = ?, rows_read = ?, rows_landed = ?, rows_quarantined = ?,
               watermark_to = ?, written_at = ?
         where batch_id = ? and status in (?, ?)
        """,
        [
            WRITTEN,
            rows_read,
            rows_landed,
            rows_quarantined,
            watermark_to,
            written_at,
            batch_id,
            OPEN,
            WRITTEN,
        ],
    )


def mark_failed(connection: Any, batch_id: str, reason: str, ended_at: dt.datetime) -> None:
    connection.execute(
        "update ops.batch_registry set status = ?, failure_reason = ?, ended_at = ? "
        "where batch_id = ? and status <> ?",
        [FAILED, reason, ended_at, batch_id, REGISTERED],
    )


def mark_registered(connection: Any, batch_id: str, ended_at: dt.datetime) -> None:
    """Transition a `written` batch to `registered`.

    A batch that is already `registered` is refused rather than re-registered, so bronze
    immutability is enforced here and not merely assumed by the key scheme (ADR 0008).
    """
    current = connection.execute(
        "select status from ops.batch_registry where batch_id = ?", [batch_id]
    ).fetchone()
    if current is None:
        raise RegistryError(f"batch {batch_id} is not in the registry")
    if current[0] == REGISTERED:
        raise RegistryError(
            f"batch {batch_id} is already registered; a registered partition is final and "
            "a re-run allocates the next sequence instead"
        )
    if current[0] != WRITTEN:
        raise RegistryError(f"batch {batch_id} is {current[0]}, not {WRITTEN}")
    connection.execute(
        "update ops.batch_registry set status = ?, ended_at = ? where batch_id = ?",
        [REGISTERED, ended_at, batch_id],
    )


def advance_watermark(
    connection: Any,
    source_system: str,
    entity: str,
    watermark_to: dt.datetime | None,
    batch_id: str,
    now: dt.datetime,
) -> None:
    """Move the watermark to the batch's observed maximum.

    An empty batch observed no maximum, so the watermark does not move: there is nothing
    beyond it to move to, and moving it to the window's upper bound would skip a row that
    commits into the window afterwards.
    """
    if watermark_to is None:
        connection.execute(
            """
            insert into ops.extract_watermark (source_system, entity, watermark_at,
                                               advanced_by_batch_id, updated_at)
            values (?, ?, null, null, ?)
            on conflict (source_system, entity) do nothing
            """,
            [source_system, entity, now],
        )
        return
    connection.execute(
        """
        insert into ops.extract_watermark (source_system, entity, watermark_at,
                                           advanced_by_batch_id, updated_at)
        values (?, ?, ?, ?, ?)
        on conflict (source_system, entity) do update
            set watermark_at = excluded.watermark_at,
                advanced_by_batch_id = excluded.advanced_by_batch_id,
                updated_at = excluded.updated_at
        """,
        [source_system, entity, watermark_to, batch_id, now],
    )
