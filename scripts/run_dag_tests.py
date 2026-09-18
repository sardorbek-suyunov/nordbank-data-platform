"""Run the DAG integrity tests, and say where they ran.

Airflow does not import on every host this project is developed on, so the tests run inside the
project image where it does not. Which path was taken is printed, never inferred silently.
"""

from __future__ import annotations

import subprocess
import sys

PYTEST_ARGS = ["-q", "-m", "dags"]


def airflow_import_error() -> str | None:
    probe = subprocess.run(
        [sys.executable, "-c", "import airflow; print(airflow.__version__)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return None
    lines = [line for line in probe.stderr.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "airflow did not import"


def main() -> int:
    error = airflow_import_error()

    if error is None:
        print("test-dags: running natively, Airflow imports in this environment")
        return subprocess.run(
            [sys.executable, "-m", "pytest", *PYTEST_ARGS, "airflow/tests"], check=False
        ).returncode

    print("test-dags: running inside the project image, Airflow does not import here")
    print(f"test-dags: local import failed with: {error}")
    return subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/bin/bash",
            "airflow-scheduler",
            "-c",
            f"cd /opt/airflow && pytest {' '.join(PYTEST_ARGS)} tests",
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
