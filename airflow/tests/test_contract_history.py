"""Contract-of-the-time: the version chain on disk, and selection by interval (spec 006 section 5).

Unit tests, no stack. The selection runs against a real in-memory DuckDB with the warehouse
schema applied, because it is SQL and a fake would test the fake.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

import duckdb
import pytest
import yaml
from data_contract import ContractError, in_force, load_history
from nordbank_ops import contracts as selection

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONTRACTS = ROOT / "contracts" / "corebank"
SCHEMA_DIR = ROOT / "infra" / "warehouse" / "schema"
NOW = dt.datetime(2026, 9, 23, 9, 0, tzinfo=dt.UTC)
WIDENED_ON = dt.date(2026, 8, 26)


@pytest.fixture
def warehouse():
    connection = duckdb.connect(":memory:")
    for sql in sorted(SCHEMA_DIR.glob("*.sql")):
        code = "\n".join(
            line
            for line in sql.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for statement in code.split(";"):
            if statement.strip():
                connection.execute(statement)
    yield connection
    connection.close()


@pytest.fixture
def tree(tmp_path) -> Path:
    copy = tmp_path / "corebank"
    shutil.copytree(CONTRACTS, copy)
    return copy


def test_the_committed_tree_is_a_valid_chain_per_entity() -> None:
    chains = load_history(CONTRACTS)
    assert len(chains) == 45
    assert [c.contract_version for c in chains["payments"]] == [1, 2]
    assert chains["payments"][0].in_force_from is None
    assert chains["payments"][1].in_force_from == WIDENED_ON
    single = [entity for entity, versions in chains.items() if len(versions) == 1]
    assert len(single) == 44
    assert all(chains[e][0].in_force_from is None for e in single)


def test_the_version_in_force_changes_on_the_day_the_bump_names() -> None:
    payments = load_history(CONTRACTS)["payments"]
    assert in_force(payments, WIDENED_ON - dt.timedelta(days=1)).contract_version == 1
    assert in_force(payments, WIDENED_ON).contract_version == 2
    widened = in_force(payments, WIDENED_ON).column("remittance_reference").data_type
    narrow = in_force(payments, dt.date(2026, 7, 20)).column("remittance_reference").data_type
    assert (narrow, widened) == ("character varying(140)", "character varying(280)")


def test_every_bump_that_accepts_a_scripted_event_takes_over_on_the_day_it_fires() -> None:
    """The one place the anchor-relative timeline and the absolute contracts must agree.

    A drift event fires at an offset from the anchor and a contract names an absolute business
    day. The contracts are authored against `ACCEPTANCE_ANCHOR`, and this asserts that for every
    version that accepted an event, which is what makes a replay of the acceptance history
    select the right version on every day without anyone editing a file.
    """
    from generator import drift
    from generator.drift.timeline import ACCEPTANCE_ANCHOR, TYPE_WIDENED

    checked = 0
    for entity, versions in load_history(CONTRACTS).items():
        for earlier, later in zip(versions, versions[1:], strict=False):
            for event in drift.events():
                if (event.target_schema, event.target_table) != (later.source_schema, entity):
                    continue
                shape = event.widened_to if event.drift_type == TYPE_WIDENED else None
                shape = shape or (event.added.data_type if event.added else None)
                before = earlier.column(event.target_column)
                after = later.column(event.target_column)
                accepted_here = after is not None and after.data_type == shape
                accepted_before = before is not None and before.data_type == shape
                if accepted_here and not accepted_before:
                    assert later.in_force_from == event.fires_on(ACCEPTANCE_ANCHOR), (
                        f"{entity} version {later.contract_version} accepts {event.name}, which "
                        f"fires on {event.fires_on(ACCEPTANCE_ANCHOR)} at the acceptance anchor, "
                        f"and says it takes over on {later.in_force_from}"
                    )
                    checked += 1
    assert checked >= 1, "no contract version accepts a scripted event, so nothing was checked"


def _rewrite(path: Path, **changes) -> None:
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    body.update(changes)
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def test_a_later_version_without_a_start_day_is_refused(tree) -> None:
    _rewrite(tree / "payments.yml", in_force_from=None)
    with pytest.raises(ContractError, match="has no in_force_from"):
        load_history(tree)


def test_a_superseded_first_version_must_apply_from_the_start(tree) -> None:
    _rewrite(tree / "history" / "payments.v1.yml", in_force_from="2026-07-01")
    with pytest.raises(ContractError, match="oldest version must apply from the start"):
        load_history(tree)


def test_a_version_taking_over_before_its_predecessor_is_refused(tree) -> None:
    (tree / "payments.yml").rename(tree / "history" / "payments.v2.yml")
    shutil.copy(tree / "history" / "payments.v2.yml", tree / "payments.yml")
    _rewrite(tree / "payments.yml", contract_version=3, in_force_from="2026-08-01")
    with pytest.raises(ContractError, match="must take over on a later day"):
        load_history(tree)


def test_history_holding_a_version_above_the_current_file_is_refused(tree) -> None:
    shutil.copy(tree / "payments.yml", tree / "history" / "payments.v3.yml")
    _rewrite(tree / "history" / "payments.v3.yml", contract_version=3, in_force_from="2026-09-10")
    with pytest.raises(ContractError, match="the current file must be the highest"):
        load_history(tree)


def test_a_misnamed_superseded_file_is_refused(tree) -> None:
    (tree / "history" / "payments.v1.yml").rename(tree / "history" / "payments.v7.yml")
    with pytest.raises(ContractError, match="is named"):
        load_history(tree)


def test_selection_through_the_table_follows_the_interval(warehouse) -> None:
    chains = load_history(CONTRACTS)
    assert selection.sync(warehouse, chains, NOW) == 46
    assert selection.sync(warehouse, chains, NOW) == 0

    before = selection.select(warehouse, "corebank", "payments", WIDENED_ON - dt.timedelta(days=1))
    after = selection.select(warehouse, "corebank", "payments", WIDENED_ON)
    assert (before, after) == (1, 2)
    assert selection.body(chains, "payments", before).contract_version == 1
    assert selection.select(warehouse, "corebank", "accounts", dt.date(2026, 7, 20)) == 1


def test_the_two_times_are_both_recorded_and_are_not_the_same_column(warehouse) -> None:
    selection.sync(warehouse, load_history(CONTRACTS), NOW)
    rows = warehouse.execute(
        """
        select contract_version, in_force_from, first_seen_at from meta.contract_version
         where entity = 'payments' order by 1
        """
    ).fetchall()
    assert [(v, f) for v, f, _ in rows] == [(1, None), (2, WIDENED_ON)]
    assert all(seen == NOW for _, _, seen in rows)


def test_a_recorded_version_edited_in_place_is_refused(warehouse, tree) -> None:
    selection.sync(warehouse, load_history(tree), NOW)
    body = yaml.safe_load((tree / "history" / "payments.v1.yml").read_text(encoding="utf-8"))
    body["columns"][1]["nullable"] = not body["columns"][1]["nullable"]
    (tree / "history" / "payments.v1.yml").write_text(yaml.safe_dump(body), encoding="utf-8")
    with pytest.raises(selection.ContractSelectionError, match="not edited in place"):
        selection.sync(warehouse, load_history(tree), NOW)


def test_a_recorded_version_with_no_file_is_refused() -> None:
    with pytest.raises(selection.ContractSelectionError, match="no file on disk"):
        selection.body(load_history(CONTRACTS), "payments", 9)
