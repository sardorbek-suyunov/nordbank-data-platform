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

# Sixteen `core` entities and twenty-nine `ref` entities (spec 005 criterion 2).
EXPECTED = {CORE_DAG: 16, REFERENCE_DAG: 29}

TERMINAL = {"success", "failed"}
POLL_SECONDS = 3
RUN_TIMEOUT_SECONDS = 900

RESOLUTION = (
    "A breaking drift is resolved by a person, not by this loop: bump `contract_version` in "
    "the entity's contract under `contracts/corebank/`, describe the source's new shape, "
    "commit it, and run `make backfill` again. It resumes from this day."
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


def tick_to(day: dt.date) -> None:
    completed = run(
        [sys.executable, "-m", "generator.mutation", "--to", day.isoformat()], capture=False
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
    arguments = parser.parse_args(argv)

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
        if state["complete"]:
            skipped += 1
            day += dt.timedelta(days=1)
            continue

        # The tick for a day past the anchor, and only if the simulation has not reached it.
        # On a resume the tick for the failed day has already run and must not run twice.
        _anchor, simulated, _sequence = simulation_state()
        if day > anchor and simulated < day:
            print(f"backfill: ticking the source to {day}", flush=True)
            tick_to(day)

        print(f"backfill: {day}: {REFERENCE_DAG}", flush=True)
        trigger_and_wait(REFERENCE_DAG, day)
        print(f"backfill: {day}: {CORE_DAG}", flush=True)
        trigger_and_wait(CORE_DAG, day)

        state = interval_state(day, EXPECTED[CORE_DAG] + EXPECTED[REFERENCE_DAG])
        if state["failed"]:
            return halt(day, state["failed"])

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
