"""Run dbt from its own environment, with the lake credentials it needs and no others (spec 007).

dbt is installed in `/opt/dbt`, a virtual environment of its own in the image, because its
dependencies do not resolve under Airflow 3.3.2's constraints (measured: dbt-core 1.12.5 needs
pathspec below 1.1, the constraints pin 1.1.1). So it runs as a subprocess, from
`transform_bronze` and `transform_silver` inside the `warehouse_access` pool and from
`make dbt-build` through `scripts/dbt_run.py`.

**The lake credentials.** The profile reads them from `DBT_ENV_SECRET_` variables and hands
them to DuckDB through its secrets block; this module sets those variables from the Airflow lake
connection, for the child process only. dbt refuses a `DBT_ENV_SECRET_` variable anywhere but a
profile and scrubs its value from every log line, and the value never reaches `manifest.json`.

**Where dbt writes.** The project is mounted read-only, so its target and log directories are
under `/tmp/dbt`, and dbt sends no usage statistics.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

DBT = "/opt/dbt/bin/dbt"
PROJECT_DIR = "/opt/airflow/dbt"
OUTPUT_DIR = "/tmp/dbt"
BRONZE = "path:models/bronze"
# Silver and the one seed it reads (specification 008); the tests attached to silver run with it.
SILVER = ("path:seeds", "path:models/silver")
# The asset `transform_bronze` emits when a build of bronze has succeeded, and `transform_silver`
# is scheduled on.
BRONZE_BUILT = "transform_bronze/built"


def lake_settings(login: str, password: str, extra: Mapping) -> dict[str, str]:
    """The profile's lake variables, from the Airflow lake connection's parts."""
    endpoint = extra.get("endpoint_url") or ""
    parsed = urlparse(endpoint)
    if not parsed.netloc:
        raise RuntimeError("the lake connection carries no endpoint_url with a host")
    if parsed.scheme != "http":
        # The profile's secret is written with use_ssl false; an https endpoint would need it on.
        raise RuntimeError(f"the lake endpoint is {parsed.scheme}, and the profile expects http")
    return {
        "DBT_ENV_SECRET_LAKE_KEY_ID": login,
        "DBT_ENV_SECRET_LAKE_SECRET": password,
        "LAKE_S3_ENDPOINT": parsed.netloc,
        "LAKE_S3_REGION": extra.get("region_name") or "us-east-1",
    }


def environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child process's environment: the parent's, the lake variables and dbt's paths."""
    from nordbank_ops.clients import LAKE_CONN_ID, _connection

    connection = _connection(LAKE_CONN_ID)
    env = dict(os.environ if base is None else base)
    env.update(lake_settings(connection.login, connection.password, connection.extra_dejson))
    env.update(
        {
            "DBT_PROFILES_DIR": PROJECT_DIR,
            "DBT_TARGET_PATH": f"{OUTPUT_DIR}/target",
            "DBT_LOG_PATH": f"{OUTPUT_DIR}/logs",
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        }
    )
    return env


def command(arguments: Sequence[str]) -> list[str]:
    return [DBT, *arguments, "--project-dir", PROJECT_DIR]


def run(arguments: Sequence[str]) -> int:
    """Run one dbt command, printing its output as it comes. Returns dbt's exit code."""
    process = subprocess.Popen(
        command(arguments),
        env=environment(),
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip("\n"), flush=True)
    return process.wait()


# A test is built with bronze only when every model it reads is bronze. dbt's default, eager,
# selects a test when any of its parents is selected, which takes in the silver tests that read
# bronze beside silver: on a warehouse whose silver is not built yet they fail, and they belong to
# `transform_silver`'s build, which follows. Measured: 354 tests selected eagerly, 86 of them
# silver's, and 268 cautiously, the bronze tests and one that reads bronze alone.
BRONZE_TESTS = ("--indirect-selection", "cautious")


def build_bronze() -> None:
    """`dbt build --warn-error` over the bronze models; raises when dbt fails."""
    code = run(["build", "--select", BRONZE, *BRONZE_TESTS, "--warn-error"])
    if code != 0:
        raise RuntimeError(f"dbt build over bronze exited {code}; see the output above")


def build_silver() -> None:
    """`dbt build --warn-error` over the seed and the silver models; raises when dbt fails."""
    code = run(["build", "--select", *SILVER, "--warn-error"])
    if code != 0:
        raise RuntimeError(f"dbt build over silver exited {code}; see the output above")
