-- Spec 002 section 2: cards, merchants and the partner agent network.
--
-- Every character column on core.cards is held to twelve characters or fewer. A card number is
-- thirteen to nineteen digits, so no column on this table can hold one. That is the second of
-- the three checks acceptance criterion 4 is measured by, and it is why card_product_code and
-- card_status_code are narrower here than a code column elsewhere in the schema.

create table if not exists core.cards (
    card_id           bigint generated always as identity primary key,
    card_reference    character(12) not null unique,
    account_id        bigint        not null references core.accounts (account_id),
    card_product_code varchar(12)   not null references ref.card_products (code),
    card_bin          character(6)  not null,
    card_last_four    character(4)  not null,
    -- Closed domain fixed by the card lifecycle; value list in the data dictionary.
    card_status_code  varchar(12)   not null,
    issued_date       date          not null,
    expiry_date       date          not null,
    created_at        timestamptz   not null default now(),
    updated_at        timestamptz   not null default now(),
    is_deleted        boolean       not null default false,
    constraint cards_bin_ck        check (card_bin ~ '^[0-9]{6}$'),
    constraint cards_last_four_ck  check (card_last_four ~ '^[0-9]{4}$'),
    constraint cards_reference_ck  check (card_reference ~ '^[A-Z]{2}[0-9]{10}$'),
    constraint cards_status_ck
        check (card_status_code in ('issued', 'active', 'blocked', 'expired', 'cancelled', 'replaced')),
    constraint cards_expiry_after_issue_ck check (expiry_date > issued_date)
);

create table if not exists core.merchants (
    merchant_id        bigint generated always as identity primary key,
    merchant_reference varchar(40)  not null unique,
    -- Dirty by design: casing, punctuation and trailing location noise are left as the acquirer
    -- sent them, so that conformance has something real to do in silver.
    merchant_name      varchar(200) not null,
    mcc_code           character(4) not null references ref.mcc_codes (code),
    country_code       character(2) not null references ref.countries (code),
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now(),
    is_deleted         boolean      not null default false
);

create table if not exists core.agent_locations (
    agent_location_id        bigint generated always as identity primary key,
    agent_location_reference varchar(40)  not null unique,
    partner_name             varchar(200) not null,
    address_line             varchar(200),
    city                     varchar(120),
    postal_code              varchar(20),
    country_code             character(2) not null references ref.countries (code),
    active_from_date         date         not null,
    active_to_date           date,
    created_at               timestamptz  not null default now(),
    updated_at               timestamptz  not null default now(),
    is_deleted               boolean      not null default false,
    constraint agent_locations_validity_ck
        check (active_to_date is null or active_to_date >= active_from_date)
);
