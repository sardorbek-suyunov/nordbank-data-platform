"""Create the warehouse file, its six schemas and the health probe table.

Run by `airflow-init` on every start. Creating what is already there is a no-op, so a restart
costs nothing and a rebuilt volume comes back identical.
"""

import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import warehouse  # noqa: E402


def main() -> int:
    path = warehouse.warehouse_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()

    with warehouse.connect(read_only=False) as connection:
        warehouse.initialise(connection)
        present = sorted(warehouse.schema_names(connection) & set(warehouse.SCHEMAS))

    state = "opened" if existed else "created"
    print(f"warehouse {state} at {path}; schemas present: {present}")

    missing = [schema for schema in warehouse.SCHEMAS if schema not in present]
    if missing:
        print(f"warehouse initialisation failed, missing schemas: {missing}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
