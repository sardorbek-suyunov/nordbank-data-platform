"""Warehouse probe, run inside an Airflow container by `make health`.

Exit codes: 0 healthy, 3 locked by another process, 1 anything else. The warehouse file lives
on a named volume, so this cannot run from the host.
"""

import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import warehouse  # noqa: E402


def main() -> int:
    try:
        with warehouse.connect(read_only=True, attempts=2, base_delay=0.25) as connection:
            missing = warehouse.missing_schemas(connection)
    except warehouse.WarehouseBusyError as exc:
        print(f"locked: {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}")
        return 1

    if missing:
        print(f"missing schemas: {missing}")
        return 1

    print(f"{len(warehouse.SCHEMAS)} schemas present: {', '.join(warehouse.SCHEMAS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
