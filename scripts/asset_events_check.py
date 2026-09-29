"""Asset events equal registered batches, run by run (specification 006, amended).

The register step emits one asset event per batch it registered, carrying the batch id, and
nothing for a failed batch. This compares, for every ingestion run Airflow has, the batch ids its
asset events carry with the batches the registry says that run registered. Any difference is
printed and fails the check.

Events come from Airflow's metadata database and the registry from the warehouse; neither holds
a value that needs protecting. Runs on the host, stack up, and must not run while a backfill
writes the warehouse.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from env_file import read_dotenv  # noqa: E402

EVENTS_SQL = """
select e.source_run_id, a.name, coalesce(e.extra::jsonb ->> 'batch_id', ''),
       coalesce(e.extra::jsonb ->> 'empty_reason', '')
  from asset_event e join asset a on a.id = e.asset_id
 where e.source_dag_id like 'ingest\\_%'
"""

RUNS_SQL = "select dag_id, run_id, state from dag_run where dag_id like 'ingest\\_%'"

REGISTERED = """
import duckdb, json
c = duckdb.connect('/opt/warehouse/nordbank.duckdb', read_only=True)
rows = c.execute(
    "select triggering_run_id, batch_id, coalesce(empty_reason, '') from ops.batch_registry "
    "where status = 'registered'"
).fetchall()
print(json.dumps(rows))
"""


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, cwd=ROOT, check=False)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"asset-events-check: {' '.join(command[:5])} failed")
    return completed.stdout


def _psql(sql: str) -> list[list[str]]:
    user = read_dotenv(ROOT / ".env")["POSTGRES_AIRFLOW_USER"]
    out = _run(
        ["docker", "compose", "exec", "-T", "postgres-airflow", "psql", "-U", user, "-d",
         "airflow", "-At", "-F", "\t", "-c", sql]
    )  # fmt: skip
    return [line.split("\t") for line in out.splitlines() if line]


def main() -> int:
    runs = _psql(RUNS_SQL)
    events: dict[str, set[str]] = collections.defaultdict(set)
    empty_events: dict[str, set[str]] = collections.defaultdict(set)
    unlabelled = 0
    emitted = collections.Counter()
    for run_id, _asset, batch_id, empty_reason in _psql(EVENTS_SQL):
        emitted[run_id] += 1
        if not batch_id:
            unlabelled += 1
            continue
        events[run_id].add(batch_id)
        if empty_reason:
            empty_events[run_id].add(batch_id)

    registered_rows = json.loads(
        _run(["docker", "compose", "exec", "-T", "airflow-scheduler", "python", "-c", REGISTERED])
    )
    registered: dict[str, set[str]] = collections.defaultdict(set)
    empty_registered: dict[str, set[str]] = collections.defaultdict(set)
    for run_id, batch_id, empty_reason in registered_rows:
        registered[run_id].add(batch_id)
        if empty_reason:
            empty_registered[run_id].add(batch_id)

    by_dag = collections.Counter()
    nothing_registered = nothing_emitted = 0
    mismatches = []
    for dag_id, run_id, _state in runs:
        by_dag[dag_id] += 1
        if not registered[run_id]:
            nothing_registered += 1
            nothing_emitted += int(not emitted[run_id])
        differs = events[run_id] != registered[run_id] or emitted[run_id] != len(events[run_id])
        if differs or empty_events[run_id] != empty_registered[run_id]:
            mismatches.append((dag_id, run_id, events[run_id], registered[run_id]))

    total_events = sum(len(v) for v in events.values())
    total_registered = sum(len(v) for v in registered.values())
    print(f"asset-events-check: {len(runs)} ingestion run(s): {dict(sorted(by_dag.items()))}")
    print(
        f"asset-events-check: {total_events} event(s) carrying a batch id, {total_registered} "
        f"registered batch(es); {unlabelled} event(s) with no batch id"
    )
    print(
        f"asset-events-check: {nothing_registered} run(s) registered nothing, and "
        f"{nothing_emitted} of them emitted nothing"
    )
    print(
        f"asset-events-check: {sum(len(v) for v in empty_registered.values())} registered empty "
        f"batch(es), {sum(len(v) for v in empty_events.values())} event(s) carrying empty_reason"
    )
    if mismatches or unlabelled:
        for dag_id, run_id, labelled, held in mismatches[:40]:
            print(
                f"  {dag_id} {run_id}: {emitted[run_id]} event(s); batch ids "
                f"{sorted(labelled - held)} not registered, {sorted(held - labelled)} "
                "registered without an event"
            )
        print(f"asset-events-check: {len(mismatches)} run(s) differ")
        return 1
    print("asset-events-check: every run's asset events are exactly its registered batches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
