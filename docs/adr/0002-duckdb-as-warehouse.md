# 0002 — DuckDB as the warehouse

Status: Accepted
Date: 2026-09-17
Corrected: 2026-09-18. This record described DuckDB as permitting one writer alongside
read-only readers. That is wrong, and it was measured: while one process holds the file
read-write, a second process fails to open it even read-only. The decision to use DuckDB
stands; the mechanism below is corrected and the cost is stated in full.

## Context

The platform must run end to end on one machine, with no account, no billing and no network
dependency, so that the project can be cloned and run by a reader. It must still handle the
`full` profile, in the order of tens of millions of transactions, and it must show that the
transformation layer is not welded to one engine.

## Decision

DuckDB is the primary warehouse: a single file holding the `bronze`, `silver`, `gold`, `dq`,
`ops` and `meta` schemas, read directly from the parquet partitions in the lake where that is
cheaper than materialising.

DuckDB permits one process to hold a database file at a time. A read-write holder excludes
every other opener, including read-only ones; several read-only openers coexist only when no
writer holds the file.

The constraint is therefore made explicit in the orchestrator, and it covers reads as well as
writes. Every task that touches the warehouse, in either direction, acquires the Airflow pool
`warehouse_access`, which has exactly one slot. A task that finds the pool full waits rather
than retrying into a lock error. Access outside Airflow, such as a health probe, goes through
a helper that retries with bounded backoff, because the pool only governs what Airflow
schedules.

Consumers do not read the live file at all. Power BI and the Streamlit application read a
snapshot exported to `exports/`, which is also what makes a remotely deployed application
possible.

BigQuery is kept as a second dbt target against a sandbox dataset. It is not a fallback and
holds no authoritative data; it exists so the dbt project is proven to compile and run
against a second engine, which is what makes the DuckDB choice reversible.

## Consequences

Local runs are fast and free, and the whole warehouse is one file that can be deleted and
rebuilt. Concurrency problems surface as queue time in Airflow, which is visible, instead of as
intermittent lock failures, which are not. Engine-specific SQL is a defect, since it breaks the
BigQuery target.

The serialisation is the principal cost of this choice, and it is larger than write contention
alone. Because a writer excludes readers, every warehouse task queues behind every other one:
a data quality read waits for a dbt build, and a dashboard export waits for both. The platform
has no warehouse concurrency at all, only a queue. At the `full` profile that turns a
transformation run into a serial critical path that nothing can overlap with.

This is the strongest argument for the BigQuery target at M10. That target exists to prove the
transformation layer is portable, and the reason portability is worth paying for is exactly
this: the day the serial queue stops being acceptable, the models must already run somewhere
that has real concurrency.

## Alternatives considered

Postgres as the warehouse: real concurrency, but an analytical workload on a row store, and
the source system is already Postgres, which would blur the boundary the project exists to
demonstrate.

Snowflake or BigQuery as the primary: no single-writer constraint, but a billing account
becomes a prerequisite for running the project at all.
