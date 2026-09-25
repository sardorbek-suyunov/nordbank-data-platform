"""The open step for delivered files and snapshots (ADR 0013, ADR 0016, spec 006 review).

What these prove, against a real DuckDB warehouse with the schema applied and the real
contract tree, and no object store, because the open step reads only the warehouse and the
contracts:

- two deliveries for one settlement date in one run get a batch each;
- a retried day with no delivery reuses its empty batch rather than allocating another.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from nordbank_ops.feeds import phases

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = ROOT / "infra" / "warehouse" / "schema"
CONTRACTS = ROOT / "contracts"


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


def test_a_late_file_and_its_correction_in_one_run_get_a_batch_each(warehouse, contracts):
    late = _file("sha256:rev1", "2026-09-01", revision=1)
    correction = _file("sha256:rev2", "2026-09-01", revision=2)
    units = _open(dt.date(2026, 9, 4), [late, correction])
    ids = [b["batch_id"] for unit in units for b in unit["batches"]]
    assert len(ids) == len(set(ids)) == 4
    assert not any(unit.get("refuse") for unit in units)


def test_a_retried_day_with_no_delivery_reuses_its_empty_batch(warehouse, contracts):
    (unit,) = _open(dt.date(2026, 9, 5), [], found=False)
    assert unit["file"] is None
    (retry,) = _open(dt.date(2026, 9, 5), [], found=False)
    assert retry["batches"][0]["batch_id"] == unit["batches"][0]["batch_id"]
