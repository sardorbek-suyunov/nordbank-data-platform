-- Quarantine (spec 005 section 1).

create schema if not exists dq;

-- One row per rejected record. Validation short-circuits on the first failing column in
-- contract order, so a record that fails two columns is reported against the first: the row
-- says why the record was refused, not everything that is wrong with it.
--
-- `offending_value` carries the **token** when the failing column is classified `identifier`,
-- never the cleartext. Quarantine is a mutable side table beside bronze, and a cleartext
-- identifier here would be personal data in a place the vault does not cover, which is the
-- obvious hole in crypto-shredding and is closed explicitly rather than by discipline
-- (ADR 0005).
--
-- This table is a rebuildable index. The durable evidence is the quarantine Parquet in the
-- lake under `quarantine/<source>/<entity>/ingest_date=.../batch_id=...`, written by the
-- extract task before the pool is ever taken.
create table if not exists dq.quarantine_log (
    batch_id varchar not null,
    source_system varchar not null,
    entity varchar not null,
    record_key varchar,
    column_name varchar not null,
    reason varchar not null,
    offending_value varchar,
    value_is_tokenised boolean not null,
    quarantined_at timestamptz not null
);
