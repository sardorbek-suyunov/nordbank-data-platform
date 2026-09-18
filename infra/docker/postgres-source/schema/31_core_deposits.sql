-- Spec 002 section 2: accounts and the holder bridge.

create table if not exists core.accounts (
    account_id              bigint generated always as identity primary key,
    account_number          varchar(40)  not null unique,
    iban                    varchar(34)  unique,
    account_type_code       varchar(40)  not null references ref.account_types (code),
    currency_code           character(3) not null references ref.currencies (code),
    country_code            character(2) not null references ref.countries (code),
    account_status_code     varchar(40)  not null references ref.account_statuses (code),
    opened_date             date         not null,
    closed_date             date,
    overdraft_limit_amount  numeric(18,4) not null default 0,
    -- Signed: an overdrawn account carries a negative balance, and the deposit balance metric
    -- sums it as a net liability position rather than as a sum of positive balances.
    current_balance_amount  numeric(18,4) not null default 0,
    created_at              timestamptz  not null default now(),
    updated_at              timestamptz  not null default now(),
    is_deleted              boolean      not null default false,
    constraint accounts_closed_after_opened_ck
        check (closed_date is null or closed_date >= opened_date),
    constraint accounts_overdraft_limit_ck check (overdraft_limit_amount >= 0),
    constraint accounts_iban_ck            check (iban is null or iban ~ '^[A-Z]{2}[0-9A-Z]{13,32}$')
);

create table if not exists core.account_holders (
    account_holder_id bigint generated always as identity primary key,
    account_id        bigint        not null references core.accounts (account_id),
    customer_id       bigint        not null references core.customers (customer_id),
    holder_role_code  varchar(40)   not null references ref.holder_roles (code),
    -- Null for a role that carries no ownership, such as an authorised signatory. Design rule
    -- 10: every value present is in (0,1], and absence is distinguishable from zero. The rule
    -- that weights sum to one per account is a set property and is checked in dq at M7.
    ownership_weight  numeric(18,8),
    created_at        timestamptz   not null default now(),
    updated_at        timestamptz   not null default now(),
    is_deleted        boolean       not null default false,
    constraint account_holders_uq unique (account_id, customer_id),
    constraint account_holders_ownership_weight_ck
        check (ownership_weight is null or (ownership_weight > 0 and ownership_weight <= 1))
);
