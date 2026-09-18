-- Spec 002 section 1.1, the twelve domain tables the specification names.

create table if not exists ref.currencies (
    currency_id bigint generated always as identity primary key,
    code        character(3) not null unique,
    name        varchar(120) not null,
    -- ISO 4217 exponent: the number of decimal places the currency is quoted in. It is not the
    -- storage scale, which is always 4 (design rule 1); it is what a presentation layer rounds
    -- to, and what tells a reader that JPY at 100.0000 is a whole number of yen.
    minor_unit  smallint     not null,
    description text,
    is_active   boolean      not null default true,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now(),
    constraint currencies_code_ck       check (code ~ '^[A-Z]{3}$'),
    constraint currencies_minor_unit_ck check (minor_unit between 0 and 4)
);

create table if not exists ref.countries (
    country_id  bigint generated always as identity primary key,
    code        character(2) not null unique,
    name        varchar(120) not null,
    region_code varchar(40)  not null references ref.regions (code),
    is_eea      boolean      not null,
    is_sepa     boolean      not null,
    description text,
    is_active   boolean      not null default true,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now(),
    constraint countries_code_ck check (code ~ '^[A-Z]{2}$'),
    -- Every EEA state is in SEPA; the converse does not hold, which is why both flags exist.
    constraint countries_eea_implies_sepa_ck check (not is_eea or is_sepa)
);

create table if not exists ref.mcc_codes (
    mcc_code_id bigint generated always as identity primary key,
    code        character(4) not null unique,
    name        varchar(120) not null,
    category    varchar(80)  not null,
    band_code   varchar(40)  not null references ref.mcc_bands (code),
    description text,
    is_active   boolean      not null default true,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now(),
    constraint mcc_codes_code_ck check (code ~ '^[0-9]{4}$')
);

create table if not exists ref.account_types (
    account_type_id    bigint generated always as identity primary key,
    code               varchar(40)  not null unique,
    name               varchar(120) not null,
    -- Not a lookup table: used by this table alone, so the stopping rule in design rule 7 keeps
    -- it a column. The value list is documented in docs/data_dictionary.md.
    product_class_code varchar(40)  not null,
    is_deposit_taking  boolean      not null,
    description        text,
    is_active          boolean      not null default true,
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now(),
    constraint account_types_product_class_ck
        check (product_class_code in ('current', 'savings', 'loan', 'card_settlement', 'internal'))
);

create table if not exists ref.transaction_types (
    transaction_type_id   bigint generated always as identity primary key,
    code                  varchar(40)  not null unique,
    name                  varchar(120) not null,
    -- The single source of the derived flag in metric_definitions.md. It lives here, not in
    -- code, and core.transactions reaches it through the foreign key.
    is_customer_initiated boolean      not null,
    -- Direction is a property of the type, so core.transactions does not carry a second copy.
    direction             varchar(10)  not null,
    description           text,
    is_active             boolean      not null default true,
    created_at            timestamptz  not null default now(),
    updated_at            timestamptz  not null default now(),
    constraint transaction_types_direction_ck check (direction in ('debit', 'credit'))
);

create table if not exists ref.channels (
    channel_id  bigint generated always as identity primary key,
    code        varchar(40)  not null unique,
    name        varchar(120) not null,
    is_digital  boolean      not null,
    description text,
    is_active   boolean      not null default true,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now()
);

create table if not exists ref.payment_schemes (
    payment_scheme_id bigint generated always as identity primary key,
    code              varchar(40)  not null unique,
    name              varchar(120) not null,
    is_sepa           boolean      not null,
    settlement_days   smallint     not null,
    description       text,
    is_active         boolean      not null default true,
    created_at        timestamptz  not null default now(),
    updated_at        timestamptz  not null default now(),
    constraint payment_schemes_settlement_days_ck check (settlement_days >= 0)
);

create table if not exists ref.card_products (
    card_product_id    bigint generated always as identity primary key,
    -- Constrained to 12 characters because core.cards.card_product_code references it, and
    -- every character column on core.cards is held under thirteen so that no column on that
    -- table can hold a card number (acceptance criterion 4).
    code               varchar(12)  not null unique,
    name               varchar(120) not null,
    product_class_code varchar(40)  not null references ref.card_product_classes (code),
    network            varchar(40)  not null,
    is_commercial      boolean      not null,
    description        text,
    is_active          boolean      not null default true,
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now()
);

create table if not exists ref.loan_products (
    loan_product_id     bigint generated always as identity primary key,
    code                varchar(40)   not null unique,
    name                varchar(120)  not null,
    nominal_annual_rate numeric(18,8) not null,
    term_months         smallint      not null,
    is_secured          boolean       not null,
    description         text,
    is_active           boolean       not null default true,
    created_at          timestamptz   not null default now(),
    updated_at          timestamptz   not null default now(),
    constraint loan_products_rate_ck        check (nominal_annual_rate >= 0 and nominal_annual_rate <= 1),
    constraint loan_products_term_months_ck check (term_months > 0)
);

-- Not a code vocabulary and therefore the one table in ref with no code column: it is a rate
-- table keyed on a composite, and design rule 7 addresses lookups of codes. Documented as an
-- exception in docs/data_dictionary.md.
create table if not exists ref.interchange_rates (
    interchange_rate_id     bigint generated always as identity primary key,
    card_product_class_code varchar(40) not null references ref.card_product_classes (code),
    merchant_region_code    varchar(40) not null references ref.regions (code),
    mcc_band_code           varchar(40) not null references ref.mcc_bands (code),
    -- Null where the rate is undecided (metric_definitions.md). A null is an error condition at
    -- M6 and is never treated as zero: spec 002 section 11.
    rate                    numeric(18,8),
    valid_from_date         date        not null,
    valid_to_date           date,
    description             text,
    is_active               boolean     not null default true,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),
    constraint interchange_rates_key_uq
        unique (card_product_class_code, merchant_region_code, mcc_band_code, valid_from_date),
    constraint interchange_rates_validity_ck
        check (valid_to_date is null or valid_to_date >= valid_from_date),
    constraint interchange_rates_rate_ck
        check (rate is null or (rate >= 0 and rate <= 1))
);

create table if not exists ref.gl_accounts (
    gl_account_id      bigint generated always as identity primary key,
    code               varchar(40)  not null unique,
    name               varchar(120) not null,
    gl_account_type_code varchar(40) not null references ref.gl_account_types (code),
    description        text,
    is_active          boolean      not null default true,
    created_at         timestamptz  not null default now(),
    updated_at         timestamptz  not null default now()
);

create table if not exists ref.fraud_rules (
    fraud_rule_id bigint generated always as identity primary key,
    code          varchar(40)  not null unique,
    name          varchar(120) not null,
    description   text,
    is_active     boolean      not null default true,
    created_at    timestamptz  not null default now(),
    updated_at    timestamptz  not null default now()
);
