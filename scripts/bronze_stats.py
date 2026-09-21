"""Landed and quarantined counts by entity and ingest date (spec 005 section 11).

Runs inside a container, because the warehouse file is on a named volume. Reads only
`registered` batches by default, which is the rule every bronze consumer follows (ADR 0008):
a run that dies after writing objects but before registering leaves files that look exactly
like good data, and nothing about them says so. `ALL=1` includes every status, which is what
an operator wants when something has gone wrong.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import warehouse  # noqa: E402

BY_ENTITY = """
select entity, source_schema, count(*) as batches,
       sum(rows_read) as rows_read,
       sum(rows_landed) as rows_landed,
       sum(rows_quarantined) as rows_quarantined
  from ops.batch_registry
 {where}
 group by 1, 2
 order by rows_landed desc, entity
"""

BY_INGEST_DATE = """
select ingest_date, count(*) as batches,
       sum(rows_landed) as rows_landed,
       sum(rows_quarantined) as rows_quarantined
  from ops.batch_registry
 {where}
 group by 1
 order by 1
"""

BY_STATUS = """
select status, count(*) from ops.batch_registry group by 1 order by 1
"""

FAILED = """
select entity, batch_id, failure_reason
  from ops.batch_registry where status = 'failed' order by entity
"""


def table(rows, headers) -> str:
    if not rows:
        return "  (none)"
    widths = [
        max(len(str(header)), max(len(str(row[index])) for row in rows))
        for index, header in enumerate(headers)
    ]
    lines = ["  " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths, strict=True))]
    lines.append("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(
            "  "
            + "  ".join(str(value).ljust(width) for value, width in zip(row, widths, strict=True))
        )
    return "\n".join(lines)


def main() -> int:
    every = os.environ.get("ALL") == "1"
    where = "" if every else "where status = 'registered'"

    with warehouse.connect(read_only=True) as connection:
        statuses = connection.execute(BY_STATUS).fetchall()
        by_entity = connection.execute(BY_ENTITY.format(where=where)).fetchall()
        by_date = connection.execute(BY_INGEST_DATE.format(where=where)).fetchall()
        failed = connection.execute(FAILED).fetchall()
        vault = connection.execute("select count(*) from meta.pii_vault").fetchone()[0]
        quarantined = connection.execute("select count(*) from dq.quarantine_log").fetchone()[0]
        drift = connection.execute("select count(*) from meta.schema_drift_log").fetchall()

    scope = "every batch" if every else "registered batches only"
    print(f"bronze-stats ({scope})\n")
    print("batches by status:")
    print(table(statuses, ("status", "batches")))
    print("\nby entity:")
    print(table(by_entity, ("entity", "schema", "batches", "read", "landed", "quarantined")))
    print("\nby ingest date:")
    print(table(by_date, ("ingest_date", "batches", "landed", "quarantined")))
    if failed:
        print("\nfailed batches:")
        print(table(failed, ("entity", "batch_id", "reason")))
    print(f"\nvault rows: {vault}")
    print(f"quarantine rows: {quarantined}")
    print(f"drift observations: {drift[0][0] if drift else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
