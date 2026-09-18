-- Spec 002 section 2: fraud alerts and digital login sessions.

create table if not exists core.fraud_alerts (
    fraud_alert_id          bigint generated always as identity primary key,
    alert_reference         varchar(40)  not null unique,
    transaction_id          bigint       not null references core.transactions (transaction_id),
    customer_id             bigint       not null references core.customers (customer_id),
    fraud_rule_code         varchar(40)  not null references ref.fraud_rules (code),
    fraud_disposition_code  varchar(40)  not null references ref.fraud_dispositions (code),
    -- Not created_at: design rule 11 reserves that name for the audit column. This is when the
    -- rule fired, which the M3 loader writes at a historical instant, and the two would
    -- otherwise disagree on every backfilled row.
    alerted_at              timestamptz  not null,
    dispositioned_at        timestamptz,
    alert_score             numeric(18,8) not null,
    analyst_reference       varchar(40),
    created_at              timestamptz  not null default now(),
    updated_at              timestamptz  not null default now(),
    is_deleted              boolean      not null default false,
    constraint fraud_alerts_score_ck check (alert_score >= 0 and alert_score <= 1),
    constraint fraud_alerts_dispositioned_after_alerted_ck
        check (dispositioned_at is null or dispositioned_at >= alerted_at)
);

create table if not exists core.login_sessions (
    login_session_id   bigint generated always as identity primary key,
    session_reference  varchar(40)  not null unique,
    customer_id        bigint       not null references core.customers (customer_id),
    channel_code       varchar(40)  not null references ref.channels (code),
    login_outcome_code varchar(40)  not null references ref.login_outcomes (code),
    started_at         timestamptz  not null,
    ended_at           timestamptz,
    device_fingerprint varchar(128) not null,
    -- Held as text rather than inet because it is classified identifier and is replaced by a
    -- keyed hash at ingest, and a hash is not an inet.
    ip_address         varchar(45)  not null,
    -- Resolved at the source, because ip_address is tokenised and the geography Q19 groups by
    -- cannot be recovered from a hash.
    ip_country_code    character(2) references ref.countries (code),
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now(),
    is_deleted         boolean      not null default false,
    constraint login_sessions_ended_after_started_ck
        check (ended_at is null or ended_at >= started_at)
);
