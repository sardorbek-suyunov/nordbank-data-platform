"""Run one interval of one ingestion, outside Airflow, for development (spec 005 section 11).

"Outside Airflow" means outside the scheduler and its pools, not outside the image: this runs
inside a container like every other target that touches the warehouse, because the warehouse
file is on a named volume.

It calls the same four phase bodies the DAG calls. What it does not have is the scheduler, so
it also does not have the pool, which means it must not run while an ingestion DAG is running:
the `warehouse_access` pool governs what Airflow schedules and knows nothing about this. The
bounded-retry opener in `warehouse.connect` turns the collision into a wait and then an error
rather than a corrupt read, which is the most a process outside the pool can do.

Usage: `make extract DATE=2026-09-19 [SCHEMA=core|ref]`.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import uuid

sys.path.insert(0, "/opt/airflow/plugins")
sys.path.insert(0, "/opt/airflow")

from nordbank_ops import phases  # noqa: E402


class _Run:
    """The two attributes the phases read off a DAG run."""

    def __init__(self, logical_date: dt.datetime, run_id: str) -> None:
        self.logical_date = logical_date
        self.run_id = run_id


class _TaskInstance:
    """An XCom that lives for the length of this process.

    The phases exchange their reports through XCom, so this supplies the same interface rather
    than making the phases branch on who is calling them. A phase that behaved differently
    outside Airflow would not be the phase under test.
    """

    def __init__(self) -> None:
        self.pushed: list[dict] = []

    def xcom_push(self, key: str, value) -> None:
        self.pushed.append(value)

    def xcom_pull(self, task_ids: str, key: str):
        return list(self.pushed)


def main(argv: list[str]) -> int:
    date = os.environ.get("DATE") or (argv[0] if argv else "")
    schema = os.environ.get("SCHEMA", "core")
    if not date:
        print("extract: DATE=YYYY-MM-DD is required", file=sys.stderr)
        return 2

    logical_date = dt.datetime.fromisoformat(date).replace(tzinfo=dt.UTC)
    context = {
        "dag_run": _Run(logical_date, f"cli__{uuid.uuid4().hex[:12]}"),
        "ti": _TaskInstance(),
    }

    batches = phases.open_phase(source_schema=schema, context=context)
    failures = []
    for batch in batches:
        try:
            phases.extract_phase(source_schema=schema, batch=batch, context=context)
        except Exception as exc:  # noqa: BLE001 - one entity's failure is not the run's
            failures.append(f"{batch['entity']}: {type(exc).__name__}: {exc}")

    summary = phases.register_phase(source_schema=schema, context=context)
    for failure in failures:
        print(f"extract: {failure}", file=sys.stderr)

    try:
        phases.gate_phase(summary)
    except RuntimeError as exc:
        print(f"extract: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
