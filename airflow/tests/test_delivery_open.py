"""The open step for delivered files and snapshots (ADR 0013, ADR 0016, spec 006 review).

What these prove, against a real DuckDB warehouse with the schema applied and the real
contract tree, and no object store, because the open step reads only the warehouse and the
contracts:

- a delivered file is read against the contract in force on the day it was delivered, not on
  the settlement date it covers;
- a delivery refused with a verdict is parked: it allocates nothing on later runs while its
  contracts are unchanged, and is attempted again, once, when they change;
- two deliveries for one settlement date in one run get a batch each;
- a declaration already landed with different content is refused;
- a retried day with no delivery reuses its empty batch rather than allocating another.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import yaml
from nordbank_ops import registry
from nordbank_ops.feeds import identity, phases

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = ROOT / "infra" / "warehouse" / "schema"
CONTRACTS = ROOT / "contracts"
NOW = dt.datetime(2026, 9, 23, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def warehouse(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(path))
    for sql in sorted(SCHEMA_DIR.glob("*.sql")):
        code = "\n".join(
            line
            for line in sql.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for statement in code.split(";"):
            if statement.strip():
                connection.execute(statement)
    connection.close()
    monkeypatch.setenv("DUCKDB_PATH", str(path))
    return path


@pytest.fixture
def contracts(tmp_path, monkeypatch) -> Path:
    """A copy of the committed contract tree, so a test can publish a version into it."""
    root = tmp_path / "contracts"
    shutil.copytree(CONTRACTS, root)
    monkeypatch.setattr("nordbank_ops.ingest.contract_root", lambda: root)
    return root


def _query(path: Path, sql: str, parameters=()) -> list[tuple]:
    connection = duckdb.connect(str(path))
    try:
        return connection.execute(sql, list(parameters)).fetchall()
    finally:
        connection.close()


def _fail(path: Path, batches: list[dict], reason: str) -> None:
    connection = duckdb.connect(str(path))
    try:
        for batch in batches:
            registry.mark_failed(connection, batch["batch_id"], reason, NOW)
    finally:
        connection.close()


def _context(day: dt.date) -> dict:
    logical = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    return {"dag_run": SimpleNamespace(logical_date=logical, run_id=f"manual__{day}", conf={})}


def _file(checksum: str, settlement: str, *, sequence: int = 1, revision: int = 1) -> dict:
    return {
        "key": f"cardnet/NBK_CLR_{settlement.replace('-', '')}_{sequence:02d}_{checksum}.csv",
        "checksum": checksum,
        "size": 100,
        "settlement_date": settlement,
        "file_sequence": sequence,
        "revision": revision,
    }


def _open(day: dt.date, candidates: list[dict], found: bool = True) -> list[dict]:
    return phases.settlement_open(_context(day), candidates, {"found": found})


def _publish_version_3(root: Path, in_force_from: str) -> None:
    """The procedure a person follows to resolve breaking drift: supersede, then publish."""
    current = root / "cardnet" / "settlements.yml"
    shutil.copy(current, root / "cardnet" / "history" / "settlements.v2.yml")
    body = yaml.safe_load(current.read_text(encoding="utf-8"))
    body["contract_version"] = 3
    body["in_force_from"] = in_force_from
    body["columns"] = [c for c in body["columns"] if c["name"] != "masked_pan"]
    current.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def test_a_file_is_read_against_the_contract_of_its_delivery_date(warehouse, contracts):
    """R13(c): a late file for 2026-09-01 delivered on 2026-09-04, after version 2 took over."""
    (unit,) = _open(dt.date(2026, 9, 4), [_file("sha256:late", "2026-09-01")])
    detail = next(b for b in unit["batches"] if b["entity"] == "settlements")
    assert detail["batch_id"].startswith("settlements-20260901T000000-")
    assert detail["ingest_date"] == "2026-09-04"
    assert detail["contract_version"] == 2


def test_a_refused_delivery_is_parked_across_runs_and_attempted_again_when_its_contract_changes(
    warehouse, contracts
):
    candidate = _file("sha256:bad", "2026-09-10")
    (unit,) = _open(dt.date(2026, 9, 10), [candidate])
    _fail(warehouse, unit["batches"], "breaking drift: removed column(s) masked_pan")
    allocated = _query(warehouse, "select count(*) from ops.batch_registry")[0][0]
    assert allocated == 2

    for day in (dt.date(2026, 9, 11), dt.date(2026, 9, 12), dt.date(2026, 9, 13)):
        assert _open(day, [candidate]) == []
    assert _query(warehouse, "select count(*) from ops.batch_registry")[0][0] == allocated
    outcomes = _query(
        warehouse,
        "select outcome, count(*) from ops.file_sighting group by 1 order by 1",
    )
    assert outcomes == [("new", 1), ("parked", 3)]
    assert _query(warehouse, "select count(*) from ops.ingested_file")[0][0] == 0

    # A renamed copy of the same bytes is the same parked delivery, not a new one.
    renamed = {**candidate, "key": "cardnet/NBK_CLR_20260910_01_COPY.csv"}
    assert _open(dt.date(2026, 9, 13), [renamed]) == []

    _publish_version_3(contracts, "2026-09-14")
    (again,) = _open(dt.date(2026, 9, 14), [candidate])
    detail = next(b for b in again["batches"] if b["entity"] == "settlements")
    assert detail["batch_id"] == "settlements-20260910T000000-02"
    assert detail["contract_version"] == 3
    last = _query(
        warehouse, "select outcome from ops.file_sighting order by seen_at desc, outcome limit 1"
    )
    assert last == [("reattempted",)]


def test_a_delivery_failed_for_another_reason_is_not_parked(warehouse, contracts):
    candidate = _file("sha256:flaky", "2026-09-10")
    (unit,) = _open(dt.date(2026, 9, 10), [candidate])
    _fail(warehouse, unit["batches"], "the extract task did not report")
    (again,) = _open(dt.date(2026, 9, 11), [candidate])
    assert again["batches"][0]["batch_id"].endswith("-02")


def test_a_late_file_and_its_correction_in_one_run_get_a_batch_each(warehouse, contracts):
    late = _file("sha256:rev1", "2026-09-01", revision=1)
    correction = _file("sha256:rev2", "2026-09-01", revision=2)
    units = _open(dt.date(2026, 9, 4), [late, correction])
    ids = [b["batch_id"] for unit in units for b in unit["batches"]]
    assert len(ids) == len(set(ids)) == 4
    assert not any(unit.get("refuse") for unit in units)


def test_the_same_declaration_with_different_content_is_refused(warehouse, contracts):
    first = _file("sha256:one", "2026-09-01")
    connection = duckdb.connect(str(warehouse))
    try:
        identity.record_ingested(
            connection,
            checksum_="sha256:one",
            source_system="cardnet",
            entity="settlements",
            key=first["key"],
            size=100,
            business_date=dt.date(2026, 9, 1),
            publisher_version=None,
            batch_id="settlements-20260901T000000-01",
            now=NOW,
            file_sequence=1,
            revision=1,
        )
    finally:
        connection.close()
    (unit,) = _open(dt.date(2026, 9, 2), [_file("sha256:two", "2026-09-01")])
    assert unit["refuse"].startswith("declaration conflict")
    assert "settlements-20260901T000000-01" in unit["refuse"]
    assert identity.is_verdict(unit["refuse"])


def test_a_retried_day_with_no_delivery_reuses_its_empty_batch(warehouse, contracts):
    (unit,) = _open(dt.date(2026, 9, 5), [], found=False)
    assert unit["file"] is None
    (retry,) = _open(dt.date(2026, 9, 5), [], found=False)
    assert retry["batches"][0]["batch_id"] == unit["batches"][0]["batch_id"]
