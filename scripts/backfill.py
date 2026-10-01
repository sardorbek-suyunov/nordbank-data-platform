"""Tick the source one day, ingest that day, repeat (spec 005 section 9).

**The loop cannot be reordered and it cannot be batched.** M3 measured why: a later tick
re-stamps a row an earlier one wrote, so an extraction of day D run after day D+1 has ticked
reads a window that no longer holds what the tick log says changed on day D. Sixty ticks
followed by sixty extractions would leave the reconciliation unprovable rather than merely
approximate. `generator/mutation/reconcile.py` carries the measurement.

Reference data runs ahead of core banking on every day, so a new reference code exists in
bronze before a `core` fact references it. The ordering lives here rather than in a cross-DAG
dependency: the loop already sequences the tick and the two ingestions, and a second mechanism
would be a second place to get the order wrong.

**It halts on a failed batch and does not resolve it.** A breaking drift means the source's
shape moved past what the contract accepts, and the resolution is a new contract version
decided by a person and recorded in a commit. A loop that bumped the version itself would
have removed the control it exists to demonstrate. The halt names the entity, the drift kind
and the procedure, and `make backfill` resumes from the day that failed once the commit is
made.

**It is resumable and idempotent.** Where to start comes from the registry and the simulation
state rather than from an argument, so re-invoking it over a range it has already finished
changes nothing, and invoking it after a halt continues from the day that stopped it.

**A day's completeness is read from the registry, per DAG, and never from a run's state.** A
run's state says how its last try ended, and a settlement run that refused a cut-off file ends
`failed` for ever, correctly, while the day it belongs to is as complete as it can be: the
delivery waits on its sender, not on a re-run. Until this was fixed, re-invoking the loop over a
range holding such a day read the day as incomplete, tried to tick the source back to it, and
the tick guard refused. Reference data is complete when its twenty-nine entities registered,
core banking when its sixteen did, and a feed when every batch its run allocated registered or
failed with a delivery that has since landed or is parked for its sender (ADR 0016). A run state
is consulted only to wait for a run still going, and to catch a run that failed before
allocating anything.

**The whole window must lie in the past**, and that is Airflow's constraint rather than this
loop's. The scheduler refuses to schedule task instances for a run whose logical date is in
the future — `Logical date is in future` — and it refuses in silence: the run sits `running`
with no task instance ever queued, for ever. Since a run's logical date *is* the simulated day
here, a simulated day ahead of the real clock cannot be ingested at all.

`docs/project_state.md` already recorded that the simulated clock is free to run ahead of the
real one and that the anchor should be chosen deliberately. This is the second thing that
depends on the choice, after the FX feed, and it is the stricter of the two: choose the anchor
so that the last day of the backfill is on or before today. The check below fails fast and
says so, because the failure it prevents is a hang rather than an error.

It runs on the host, because the tick is a host package. It drives Airflow through its REST API
(`scripts/airflow_api.py`), and reads the registry by running a script inside the scheduler,
where the warehouse volume is.

**The day, in order, since specification 006.**

1. Tick the source to the day.
2. Deliver what third parties send that day: the processor's clearing files due on it
   (`generator.settlement`) and the sanctions publisher's list (`generator.sanctions`). Both are
   simulation, and both are idempotent, so a resumed day delivers the same bytes again.
3. `ingest_reference_data`, then `ingest_core_banking`, in that order, for the reason above.
4. The four feeds, together: `ingest_card_settlements`, `ingest_fx_rates`, and on the days
   their cadence falls, `ingest_sanctions_list` on Mondays and `ingest_macro_series` on the
   15th. They depend on nothing but core banking having run — the clearing file's card
   references are first seen by the core extraction, so they resolve to tokens the vault
   already holds — and on nothing in each other, so they are triggered together and waited
   for together.

A feed batch that fails on breaking drift halts the loop as a failed core batch does, naming
the batch and the reason, because a person resolves it with a contract commit; its delivery is
parked too, but it waits on a person, not on the sender. A delivery refused as structurally
malformed or for a declaration conflict does not halt it: the sender resolves those by sending
again, the delivery is parked (ADR 0016) and the run stays failed in Airflow, so the loop
reports it, on this run and on every later one that passes the day, and goes on.

**Bronze is built once, at the end.** `transform_bronze` is scheduled on every registration,
and during a backfill that is about four builds a day, each holding the warehouse file.
Measured over sixty-one days, it slowed the backfill by more than the 15 per cent specification
007 allows, so the loop pauses the DAG while it runs, restores the pause state it found whatever
happens, triggers one build at the end, and reports the window complete only when that build has
succeeded. Daily operation keeps the schedule. The state it found is recorded in an Airflow
Variable before the pause, so a loop killed before it could restore leaves the record behind and
the next invocation restores the state the DAG had before either of them, not the pause the
killed one left. A DAG that was paused before the loop stays paused, and bronze is not built.

**One day demonstrates the sensor waiting.** `--defer-demo DATE` triggers the settlement DAG on
that day *before* the clearing file is delivered, watches its sensor task until Airflow reports
it `deferred`, and only then delivers the file. A sensor that finds its file on the first look
never waits, and proves nothing about waiting; this is the day it does. What was observed is
written to `data/acceptance/deferral.json`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402
from airflow_api import Client  # noqa: E402

REFERENCE_DAG = "ingest_reference_data"
CORE_DAG = "ingest_core_banking"
SETTLEMENT_DAG = "ingest_card_settlements"
FX_DAG = "ingest_fx_rates"
SANCTIONS_DAG = "ingest_sanctions_list"
MACRO_DAG = "ingest_macro_series"

# In the backfill the sensor waits seconds, not the deployed four hours: a day whose file is
# late would otherwise hold the loop for the whole grace period.
SETTLEMENT_CONF = {"sensor_timeout_seconds": 40, "sensor_poke_seconds": 3}
DEMO_CONF = {"sensor_timeout_seconds": 300, "sensor_poke_seconds": 3}
EVIDENCE = ROOT / "data" / "acceptance"

# Sixteen `core` entities and twenty-nine `ref` entities (spec 005 criterion 2).
EXPECTED = {CORE_DAG: 16, REFERENCE_DAG: 29}

TERMINAL = {"success", "failed"}
# Through the REST API a poll costs milliseconds, so the loop notices a finished run within a
# second; through `docker compose exec airflow dags list-runs` it cost 2.5 seconds a call.
POLL_SECONDS = 1
RUN_TIMEOUT_SECONDS = 900

RESOLUTION = (
    "A breaking drift is resolved by a person, not by this loop: move the entity's contract "
    "to `contracts/<source>/history/<entity>.v<N>.yml`, write the next version describing the "
    "source's new shape with `in_force_from` set to this day, commit both, and run "
    "`make backfill` again. It resumes from this day, and days before it keep validating "
    "against the version that was in force then."
)


def run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=capture, text=True, cwd=ROOT, check=False)


def in_container(script: str, *arguments: str) -> str:
    """Run one of this repository's scripts inside the scheduler and return its stdout."""
    completed = run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "airflow-scheduler",
            "bash",
            "-c",
            " ".join([f"python /opt/airflow/scripts/{script}", *arguments]),
        ]
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"backfill: {script} failed inside the container")
    return completed.stdout


