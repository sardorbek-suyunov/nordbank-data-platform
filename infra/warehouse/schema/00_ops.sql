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

-- **One row per batch per source day, not one row per source day.** Keyed the other way and
-- upserted, two batches covering one day fight over the row and the last one wins: measured on
-- the first complete backfill, the narrow re-run after a drift halt overwrote nine entities'
-- day counts with the handful of rows its tail-of-the-day window had seen, and the control
-- reported a discrepancy that only its own bookkeeping had created.
--
-- Keyed per batch, the two contributions coexist and the day is their sum, which is also what
-- the day actually is: the batch before the halt landed most of it and the batch after the
-- contract bump landed the rest.
--
-- `rows_landed` counts this batch's landed rows whose own `updated_at` falls on `source_date`,
-- so a batch spanning the overlap window contributes to two source days. `rows_claimed` is the
-- source's own control total for the day, repeated on every batch that touches it, and is null
-- where no tick ran or the entity is not one the tick engine covers.
create table if not exists ops.source_reconciliation (
    source_system varchar not null,
    entity varchar not null,
    source_date date not null,
    batch_id varchar not null,
    rows_claimed bigint,
    rows_landed bigint not null,
    rows_quarantined bigint not null,
    recorded_at timestamptz not null,
    primary key (source_system, entity, source_date, batch_id)
);

-- The per-day figure, which is what criterion 15 compares against the source's claim.
--
-- **Not a sum, and not the last writer.** Two batches can land rows belonging to one source
-- day and their rows overlap rather than partition: measured on the drift day, the batch
-- before the halt covered the whole day and the batch after the contract bump re-read the
-- last fifteen minutes of it, so summing counted 267 rows twice and taking the last counted
-- 271 of them not at all.
--
-- A batch **covers** a source day when its window opened at or before the start of that day,
-- which is exactly the condition under which it read the day whole. A batch whose window
-- opened inside the day re-read part of it: those rows are real landings and are kept, and
-- they are the overlap the `>=` watermark exists to produce, but they are not a second
-- portion of the day to be added to the first.
--
-- So the day's landing is the largest a covering batch reported. One covering batch is the
-- ordinary case and the maximum is its count; two arise when a day is re-run after failing,
-- and each of them read the whole day, so the maximum is still the distinct total.
--
-- The comparison converts explicitly to UTC rather than casting a date to `timestamptz`. The
-- cast reads the session timezone, so the same view answered differently in the container,
-- which runs at UTC, and on a developer machine five hours east of it — where every batch
-- looked partial and every day looked empty.
create or replace view ops.source_reconciliation_daily as
with contribution as (
    select
        r.source_system,
        r.entity,
        r.source_date,
        r.rows_claimed,
        r.rows_landed,
        r.rows_quarantined,
        coalesce(
            (b.watermark_from at time zone 'UTC') <= r.source_date::timestamp, true
        ) as covers_the_day
    from ops.source_reconciliation as r
    left join ops.batch_registry as b on r.batch_id = b.batch_id
)

select
    source_system,
    entity,
    source_date,
    max(rows_claimed) as rows_claimed,
    coalesce(max(rows_landed) filter (where covers_the_day), 0) as rows_landed,
    coalesce(max(rows_quarantined) filter (where covers_the_day), 0) as rows_quarantined,
    case
        when max(rows_claimed) is null then null
        else
            coalesce(max(rows_landed) filter (where covers_the_day), 0)
            + coalesce(max(rows_quarantined) filter (where covers_the_day), 0)
            - max(rows_claimed)
    end as difference,
    count(*) as batches,
    count(*) filter (where not covers_the_day) as partial_batches,
    coalesce(sum(rows_landed) filter (where not covers_the_day), 0) as rows_re_read
from contribution
group by 1, 2, 3;

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

-- File and snapshot identity (spec 006 section 1, ADR 0013). One row per distinct content that
-- has landed, keyed on the SHA-256 of the bytes, so the same file reprocessed never lands twice
-- and a file renamed but unchanged is recognised. Written by the register step when the file's
-- batch registers, and not before: a file whose batch failed is not ingested, and is picked up
-- again once the reason it failed is resolved.
--
-- `business_date` is the settlement date a file covers or the publication date of a snapshot.
-- `publisher_version` is a snapshot's own version string, which identifies a publication and
-- is recorded, but is not the identity: the publisher can issue a new version string over the
-- same content, and that is a no-op here.
create table if not exists ops.ingested_file (
    content_checksum varchar primary key,
    source_system varchar not null,
    entity varchar not null,
    object_key varchar not null,
    byte_size bigint not null,
    business_date date,
    publisher_version varchar,
    batch_id varchar not null,
    first_ingested_at timestamptz not null
);

-- Every time discovery sees a file, and what it concluded. The evidence that a reprocessed or
-- renamed file was recognised rather than merely not duplicated: a sighting of a known checksum
-- under a new key is recorded as `already_ingested` with the batch it landed as.
create table if not exists ops.file_sighting (
    content_checksum varchar not null,
    source_system varchar not null,
    object_key varchar not null,
    ingest_date date not null,
    outcome varchar not null,
    batch_id varchar,
    triggering_run_id varchar not null,
    seen_at timestamptz not null,
    check (outcome in ('new', 'already_ingested'))
);

-- One row per request an interval feed made: how many attempts it took, the status of the
-- last one, and whether it landed rows, found the date unpublished, or failed. A date the
-- publisher did not publish is `absent` with zero rows, which is how bronze records a gap
-- without writing a row for it.
create table if not exists ops.feed_request (
    batch_id varchar not null,
    source_system varchar not null,
    entity varchar not null,
    request_key varchar not null,
    attempts integer not null,
    final_status integer,
    outcome varchar not null,
    detail varchar,
    rows_landed bigint not null,
    requested_at timestamptz not null,
    primary key (batch_id, request_key),
    check (outcome in ('landed', 'absent', 'failed'))
);
