"""Start the stack and block until every service is healthy.

`docker compose up --wait` does not deal with one-shot services, which exit as soon as their
work is done, so the waiting is done here: long-running services must report healthy, one-shot
services must have exited 0. On timeout the offending service is named and its last log lines
are printed, because the name alone never tells you what happened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

LONG_RUNNING = (
    "postgres-source",
    "postgres-airflow",
    "minio",
    "airflow-apiserver",
    "airflow-scheduler",
    "airflow-dag-processor",
    "airflow-triggerer",
)
ONE_SHOT = ("minio-init", "airflow-init")
TIMEOUT_SECONDS = 600
POLL_SECONDS = 5
LOG_LINES = 40


def compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=capture,
        text=True,
        check=False,
    )


def _rows(stdout: str) -> list[dict]:
    """Compose has emitted both a JSON array and one object per line; accept either."""
    stdout = stdout.strip()
    if not stdout:
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return payload if isinstance(payload, list) else [payload]


def service_states() -> dict[str, dict]:
    result = compose("ps", "--all", "--format", "json", capture=True)
    return {row.get("Service", ""): row for row in _rows(result.stdout)}


def pending(states: dict[str, dict]) -> list[str]:
    waiting = []
    for name in LONG_RUNNING:
        row = states.get(name)
        if row is None or row.get("State") != "running" or row.get("Health") != "healthy":
            waiting.append(name)
    for name in ONE_SHOT:
        row = states.get(name)
        if row is None or row.get("State") != "exited" or row.get("ExitCode", 1) != 0:
            waiting.append(name)
    return waiting


def report_failure(waiting: list[str]) -> None:
    print(f"\nstack: not ready after {TIMEOUT_SECONDS}s: {', '.join(waiting)}", file=sys.stderr)
    for name in waiting:
        print(f"\n--- last {LOG_LINES} log lines: {name} ---", file=sys.stderr)
        logs = compose("logs", "--tail", str(LOG_LINES), name, capture=True)
        print(logs.stdout or logs.stderr, file=sys.stderr)


def main() -> int:
    preflight = subprocess.run([sys.executable, "scripts/docker_preflight.py"], check=False)
    if preflight.returncode != 0:
        return preflight.returncode

    started = time.monotonic()
    # SKIP_BUILD=1 is for a caller that has already built the image, such as CI building it
    # through buildx with a layer cache. Everywhere else, build if the image is stale.
    skip_build = os.environ.get("SKIP_BUILD") == "1"
    up_args = ["up", "--detach"] if skip_build else ["up", "--build", "--detach"]
    build = compose(*up_args)
    if build.returncode != 0:
        return build.returncode

    while time.monotonic() - started < TIMEOUT_SECONDS:
        waiting = pending(service_states())
        if not waiting:
            elapsed = time.monotonic() - started
            print(f"stack: healthy in {elapsed:.0f}s")
            return 0
        print(f"stack: waiting for {', '.join(waiting)}", flush=True)
        time.sleep(POLL_SECONDS)

    report_failure(pending(service_states()))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
