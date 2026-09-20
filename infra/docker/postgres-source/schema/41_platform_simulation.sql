-- Spec 004 sections 2, 3, 4 and 7: the mutation engine's own state and its logs.
--
-- These are platform tables, not the simulated bank's own data (spec 002 section 1a). A real
-- core banking system does not carry a record of the simulation advancing it, and the schema
-- name is what says which of the two a table is.
--
-- They carry created_at and updated_at and neither is_deleted nor is_active (design rule 3),
-- and the catalogue loop in 50_audit_and_indexes.sql gives them the updated_at index, the
-- set_updated_at trigger and an index on every foreign key column.
--
-- **These tables carry real time, not simulated time.** The trigger reads a simulation clock
-- from M3 onward, and a tick clears it before writing anything here, so that a tick_log row
-- says when the tick actually ran. tick_log is a reconciliation control M7 reads, and a
-- control that lies about when it ran is useless. Spec 002's amendment of design rule 4
-- states the ordering rule this depends on.

-- The simulation's position: which date the source has been advanced to, under which seed and
-- profile. Exactly one row, which is why the unique index is on a constant: a second row would
-- mean two answers to "what date is the source at", and there is no sensible way to pick.
create table if not exists platform.simulation_state (
    simulation_state_id    bigint       generated always as identity primary key,
    profile                varchar(20)  not null,
    seed                   bigint       not null,
    -- The date the historical load ended on. `make seed` resets simulated_date to it, so it is
    -- kept rather than recomputed: the anchor is an input to the load and not a function of
    -- anything still in the database.
    anchor_date            date         not null,
    -- The date the source has been advanced to. Equal to anchor_date before the first tick.
    simulated_date         date         not null,
    -- Ticks completed since the last seed. Zero before the first tick.
    tick_sequence          integer      not null default 0,
    last_tick_completed_at timestamptz,
    created_at             timestamptz  not null default now(),
    updated_at             timestamptz  not null default now(),
    constraint simulation_state_not_before_anchor_ck check (simulated_date >= anchor_date),
    constraint simulation_state_tick_sequence_ck     check (tick_sequence >= 0)
);

create unique index if not exists simulation_state_singleton
    on platform.simulation_state ((true));

-- One row per tick. The reconciliation control: at M4 the platform asserts that bronze
-- received exactly what the source says it changed, and at M7 that becomes a check. The window
-- a tick's changes fall in is [simulated_date, simulated_date + 1), which is what acceptance
-- criterion 9 reconciles against.
create table if not exists platform.tick_log (
    tick_log_id    bigint      generated always as identity primary key,
    tick_sequence  integer     not null,
    simulated_date date        not null,
    profile        varchar(20) not null,
    seed           bigint      not null,
    -- Real time, both of them. See the header.
    started_at     timestamptz not null,
    completed_at   timestamptz not null,
    duration_ms    integer     not null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    constraint tick_log_sequence_uq    unique (tick_sequence),
    constraint tick_log_date_uq        unique (simulated_date),
    constraint tick_log_duration_ck    check (duration_ms >= 0),
    constraint tick_log_completed_ck   check (completed_at >= started_at)
);

-- Per-table counts for one tick, by operation class. A child table rather than a JSON column
-- on tick_log, because M4 and M7 read this by table name and join it to a bronze batch: a
-- document would make that a parse rather than a join.
create table if not exists platform.tick_table_counts (
    tick_table_count_id  bigint      generated always as identity primary key,
    tick_log_id          bigint      not null references platform.tick_log (tick_log_id),
    table_name           varchar(63) not null,
    rows_inserted        integer     not null default 0,
    rows_updated         integer     not null default 0,
    rows_soft_deleted    integer     not null default 0,
    -- Inserted during this tick with a business timestamp earlier than the tick's date. A
    -- subset of rows_inserted, not a fourth disjoint class, because the row is both.
    rows_late_arriving   integer     not null default 0,
    -- The only physical delete the source performs (spec 004 section 2, as amended).
    rows_deleted         integer     not null default 0,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    constraint tick_table_counts_uq unique (tick_log_id, table_name),
    constraint tick_table_counts_nonneg_ck check (
        rows_inserted >= 0 and rows_updated >= 0 and rows_soft_deleted >= 0
        and rows_late_arriving >= 0 and rows_deleted >= 0
    ),
    constraint tick_table_counts_late_subset_ck check (rows_late_arriving <= rows_inserted)
);

-- The keys a physical delete removed, so that M7's primary-key reconciliation has a known
-- positive set to validate against rather than a count. docs/architecture.md schedules that
-- control precisely because watermark extraction cannot see a DELETE; without a real instance
-- of the condition it is written to detect, an untested reconciler reads as a working one.
create table if not exists platform.tick_deleted_keys (
    tick_deleted_key_id bigint      generated always as identity primary key,
    tick_log_id         bigint      not null references platform.tick_log (tick_log_id),
    table_name          varchar(63) not null,
    deleted_key         bigint      not null,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint tick_deleted_keys_uq unique (table_name, deleted_key)
);

-- One row per drift event that has fired. `make schema-check` reads this and computes the
-- expected schema as the committed data dictionary plus the deltas recorded here (spec 004
-- section 3, as amended), which is what lets the source schema move mid-history without the
-- dictionary being rewritten at runtime.
create table if not exists platform.drift_log (
    drift_log_id   bigint      generated always as identity primary key,
    event_name     varchar(100) not null,
    drift_type     varchar(40)  not null,
    target_schema  varchar(63)  not null,
    target_table   varchar(63)  not null,
    target_column  varchar(63)  not null,
    simulated_date date         not null,
    tick_sequence  integer      not null,
    -- Real time: when the DDL actually ran, not the date it simulates.
    applied_at     timestamptz  not null,
    created_at     timestamptz  not null default now(),
    updated_at     timestamptz  not null default now(),
    constraint drift_log_event_uq unique (event_name)
);

-- The permitted vocabulary, dropped and re-added rather than declared inline, so that adding a
-- drift type is a change this file can make to a table that already exists. M3 implements the
-- first two; the rest arrive at M4 with the contracts that have to survive them.
alter table platform.drift_log
    drop constraint if exists drift_log_type_ck;

alter table platform.drift_log
    add constraint drift_log_type_ck
    check (drift_type in ('column_added', 'type_widened'));
