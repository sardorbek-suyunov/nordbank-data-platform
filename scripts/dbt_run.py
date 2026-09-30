"""Run a dbt command inside the stack, the way `transform_bronze` runs it (spec 007).

`make dbt-build` and `make dbt-docs` call this inside the scheduler container, where dbt's own
environment, the warehouse volume and the lake connection are. Outside Airflow nothing holds
the `warehouse_access` pool, so this waits, with the bounded backoff every out-of-Airflow
warehouse access uses, until no other process holds the warehouse file.

Usage: `python scripts/dbt_run.py <dbt arguments>`, for example `build --select path:models/bronze`.
"""

from __future__ import annotations

import sys

for _path in ("/opt/airflow/plugins", "/opt/airflow/scripts"):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nordbank_ops import transform, warehouse  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv:
        print("dbt-run: name a dbt command, such as `build --select path:models/bronze`")
        return 2
    # Waits for any holder of the file to finish, then releases it for dbt to open.
    with warehouse.connect(read_only=True, attempts=20, base_delay=1.0):
        pass
    return transform.run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
