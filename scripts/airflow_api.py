"""The local Airflow REST API, from the host.

The backfill loop drives every DAG run through it: triggering a run for a logical date, listing a
DAG's runs, and reading a run's and a task's state. Specification 007 moved the loop here from
`docker compose exec airflow ...`, which cost 2.5 seconds a call to start the CLI in the
container, polled every three seconds, and took about 42 of the 100 seconds of an ingested day
on the fourth acceptance run's stack; the stack job's week came within ninety seconds of its
timeout. A call here costs milliseconds.

And one thing the CLI cannot do: clearing a run onto the DAG's latest version.
`airflow tasks clear` re-runs a cleared run on the version it was created with; measured on
2026-09-28 on the stack after the third acceptance run, after a DAG file changed it re-ran three
of `ops_stack_healthcheck`'s four tasks on the new version and one on the old. The REST clear
with `run_on_latest_version` re-ran all four on the new one. The parameter is marked
experimental in Airflow 3.3.2.

Credentials come from `.env`, the admin account `make up` provisions, and are never printed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from env_file import read_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class AirflowApiError(RuntimeError):
    """The API refused a request; carries the method, path and status, never a credential."""


class Client:
    def __init__(self, env_file: Path = ENV_FILE) -> None:
        env = read_dotenv(env_file)
        self.base = f"http://localhost:{env.get('AIRFLOW_WEB_PORT', '8080')}"
        self._credentials = {
            "username": env["AIRFLOW_ADMIN_USER"],
            "password": env["AIRFLOW_ADMIN_PASSWORD"],
        }
        self._token: str | None = None

    def call(self, method: str, path: str, body: dict | None = None, *, _retried: bool = False):
        if self._token is None and path != "/auth/token":
            self._token = self.call("POST", "/auth/token", self._credentials)["access_token"]
        request = urllib.request.Request(  # noqa: S310 - the local stack's own API
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
        )
        request.add_header("Content-Type", "application/json")
        if self._token and path != "/auth/token":
            request.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            # A token outlives most backfills; one that expired is taken again, once.
            if error.code == 401 and path != "/auth/token" and not _retried:
                self._token = None
                return self.call(method, path, body, _retried=True)
            raise AirflowApiError(f"{method} {path}: HTTP {error.code}") from None
        return json.loads(text) if text else None

    @staticmethod
    def _run(row: dict) -> dict:
        """A run as the loop uses it, in the CLI's field names."""
        return {
            "run_id": row["dag_run_id"],
            "logical_date": row.get("logical_date"),
            "run_after": row.get("run_after"),
            "state": row.get("state"),
        }

    def runs(self, dag_id: str) -> list[dict]:
        """Every run of a DAG, oldest first."""
        out, offset = [], 0
        while True:
            reply = self.call(
                "GET", f"/api/v2/dags/{dag_id}/dagRuns?limit=100&offset={offset}&order_by=run_after"
            )
            batch = reply["dag_runs"]
            out.extend(self._run(row) for row in batch)
            offset += len(batch)
            if not batch or offset >= reply.get("total_entries", 0):
                return out

    def trigger(self, dag_id: str, logical_date: str, conf: dict | None = None) -> str:
        """Create one run for a logical date; returns its run id."""
        body = {"logical_date": logical_date, "conf": conf or {}}
        return self.call("POST", f"/api/v2/dags/{dag_id}/dagRuns", body)["dag_run_id"]

    def run_state(self, dag_id: str, run_id: str) -> str | None:
        return self.call("GET", f"/api/v2/dags/{dag_id}/dagRuns/{run_id}").get("state")

    def task_state(self, dag_id: str, run_id: str, task_id: str) -> str:
        reply = self.call("GET", f"/api/v2/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task_id}")
        return reply.get("state") or "none"

    def clear_onto_latest_version(self, dag_id: str, run_id: str) -> None:
        """Clear every task instance of one run, and re-run it on the DAG's latest version."""
        self.call(
            "POST",
            f"/api/v2/dags/{dag_id}/clearTaskInstances",
            {
                "dag_run_id": run_id,
                "dry_run": False,
                "only_failed": False,
                "reset_dag_runs": True,
                "run_on_latest_version": True,
            },
        )

    def task_versions(self, dag_id: str, run_id: str) -> dict[str, int | None]:
        """The DAG version each task instance of a run last ran on, by task id.

        A mapped instance its re-run did not expand to is `removed` and never runs again, so it
        keeps the version of the try that created it; it is left out.
        """
        reply = self.call("GET", f"/api/v2/dags/{dag_id}/dagRuns/{run_id}/taskInstances?limit=1000")
        out: dict[str, int | None] = {}
        for instance in reply["task_instances"]:
            if instance.get("state") == "removed":
                continue
            version = (instance.get("dag_version") or {}).get("version_number")
            key = instance["task_id"]
            if instance.get("map_index", -1) >= 0:
                key = f"{key}[{instance['map_index']}]"
            out[key] = version
        return out

    def latest_version(self, dag_id: str) -> int:
        reply = self.call(
            "GET", f"/api/v2/dags/{dag_id}/dagVersions?order_by=-version_number&limit=1"
        )
        return max(v["version_number"] for v in reply["dag_versions"])
