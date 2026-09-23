"""Refuse `make test-integration` when the warehouse holds batches, unless forced.

The generator's integration tests reseed the source before they assert anything. Against a
warehouse holding a backfill's batches that leaves the registry describing a history the
source no longer has: the next resume reads a source that never produced what the registry
says it did. It destroyed two acceptance runs, and `docs/runbook.md` saying so did not prevent
the second, because documentation is not a guard.

So this runs first, inside the container where the warehouse volume is, and refuses when
`ops.batch_registry` holds any batch. `FORCE=1` proceeds anyway, for the case where the
backfill is finished with or abandoned. A warehouse with no registry table, as on a fresh CI
stack where `make warehouse-apply` has not run, has nothing to destroy and passes.
"""

from __future__ import annotations

import os
import sys

for candidate in ("/opt/airflow/plugins", "/opt/airflow/scripts"):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)


def main() -> int:
    from nordbank_ops import warehouse

    if not warehouse.warehouse_path().exists():
        print("integration-guard: no warehouse file, nothing a reseed could orphan")
        return 0

    with warehouse.connect(read_only=True) as connection:
        present = connection.execute(
            """
            select count(*) from information_schema.tables
             where table_schema = 'ops' and table_name = 'batch_registry'
            """
        ).fetchone()[0]
        if not present:
            print("integration-guard: no batch registry, nothing a reseed could orphan")
            return 0
        rows = connection.execute(
            """
            select source_system, count(*), min(ingest_date), max(ingest_date)
              from ops.batch_registry group by 1 order by 1
            """
        ).fetchall()

    if not rows:
        print("integration-guard: the batch registry is empty, proceeding")
        return 0

    total = sum(count for _system, count, _first, _last in rows)
    for system, count, first, last in rows:
        print(f"integration-guard: {system}: {count} batch(es), ingest dates {first} to {last}")

    if os.environ.get("FORCE") == "1":
        print(f"integration-guard: FORCE=1, proceeding over {total} registered batch(es)")
        return 0

    print(
        f"\nintegration-guard: refusing. The warehouse holds {total} batch(es), and the generator\n"
        "integration tests reseed the source, which would leave the registry describing a\n"
        "history the source no longer has. Finish or abandon the backfill first, then either\n"
        "`make nuke` for a clean stack or re-run with FORCE=1 if the batches are expendable."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
