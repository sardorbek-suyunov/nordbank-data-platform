-- Spec 002 sections 1a and 5.
--
-- Not in ref: ref is the simulated bank's own reference data, and a PII classification
-- maintained by the data platform is not something a bank's core system would publish about
-- itself. This is platform metadata about a source, co-located with the source so the
-- extraction layer at M4 needs one connection rather than two.
--
-- Rows are generated from docs/data_dictionary.md by `make schema-apply`. There is no
-- hand-written seed for this table, because it would be the second copy the table exists to
-- prevent.

create table if not exists platform.column_classifications (
    column_classification_id bigint generated always as identity primary key,
    schema_name              varchar(63) not null,
    table_name               varchar(63) not null,
    column_name              varchar(63) not null,
    classification           varchar(20) not null,
    rationale                text        not null,
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now(),
    constraint column_classifications_uq unique (schema_name, table_name, column_name)
);

-- The permitted vocabulary, dropped and re-added rather than declared inline, so that adding a
-- class is a change this file can make to a table that already exists. pseudonymous_key was
-- added at M2 after the first pass classified every join path to a person as non-personal,
-- which was wrong: an internal customer number is pseudonymised personal data.
alter table platform.column_classifications
    drop constraint if exists column_classifications_classification_ck;

alter table platform.column_classifications
    add constraint column_classifications_classification_ck
    check (classification in
        ('identifier', 'quasi-identifier', 'pseudonymous_key', 'sensitive', 'non-personal'));
