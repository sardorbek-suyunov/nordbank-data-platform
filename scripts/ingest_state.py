"""Print the registry's view of one interval, as JSON, from inside the container.

The backfill loop runs on the host — it drives the generator, which is a host package, and the
Airflow CLI, which is in the container — but the registry is in the warehouse, which the host
cannot reach. This is the one query it needs, in the one place that can run it.

`--interval` is the run's logical date. Output is a JSON object so the caller parses a document
rather than a table.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import warehouse  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", required=True, help="the run's logical date, YYYY-MM-DD")
    parser.add_argument("--expected", type=int, default=0, help="entities expected for the day")
    arguments = parser.parse_args(argv)

    interval = dt.datetime.fromisoformat(arguments.interval).replace(tzinfo=dt.UTC)

    with warehouse.connect(read_only=True) as connection:
        rows = connection.execute(
            """
            select entity, source_schema, batch_sequence, status, failure_reason,
                   rows_read, rows_landed, rows_quarantined
              from ops.batch_registry
             where interval_start = ?
             order by entity, batch_sequence
            """,
            [interval],
        ).fetchall()

    batches = [
        {
            "entity": row[0],
            "source_schema": row[1],
            "sequence": row[2],
            "status": row[3],
            "failure_reason": row[4],
            "rows_read": row[5],
            "rows_landed": row[6],
            "rows_quarantined": row[7],
        }
        for row in rows
    ]
    # An entity is done for this interval when any of its batches is registered. A failed batch
    # beside a registered one is a resolved drift, not an outstanding one.
    registered = {b["entity"] for b in batches if b["status"] == "registered"}
    failed = [b for b in batches if b["status"] == "failed" and b["entity"] not in registered]

    print(
        json.dumps(
            {
                "interval": arguments.interval,
                "batches": batches,
                "registered_entities": sorted(registered),
                "failed": failed,
                "complete": bool(arguments.expected) and len(registered) >= arguments.expected,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
