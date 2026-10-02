"""The backfill resumes from the registry, never re-ticks a past day, and restores the pause.

A day's completeness is read from the registry; the source is ticked only from the day before;
`transform_bronze`'s pause state is restored as it was found. The loop runs against a world held in
memory: Airflow's runs, the registry's batches and the simulation's day, and every action the
loop takes is recorded so a test can say what it did not do.
"""

from __future__ import annotations

import datetime as dt

import backfill
import pytest

ANCHOR = dt.date(2026, 7, 20)
CUT_OFF_DAY = dt.date(2026, 8, 20)
MALFORMED = "structurally malformed: the file ends part way through a detail record"
DRIFT = "breaking drift: settlements lost merchant_name"
DAY = dt.date(2026, 9, 16)


def _relational(day: dt.date, *, core: int = 16, ref: int = 29) -> list[dict]:
    """A registry interval with `ref` reference entities and `core` core entities registered."""
    return [
        {"source_schema": schema, "entity": f"{schema}_{n}", "status": "registered"}
        for schema, count in (("ref", ref), ("core", core))
        for n in range(count)
    ]


def _batch(entity: str, status: str, *, reason=None, delivery=None, interval="2026-08-20"):
    return {
        "entity": entity,
        "interval_start": f"{interval}T00:00:00+00:00",
        "status": status,
        "failure_reason": reason,
        "delivery": delivery,
    }


def _cut_off() -> list[dict]:
    """The two sibling batches the cut-off delivery failed, both parked for the sender."""
    return [
        _batch("settlements", "failed", reason=MALFORMED, delivery="parked"),
        _batch("settlement_totals", "failed", reason=MALFORMED, delivery="parked"),
    ]


# --- A day's completeness comes from the registry ---------------------------------------------


def test_reference_and_core_are_judged_each_against_their_own_entity_count() -> None:
    whole = _relational(CUT_OFF_DAY)
    assert len(whole) >= 45
    assert backfill.relational_complete(whole) == {
        backfill.REFERENCE_DAG: True,
        backfill.CORE_DAG: True,
    }

    short_of_core = _relational(CUT_OFF_DAY, core=15)
    assert backfill.relational_complete(short_of_core) == {
        backfill.REFERENCE_DAG: True,
        backfill.CORE_DAG: False,
    }

    # A drift resolved by a contract bump leaves a failed batch beside the registered one.
    resolved = [*whole, {"source_schema": "core", "entity": "core_0", "status": "failed"}]
    assert backfill.relational_complete(resolved)[backfill.CORE_DAG] is True


FEED_CASES = [
    # (name, run state, batches, complete, parked, failures)
    ("never ran", None, [], False, 0, 0),
    ("ran and registered", "success", [_batch("settlements", "registered")], True, 0, 0),
    ("failed on a delivery parked for its sender", "failed", _cut_off(), True, 2, 0),
    (
        "failed on breaking drift, parked for a person",
        "failed",
        [_batch("settlements", "failed", reason=DRIFT, delivery="parked")],
        False,
        0,
        1,
    ),
    ("failed before allocating anything", "failed", [], False, 0, 1),
    (
        "an API feed whose retry registered the interval",
        "success",
        [
            _batch("fx_rates", "failed", reason="HTTP 503 after 5 attempts"),
            _batch("fx_rates", "registered"),
        ],
        True,
        0,
        0,
    ),
    (
        "an API feed that failed and was never registered",
        "failed",
        [_batch("fx_rates", "failed", reason="HTTP 503 after 5 attempts")],
        False,
        0,
        1,
    ),
    (
        "a refused delivery that has since landed",
        "failed",
        [_batch("settlements", "failed", reason=MALFORMED, delivery="landed")],
        True,
        0,
        0,
    ),
    (
        "a refused delivery that is neither parked nor landed",
        "failed",
        [_batch("settlements", "failed", reason=MALFORMED, delivery=None)],
        False,
        0,
        1,
    ),
]


def test_the_feed_cases_cover_every_outcome() -> None:
    assert len(FEED_CASES) >= 9
    assert {case[3] for case in FEED_CASES} == {True, False}


