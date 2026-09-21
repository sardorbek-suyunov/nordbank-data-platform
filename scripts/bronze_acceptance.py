"""Specification 005's evidence, gathered from the warehouse and the lake.

The counterpart of `tick_acceptance.py` for M3: one command that reports what the acceptance
criteria ask for, against whatever a backfill has actually produced, so the evidence in a
checkpoint is a transcript rather than a recollection.

It asserts nothing and fails nothing. Criteria 4, 5, 6, 11, 12 and 13 are experiments rather
than measurements — they need a failure induced or a contract narrowed — and those live in
`airflow/tests/test_ingest_integration.py`, where they can set up the condition they test.
What is here is everything a finished backfill can be asked.

Runs inside a container: the warehouse is on a named volume.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/opt/airflow/plugins")

from nordbank_ops import clients, warehouse  # noqa: E402


def heading(number: int, text: str) -> None:
    print(f"\n--- criterion {number}: {text}")


def rows(connection, sql: str, parameters=None):
    return connection.execute(sql, parameters or []).fetchall()


def main() -> int:
    with warehouse.connect(read_only=True) as c:
        heading(2, "contracts in force, by entity and version")
        for row in rows(
            c,
            "select contract_version, count(*) from meta.contract_version group by 1 order by 1",
        ):
            print(f"  version {row[0]}: {row[1]} entity(ies)")

        heading(3, "batches by status")
        for row in rows(c, "select status, count(*) from ops.batch_registry group by 1 order by 1"):
            print(f"  {row[0]}: {row[1]}")

        heading(14, "landed plus quarantined against rows read, per batch")
        bad = rows(
            c,
            "select count(*) from ops.batch_registry "
            "where rows_landed + rows_quarantined <> rows_read",
        )[0][0]
        total = rows(c, "select count(*) from ops.batch_registry")[0][0]
        quarantined = rows(c, "select coalesce(sum(rows_quarantined), 0) from ops.batch_registry")
        print(f"  {total - bad} of {total} batch(es) satisfy the identity, {bad} do not")
        print(f"  quarantined rows in total: {quarantined[0][0]}")

        heading(15, "reconciliation against platform.tick_log, per entity per tick day")
        claimed = rows(
            c,
            """
            select count(*), count(*) filter (where difference = 0), coalesce(sum(rows_claimed), 0)
              from ops.source_reconciliation where rows_claimed is not null
            """,
        )[0]
        print(f"  {claimed[1]} of {claimed[0]} claimed entity-days reconcile exactly")
        print(f"  rows claimed by the source over those days: {claimed[2]}")
        for row in rows(
            c,
            """
            select source_date, entity, rows_claimed, rows_landed, difference
              from ops.source_reconciliation
             where rows_claimed is not null and difference <> 0
             order by source_date, entity limit 20
            """,
        ):
            print(f"  MISMATCH {row[0]} {row[1]}: claimed {row[2]}, landed {row[3]}, {row[4]}")
        unclaimed = rows(
            c, "select count(*) from ops.source_reconciliation where rows_claimed is null"
        )[0][0]
        print(f"  {unclaimed} entity-day(s) carry no claim, which is the initial load and `ref`")

        heading(10, "the vault")
        vault = rows(
            c,
            "select count(*), count(distinct token), count(distinct raw_value) from meta.pii_vault",
        )[0]
        print(f"  {vault[0]} row(s), {vault[1]} distinct token(s), {vault[2]} distinct value(s)")
        for row in rows(
            c,
            """
            select first_seen_entity, first_seen_column, count(*)
              from meta.pii_vault group by 1, 2 order by 3 desc
            """,
        ):
            print(f"  first seen in {row[0]}.{row[1]}: {row[2]}")

        heading(12, "drift observed at ingest")
        for row in rows(
            c,
            """
            select entity, column_name, drift_kind, action_taken, count(*)
              from meta.schema_drift_log group by 1, 2, 3, 4 order by 1, 2
            """,
        ):
            print(f"  {row[0]}.{row[1]} {row[2]}: {row[3]} ({row[4]} batch(es))")

        heading(13, "batches that failed, with their reason")
        failed = rows(
            c,
            "select entity, batch_id, failure_reason from ops.batch_registry "
            "where status = 'failed' order by batch_id",
        )
        for row in failed:
            print(f"  {row[0]} {row[1]}: {row[2]}")
        if not failed:
            print("  none")

        heading(17, "the backfill's span")
        span = rows(
            c,
            """
            select min(interval_start)::date, max(interval_start)::date,
                   count(distinct interval_start), count(*)
              from ops.batch_registry
            """,
        )[0]
        print(f"  {span[0]} to {span[1]}: {span[2]} interval(s), {span[3]} batch(es)")
        unresolved = rows(
            c,
            "select count(*) from ops.batch_registry where status not in ('registered', 'failed')",
        )[0][0]
        print(f"  batches in neither a registered nor a failed state: {unresolved}")

        heading(16, "late arrivals, by the partition they landed in")
        # The initial load is excluded, and excluding it is the whole point of the
        # measurement. That batch lands the entire historical book in one partition, so every
        # row in it has a business date earlier than its ingest date — by up to three years —
        # and including it would drown the thing criterion 16 is about: a transaction that
        # arrives days after the customer made it and lands in the partition of its arrival
        # rather than of its business date. The initial load is the batch with no lower
        # watermark, which is exactly how the registry records "this is the first read".
        prefixes = [
            row[0]
            for row in rows(
                c,
                "select object_prefix from ops.batch_registry "
                "where status = 'registered' and entity = 'transactions' "
                "and watermark_from is not null",
            )
        ]

        heading(6, "intervals carrying more than one batch sequence")
        for row in rows(
            c,
            """
            select entity, interval_start::date, count(*)
              from ops.batch_registry group by 1, 2 having count(*) > 1 order by 2, 1
            """,
        ):
            print(f"  {row[0]} {row[1]}: {row[2]} sequences")

        heading(7, "the overlap: rows re-read from the previous day's window")
        for row in rows(
            c,
            """
            select entity, count(*), sum(rows_read - rows_landed_on_own_day) as re_read
              from (
                select r.entity, r.rows_read,
                       coalesce(s.rows_landed, 0) as rows_landed_on_own_day
                  from ops.batch_registry r
                  left join ops.source_reconciliation s
                    on s.entity = r.entity and s.source_date = r.interval_start::date
                 where r.status = 'registered' and r.source_schema = 'core'
              ) t group by 1 having sum(rows_read - rows_landed_on_own_day) > 0
             order by 3 desc limit 8
            """,
        ):
            print(
                f"  {row[0]}: {row[2]} row(s) read beyond their own source day, "
                f"over {row[1]} batch(es)"
            )

    _late_arrivals(prefixes)
    return 0


def _late_arrivals(prefixes: list[str]) -> None:
    """Criterion 16, read off the objects themselves.

    A late arrival is a transaction whose business date is earlier than the ingest date of the
    partition holding it. The claim is that it lands in the partition of its arrival, so the
    evidence is the count of rows whose `booked_at` date precedes their partition's date.
    """
    import io

    import pyarrow.parquet as pq

    client = clients.lake_client()
    bucket = clients.lake_bucket()

    late = 0
    same = 0
    by_lag: dict[int, int] = {}
    for prefix in sorted(set(prefixes)):
        ingest_date = prefix.split("ingest_date=")[1].split("/")[0]
        listing = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        for item in listing.get("Contents", []):
            body = client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            table = pq.read_table(io.BytesIO(body), columns=["booked_at"])
            for value in table.column("booked_at").to_pylist():
                if value is None:
                    continue
                lag = (_date(ingest_date) - value.date()).days
                if lag > 0:
                    late += 1
                    by_lag[lag] = by_lag.get(lag, 0) + 1
                else:
                    same += 1

    print("  incremental batches only; the initial load of the historical book is excluded")
    print(f"  {late} transaction row(s) landed in a partition later than their business date")
    print(f"  {same} landed in the partition of their business date")
    for lag in sorted(by_lag):
        print(f"    {lag} day(s) late: {by_lag[lag]}")


def _date(text: str):
    import datetime as dt

    return dt.date.fromisoformat(text)


if __name__ == "__main__":
    raise SystemExit(main())
