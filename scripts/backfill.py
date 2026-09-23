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

It runs on the host, because the tick is a host package and the Airflow CLI and the registry
are both in the container, and nothing can reach all three from one place.

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

A day is complete when core banking registered all forty-five entities for it and every feed
due that day has a successful run. A feed run that fails halts the loop exactly as a failed
core batch does, naming the batch and the reason.

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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402

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
POLL_SECONDS = 3
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


def airflow(*arguments: str) -> str:
    completed = run(["docker", "compose", "exec", "-T", "airflow-scheduler", "airflow", *arguments])
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"backfill: airflow {' '.join(arguments)} failed")
    return completed.stdout


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


def existing_run(dag_id: str, day: dt.date) -> dict | None:
    stamp = f"{day.isoformat()}T00:00:00+00:00"
    listed = json.loads(airflow("dags", "list-runs", dag_id, "-o", "json") or "[]")
    for row in listed:
        if row["logical_date"] == stamp:
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


def run_state(dag_id: str, day: dt.date) -> str | None:
    held = existing_run(dag_id, day)
    return held["state"] if held else None


def start_run(dag_id: str, day: dt.date, conf: dict | None = None) -> str:
    """Start one run for one logical date without waiting, clearing it if it already exists."""
    held = existing_run(dag_id, day)
    if held is not None:
        print(f"backfill: clearing the existing run of {dag_id} for {day}", flush=True)
        airflow(
            "tasks", "clear", dag_id, "--yes",
            "--start-date", day.isoformat(), "--end-date", day.isoformat(),
        )  # fmt: skip
        return held["run_id"]
    listed = json.loads(airflow("dags", "list-runs", dag_id, "-o", "json") or "[]")
    before = {row["run_id"] for row in listed}
    arguments = ["dags", "trigger", dag_id, "--logical-date", f"{day.isoformat()}T00:00:00+00:00"]
    if conf:
        arguments += ["--conf", json.dumps(conf)]
    airflow(*arguments)
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        runs = json.loads(airflow("dags", "list-runs", dag_id, "-o", "json") or "[]")
        fresh = [row for row in runs if row["run_id"] not in before]
        if fresh:
            return sorted(fresh, key=lambda row: row["run_after"])[-1]["run_id"]
        time.sleep(POLL_SECONDS)
    raise SystemExit(f"backfill: {dag_id} for {day} never appeared")


def task_state(dag_id: str, task_id: str, run_id: str) -> str:
    completed = run(
        ["docker", "compose", "exec", "-T", "airflow-scheduler", "airflow", "tasks", "state",
         dag_id, task_id, run_id]
    )  # fmt: skip
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else "unknown"


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


def feed_failures(run_id: str) -> list[dict]:
    payload = in_container("feed_state.py", "--run-id", run_id)
    return json.loads(payload.strip().splitlines()[-1])["failed"]


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

    `airflow dags trigger --logical-date` rather than `airflow backfill create`:
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
        print(f"backfill: clearing the existing run of {dag_id} for {day}", flush=True)
        airflow(
            "tasks",
            "clear",
            dag_id,
            "--yes",
            "--start-date",
            day.isoformat(),
            "--end-date",
            day.isoformat(),
        )
        return wait_for(dag_id, held["run_id"])

    listed = json.loads(airflow("dags", "list-runs", dag_id, "-o", "json") or "[]")
    before = {row["run_id"] for row in listed}
    airflow("dags", "trigger", dag_id, "--logical-date", f"{day.isoformat()}T00:00:00+00:00")

    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        runs = json.loads(airflow("dags", "list-runs", dag_id, "-o", "json") or "[]")
        fresh = [row for row in runs if row["run_id"] not in before]
        if fresh:
            return wait_for(dag_id, sorted(fresh, key=lambda row: row["run_after"])[-1]["run_id"])
        time.sleep(POLL_SECONDS)
    raise SystemExit(f"backfill: {dag_id} for {day} never appeared")


def wait_for(dag_id: str, run_id: str) -> str:
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for row in json.loads(airflow("dags", "list-runs", dag_id, "-o", "json") or "[]"):
            if row["run_id"] == run_id and row["state"] in TERMINAL:
                return row["state"]
        time.sleep(POLL_SECONDS)
    raise SystemExit(
        f"backfill: {dag_id} run {run_id} did not finish within {RUN_TIMEOUT_SECONDS}s"
    )


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

    total = (end - start).days + 1
    skipped = 0
    processed = 0
    day = start
    while day <= end:
        # Both DAGs write into one registry, so one interval's registered set covers both and
        # a day is complete when all forty-five entities are in it.
        state = interval_state(day, EXPECTED[CORE_DAG] + EXPECTED[REFERENCE_DAG])
        pending = [dag for dag in feeds_due(day) if run_state(dag, day) != "success"]
        if state["complete"] and not pending:
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

        if not state["complete"]:
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
        failed_feeds = []
        for dag, run_id in started.items():
            if wait_for(dag, run_id) != "success":
                failed_feeds.extend(
                    feed_failures(run_id)
                    or [
                        {
                            "entity": dag,
                            "failure_reason": (
                                "the run failed before any batch failed; see its tasks"
                            ),
                        }
                    ]
                )
        if failed_feeds:
            return halt(day, failed_feeds)

        processed += 1
        day += dt.timedelta(days=1)

    print(
        f"\nbackfill: {arguments.start} to {arguments.end} complete: {processed} day(s) "
        f"processed, {skipped} already complete, {total} in range",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
