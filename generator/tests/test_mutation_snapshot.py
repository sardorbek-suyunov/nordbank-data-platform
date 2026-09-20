"""The snapshot's identity handling, which is where a silent collision would come from.

No database. What is worth pinning without one is that the tick assigns keys from the
high-water mark it read rather than from a sequence, and that it covers every table it might
insert into — a table missing from the identity map would raise on its first insert, but a
table whose key was taken from a stale mark would collide, and the two failures are very
different to diagnose.
"""

from __future__ import annotations

from generator.mutation.snapshot import IDENTITY, Snapshot, _max_ids_sql
from generator.tables import LOAD_ORDER, TABLE_COLUMNS


def test_every_core_table_has_an_identity_the_tick_can_assign_from():
    assert set(IDENTITY) == set(LOAD_ORDER)


def test_the_identity_column_matches_the_loader_definition():
    # One definition of "the key of this table" for the load and for the tick. Two would
    # eventually disagree, and the symptom would be a primary key collision mid-tick.
    for table, column in IDENTITY.items():
        assert column == TABLE_COLUMNS[table][0]


def test_the_high_water_query_covers_every_table_once():
    sql = _max_ids_sql()
    for table in IDENTITY:
        assert f"from core.{table}" in sql
    assert sql.count("union all") == len(IDENTITY) - 1


def test_keys_are_taken_from_the_high_water_mark_and_advance():
    snapshot = Snapshot(next_id={"transactions": 2_321_273})
    assert snapshot.take_id("transactions") == 2_321_274
    assert snapshot.take_id("transactions") == 2_321_275
    assert snapshot.next_id["transactions"] == 2_321_275


def test_an_empty_table_starts_at_one():
    snapshot = Snapshot(next_id={"loans": 0})
    assert snapshot.take_id("loans") == 1


def test_cards_of_an_account_with_none_is_empty_rather_than_missing():
    assert Snapshot().cards_of(4711) == []
