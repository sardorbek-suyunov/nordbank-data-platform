-- Spec 002 section 1.2. Three tables exist because a value is a join key shared between two
-- tables, and a string that must match across tables without a foreign key produces a silent
-- no-match rather than an error. interchange_rates is the case that forces all three.

create table if not exists ref.regions (
    region_id   bigint generated always as identity primary key,
    code        varchar(40)  not null unique,
    name        varchar(120) not null,
    description text,
    is_active   boolean      not null default true,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now()
);

create table if not exists ref.card_product_classes (
    card_product_class_id bigint generated always as identity primary key,
    code                  varchar(40)  not null unique,
    name                  varchar(120) not null,
    description           text,
    is_active             boolean      not null default true,
    created_at            timestamptz  not null default now(),
    updated_at            timestamptz  not null default now()
);

create table if not exists ref.mcc_bands (
    mcc_band_id bigint generated always as identity primary key,
    code        varchar(40)  not null unique,
    name        varchar(120) not null,
    description text,
    is_active   boolean      not null default true,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now()
);
