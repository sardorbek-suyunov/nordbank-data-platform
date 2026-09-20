"""The drift timeline, and the property that makes `schema-check` survive it.

The expected schema is the committed dictionary plus the deltas of the events that have fired.
That is one sentence and two failure modes: a delta that does not match the DDL beside it, and a
delta applied in an order the database did not use. Both are pinned here, because neither shows
up until a tick has run and a schema check has failed for a reason that looks like a documented
column going missing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from schema_contract import Column

from generator import drift
from generator.drift.timeline import COLUMN_ADDED, TYPE_WIDENED

ANCHOR = dt.date(2026, 9, 18)


def dictionary() -> list[Column]:
    return [
        Column("core", "merchants", "merchant_id", "bigint", False, "non-personal", "Key.", "-"),
        Column(
            "core",
            "payments",
            "remittance_reference",
            "character varying(140)",
            True,
            "quasi-identifier",
            "Free text.",
            "-",
        ),
    ]


def test_every_event_has_a_unique_name():
    names = [event.name for event in drift.events()]
    assert len(names) == len(set(names))


def test_m3_implements_one_additive_event_and_one_widening():
    kinds = sorted(event.drift_type for event in drift.events())
    assert kinds == [COLUMN_ADDED, TYPE_WIDENED]


def test_an_additive_event_declares_the_column_it_adds():
    for event in drift.events():
        if event.drift_type == COLUMN_ADDED:
            assert event.added is not None
            assert (event.added.schema, event.added.table, event.added.name) == event.key


def test_a_widening_declares_the_type_it_widens_to():
    for event in drift.events():
        if event.drift_type == TYPE_WIDENED:
            assert event.widened_to
            assert event.widened_to in event.apply_sql


def test_both_directions_are_idempotent_or_naturally_repeatable():
    # `make schema-apply` re-applies fired events and `make seed` reverts them, and either can
    # run twice. An `add column` needs `if not exists` to survive that; an `alter column type`
    # is repeatable on its own.
    for event in drift.events():
        for statement in (event.apply_sql, event.revert_sql):
            lowered = statement.lower()
            assert "if not exists" in lowered or "if exists" in lowered or "type " in lowered


def test_every_event_says_what_the_platform_should_do_when_it_meets_it():
    # Spec 004 section 3: the timeline declares the behaviour the platform is expected to
    # exhibit. M4 reads these, so an event with no stated expectation is an event M4 cannot plan
    # against.
    for event in drift.events():
        assert event.expectation


def test_an_event_does_not_fire_before_its_date():
    names = {event.name for event in drift.due_events(ANCHOR, ANCHOR, set())}
    assert names == set()


def test_an_event_fires_on_its_offset():
    event = drift.events()[0]
    fires = event.fires_on(ANCHOR)
    assert event not in drift.due_events(ANCHOR, fires - dt.timedelta(days=1), set())
    assert event in drift.due_events(ANCHOR, fires, set())


def test_an_event_that_has_fired_is_not_offered_again():
    event = drift.events()[0]
    fires = event.fires_on(ANCHOR)
    assert event not in drift.due_events(ANCHOR, fires, {event.name})


def test_a_date_that_was_never_ticked_still_fires_on_the_next_tick():
    # `<=` rather than `==`, so an event whose date fell inside a window nothing ticked is not
    # lost for the rest of the run.
    event = drift.events()[0]
    late = event.fires_on(ANCHOR) + dt.timedelta(days=40)
    assert event in drift.due_events(ANCHOR, late, set())


def test_an_additive_delta_adds_exactly_one_documented_column():
    before = dictionary()
    after = drift.apply_deltas(before, ["merchants_risk_score_added"])
    assert len(after) == len(before) + 1
    added = after[-1]
    assert (added.schema, added.table, added.name) == ("core", "merchants", "merchant_risk_score")
    assert added.classification == "non-personal"
    assert added.description


def test_a_widening_delta_changes_the_type_and_nothing_else():
    before = dictionary()
    after = drift.apply_deltas(before, ["payments_remittance_widened"])
    assert len(after) == len(before)
    widened = next(column for column in after if column.name == "remittance_reference")
    original = next(column for column in before if column.name == "remittance_reference")
    assert widened.data_type == "character varying(280)"
    assert widened.classification == original.classification
    assert widened.description == original.description
    assert widened.is_nullable == original.is_nullable


def test_an_unfired_event_changes_nothing():
    assert drift.apply_deltas(dictionary(), []) == dictionary()


def test_an_unknown_event_name_is_ignored_rather_than_crashing_a_schema_check():
    # The log can outlive a timeline entry — a branch that removed an event, say. A schema check
    # that crashed on it would be harder to diagnose than one that reports the difference.
    assert drift.apply_deltas(dictionary(), ["no_such_event"]) == dictionary()


def test_only_an_additive_event_needs_a_classification_row():
    rows = drift.classification_rows(["merchants_risk_score_added", "payments_remittance_widened"])
    assert [column.name for column in rows] == ["merchant_risk_score"]


def test_recording_an_event_that_is_not_in_the_timeline_is_refused():
    class Cursor:
        def execute(self, *args: object) -> None:
            raise AssertionError("nothing should be written for an unknown event")

    with pytest.raises(ValueError, match="not in the timeline"):
        drift.record_fired(
            Cursor(),
            ["no_such_event"],
            simulated_date=ANCHOR,
            tick_sequence=1,
            applied_at=None,
        )


def test_reverting_undoes_the_newest_event_first():
    # A later event may depend on an earlier one's column, so the order is the log's, reversed.
    undone: list[str] = []
    drift.revert_all(
        lambda sql: undone.append(sql),
        ["merchants_risk_score_added", "payments_remittance_widened"],
    )
    assert "remittance_reference" in undone[0]
    assert "merchant_risk_score" in undone[1]
