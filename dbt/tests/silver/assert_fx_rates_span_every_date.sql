-- `sl_fx_rates` holds every calendar date for every currency, with no gap, from the currency's
-- first landed rate to the later of its last rate date and the last fact date (specification
-- 008 section 6, criterion 8). Returns each currency whose span differs.
with landed as (
    select
        quote_currency as currency_code,
        min(rate_date) as first_rate_date,
        max(rate_date) as last_rate_date
    from {{ ref('br_ecb__fx_rates') }}
    group by quote_currency
),

last_fact as (
    select
        greatest(
            (select max(cast(timezone('UTC', booked_at) as date)) from {{ ref('sl_transactions') }}),
            (select max(cast(timezone('UTC', initiated_at) as date)) from {{ ref('sl_payments') }})
        ) as last_fact_date
),

held as (
    select
        currency_code,
        min(calendar_date) as first_date,
        max(calendar_date) as last_date,
        count(*) as dates
    from {{ ref('sl_fx_rates') }}
    group by currency_code
)

select
    l.currency_code,
    h.first_date,
    h.last_date,
    h.dates
from landed as l
cross join last_fact as f
left join held as h on l.currency_code = h.currency_code
where
    h.currency_code is null
    or h.first_date <> l.first_rate_date
    or h.last_date <> greatest(l.last_rate_date, coalesce(f.last_fact_date, l.last_rate_date))
    or h.dates <> date_diff('day', h.first_date, h.last_date) + 1
