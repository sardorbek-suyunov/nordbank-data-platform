"""Print one feed run's batches, as JSON, from inside the container.

The backfill halts when a feed run fails and has to say which batch failed and why, the way it
does for the core banking source. A feed's batch is keyed on its own interval — a clearing
file's settlement date, a snapshot's export time — which need not be the day being ingested, so
this reads the batches the run itself allocated, by the run id that triggered them.
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import warehouse  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)
    with warehouse.connect(read_only=True) as connection:
        rows = connection.execute(
            """
            select source_system, entity, batch_id, status, failure_reason, rows_read,
                   rows_landed, rows_quarantined
              from ops.batch_registry
             where triggering_run_id = ?
             order by batch_id
            """,
            [arguments.run_id],
        ).fetchall()
    keys = (
        "source_system",
        "entity",
        "batch_id",
        "status",
        "failure_reason",
        "rows_read",
        "rows_landed",
        "rows_quarantined",
    )
    batches = [dict(zip(keys, row, strict=True)) for row in rows]
    print(
        json.dumps({"batches": batches, "failed": [b for b in batches if b["status"] == "failed"]})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
