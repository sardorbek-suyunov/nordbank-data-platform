"""Trigger the health-check DAG from the CLI and verify the run from the task instance record.

Everything here is scriptable on purpose: the UI is never part of the path, so the same steps
run locally and in CI. Pool evidence comes from the `task_instance` rows, which is the record
Airflow itself scheduled against.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

from env_file import read_dotenv

CONFIG: dict[str, str] = {}
DAG_ID = "ops_stack_healthcheck"
POOL = "warehouse_access"
POOLED_TASKS = ("write_warehouse_probe", "read_warehouse_probe")
TIMEOUT_SECONDS = 300
POLL_SECONDS = 5


def run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return result.returncode, (result.stdout + result.stderr).strip()


def airflow(*args: str) -> tuple[int, str]:
    return run(["docker", "compose", "exec", "-T", "airflow-scheduler", "airflow", *args])


def psql(query: str) -> tuple[int, str]:
    return run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres-airflow",
            "psql",
            "-U",
            CONFIG["POSTGRES_AIRFLOW_USER"],
            "-d",
            CONFIG["POSTGRES_AIRFLOW_DB"],
            "-At",
            "-F",
            "|",
            "-c",
            query,
        ]
    )


def trigger(run_id: str) -> int:
    code, output = airflow("dags", "trigger", DAG_ID, "--run-id", run_id)
    print(f"trigger: {output.splitlines()[-1] if output else 'no output'}")
    return code


def wait_for_run(run_id: str) -> str:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        code, output = psql(
            f"select state from dag_run where dag_id = '{DAG_ID}' and run_id = '{run_id}'"
        )
        state = output.strip().splitlines()[-1] if code == 0 and output.strip() else ""
        if state in {"success", "failed"}:
            return state
        print(f"waiting for {run_id}: state={state or 'unknown'}", flush=True)
        time.sleep(POLL_SECONDS)
    return "timeout"


def task_instances(run_id: str) -> list[dict]:
    code, output = psql(
        "select task_id, state, coalesce(pool, '') from task_instance "
        f"where dag_id = '{DAG_ID}' and run_id = '{run_id}' order by task_id"
    )
    if code != 0:
        raise RuntimeError(f"could not read task instances: {output}")
    rows = []
    for line in output.strip().splitlines():
        if line.count("|") != 2:
            continue
        task_id, state, pool = line.split("|")
        rows.append({"task_id": task_id, "state": state, "pool": pool})
    return rows


def main() -> int:
    CONFIG.update(read_dotenv())
    run_id = f"verify_{uuid.uuid4().hex[:8]}"

    if trigger(run_id) != 0:
        return 1

    state = wait_for_run(run_id)
    rows = task_instances(run_id)

    print(f"\ndag run {run_id}: {state}")
    print(json.dumps(rows, indent=2))

    if state != "success":
        print(f"\n{DAG_ID} did not succeed: {state}", file=sys.stderr)
        return 1

    failures = []
    for task in POOLED_TASKS:
        row = next((item for item in rows if item["task_id"] == task), None)
        if row is None:
            failures.append(f"{task}: no task instance recorded")
        elif row["pool"] != POOL:
            failures.append(f"{task}: ran in pool {row['pool']!r}, expected {POOL!r}")

    non_success = [row["task_id"] for row in rows if row["state"] != "success"]
    if non_success:
        failures.append(f"tasks not successful: {non_success}")

    if failures:
        for failure in failures:
            print(f"verify: {failure}", file=sys.stderr)
        return 1

    print(f"\nverified: {DAG_ID} succeeded and both warehouse tasks ran in pool {POOL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
