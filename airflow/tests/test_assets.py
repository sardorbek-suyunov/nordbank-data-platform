"""Asset events equal registered batches, per run (spec 006 review, C1).

Against a real DuckDB warehouse with the schema applied: the settlement open step allocates the
batches, the feed register step registers or fails them, and the events the register step would
add to its alias are compared with what the registry says it registered. A run whose batches all
failed emits nothing; a registered empty batch emits and carries its `empty_reason`.

No Airflow and no stack: `nordbank_ops.assets.events` is the whole of the decision, and `emit`
only hands its pairs to the alias.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest
import test_delivery_open as delivery
from nordbank_ops import assets
from nordbank_ops.feeds import phases
from nordbank_ops.feeds.register import register_feed_run

# The open step's harness, shared rather than copied: a warehouse with the schema applied and a
# copy of the contract tree.
warehouse = delivery.warehouse
contracts = delivery.contracts
NOW, _file, _open = delivery.NOW, delivery._file, delivery._open

DAG_ID = "ingest_card_settlements"
ENTITIES = phases.SETTLEMENT_ENTITIES


def _report(batch: dict, *, failed: str | None = None, empty_reason: str | None = None) -> dict:
    report = {
        "entity": batch["entity"],
        "batch_id": batch["batch_id"],
        "status": "failed" if failed else "written",
        "rows_read": 0,
        "rows_landed": 0,
        "rows_quarantined": 0,
    }
    if failed:
        report["failure_reason"] = failed
    if empty_reason:
        report["empty_reason"] = empty_reason
    return report


def _register(path, reports: list[dict]) -> dict:
    connection = duckdb.connect(str(path))
    try:
        summary = register_feed_run(
            connection=connection,
            client=None,
            lake_bucket="unused",
            reports=reports,
            tokeniser=None,
            identifier_values=lambda _entry: [],
            now=NOW,
        )
        registered = connection.execute(
            "select batch_id from ops.batch_registry where status = 'registered' order by 1"
        ).fetchall()
    finally:
        connection.close()
    return {"summary": summary.as_dict(), "registered": [batch_id for (batch_id,) in registered]}


def _events(summary: dict) -> list[tuple[str, dict]]:
    return assets.events(DAG_ID, ENTITIES, summary["registered_batches"])


def test_a_run_whose_batches_all_failed_emits_nothing(warehouse, contracts):
    (unit,) = _open(dt.date(2026, 8, 20), [_file("sha256:cut", "2026-08-20")])
    reason = "structurally malformed file: the last line is not an end record"
    result = _register(warehouse, [_report(b, failed=reason) for b in unit["batches"]])
    assert result["registered"] == []
    assert _events(result["summary"]) == []


def test_a_registered_empty_batch_emits_and_says_why_it_is_empty(warehouse, contracts):
    (unit,) = _open(dt.date(2026, 9, 5), [], found=False)
    reports = [_report(b, empty_reason=unit["empty_reason"]) for b in unit["batches"]]
    result = _register(warehouse, reports)
    events = _events(result["summary"])
    assert len(events) == len(result["registered"]) == 2
    assert {name for name, _extra in events} == {f"{DAG_ID}/{e}" for e in ENTITIES}
    assert all(extra["empty_reason"] == phases.NO_ARRIVAL for _name, extra in events)


def test_events_equal_registered_batches_when_a_run_mixes_outcomes(warehouse, contracts):
    # A late file that fails beside the day's own empty batches: the registry registers two
    # batches and fails two, and exactly the two registered ones emit, one event each.
    late, empty = _open(dt.date(2026, 9, 5), [_file("sha256:late", "2026-09-02")], found=False)
    reports = [_report(b, failed="breaking drift: removed column(s) x") for b in late["batches"]]
    reports += [_report(b, empty_reason=empty["empty_reason"]) for b in empty["batches"]]
    result = _register(warehouse, reports)
    events = _events(result["summary"])
    assert sorted(extra["batch_id"] for _name, extra in events) == result["registered"]
    assert len(result["registered"]) == 2


def test_two_registered_batches_of_one_entity_emit_two_events(warehouse, contracts):
    first, second = _open(
        dt.date(2026, 9, 5),
        [_file("sha256:a", "2026-09-05"), _file("sha256:b", "2026-09-02")],
    )
    reports = [_report(b) for unit in (first, second) for b in unit["batches"]]
    result = _register(warehouse, reports)
    events = _events(result["summary"])
    assert len(events) == len(result["registered"]) == 4
    assert sum(1 for name, _extra in events if name == f"{DAG_ID}/settlements") == 2


def test_a_batch_for_an_entity_the_dag_does_not_declare_is_refused():
    with pytest.raises(ValueError, match="no asset for entity"):
        assets.events(DAG_ID, ENTITIES, [{"entity": "other", "batch_id": "x", "rows_landed": 0}])
