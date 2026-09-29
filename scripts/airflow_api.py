"""The local Airflow REST API, from the host, for what the CLI cannot do.

One thing so far: clearing a run onto the DAG's latest version. `airflow tasks clear` re-runs a
cleared run on the version it was created with; measured on 2026-09-28 on the stack after the
third acceptance run, after a DAG file changed it re-ran three of `ops_stack_healthcheck`'s four
tasks on the new version and one on the old. The REST clear with `run_on_latest_version` re-ran
all four on the new one. The parameter is marked experimental in Airflow 3.3.2.

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

    def call(self, method: str, path: str, body: dict | None = None):
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
            raise AirflowApiError(f"{method} {path}: HTTP {error.code}") from None
        return json.loads(text) if text else None

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