@pytest.mark.parametrize(
    ("state", "batches", "complete", "parked", "failures"),
    [case[1:] for case in FEED_CASES],
    ids=[case[0] for case in FEED_CASES],
)
def test_a_feed_is_judged_by_its_batches(state, batches, complete, parked, failures) -> None:
    verdict = backfill.judge_feed(backfill.SETTLEMENT_DAG, state, batches)
    assert verdict.complete is complete
    assert len(verdict.parked) == parked
    assert len(verdict.failures) == failures


# --- The tick only from the day before --------------------------------------------------------


TICK_CASES = [
    # (name, simulated, relational pending, expected: True, False or "refuse")
    ("the source one day behind, a new day", DAY - dt.timedelta(days=1), True, True),
    ("the source at the day, interrupted after its tick", DAY, True, False),
    ("the source at the day, only a feed outstanding", DAY, False, False),
    ("the source past the day, only a feed outstanding", DAY + dt.timedelta(days=3), False, False),
    ("the source past the day, core outstanding", DAY + dt.timedelta(days=3), True, "refuse"),
    ("the source two days behind", DAY - dt.timedelta(days=2), True, "refuse"),
    ("the registry ahead of the source", DAY - dt.timedelta(days=1), False, "refuse"),
]


def test_the_tick_cases_cover_ticking_waiting_and_refusing() -> None:
    assert len(TICK_CASES) >= 7
    assert {str(case[3]) for case in TICK_CASES} == {"True", "False", "refuse"}


@pytest.mark.parametrize(
    ("simulated", "pending", "expected"),
    [case[1:] for case in TICK_CASES],
    ids=[case[0] for case in TICK_CASES],
)
def test_the_source_is_ticked_only_from_the_day_before(simulated, pending, expected) -> None:
    if expected == "refuse":
        with pytest.raises(SystemExit):
            backfill.tick_needed(DAY, simulated, ANCHOR, pending)
    else:
        assert backfill.tick_needed(DAY, simulated, ANCHOR, pending) is expected


def test_the_anchor_is_never_ticked() -> None:
    assert backfill.tick_needed(ANCHOR, ANCHOR, ANCHOR, True) is False


class World:
    """Airflow's runs, the registry and the simulation, as the loop sees them."""

    def __init__(self, simulated: dt.date) -> None:
        self.simulated = simulated
        self.runs: dict[tuple[str, dt.date], dict] = {}
        self.feed_batches: dict[str, list[dict]] = {}
        self.intervals: dict[dt.date, list[dict]] = {}
        self.actions: list[tuple] = []

    def complete_day(self, day: dt.date, *, settlement: list[dict] | None = None) -> None:
        self.intervals[day] = _relational(day)
        for dag in backfill.feeds_due(day):
            batches = settlement if dag == backfill.SETTLEMENT_DAG and settlement else []
            state = "failed" if any(b["status"] == "failed" for b in batches) else "success"
            self.add_run(dag, day, state, batches or [_batch(dag, "registered")])

    def add_run(self, dag: str, day: dt.date, state: str, batches: list[dict]) -> None:
        run_id = f"{dag}__{day}"
        self.runs[(dag, day)] = {"run_id": run_id, "state": state}
        self.feed_batches[run_id] = batches

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(backfill, "existing_run", lambda dag, day: self.runs.get((dag, day)))
        monkeypatch.setattr(backfill, "feed_batches", lambda run_id: self.feed_batches[run_id])
        monkeypatch.setattr(backfill, "simulation_state", lambda: (ANCHOR, self.simulated, 0))
        monkeypatch.setattr(backfill, "interval_state", self.interval_state)
        monkeypatch.setattr(backfill, "tick", self.tick)
        monkeypatch.setattr(
            backfill, "deliver", lambda day, **_: self.actions.append(("deliver", day))
        )
        monkeypatch.setattr(backfill, "trigger_and_wait", self.trigger_and_wait)
        monkeypatch.setattr(backfill, "start_run", self.start_run)
        monkeypatch.setattr(backfill, "wait_for", self.wait_for)

    def interval_state(self, day: dt.date, _expected: int) -> dict:
        return {"batches": self.intervals.get(day, []), "failed": []}

    def tick(self, day: dt.date) -> None:
        self.actions.append(("tick", day))
        self.simulated = day

    def trigger_and_wait(self, dag: str, day: dt.date) -> str:
        self.actions.append(("run", dag, day))
        schema = backfill.RELATIONAL[dag]
        kept = [b for b in self.intervals.get(day, []) if b["source_schema"] != schema]
        count = backfill.EXPECTED[dag]
        fresh = [b for b in _relational(day) if b["source_schema"] == schema][:count]
        self.intervals[day] = kept + fresh
        return "success"

    def start_run(self, dag: str, day: dt.date, _conf=None) -> str:
        self.actions.append(("run", dag, day))
        self.add_run(dag, day, "success", [_batch(dag, "registered")])
        return self.runs[(dag, day)]["run_id"]

    def wait_for(self, dag: str, run_id: str) -> str:
        return next(r["state"] for r in self.runs.values() if r["run_id"] == run_id)


