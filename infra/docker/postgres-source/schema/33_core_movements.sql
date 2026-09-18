-- Spec 002 section 2: card and cash transactions, and payment instructions.

create table if not exists core.transactions (
    transaction_id              bigint generated always as identity primary key,
    transaction_reference       varchar(40)  not null unique,
    account_id                  bigint       not null references core.accounts (account_id),
    -- Nullable by design: a transaction has at most one of a card, a merchant and an agent
    -- location, and a transfer has none of them.
    card_id                     bigint       references core.cards (card_id),
    merchant_id                 bigint       references core.merchants (merchant_id),
    agent_location_id           bigint       references core.agent_locations (agent_location_id),
    transaction_type_code       varchar(40)  not null references ref.transaction_types (code),
    channel_code                varchar(40)  not null references ref.channels (code),
    transaction_status_code     varchar(40)  not null references ref.transaction_statuses (code),
    -- Null for anything that was not authorised through the card rails. Closed domain fixed by
    -- the authorisation protocol; value list in the data dictionary.
    authorisation_outcome_code  varchar(40),
    booked_at                   timestamptz  not null,
    value_date                  date         not null,
    transaction_amount          numeric(18,4) not null,
    transaction_currency_code   character(3) not null references ref.currencies (code),
    -- A reversal points at the transaction it reverses. Self-referencing rather than a free
    -- text reference, so a reversal of a transaction that does not exist cannot be written.
    reversal_of_transaction_id  bigint       references core.transactions (transaction_id),
    counterparty_reference      varchar(140),
    created_at                  timestamptz  not null default now(),
    updated_at                  timestamptz  not null default now(),
    is_deleted                  boolean      not null default false,
    constraint transactions_authorisation_outcome_ck
        check (authorisation_outcome_code is null
               or authorisation_outcome_code in
                  ('approved', 'declined_funds', 'declined_fraud', 'referred', 'timeout')),
    constraint transactions_amount_ck check (transaction_amount <> 0),
    constraint transactions_reversal_not_self_ck
        check (reversal_of_transaction_id is distinct from transaction_id),
    -- A card transaction has a card; an agent cash transaction has an agent location. Both
    -- cannot be true of one row.
    constraint transactions_single_counterparty_ck
        check (num_nonnulls(card_id, agent_location_id) <= 1)
);

create table if not exists core.payments (
    payment_id               bigint generated always as identity primary key,
    payment_reference        varchar(40)  not null unique,
    account_id               bigint       not null references core.accounts (account_id),
    payment_type_code        varchar(40)  not null references ref.payment_types (code),
    payment_scheme_code      varchar(40)  not null references ref.payment_schemes (code),
    payment_status_code      varchar(40)  not null references ref.payment_statuses (code),
    initiated_at             timestamptz  not null,
    -- When the account was debited or credited, which is not when the scheme settled. An
    -- outbound SEPA payment books immediately and settles later; the active account rule reads
    -- the first of the two.
    booked_at                timestamptz,
    settled_at               timestamptz,
    payment_amount           numeric(18,4) not null,
    payment_currency_code    character(3) not null references ref.currencies (code),
    counterparty_iban        varchar(34),
    -- What a sanctions screen matches on. Classified identifier, so it is tokenised at ingest
    -- and screening runs as a governance job with vault access: spec 002 section 12.
    counterparty_name        varchar(200),
    counterparty_country_code character(2) references ref.countries (code),
    remittance_reference     varchar(140),
    created_at               timestamptz  not null default now(),
    updated_at               timestamptz  not null default now(),
    is_deleted               boolean      not null default false,
    constraint payments_amount_ck            check (payment_amount > 0),
    constraint payments_booked_after_initiated_ck
        check (booked_at is null or booked_at >= initiated_at),
    constraint payments_settled_after_booked_ck
        check (settled_at is null or (booked_at is not null and settled_at >= booked_at)),
    constraint payments_iban_ck
        check (counterparty_iban is null or counterparty_iban ~ '^[A-Z]{2}[0-9A-Z]{13,32}$')
);
