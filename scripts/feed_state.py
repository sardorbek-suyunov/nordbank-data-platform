"""Print one feed run's batches, as JSON, from inside the container.

The backfill decides from this whether a feed is complete for a day, and when it halts it says
which batch failed and why, the way it does for the core banking source. A feed's batch is keyed
on its own interval — a clearing file's settlement date, a snapshot's export time — which need
not be the day being ingested, so this reads the batches the run itself allocated, by the run id
that triggered them. A cleared run keeps its id, so every try of the run is here.

**Each failed batch carries what became of its delivery.** `landed` when the same bytes have
since registered, `parked` when the delivery's latest attempt was refused with a verdict and
nothing has changed for it since (ADR 0016), and null for a batch with no delivery behind it,
an API feed's. The parked state is derived by the same function the open step uses to decide
whether to attempt a delivery, so the backfill and the feed cannot disagree about it.
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import warehouse  # noqa: E402

KEYS = (
    "source_system",
    "entity",
    "batch_id",
    "interval_start",
    "status",
    "failure_reason",
    "rows_read",
    "rows_landed",
    "rows_quarantined",
)


def delivery_of(connection, source_system: str, batch_id: str) -> str | None:
    """`landed`, `parked` or None: what became of the delivery a failed batch attempted."""
    from nordbank_ops.feeds import identity

    # The open step records an attempt's sighting against the first of the delivery's batches;
    # its siblings share the interval and sequence after the entity name.
    row = connection.execute(
        """
        select content_checksum
          from ops.file_sighting
         where source_system = ? and outcome in (?, ?) and batch_id like ?
         limit 1
        """,
        [
            source_system,
            identity.NEW,
            identity.REATTEMPTED,
            "%-" + identity.sibling_suffix(batch_id),
        ],
    ).fetchone()
    if row is None:
        return None
    checksum = row[0]
    if identity.landed_as(connection, [checksum]):
        return "landed"
    if checksum in identity.parked(connection, source_system, [checksum]):
        return "parked"
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)
    # The loop reads between runs, when transform_bronze may be mid-build (warehouse.LONG_WAIT).
    with warehouse.connect(read_only=True, **warehouse.LONG_WAIT) as connection:
        rows = connection.execute(
            """
            select source_system, entity, batch_id, interval_start, status, failure_reason,
                   rows_read, rows_landed, rows_quarantined
              from ops.batch_registry
             where triggering_run_id = ?
             order by batch_id
            """,
            [arguments.run_id],
        ).fetchall()
        batches = [dict(zip(KEYS, row, strict=True)) for row in rows]
        for batch in batches:
            batch["interval_start"] = batch["interval_start"].isoformat()
            batch["delivery"] = (
                delivery_of(connection, batch["source_system"], batch["batch_id"])
                if batch["status"] == "failed"
                else None
            )
    print(
        json.dumps({"batches": batches, "failed": [b for b in batches if b["status"] == "failed"]})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
