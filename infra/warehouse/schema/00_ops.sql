-- Operational state for extraction (spec 005 section 1).
--
-- Written by Python in the ingestion DAGs, not by dbt: these are platform state rather than
-- derived models, and dbt owns nothing it did not derive.
--
-- Every statement is `if not exists`, so `make warehouse-apply` is safe to repeat and a
-- rebuilt volume comes back identical (ADR 0009 applies the same rule to the source schema).

create schema if not exists ops;

-- The highest `updated_at` successfully registered, per source system and entity. It advances
-- in the register step and nowhere else, so a run that dies after writing objects repeats
-- rather than skips.
create table if not exists ops.extract_watermark (
    source_system varchar not null,
    entity varchar not null,
    watermark_at timestamptz,
    advanced_by_batch_id varchar,
    updated_at timestamptz not null,
    primary key (source_system, entity)
);

-- One row per batch. `status` is the only discriminator the batch id sequencing consults,
-- because Airflow's own identifiers do not survive a retry, a clear or a backfill reprocess
-- (spec 005 section 4). `triggering_run_id` is provenance and is never a key.
--
-- `opened_at` is the value every record of the batch carries in `_ingested_at`. It is a
-- property of the batch rather than of the record, so a retry reads it back instead of taking
-- a new clock reading and producing objects that differ from the ones it replaces.
--
-- `rows_landed` counts the whole batch, including the overlap window's re-read of the previous
-- day. `ops.source_reconciliation` counts a source day. They are different numbers and neither
-- substitutes for the other.
create table if not exists ops.batch_registry (
    batch_id varchar primary key,
    source_system varchar not null,
    source_schema varchar not null,
    entity varchar not null,
    ingest_date date not null,
    interval_start timestamptz not null,
    interval_end timestamptz not null,
    batch_sequence integer not null,
    contract_version integer not null,
    watermark_from timestamptz,
    watermark_to timestamptz,
    rows_read bigint not null default 0,
    rows_landed bigint not null default 0,
    rows_quarantined bigint not null default 0,
    object_prefix varchar not null,
    status varchar not null,
    failure_reason varchar,
    opened_at timestamptz not null,
    written_at timestamptz,
    ended_at timestamptz,
    triggering_run_id varchar not null,
    check (status in ('open', 'written', 'registered', 'failed'))
);

-- Per entity per source day, written by the register step of the batch whose interval is that
-- day. `rows_claimed` is the source's own control total, read from `platform.tick_log` and
-- `platform.tick_table_counts`; it is null for a day no tick wrote, such as the initial load of
-- the historical book.
create table if not exists ops.source_reconciliation (
    source_system varchar not null,
    entity varchar not null,
    source_date date not null,
    rows_claimed bigint,
    rows_landed bigint not null,
    rows_quarantined bigint not null,
    difference bigint,
    batch_id varchar not null,
    recorded_at timestamptz not null,
    primary key (source_system, entity, source_date)
);

-- Task failures, written by the DAG's `on_failure_callback`. It cannot hold the
-- `warehouse_access` pool, because a pool governs task scheduling and not a callback, so it
-- reaches the warehouse through the bounded-retry helper and is written so it cannot itself
-- raise: a callback that fails while reporting a failure reports nothing.
create table if not exists ops.task_failure (
    dag_id varchar not null,
    run_id varchar not null,
    task_id varchar not null,
    map_index integer not null,
    try_number integer not null,
    entity varchar,
    reason varchar not null,
    failed_at timestamptz not null
);
