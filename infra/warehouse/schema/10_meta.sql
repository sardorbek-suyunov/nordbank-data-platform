-- Contract, drift and vault metadata (spec 005 section 1).

create schema if not exists meta;

-- Token to raw value. **The primary key is the token**, not the token with its entity and
-- column: one raw value legitimately occurs in more than one entity, and measured on the `ci`
-- book 949 payments carry a `counterparty_name` equal to some customer's `full_name`. Keying
-- on the triple would produce two rows for one value and break the one-row-per-value property
-- criterion 10 asserts.
--
-- `first_seen_*` records where the value was first sighted and is not part of the key.
--
-- This is the only place in the platform where a raw identifier exists after ingestion
-- (ADR 0005). Erasure is the deletion of a row here, which is what makes the surviving tokens
-- unresolvable. The raw value is not encrypted: the reason is in spec 005 section 6 and in
-- ADR 0005, and it is narrower than key-management theatre.
create table if not exists meta.pii_vault (
    token varchar primary key,
    raw_value varchar not null,
    classification varchar not null,
    first_seen_source_system varchar not null,
    first_seen_entity varchar not null,
    first_seen_column varchar not null,
    first_seen_batch_id varchar not null,
    created_at timestamptz not null
);

-- Every contract version per entity and the business day it applies from. The open step
-- selects the version in force for a batch from here, by the batch's interval, so replaying a
-- day from before a version bump validates against the version of that day without anyone
-- checking out an old file.
--
-- Two times, and they answer different questions. `in_force_from` is **source time**: the first
-- business day the version applies to, authored in the contract, null for a version that
-- applies from the start of history. `first_seen_at` is **real time**: when this platform first
-- recorded the version. Specification 005 had one column, named `in_force_from` and holding the
-- second, which made selecting by interval compare a simulated day against a wall clock.
--
-- The contract body is not stored here. It is on disk, the current version beside its
-- superseded ones under `history/`, and the fingerprint is what ties a row to a file: a
-- version whose file no longer matches its recorded fingerprint is refused, because an edited
-- old contract would change a replay silently. `dictionary_revision` is null for a contract
-- authored against a published file or API specification rather than bootstrapped from the
-- data dictionary.
create table if not exists meta.contract_version (
    source_system varchar not null,
    entity varchar not null,
    contract_version integer not null,
    dictionary_revision varchar,
    contract_fingerprint varchar not null,
    in_force_from date,
    first_seen_at timestamptz not null,
    primary key (source_system, entity, contract_version)
);

-- Drift observed at ingest: the source's shape against the contract in force. An additive
-- column is recorded and not written to bronze; a breaking change fails the batch. The log is
-- the record either way, so a column that appeared is visible without reading a task log.
create table if not exists meta.schema_drift_log (
    batch_id varchar not null,
    source_system varchar not null,
    entity varchar not null,
    column_name varchar not null,
    drift_kind varchar not null,
    contract_version integer not null,
    action_taken varchar not null,
    detail varchar,
    observed_at timestamptz not null,
    primary key (batch_id, column_name, drift_kind)
);
