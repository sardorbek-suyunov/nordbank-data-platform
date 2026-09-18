"""Per-component health probes for the running stack.

Exit codes: 0 when every component passes, 1 when any fails. A warehouse held by another
process reports `busy` rather than `fail`, because a held lock is the single-access design
working (ADR 0002). `STRICT=1` treats `busy` as a failure, which is what CI uses so its runs
stay deterministic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from env_file import read_dotenv

PASS, FAIL, BUSY = "pass", "fail", "busy"
CONFIG: dict[str, str] = {}


def env(name: str, default: str = "") -> str:
    return CONFIG.get(name) or os.environ.get(name, default)


def run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


def probe_database(service: str, user: str, database: str) -> tuple[str, str]:
    code, output = run(
        ["docker", "compose", "exec", "-T", service, "pg_isready", "-U", user, "-d", database]
    )
    detail = output.splitlines()[-1] if output else ""
    return (PASS if code == 0 else FAIL), detail


def probe_bucket(bucket: str) -> tuple[str, str]:
    script = (
        'mc alias set local "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" '
        f'>/dev/null && mc ls "local/{bucket}"'
    )
    code, output = run(
        [
            "docker",
            "compose",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/bin/sh",
            "minio-init",
            "-c",
            script,
        ]
    )
    if code != 0:
        return FAIL, output.splitlines()[-1] if output else "mc ls failed"
    entries = [line for line in output.splitlines() if line.strip()]
    return PASS, f"{len(entries)} top-level entries"


def probe_warehouse() -> tuple[str, str]:
    code, output = run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "airflow-scheduler",
            "python",
            "/opt/airflow/scripts/probe_warehouse.py",
        ]
    )
    detail = output.splitlines()[-1] if output else ""
    if code == 0:
        return PASS, detail
    if code == 3:
        return BUSY, detail
    return FAIL, detail


def probe_airflow_api(port: str) -> tuple[str, str]:
    url = f"http://localhost:{port}/api/v2/monitor/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return FAIL, f"{type(exc).__name__}: {exc}"

    statuses = {
        component: str(detail.get("status"))
        for component, detail in payload.items()
        if isinstance(detail, dict)
    }
    unhealthy = [name for name, status in statuses.items() if status != "healthy"]
    detail = ", ".join(f"{name}={status}" for name, status in sorted(statuses.items()))
    return (FAIL if unhealthy else PASS), detail


def main() -> int:
    CONFIG.update(read_dotenv())
    strict = os.environ.get("STRICT") == "1"

    results = [
        (
            "postgres-source",
            *probe_database(
                "postgres-source", env("POSTGRES_SOURCE_SUPERUSER"), env("POSTGRES_SOURCE_DB")
            ),
        ),
        (
            "postgres-airflow",
            *probe_database(
                "postgres-airflow", env("POSTGRES_AIRFLOW_USER"), env("POSTGRES_AIRFLOW_DB")
            ),
        ),
        ("lake-bucket", *probe_bucket(env("LAKE_BUCKET", "nordbank-lake"))),
        ("warehouse", *probe_warehouse()),
        ("airflow-api", *probe_airflow_api(env("AIRFLOW_WEB_PORT", "8080"))),
    ]

    width = max(len(name) for name, _, _ in results)
    print(f"{'component'.ljust(width)}  status  detail")
    print(f"{'-' * width}  ------  ------")
    for name, status, detail in results:
        print(f"{name.ljust(width)}  {status.ljust(6)}  {detail}")

    failures = [name for name, status, _ in results if status == FAIL]
    busy = [name for name, status, _ in results if status == BUSY]

    if busy:
        note = "counted as failure (STRICT=1)" if strict else "counted as pass"
        print(f"\nbusy: {', '.join(busy)} — {note}")
    if strict:
        failures += busy

    if failures:
        print(f"\nunhealthy: {', '.join(failures)}", file=sys.stderr)
        return 1

    print("\nall components healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
