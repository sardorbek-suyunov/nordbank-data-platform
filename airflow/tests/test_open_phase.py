"""The open step's allocations are the mapped extract task's expansion input.

`test_dag_integrity.py` can prove that the extract task is mapped over the open step's return
value, and cannot prove that the value is non-empty, because it exists only at run time. An
empty list there is the M4 defect in its other form: a DAG that parses, maps over nothing,
registers nothing and succeeds. So this runs the real open step against a throwaway warehouse
with the real contract tree and asserts one allocation per contracted entity.

No Airflow and no stack: the open step needs only DuckDB, the schema files and the contracts.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from nordbank_ops.phases import open_phase

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "infra" / "warehouse" / "schema"

# The entity counts spec 005 fixes. Stated as numbers rather than read from the contract tree,
# because reading them from the tree would make the assertion agree with whatever the tree
# happened to hold, including nothing.
EXPECTED = {"core": 16, "ref": 29}


def _apply_schema(path: Path) -> None:
    connection = duckdb.connect(str(path))
    try:
        for sql in sorted(SCHEMA_DIR.glob("*.sql")):
            code = "\n".join(
                line
                for line in sql.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith("--")
            )
            for statement in code.split(";"):
                if statement.strip():
                    connection.execute(statement)
    finally:
        connection.close()


@pytest.fixture
def warehouse(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "warehouse.duckdb"
    _apply_schema(path)
    monkeypatch.setenv("DUCKDB_PATH", str(path))
    return path


def _context(day: dt.date) -> dict:
    logical = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    return {"dag_run": SimpleNamespace(logical_date=logical, run_id=f"manual__{day}")}


@pytest.mark.parametrize("schema", sorted(EXPECTED))
def test_the_open_step_allocates_one_batch_per_contracted_entity(warehouse, schema) -> None:
    allocations = open_phase(source_schema=schema, context=_context(dt.date(2026, 8, 1)))
    assert len(allocations) == EXPECTED[schema]
    assert len({a["entity"] for a in allocations}) == EXPECTED[schema]
    assert all(a["batch_id"].endswith("-01") for a in allocations)

    connection = duckdb.connect(str(warehouse), read_only=True)
    try:
        opened = connection.execute(
            "select count(*) from ops.batch_registry where status = 'open' and source_schema = ?",
            [schema],
        ).fetchone()[0]
    finally:
        connection.close()
    assert opened == EXPECTED[schema]


def test_the_open_step_selects_the_contract_of_the_interval(warehouse) -> None:
    """Contract-of-the-time at the open step, with the committed contracts and no edit to disk.

    `payments` version 2 takes over on 2026-08-26. A batch opened for the day before carries
    version 1 and one opened for that day carries version 2; every other entity carries 1.
    """
    before = open_phase(source_schema="core", context=_context(dt.date(2026, 8, 25)))
    on = open_phase(source_schema="core", context=_context(dt.date(2026, 8, 26)))
    version = {a["entity"]: a["contract_version"] for a in before}
    assert version["payments"] == 1
    assert {a["entity"]: a["contract_version"] for a in on}["payments"] == 2
    assert {v for e, v in version.items() if e != "payments"} == {1}
    assert len(version) == EXPECTED["core"]