_API: Client | None = None


def api() -> Client:
    global _API  # noqa: PLW0603 - one authenticated client per invocation
    if _API is None:
        _API = Client()
    return _API


def clear(dag_id: str, day: dt.date, run_id: str) -> None:
    """Re-run an existing run on the DAG's latest version, through the REST API.

    Not `airflow tasks clear`: a run it clears keeps the DAG version it was created with, so a
    resume after a code fix re-ran the old code, and after a DAG change it put three of four
    tasks on the new version and one on the old (measured 2026-09-28). The REST clear with
    `run_on_latest_version`, experimental in Airflow 3.3.2, re-runs every task on the latest
    version, and `wait_for` confirms it did.
    """
    print(f"backfill: clearing the existing run of {dag_id} for {day} onto its latest version")
    api().clear_onto_latest_version(dag_id, run_id)
    CLEARED[run_id] = dag_id


# Runs this invocation cleared, whose task instances must all end on the latest DAG version.
CLEARED: dict[str, str] = {}


def simulation_state() -> tuple[dt.date, dt.date, int]:
    """The anchor, where the simulation has reached, and how many ticks have run."""
    rows = db.executor()(
        "select anchor_date, simulated_date, tick_sequence from platform.simulation_state"
    )
    if not rows:
        raise SystemExit("backfill: the source has no simulation state; run `make seed` first")
    anchor, simulated, sequence = rows[0]
    return dt.date.fromisoformat(anchor), dt.date.fromisoformat(simulated), int(sequence)


