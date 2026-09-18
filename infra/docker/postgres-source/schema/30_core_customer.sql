-- Spec 002 section 2: customers and their address history.

create table if not exists core.customers (
    customer_id         bigint generated always as identity primary key,
    customer_reference  varchar(40)  not null unique,
    full_name           varchar(200) not null,
    email               varchar(320),
    phone               varchar(40),
    national_identifier varchar(60),
    date_of_birth       date         not null,
    -- Closed domain, no named consumer of any property of it, and classified sensitive, so its
    -- vocabulary is not published as a joinable dimension. Value list in the data dictionary.
    kyc_status_code     varchar(40)  not null,
    risk_band_code      varchar(40)  references ref.risk_bands (code),
    signup_date         date         not null,
    residence_country_code character(2) not null references ref.countries (code),
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),
    is_deleted          boolean      not null default false,
    constraint customers_kyc_status_ck
        check (kyc_status_code in ('pending', 'verified', 'review', 'rejected', 'expired')),
    constraint customers_date_of_birth_ck check (date_of_birth < current_date),
    constraint customers_signup_after_birth_ck check (signup_date > date_of_birth)
);

create table if not exists core.customer_addresses (
    customer_address_id bigint generated always as identity primary key,
    customer_id         bigint       not null references core.customers (customer_id),
    -- Closed two-value domain whose only candidate attribute would restate the code.
    address_type_code   varchar(40)  not null,
    address_line_1      varchar(200) not null,
    address_line_2      varchar(200),
    city                varchar(120) not null,
    -- Stored in full and classified quasi-identifier. The district used before gold is
    -- left(postal_code, 2), a deliberate simplification: EU postcode formats differ and a
    -- production system would apply a country-specific rule. See docs/pii_classification.md.
    postal_code         varchar(20)  not null,
    country_code        character(2) not null references ref.countries (code),
    valid_from_date     date         not null,
    valid_to_date       date,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),
    is_deleted          boolean      not null default false,
    constraint customer_addresses_type_ck
        check (address_type_code in ('residential', 'correspondence')),
    constraint customer_addresses_version_uq
        unique (customer_id, address_type_code, valid_from_date),
    constraint customer_addresses_validity_ck
        check (valid_to_date is null or valid_to_date >= valid_from_date)
);
