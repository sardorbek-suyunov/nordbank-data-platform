-- Spec 002 section 2: the lending funnel, the loan book and its repayment schedule.

create table if not exists core.loan_applications (
    loan_application_id         bigint generated always as identity primary key,
    application_reference       varchar(40)  not null unique,
    customer_id                 bigint       not null references core.customers (customer_id),
    loan_product_code           varchar(40)  not null references ref.loan_products (code),
    loan_application_status_code varchar(40) not null references ref.loan_application_statuses (code),
    -- Assigned at decision, so null until then. The approval rate rule uses the band assigned
    -- at decision time, not the customer's band today.
    risk_band_code              varchar(40)  references ref.risk_bands (code),
    decision_reason_code        varchar(40)  references ref.decision_reasons (code),
    applied_at                  timestamptz  not null,
    decided_at                  timestamptz,
    requested_amount            numeric(18,4) not null,
    approved_amount             numeric(18,4),
    application_currency_code   character(3) not null references ref.currencies (code),
    created_at                  timestamptz  not null default now(),
    updated_at                  timestamptz  not null default now(),
    is_deleted                  boolean      not null default false,
    constraint loan_applications_requested_amount_ck check (requested_amount > 0),
    constraint loan_applications_approved_amount_ck
        check (approved_amount is null or approved_amount >= 0),
    constraint loan_applications_decided_after_applied_ck
        check (decided_at is null or decided_at >= applied_at)
);

create table if not exists core.loans (
    loan_id              bigint generated always as identity primary key,
    loan_reference       varchar(40)  not null unique,
    loan_application_id  bigint       not null references core.loan_applications (loan_application_id),
    customer_id          bigint       not null references core.customers (customer_id),
    loan_product_code    varchar(40)  not null references ref.loan_products (code),
    loan_status_code     varchar(40)  not null references ref.loan_statuses (code),
    -- The origination vintage is the month of this date, never the application or approval
    -- month: money leaving the bank is what starts the risk.
    disbursed_date       date         not null,
    maturity_date        date         not null,
    written_off_date     date,
    principal_amount     numeric(18,4) not null,
    loan_currency_code   character(3) not null references ref.currencies (code),
    -- The rate in force for this loan, copied from the product at disbursement. The product
    -- rate changes over time; a disbursed loan keeps the terms it was written under.
    nominal_annual_rate  numeric(18,8) not null,
    term_months          smallint     not null,
    created_at           timestamptz  not null default now(),
    updated_at           timestamptz  not null default now(),
    is_deleted           boolean      not null default false,
    constraint loans_principal_ck        check (principal_amount > 0),
    constraint loans_rate_ck             check (nominal_annual_rate >= 0 and nominal_annual_rate <= 1),
    constraint loans_term_months_ck      check (term_months > 0),
    constraint loans_maturity_after_disbursement_ck check (maturity_date >= disbursed_date),
    constraint loans_written_off_after_disbursement_ck
        check (written_off_date is null or written_off_date >= disbursed_date)
);

create table if not exists core.loan_installments (
    loan_installment_id       bigint generated always as identity primary key,
    loan_id                   bigint       not null references core.loans (loan_id),
    installment_number        smallint     not null,
    due_date                  date         not null,
    due_amount                numeric(18,4) not null,
    paid_amount               numeric(18,4) not null default 0,
    paid_at                   timestamptz,
    installment_currency_code character(3) not null references ref.currencies (code),
    created_at                timestamptz  not null default now(),
    updated_at                timestamptz  not null default now(),
    is_deleted                boolean      not null default false,
    constraint loan_installments_uq unique (loan_id, installment_number),
    constraint loan_installments_number_ck      check (installment_number > 0),
    constraint loan_installments_due_amount_ck  check (due_amount >= 0),
    constraint loan_installments_paid_amount_ck check (paid_amount >= 0)
);