def interval_state(day: dt.date, expected: int) -> dict:
    payload = in_container(
        "ingest_state.py", "--interval", day.isoformat(), "--expected", str(expected)
    )
    return json.loads(payload.strip().splitlines()[-1])


def _logical(day: dt.date) -> str:
    return f"{day.isoformat()}T00:00:00+00:00"


def existing_run(dag_id: str, day: dt.date) -> dict | None:
    wanted = dt.datetime.fromisoformat(_logical(day))
    for row in api().runs(dag_id):
        stamp = row["logical_date"]
        if stamp and dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) == wanted:
            return row
    return None


def feeds_due(day: dt.date) -> list[str]:
    """The feeds whose cadence falls on a day, in the order they are triggered."""
    due = [SETTLEMENT_DAG, FX_DAG]
    if day.weekday() == 0:
        due.append(SANCTIONS_DAG)
    if day.day == 15:
        due.append(MACRO_DAG)
    return due


def start_run(dag_id: str, day: dt.date, conf: dict | None = None) -> str:
    """Start one run for one logical date without waiting, clearing it if it already exists."""
    held = existing_run(dag_id, day)
    if held is not None:
        clear(dag_id, day, held["run_id"])
        return held["run_id"]
    return api().trigger(dag_id, _logical(day), conf)


def task_state(dag_id: str, task_id: str, run_id: str) -> str:
    return api().task_state(dag_id, run_id, task_id)


def deliver(day: dt.date, *, settlement_files: bool = True) -> None:
    """What third parties send on a day. Simulation, and idempotent."""
    if settlement_files:
        completed = run(
            [sys.executable, "-m", "generator.settlement", "--date", day.isoformat()], capture=False
        )
        if completed.returncode != 0:
            raise SystemExit(f"backfill: delivering the clearing files for {day} failed")
    completed = run(
        [sys.executable, "-m", "generator.sanctions", "--date", day.isoformat()], capture=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"backfill: publishing the sanctions list for {day} failed")


# Refusals a sender resolves by sending again, not a person by committing a contract.
SENDER_RESOLVES = ("structurally malformed", "declaration conflict")