def test_a_re_run_over_the_parked_day_completes_and_does_nothing(monkeypatch, capsys) -> None:
    """Measurement (a) in miniature: the defect's own range, re-run after the window finished."""
    world = World(simulated=dt.date(2026, 9, 18))
    days = [CUT_OFF_DAY + dt.timedelta(days=n) for n in (-1, 0, 1)]
    for day in days:
        world.complete_day(day, settlement=_cut_off() if day == CUT_OFF_DAY else None)
    world.install(monkeypatch)

    assert backfill.ingest_days(days[0], days[-1], ANCHOR, None) == 0

    assert world.actions == []
    out = capsys.readouterr().out
    parked = [line for line in out.splitlines() if "parked, for the sender" in line]
    assert len(parked) >= 2
    assert all(str(CUT_OFF_DAY) in line for line in parked)
    assert "0 day(s) processed, 3 already complete" in out


def test_a_resumed_day_re_runs_only_its_incomplete_dags(monkeypatch) -> None:
    """Measurement (b) in miniature: reference data and the clearing file done, the rest not."""
    world = World(simulated=DAY)
    world.intervals[DAY] = _relational(DAY, core=7)
    world.add_run(backfill.SETTLEMENT_DAG, DAY, "success", [_batch("settlements", "registered")])
    world.install(monkeypatch)

    assert backfill.ingest_days(DAY, DAY, ANCHOR, None) == 0

    runs = [action for action in world.actions if action[0] == "run"]
    assert len(runs) >= 2
    assert runs == [("run", backfill.CORE_DAG, DAY), ("run", backfill.FX_DAG, DAY)]
    assert not [action for action in world.actions if action[0] == "tick"]


def test_a_feed_outstanding_on_a_past_day_is_re_run_without_a_tick(monkeypatch) -> None:
    world = World(simulated=dt.date(2026, 9, 18))
    world.complete_day(DAY)
    del world.runs[(backfill.FX_DAG, DAY)]
    world.install(monkeypatch)

    assert backfill.ingest_days(DAY, DAY, ANCHOR, None) == 0

    assert [a for a in world.actions if a[0] in ("run", "tick")] == [("run", backfill.FX_DAG, DAY)]


def test_core_outstanding_on_a_past_day_halts_rather_than_extracting(monkeypatch) -> None:
    world = World(simulated=dt.date(2026, 9, 18))
    world.complete_day(DAY)
    world.intervals[DAY] = _relational(DAY, core=15)
    world.install(monkeypatch)

    with pytest.raises(SystemExit, match="past it"):
        backfill.ingest_days(DAY, DAY, ANCHOR, None)
    assert world.actions == []


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
    monkeypatch.setattr(backfill, "load_fx_history", lambda anchor: 0)
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
    monkeypatch.setattr(backfill, "load_fx_history", lambda anchor: 0)

    def boom(*_):
        raise SystemExit("backfill: halted")

    monkeypatch.setattr(backfill, "ingest_days", boom)

    with pytest.raises(SystemExit):
        backfill.main(["--from", str(DAY), "--to", str(DAY)])

    assert airflow.pauses == [True, False]
    assert airflow.variables == {}
