-- A backdated change joins by business validity, read from the current system-time version
-- (specification 008 section 3, criterion 5). The data holds no backdated address change, so
-- this one is planted:
--
--   2026-01-10  address 10 recorded, valid from 2026-01-01, open
--   2026-08-10  a move recorded late: address 11 valid from 2026-06-01, and address 10 closed on
--               2026-05-31, its inclusive last day
--
-- A fact dated 2026-07-15, before the move was recorded, resolves to address 11; a bitemporal
-- join would give address 10. The last inclusive day, 2026-05-31, still resolves to address 10,
-- and the first day of the move to 11. Returns every fact that does not resolve to exactly the
-- expected address.
with fixture (
    customer_address_id, customer_id, city, valid_from_date, valid_to_date,
    created_at, updated_at, is_deleted, _batch_id
) as (
    values
    (
        10, 7, 'Lyon', date '2026-01-01', null::date,
        timestamptz '2026-01-10 08:00:00+00', timestamptz '2026-01-10 08:00:00+00', false, 'b-0110'
    ),
    (
        10, 7, 'Lyon', date '2026-01-01', date '2026-05-31',
        timestamptz '2026-01-10 08:00:00+00', timestamptz '2026-08-10 08:00:00+00', false, 'b-0810'
    ),
    (
        11, 7, 'Porto', date '2026-06-01', null::date,
        timestamptz '2026-08-10 08:00:00+00', timestamptz '2026-08-10 08:00:00+00', false, 'b-0810'
    )
),

addresses as (
    select
        *,
        valid_from_date as _business_valid_from_date,
        {{ business_valid_to('valid_to_date') }} as _business_valid_to_date
    from (
        {{ scd2('fixture', ['customer_address_id'],
                columns=['customer_address_id', 'customer_id', 'city', 'valid_from_date',
                         'valid_to_date', 'created_at', 'updated_at', 'is_deleted', '_batch_id']) }}
    )
),

facts (fact_id, customer_id, fact_date, expected_address_id) as (
    values
    (1, 7, date '2026-03-01', 10),
    (2, 7, date '2026-05-31', 10),
    (3, 7, date '2026-06-01', 11),
    (4, 7, date '2026-07-15', 11),
    (5, 7, date '2026-09-01', 11)
),

resolved as (
    select
        f.fact_id,
        f.expected_address_id,
        count(a.customer_address_id) as matches,
        min(a.customer_address_id) as address_id
    from facts as f
    left join addresses as a
        on
            f.customer_id = a.customer_id
            and {{ business_valid_on('f.fact_date', 'a') }}
    group by f.fact_id, f.expected_address_id
)

select *
from resolved
where matches <> 1 or address_id <> expected_address_id