def sender_resolves(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith(SENDER_RESOLVES)


def feed_batches(run_id: str) -> list[dict]:
    """Every batch a feed run allocated, over all its tries, with each failed delivery's fate."""
    payload = in_container("feed_state.py", "--run-id", run_id)
    return json.loads(payload.strip().splitlines()[-1])["batches"]


# Reference data and core banking write into one registry interval; the schema tells them apart.
RELATIONAL = {REFERENCE_DAG: "ref", CORE_DAG: "core"}


def relational_complete(batches: list[dict]) -> dict[str, bool]:
    """Whether reference data and core banking each registered every entity for the day.

    An entity is done when any of its batches registered: a failed batch beside a registered
    one is a drift that was resolved, not one outstanding.
    """
    registered = {(b["source_schema"], b["entity"]) for b in batches if b["status"] == "registered"}
    return {
        dag: sum(1 for schema_, _ in registered if schema_ == schema) >= EXPECTED[dag]
        for dag, schema in RELATIONAL.items()
    }


@dataclass(frozen=True)
class FeedDay:
    """One feed's standing for one day, as the registry records it."""

    complete: bool
    parked: tuple[dict, ...] = ()
    failures: tuple[dict, ...] = ()


def judge_feed(dag_id: str, run_state_: str | None, batches: list[dict]) -> FeedDay:
    """Whether a feed is complete for a day, from the batches its run allocated.

    Complete when every failed batch is accounted for: its delivery has since landed, or it is
    parked and waits on its sender, or, for an API feed with no delivery, a later try of the run
    registered the same entity and interval. A run that never happened is incomplete, and so is
    one that failed without a failed batch to say why. Parked for breaking drift is a failure:
    a person resolves it, with a contract commit, and the loop halts for it.
    """
    if run_state_ is None:
        return FeedDay(complete=False)
    registered = {
        (b["entity"], b["interval_start"]) for b in batches if b["status"] == "registered"
    }
    parked: list[dict] = []
    failures: list[dict] = []
    for batch in batches:
        if batch["status"] != "failed":
            continue
        delivery = batch.get("delivery")
        if delivery == "landed":
            continue
        if delivery == "parked" and sender_resolves(batch["failure_reason"]):
            parked.append(batch)
        elif delivery is None and (batch["entity"], batch["interval_start"]) in registered:
            continue
        else:
            failures.append(batch)
    if run_state_ == "failed" and not any(b["status"] == "failed" for b in batches):
        reason = "the run failed before any batch failed; see its tasks"
        failures.append({"entity": dag_id, "failure_reason": reason})
    return FeedDay(complete=not failures, parked=tuple(parked), failures=tuple(failures))


def feed_day(dag_id: str, day: dt.date) -> FeedDay:
    held = existing_run(dag_id, day)
    if held is None:
        return FeedDay(complete=False)
    return judge_feed(dag_id, held["state"], feed_batches(held["run_id"]))


def report_parked(day: dt.date, feeds: dict[str, FeedDay]) -> None:
    for verdict in feeds.values():
        for batch in verdict.parked:
            print(
                f"backfill: {day}: {batch['entity']} parked, for the sender to resolve: "
                f"{batch['failure_reason']}",
                flush=True,
            )


def deferral_demo(day: dt.date) -> str:
    """Trigger the settlement DAG before its file exists, watch it defer, then deliver the file."""
    run_id = start_run(SETTLEMENT_DAG, day, DEMO_CONF)
    observed: list[dict] = []
    deadline = time.monotonic() + 240
    deferred_at = None
    while time.monotonic() < deadline:
        state = task_state(SETTLEMENT_DAG, "wait_for_file", run_id)
        stamp = dt.datetime.now(dt.UTC).isoformat()
        if not observed or observed[-1]["state"] != state:
            observed.append({"at": stamp, "state": state})
            print(f"backfill: {day}: wait_for_file is {state}", flush=True)
        if state == "deferred":
            deferred_at = stamp
            break
        if state in ("success", "failed", "skipped", "upstream_failed"):
            break
        time.sleep(1)
    if deferred_at is None:
        raise SystemExit(
            f"backfill: the settlement sensor for {day} never deferred; observed {observed}"
        )
    print(f"backfill: {day}: the sensor is deferred; delivering the clearing file now", flush=True)
    deliver(day, settlement_files=True)
    delivered_at = dt.datetime.now(dt.UTC).isoformat()
    final = wait_for(SETTLEMENT_DAG, run_id)
    observed.append({"at": dt.datetime.now(dt.UTC).isoformat(),
                     "state": task_state(SETTLEMENT_DAG, "wait_for_file", run_id)})  # fmt: skip
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "deferral.json").write_text(
        json.dumps(
            {"day": day.isoformat(), "run_id": run_id, "deferred_at": deferred_at,
             "file_delivered_at": delivered_at, "run_state": final, "sensor_states": observed},
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )  # fmt: skip
    print(f"backfill: {day}: the demonstration run ended {final}", flush=True)
    return run_id


def trigger_and_wait(dag_id: str, day: dt.date) -> str:
    """Start one run for one logical date and wait for it to reach a terminal state.

    Two Airflow constraints shape this and both were measured rather than assumed.

    A run created for a logical date, through the REST API, rather than `airflow backfill
    create`:
    `backfill create` refuses a DAG whose schedule is not periodic —
    `DagNonPeriodicScheduleException` — and these DAGs are deliberately unscheduled, because
    the source's clock is simulated and a wall-clock schedule has no meaning against it. The
    phases read `logical_date` and never the data interval, which a manual run fills with the
    wall clock at trigger time.

    **A dag has at most one run per logical date.** `dag_run` carries a unique constraint on
    `(dag_id, logical_date)`, so a second trigger for a day that already ran fails on it. Re-
    running an interval therefore means **clearing** the existing run, not creating another —
    which is exactly why the registry's sequence rule reads its own status and not the run id:
    a cleared run keeps its id and increments `try_number`, so it is indistinguishable from a
    retry by run identity and the registry has to be the thing that tells them apart.
    """
    held = existing_run(dag_id, day)
    if held is not None:
        clear(dag_id, day, held["run_id"])
        return wait_for(dag_id, held["run_id"])

    return wait_for(dag_id, api().trigger(dag_id, _logical(day)))


def wait_for(dag_id: str, run_id: str) -> str:
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = api().run_state(dag_id, run_id)
        if state in TERMINAL:
            if run_id in CLEARED:
                on_latest_version(dag_id, run_id)
            return state
        time.sleep(POLL_SECONDS)
    raise SystemExit(
        f"backfill: {dag_id} run {run_id} did not finish within {RUN_TIMEOUT_SECONDS}s"
    )


def on_latest_version(dag_id: str, run_id: str) -> None:
    """Refuse to go on if a cleared run ran any task on a DAG version but the latest."""
    latest = api().latest_version(dag_id)
    versions = api().task_versions(dag_id, run_id)
    stale = {task: version for task, version in versions.items() if version != latest}
    if stale:
        raise SystemExit(
            f"backfill: the cleared run {run_id} of {dag_id} ran {len(stale)} task(s) on a "
            f"version other than the latest, {latest}: {stale}"
        )
    print(f"backfill: {dag_id} {run_id}: all {len(versions)} task(s) ran on version {latest}")


def tick_one_day(day: dt.date, simulated: dt.date) -> None:
    """Advance the simulation by exactly one day, or refuse.

    **One day, never a catch-up.** `make tick-to` will happily run forty-four ticks in one
    call, and using it here would break the invariant the whole loop exists to preserve: a
    later tick re-stamps a row an earlier one wrote, so ticking in bulk and then extracting
    reads a window that no longer holds what the log says changed. It would not fail; it would
    produce a reconciliation that quietly disagrees.

    **The check is two-sided, and the second side was missing for an hour.** At the moment of
    ingesting day D the simulation must be at D, already ticked, or at D-1, about to be. A
    simulation *ahead* of D breaks the same invariant as one behind it and was not caught by
    the first version of this guard, because "is it behind" is the obvious question and only
    half the property. It was found the way these things are: by reseeding the source while a
    backfill's state was still in the warehouse, and watching the loop resume happily against
    a source that no longer had the history the registry was recording.
    """
    if simulated == day:
        return
    if simulated != day - dt.timedelta(days=1):
        distance = (day - simulated).days
        behind = (
            f"{distance} tick(s) short of it" if distance > 0 else f"{-distance} day(s) past it"
        )
        raise SystemExit(
            f"backfill: the simulation is at {simulated} and the next day to ingest is {day}, "
            f"which is {behind}. Either way the interleaving the reconciliation depends on is "
            "already broken: a later tick re-stamps a row an earlier one wrote, so extracting "
            "day D once the simulation has moved past it reads a window that no longer holds "
            "what the tick log says changed on D, and catching up in bulk first produces the "
            "same thing. The simulation and the registry disagree about what has happened; "
            "reseed and clear the warehouse together, or pick a range that starts where the "
            "simulation is. "
            "The usual cause is something that reseeds the source while a backfill's state is "
            "still in the warehouse. `make test-integration` is one: its generator tests seed "
            "the database, so running it against a loaded acceptance run destroys that run."
        )
    completed = run(
        [sys.executable, "-m", "generator.mutation", "--date", day.isoformat()], capture=False
    )
    if completed.returncode != 0:
        raise SystemExit(f"backfill: the tick to {day} failed")


TRANSFORM_DAG = "transform_bronze"
ACTIVE = {"queued", "running"}
# How long transform_bronze must stay idle before the backfill calls bronze built. An
# asset-triggered run is created a few seconds after the registration that triggers it.
SETTLED_SECONDS = 15


def _after(run_: dict, since: dt.datetime) -> bool:
    stamp = run_["run_after"]
    return bool(stamp) and dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= since


# Where the loop records the pause state `transform_bronze` had before it, for as long as the
# loop holds the DAG. In the stack rather than on the host, so it goes with the stack it describes.
HOLD_RECORD = "backfill_transform_bronze_paused_before"


def hold_transforms() -> bool:
    """Pause `transform_bronze` for the loop; returns the pause state to restore after it.

    Specification 007's rule, applied by measurement: building bronze after every registration
    slowed the sixty-one-day backfill by more than 15 per cent of its wall time, so the backfill
    builds it once, at the end. Daily operation is unchanged: the DAG stays scheduled on every
    registration, and is paused only while a backfill runs. An event that arrives while a DAG is
    paused queues nothing, so no backlog of builds follows the restore.

    **The prior state is recorded before the pause, and restored explicitly.** Kept only in this
    process, it is lost when the process is killed before its `finally` runs, and the next
    invocation finds the DAG paused by the killed one and takes that pause for the operator's: it
    would leave the DAG paused for good and never build bronze. A record left behind is that
    case, and its state, not the DAG's present one, is what is restored.
    """
    recorded = api().get_variable(HOLD_RECORD)
    if recorded is None:
        prior = api().is_paused(TRANSFORM_DAG)
        api().set_variable(HOLD_RECORD, json.dumps(prior))
    else:
        prior = bool(json.loads(recorded))
        print(
            f"backfill: an earlier backfill ended without restoring {TRANSFORM_DAG}; it was "
            f"{'paused' if prior else 'unpaused'} before that one, and that is what is restored",
            flush=True,
        )
    if prior:
        print(f"backfill: {TRANSFORM_DAG} was paused before the loop and stays paused", flush=True)
    else:
        api().set_paused(TRANSFORM_DAG, True)
        print(f"backfill: {TRANSFORM_DAG} paused while the loop runs; it builds once at the end")
    return prior


def release_transforms(prior: bool) -> None:
    """Restore the pause state `hold_transforms` recorded, whatever happened in between."""
    api().set_paused(TRANSFORM_DAG, prior)
    api().delete_variable(HOLD_RECORD)
    print(f"backfill: {TRANSFORM_DAG} {'left paused' if prior else 'unpaused'}", flush=True)


def build_once() -> None:
    """Trigger the one build of what the backfill landed, and wait for it."""
    run_id = api().trigger(TRANSFORM_DAG, None)
    print(f"backfill: {TRANSFORM_DAG}: building bronze once, {run_id}", flush=True)
    wait_for(TRANSFORM_DAG, run_id)


def settle_transforms(since: dt.datetime) -> int:
    """Wait for the bronze builds this backfill's registrations triggered, and report them.

    Every registration triggers `transform_bronze`, which holds the warehouse file for a whole
    `dbt build`. Measured in the stack job: the last day's build still held it when the next
    step opened the warehouse, and that step gave up. So the backfill is complete when bronze
    is built from what it landed: when the DAG has had no queued or running run for a while.
    A failed last build is a bronze test failing on the data, and fails the backfill.
    """
    idle_since = None
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        runs = [r for r in api().runs(TRANSFORM_DAG) if _after(r, since)]
        if any(r["state"] in ACTIVE for r in runs):
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= SETTLED_SECONDS:
            states = [r["state"] for r in runs]
            last = f"is {states[-1]}" if states else "never ran"
            print(
                f"backfill: {TRANSFORM_DAG}: {len(runs)} build(s) during the backfill, "
                f"{states.count('success')} succeeded, {states.count('failed')} failed; "
                f"the last {last}",
                flush=True,
            )
            if states and states[-1] == "failed":
                print(
                    f"backfill: the last {TRANSFORM_DAG} build failed: a bronze test found the "
                    "data wrong, and its log names the model and the test",
                    flush=True,
                )
                return 1
            return 0
        time.sleep(POLL_SECONDS)
    raise SystemExit(f"backfill: {TRANSFORM_DAG} did not settle within {RUN_TIMEOUT_SECONDS}s")


def halt(day: dt.date, failed: list[dict]) -> int:
    print(
        f"\nbackfill: halted at {day}. {len(failed)} batch(es) failed and were not registered.",
        flush=True,
    )
    for batch in failed:
        print(f"  {batch['entity']}: {batch['failure_reason']}", flush=True)
    print(f"\n{RESOLUTION}")
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--defer-demo", dest="defer_demo", default=None)
    arguments = parser.parse_args(argv)
    demo = dt.date.fromisoformat(arguments.defer_demo) if arguments.defer_demo else None

    start = dt.date.fromisoformat(arguments.start)
    end = dt.date.fromisoformat(arguments.end)
    if end < start:
        raise SystemExit("backfill: TO is before FROM")

    today = dt.datetime.now(dt.UTC).date()
    if end > today:
        raise SystemExit(
            f"backfill: TO is {end}, which is after today ({today}). Airflow will not schedule "
            "a run whose logical date is in the future, and it declines in silence: the run "
            "stays `running` with no task instance ever queued. A run's logical date is the "
            "simulated day here, so seed with an anchor far enough back that the last day of "
            "the backfill is on or before today."
        )

    db.require_stack()
    anchor, _simulated, _sequence = simulation_state()
    started_at = dt.datetime.now(dt.UTC)

    prior = hold_transforms()
    try:
        code = ingest_days(start, end, anchor, demo)
    finally:
        release_transforms(prior)
    if code != 0:
        return code
    if prior:
        # A paused DAG does not run a triggered run, so there is nothing to build into.
        print(f"backfill: bronze is not built: {TRANSFORM_DAG} is paused", flush=True)
        return 0
    build_once()
    return settle_transforms(started_at)


def ingest_days(start: dt.date, end: dt.date, anchor: dt.date, demo: dt.date | None) -> int:
    """Tick, deliver and ingest each day of the window; 0, or the halt's code."""
    total = (end - start).days + 1
    skipped = 0
    processed = 0
    day = start
    while day <= end:
        # Both DAGs write into one registry, so one interval's registered set covers both and
        # a day is complete when all forty-five entities are in it.
        state = interval_state(day, EXPECTED[CORE_DAG] + EXPECTED[REFERENCE_DAG])
        relational = all(relational_complete(state["batches"]).values())
        feeds = {dag: feed_day(dag, day) for dag in feeds_due(day)}
        report_parked(day, feeds)
        pending = [dag for dag, verdict in feeds.items() if not verdict.complete]
        if relational and not pending:
            skipped += 1
            day += dt.timedelta(days=1)
            continue

        # The tick for a day past the anchor, and only if the simulation has not reached it.
        # On a resume the tick for the failed day has already run and must not run twice.
        _anchor, simulated, _sequence = simulation_state()
        if day > anchor:
            if simulated < day:
                print(f"backfill: ticking the source to {day}", flush=True)
            tick_one_day(day, simulated)

        demo_today = demo == day and SETTLEMENT_DAG in pending
        deliver(day, settlement_files=not demo_today)

        if not relational:
            print(f"backfill: {day}: {REFERENCE_DAG}", flush=True)
            trigger_and_wait(REFERENCE_DAG, day)
            print(f"backfill: {day}: {CORE_DAG}", flush=True)
            trigger_and_wait(CORE_DAG, day)

            state = interval_state(day, EXPECTED[CORE_DAG] + EXPECTED[REFERENCE_DAG])
            if state["failed"]:
                return halt(day, state["failed"])

        started: dict[str, str] = {}
        if demo_today:
            print(f"backfill: {day}: {SETTLEMENT_DAG}, triggered before its file", flush=True)
            started[SETTLEMENT_DAG] = deferral_demo(day)
        for dag in pending:
            if dag in started:
                continue
            conf = SETTLEMENT_CONF if dag == SETTLEMENT_DAG else None
            print(f"backfill: {day}: {dag}", flush=True)
            started[dag] = start_run(dag, day, conf)
        verdicts = {}
        for dag, run_id in started.items():
            verdicts[dag] = judge_feed(dag, wait_for(dag, run_id), feed_batches(run_id))
        report_parked(day, verdicts)
        failed_feeds = [batch for verdict in verdicts.values() for batch in verdict.failures]
        if failed_feeds:
            return halt(day, failed_feeds)

        processed += 1
        day += dt.timedelta(days=1)

    print(
        f"\nbackfill: {start} to {end} complete: {processed} day(s) "
        f"processed, {skipped} already complete, {total} in range",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
