"""Check that Docker is usable before anything tries to start a stack in it.

The stack is validated at a documented floor rather than at whatever a developer machine has,
so this fails loudly and with a number when the daemon has less memory than the floor.
"""

from __future__ import annotations

import json
import subprocess
import sys

MINIMUM_BYTES = 6 * 1024**3

RAISE_MEMORY = (
    "Raise the memory allocation in Docker Desktop (Settings, Resources), or on WSL2 "
    "set memory= in %UserProfile%\\.wslconfig and run `wsl --shutdown`."
)


def docker_info() -> dict:
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "the Docker daemon is not reachable. Start Docker Desktop (or the docker service) "
            f"and try again. docker said: {result.stderr.strip() or result.stdout.strip()}"
        )
    return json.loads(result.stdout)


def main() -> int:
    try:
        info = docker_info()
    except RuntimeError as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 1

    total = int(info.get("MemTotal", 0))
    gib = total / 1024**3
    floor = MINIMUM_BYTES / 1024**3

    if total < MINIMUM_BYTES:
        print(
            f"preflight: Docker reports {gib:.1f} GiB of memory, below the {floor:.0f} GiB "
            f"floor this stack is validated at. {RAISE_MEMORY}",
            file=sys.stderr,
        )
        return 1

    print(
        f"preflight: docker {info.get('ServerVersion', '?')}, {info.get('NCPU', '?')} CPUs, "
        f"{gib:.1f} GiB available, storage driver {info.get('Driver', '?')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
