-- Spec 002 section 1.3. Each table here earns its place under design rule 7: twelve carry a
-- behavioural attribute a named model or metric consumes, and decision_reasons qualifies on the
-- open-vocabulary limb alone and deliberately carries no attribute at all.

create table if not exists ref.entry_sides (
    entry_side_id   bigint generated always as identity primary key,
    code            character(1)  not null unique,
    name            varchar(120)  not null,
    -- Consumed by mart_control_gl_integrity (Q16). The same fact is enforced row by row by the
    -- check constraint on core.gl_entries, because a check constraint cannot read another
    -- table; 20_ref_domain.sql seeds the two to agree and an integration test asserts it.
    sign_multiplier smallint      not null,
    description     text,
    is_active       boolean       not null default true,
    created_at      timestamptz   not null default now(),
    updated_at      timestamptz   not null default now(),
    constraint entry_sides_sign_multiplier_ck check (sign_multiplier in (-1, 1))
);

create table if not exists ref.gl_account_types (
    gl_account_type_id bigint generated always as identity primary key,
    code               varchar(40)  not null unique,
    name               varchar(120) not null,
    -- Normal balance side is a function of the account type, so it lives here and gl_accounts
    -- reaches it through the foreign key rather than storing a second copy that can disagree.
    normal_side_code   character(1) not null references ref.entry_sides (code),
    description        text,
    is_active          boolean      not null default true,
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now()
);

-- The vocabulary of business events that produce a posting batch, and the code half of the
-- polymorphic reference on core.gl_transactions. It earns a table on design rule 7's
-- open-vocabulary limb: posting sources grow as the bank models more events, and under a check
-- constraint every new one would be a schema migration. It carries no attribute beyond code,
-- name and is_active, and none is invented for it.
create table if not exists ref.gl_source_entities (
    gl_source_entity_id bigint generated always as identity primary key,
    code                varchar(40)  not null unique,
    name                varchar(120) not null,
    description         text,
    is_active           boolean      not null default true,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now()
);

create table if not exists ref.account_statuses (
    account_status_id bigint generated always as identity primary key,
    code              varchar(40)  not null unique,
    name              varchar(120) not null,
    is_open           boolean      not null,
    description       text,
    is_active         boolean      not null default true,
    created_at        timestamptz  not null default now(),
    updated_at        timestamptz  not null default now()
);

create table if not exists ref.transaction_statuses (
    transaction_status_id bigint generated always as identity primary key,
    code                  varchar(40)  not null unique,
    name                  varchar(120) not null,
    is_posted             boolean      not null,
    description           text,
    is_active             boolean      not null default true,
    created_at            timestamptz  not null default now(),
    updated_at            timestamptz  not null default now()
);

create table if not exists ref.payment_statuses (
    payment_status_id bigint generated always as identity primary key,
    code              varchar(40)  not null unique,
    name              varchar(120) not null,
    is_declined       boolean      not null,
    is_final          boolean      not null,
    -- Whether the account has moved. The mirror of ref.transaction_statuses.is_posted, and the
    -- payment half of the balance reconciliation in spec 003 invariant 4 and of the deposit
    -- balance metric. Added at M2's data half: settling the rule in loader code instead would
    -- have put it somewhere dbt cannot read it at M5.
    is_posted         boolean      not null default false,
    description       text,
    is_active         boolean      not null default true,
    created_at        timestamptz  not null default now(),
    updated_at        timestamptz  not null default now()
);

-- Re-appliable against a database created before the M2 data half, where the table exists
-- without the column (ADR 0009: every file here applies twice with no effect). The default is
-- what makes the alter safe on seeded rows, and it points the safe way: a status whose posting
-- behaviour nobody has stated does not move a balance.
alter table ref.payment_statuses
    add column if not exists is_posted boolean not null default false;

create table if not exists ref.loan_statuses (
    loan_status_id  bigint generated always as identity primary key,
    code            varchar(40)  not null unique,
    name            varchar(120) not null,
    is_open         boolean      not null,
    implies_default boolean      not null,
    description     text,
    is_active       boolean      not null default true,
    created_at      timestamptz  not null default now(),
    updated_at      timestamptz  not null default now()
);

