"""Unit tests for batch identity, sequencing and the registry transitions.

The sequencing rule is the one piece of this milestone that was rewritten because a probe
contradicted the reasoning behind it. Airflow's own identifiers do not survive a retry, a clear
or a backfill reprocess, so the rule consults the registry's status and nothing else, and these
tests enumerate the three cases that look identical from outside it.

The registry tests run against a real in-memory DuckDB, because the transitions are SQL and a
fake would test the fake. They need no stack: `duckdb` is a dev dependency and the schema files
are in the repository.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest
from nordbank_ops.registry import (
    FAILED,
    OPEN,
    REGISTERED,
    WRITTEN,
    BatchKey,
    RegistryError,
    advance_watermark,
    allocate,
    batch,
    bronze_prefix,
    choose_sequence,
    format_batch_id,
    mark_failed,
    mark_registered,
    mark_written,
    quarantine_prefix,
    watermark,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "warehouse" / "schema"

SYSTEM = "corebank"
ENTITY = "accounts"
INTERVAL = dt.datetime(2026, 9, 19, tzinfo=dt.UTC)
INTERVAL_END = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
INGEST_DATE = dt.date(2026, 9, 19)
OPENED_AT = dt.datetime(2026, 9, 21, 4, 0, tzinfo=dt.UTC)


@pytest.fixture
def connection():
    con = duckdb.connect(":memory:")
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        code = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for statement in code.split(";"):
            if statement.strip():
                con.execute(statement)
    yield con
    con.close()


def key() -> BatchKey:
    return BatchKey(source_system=SYSTEM, entity=ENTITY, interval_start=INTERVAL)


def open_one(connection, **overrides):
    arguments = {
        "source_schema": "core",
        "ingest_date": INGEST_DATE,
        "interval_end": INTERVAL_END,
        "contract_version": 1,
        "watermark_from": None,
        "opened_at": OPENED_AT,
        "triggering_run_id": "backfill__2026-09-19T00:00:00+00:00",
    }
    arguments.update(overrides)
    return allocate(connection, key(), **arguments)


# --- identity and sequencing -----------------------------------------------------------


def test_the_batch_id_carries_the_entity_the_interval_and_the_sequence():
    assert format_batch_id("accounts", INTERVAL, 1) == "accounts-20260919T000000-01"
    assert format_batch_id("accounts", INTERVAL, 12) == "accounts-20260919T000000-12"


def test_the_object_prefixes_carry_the_ingest_date_and_the_batch_id():
    batch_id = format_batch_id(ENTITY, INTERVAL, 1)
    assert bronze_prefix(SYSTEM, ENTITY, INGEST_DATE, batch_id) == (
        "bronze/corebank/accounts/ingest_date=2026-09-19/batch_id=accounts-20260919T000000-01/"
    )
    assert quarantine_prefix(SYSTEM, ENTITY, INGEST_DATE, batch_id).startswith("quarantine/")


def test_the_first_batch_of_an_interval_is_sequence_one():
    assert choose_sequence([]) == (1, False)


@pytest.mark.parametrize("status", [OPEN, WRITTEN])
def test_an_unregistered_batch_is_reused(status):
    """The retry, the cleared task and the backfill reprocess are all this case."""
    assert choose_sequence([(1, status)]) == (1, True)


def test_a_registered_batch_yields_the_next_sequence():
    assert choose_sequence([(1, REGISTERED)]) == (2, False)


def test_a_failed_batch_yields_the_next_sequence_rather_than_being_overwritten():
    assert choose_sequence([(1, FAILED)]) == (2, False)


def test_an_unregistered_batch_above_a_registered_one_is_reused():
    assert choose_sequence([(1, REGISTERED), (2, WRITTEN)]) == (2, True)


def test_the_highest_unregistered_batch_wins():
    assert choose_sequence([(1, OPEN), (2, OPEN)]) == (2, True)


def test_sequences_continue_past_a_mixed_history():
    assert choose_sequence([(1, REGISTERED), (2, FAILED), (3, REGISTERED)]) == (4, False)


# --- registry transitions ---------------------------------------------------------------


def test_allocating_twice_reuses_the_open_batch_and_its_opened_at(connection):
    first = open_one(connection)
    second = open_one(connection, opened_at=OPENED_AT + dt.timedelta(hours=1))
    assert first.batch_id == second.batch_id
    assert second.reused is True
    assert batch(connection, first.batch_id)["opened_at"] == OPENED_AT


def test_allocating_after_registration_makes_a_new_batch(connection):
    first = open_one(connection)
    mark_written(
        connection,
        first.batch_id,
        rows_read=10,
        rows_landed=10,
        rows_quarantined=0,
        watermark_to=INTERVAL_END,
        written_at=OPENED_AT,
    )
    mark_registered(connection, first.batch_id, OPENED_AT)
    second = open_one(connection)
    assert second.batch_id != first.batch_id
    assert second.sequence == 2
    assert batch(connection, first.batch_id)["status"] == REGISTERED


def test_a_reused_batch_has_its_counts_reset(connection):
    first = open_one(connection)
    mark_written(
        connection,
        first.batch_id,
        rows_read=10,
        rows_landed=9,
        rows_quarantined=1,
        watermark_to=INTERVAL_END,
        written_at=OPENED_AT,
    )
    open_one(connection)
    record = batch(connection, first.batch_id)
    assert (record["rows_read"], record["rows_landed"], record["rows_quarantined"]) == (0, 0, 0)
    assert record["watermark_to"] is None
    assert record["status"] == OPEN


def test_registering_an_already_registered_batch_is_refused(connection):
    allocation = open_one(connection)
    mark_written(
        connection,
        allocation.batch_id,
        rows_read=1,
        rows_landed=1,
        rows_quarantined=0,
        watermark_to=INTERVAL_END,
        written_at=OPENED_AT,
    )
    mark_registered(connection, allocation.batch_id, OPENED_AT)
    with pytest.raises(RegistryError, match="already registered"):
        mark_registered(connection, allocation.batch_id, OPENED_AT)


def test_registering_an_open_batch_is_refused(connection):
    allocation = open_one(connection)
    with pytest.raises(RegistryError, match="not written"):
        mark_registered(connection, allocation.batch_id, OPENED_AT)


def test_registering_an_unknown_batch_is_refused(connection):
    with pytest.raises(RegistryError, match="not in the registry"):
        mark_registered(connection, "accounts-20260101T000000-01", OPENED_AT)


def test_a_failed_batch_keeps_its_reason(connection):
    allocation = open_one(connection)
    mark_failed(connection, allocation.batch_id, "type_changed on remittance_reference", OPENED_AT)
    record = batch(connection, allocation.batch_id)
    assert record["status"] == FAILED
    assert "remittance_reference" in record["failure_reason"]


def test_a_registered_batch_cannot_be_failed(connection):
    allocation = open_one(connection)
    mark_written(
        connection,
        allocation.batch_id,
        rows_read=1,
        rows_landed=1,
        rows_quarantined=0,
        watermark_to=INTERVAL_END,
        written_at=OPENED_AT,
    )
    mark_registered(connection, allocation.batch_id, OPENED_AT)
    mark_failed(connection, allocation.batch_id, "too late", OPENED_AT)
    assert batch(connection, allocation.batch_id)["status"] == REGISTERED


# --- watermarks --------------------------------------------------------------------------


def test_the_watermark_starts_absent(connection):
    assert watermark(connection, SYSTEM, ENTITY) is None


def test_the_watermark_advances_to_the_observed_maximum(connection):
    observed = dt.datetime(2026, 9, 19, 23, 45, tzinfo=dt.UTC)
    advance_watermark(connection, SYSTEM, ENTITY, observed, "batch-1", OPENED_AT)
    assert watermark(connection, SYSTEM, ENTITY) == observed


def test_an_empty_batch_leaves_the_watermark_where_it_was(connection):
    observed = dt.datetime(2026, 9, 19, 23, 45, tzinfo=dt.UTC)
    advance_watermark(connection, SYSTEM, ENTITY, observed, "batch-1", OPENED_AT)
    advance_watermark(connection, SYSTEM, ENTITY, None, "batch-2", OPENED_AT)
    assert watermark(connection, SYSTEM, ENTITY) == observed


def test_an_empty_first_batch_records_the_entity_without_a_watermark(connection):
    advance_watermark(connection, SYSTEM, "agent_locations", None, "batch-1", OPENED_AT)
    assert watermark(connection, SYSTEM, "agent_locations") is None
