"""The backfill restores `transform_bronze`'s pause state as it found it.

The state is recorded before the pause and restored explicitly, including after a loop that was
killed while it held the DAG.
"""

from __future__ import annotations

import datetime as dt

import backfill
import pytest

ANCHOR = dt.date(2026, 7, 20)
DAY = dt.date(2026, 9, 16)


# --- The pause state is restored as it was found ----------------------------------------------


class Airflow:
    """The three things the hold touches: the DAG's pause flag and one Variable."""

    def __init__(self, paused: bool, record: str | None = None) -> None:
        self.paused = paused
        self.variables = {} if record is None else {backfill.HOLD_RECORD: record}
        self.pauses: list[bool] = []

    def is_paused(self, _dag: str) -> bool:
        return self.paused

    def set_paused(self, _dag: str, paused: bool) -> None:
        self.pauses.append(paused)
        self.paused = paused

    def get_variable(self, key: str) -> str | None:
        return self.variables.get(key)

    def set_variable(self, key: str, value: str) -> None:
        self.variables[key] = value

    def delete_variable(self, key: str) -> None:
        self.variables.pop(key)


HOLD_CASES = [
    # (name, paused before, a record left by a killed loop, paused after)
    ("unpaused before", False, None, False),
    ("paused before", True, None, True),
    ("paused by a killed loop that found it unpaused", True, "false", False),
    ("paused by a killed loop that found it paused", True, "true", True),
]


def test_the_hold_cases_cover_both_prior_states_and_a_killed_loop() -> None:
    assert len(HOLD_CASES) >= 4
    assert {case[2] is None for case in HOLD_CASES} == {True, False}


@pytest.mark.parametrize(
    ("before", "record", "after"),
    [case[1:] for case in HOLD_CASES],
    ids=[case[0] for case in HOLD_CASES],
)
def test_the_pause_state_is_restored_as_it_was_found(monkeypatch, before, record, after) -> None:
    airflow = Airflow(paused=before, record=record)
    monkeypatch.setattr(backfill, "_API", airflow)

    prior = backfill.hold_transforms()
    assert airflow.paused is True
    assert backfill.HOLD_RECORD in airflow.variables
    backfill.release_transforms(prior)

    assert airflow.paused is after
    assert airflow.variables == {}
    assert len(airflow.pauses) >= 1


def test_a_dag_paused_before_the_loop_is_never_unpaused(monkeypatch) -> None:
    airflow = Airflow(paused=True)
    monkeypatch.setattr(backfill, "_API", airflow)

    backfill.release_transforms(backfill.hold_transforms())

    assert len(airflow.pauses) >= 1
    assert False not in airflow.pauses


def test_the_record_outlives_a_loop_killed_while_it_held_the_dag(monkeypatch) -> None:
    airflow = Airflow(paused=False)
    monkeypatch.setattr(backfill, "_API", airflow)

    backfill.hold_transforms()  # and the process is killed: no release

    assert airflow.paused is True
    assert airflow.variables == {backfill.HOLD_RECORD: "false"}


def test_the_build_is_skipped_and_the_state_restored_when_the_dag_was_paused(monkeypatch) -> None:
    airflow = Airflow(paused=True)
    monkeypatch.setattr(backfill, "_API", airflow)
    monkeypatch.setattr(backfill.db, "require_stack", lambda: None)
    monkeypatch.setattr(backfill, "simulation_state", lambda: (ANCHOR, DAY, 0))
    monkeypatch.setattr(backfill, "ingest_days", lambda *_: 0)
    built: list[bool] = []
    monkeypatch.setattr(backfill, "build_once", lambda: built.append(True))

    assert backfill.main(["--from", str(DAY), "--to", str(DAY)]) == 0

    assert built == []
    assert airflow.paused is True


def test_the_state_is_restored_when_the_loop_fails(monkeypatch) -> None:
    airflow = Airflow(paused=False)
    monkeypatch.setattr(backfill, "_API", airflow)
    monkeypatch.setattr(backfill.db, "require_stack", lambda: None)
    monkeypatch.setattr(backfill, "simulation_state", lambda: (ANCHOR, DAY, 0))

    def boom(*_):
        raise SystemExit("backfill: halted")

    monkeypatch.setattr(backfill, "ingest_days", boom)

    with pytest.raises(SystemExit):
        backfill.main(["--from", str(DAY), "--to", str(DAY)])

    assert airflow.pauses == [True, False]
    assert airflow.variables == {}
