"""Run the unit and DAG suites in a container with no network at all (spec 006 criterion 8).

`docker run --network none` gives the container a loopback interface and nothing else: no
route out, no DNS. The repository is mounted read-only and the whole non-integration suite
runs inside the project image, which is also the only place the DAG suite can run on a host
where Airflow does not import. A test that needed the network would fail here with a
connection or resolution error, not a skip.

The integration suite is not run here, and cannot be: it exists to talk to the stack's own
services, which need a network to be reached. Its guarantee is the other half of the proof —
every suite installs `scripts/network_guard.py`, which refuses a connection to any public
address, so the integration suite reaches the stack and nothing beyond it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "nordbank/airflow:3.3.2"


def main() -> int:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/bin/bash",
        "-e",
        "PYTHONPATH=/repo:/repo/airflow/plugins:/repo/scripts",
        "-e",
        "AIRFLOW__CORE__LOAD_EXAMPLES=False",
        "-v",
        f"{ROOT.as_posix()}:/repo:ro",
        IMAGE,
        "-c",
        # The network is proven absent before the suite runs, so a pass cannot be explained by a
        # container that had a route out after all.
        "set -e; cd /repo; "
        'python -c "import socket; s=socket.socket(); s.settimeout(3); '
        "r=s.connect_ex(('1.1.1.1', 443)); print('offline: connect to 1.1.1.1:443 ->', r); "
        'import sys; sys.exit(0 if r != 0 else 1)"; '
        "cat /proc/net/route | awk 'NR>1' | wc -l | xargs -I{} echo 'offline: {} route(s)'; "
        "pytest -q -p no:cacheprovider -m 'not integration' airflow/tests generator/tests",
    ]
    print("test-offline: running the unit and DAG suites with --network none")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