create table if not exists ref.loan_application_statuses (
    loan_application_status_id bigint generated always as identity primary key,
    code                       varchar(40)  not null unique,
    name                       varchar(120) not null,
    is_decided                 boolean      not null,
    is_approved                boolean      not null,
    description                text,
    is_active                  boolean      not null default true,
    created_at                 timestamptz  not null default now(),
    updated_at                 timestamptz  not null default now(),
    -- An approved application is by definition decided; the approval rate rule divides one by
    -- the other, so a row where this is false would let the metric exceed 1.
    constraint loan_application_statuses_approved_is_decided_ck
        check (not is_approved or is_decided)
);

create table if not exists ref.login_outcomes (
    login_outcome_id bigint generated always as identity primary key,
    code             varchar(40)  not null unique,
    name             varchar(120) not null,
    is_successful    boolean      not null,
    description      text,
    is_active        boolean      not null default true,
    created_at       timestamptz  not null default now(),
    updated_at       timestamptz  not null default now()
);

create table if not exists ref.fraud_dispositions (
    fraud_disposition_id bigint generated always as identity primary key,
    code                 varchar(40)  not null unique,
    name                 varchar(120) not null,
    is_final             boolean      not null,
    is_confirmed_fraud   boolean      not null,
    description          text,
    is_active            boolean      not null default true,
    created_at           timestamptz  not null default now(),
    updated_at           timestamptz  not null default now(),
    -- Confirmed fraud is a final disposition. The alert precision rule counts only final ones,
    -- so a confirmed-but-not-final row would land in neither the numerator nor the backlog.
    constraint fraud_dispositions_confirmed_is_final_ck
        check (not is_confirmed_fraud or is_final)
);

create table if not exists ref.holder_roles (
    holder_role_id    bigint generated always as identity primary key,
    code              varchar(40)  not null unique,
    name              varchar(120) not null,
    is_primary        boolean      not null,
    -- What makes a null ownership_weight legitimate on core.account_holders: an authorised
    -- signatory operates the account without owning any part of its balance.
    carries_ownership boolean      not null,
    description       text,
    is_active         boolean      not null default true,
    created_at        timestamptz  not null default now(),
    updated_at        timestamptz  not null default now()
);

create table if not exists ref.risk_bands (
    risk_band_id   bigint generated always as identity primary key,
    code           varchar(40)   not null unique,
    name           varchar(120)  not null,
    band_ordinal   smallint      not null unique,
    pd_lower_bound numeric(18,8) not null,
    pd_upper_bound numeric(18,8) not null,
    description    text,
    is_active      boolean       not null default true,
    created_at     timestamptz   not null default now(),
    updated_at     timestamptz   not null default now(),
    constraint risk_bands_pd_range_ck
        check (pd_lower_bound >= 0 and pd_upper_bound <= 1 and pd_upper_bound > pd_lower_bound)
);

create table if not exists ref.payment_types (
    payment_type_id       bigint generated always as identity primary key,
    code                  varchar(40)  not null unique,
    name                  varchar(120) not null,
    is_customer_initiated boolean      not null,
    direction             varchar(10)  not null,
    description           text,
    is_active             boolean      not null default true,
    created_at            timestamptz  not null default now(),
    updated_at            timestamptz  not null default now(),
    constraint payment_types_direction_ck check (direction in ('debit', 'credit'))
);

-- Earns a table on the open-vocabulary limb of design rule 7 and on nothing else. Underwriting
-- reason codes grow as the generator models more decision paths, and under a check constraint
-- every new reason would be a schema migration. No consumer needs an attribute, so none is
-- invented.
create table if not exists ref.decision_reasons (
    decision_reason_id bigint generated always as identity primary key,
    code               varchar(40)  not null unique,
    name               varchar(120) not null,
    description        text,
    is_active          boolean      not null default true,
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now()
);
