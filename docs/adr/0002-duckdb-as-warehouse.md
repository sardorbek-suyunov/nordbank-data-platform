# 0002 — DuckDB as the warehouse

Status: Accepted
Date: 2026-09-17

## Context

The platform must run end to end on one machine, with no account, no billing and no network
dependency, so that the project can be cloned and run by a reader. It must still handle the
`full` profile, in the order of tens of millions of transactions, and it must show that the
transformation layer is not welded to one engine.

## Decision

DuckDB is the primary warehouse: a single file holding the `bronze`, `silver`, `gold`, `dq`,
`ops` and `meta` schemas, read directly from the parquet partitions in the lake where that is
cheaper than materialising.

DuckDB permits one writer per database file. Rather than hope that no two tasks overlap, the
constraint is made explicit in the orchestrator. Every task that writes to the warehouse
acquires the Airflow pool `warehouse_write`, which is configured with exactly one slot.
Readers, including the Streamlit application and dbt docs, open the file with
`read_only=true` and are not subject to the pool. A task that needs to write and finds the
pool full waits; it does not retry into a lock error.

BigQuery is kept as a second dbt target against a sandbox dataset. It is not a fallback and
holds no authoritative data; it exists so the dbt project is proven to compile and run
against a second engine, which is what makes the DuckDB choice reversible.

## Consequences

Local runs are fast and free, and the whole warehouse is one file that can be deleted and
rebuilt. Write parallelism is capped at one, so the transformation critical path is serial by
construction and a long dbt run blocks other writers. Concurrency problems surface as queue
time in Airflow, which is visible, instead of as intermittent lock failures, which are not.
Engine-specific SQL is a defect, since it breaks the BigQuery target.

## Alternatives considered

Postgres as the warehouse: real concurrency, but an analytical workload on a row store, and
the source system is already Postgres, which would blur the boundary the project exists to
demonstrate.

Snowflake or BigQuery as the primary: no single-writer constraint, but a billing account
becomes a prerequisite for running the project at all.
