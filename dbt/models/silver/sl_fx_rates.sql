-- One rate per currency per calendar date (specification 008 section 6): the conversion rule
-- evaluated at 23:59:59 UTC of the date, so the row says which rate a fact at the end of that day
-- converts at, whether it was carried there and whether it is still provisional. Gap-filled
-- forward from each currency's first landed rate to the later of its last rate date and the last
-- fact date, across weekends, TARGET holidays and days whose rate has not landed yet.
--
-- Rates are quoted as currency per euro, as the ECB publishes them and Frankfurter serves them.
with rates as (
    {{ fx_published_rates() }}
),

last_fact as (
    select max(fact_date) as last_fact_date
    from (
        select max({{ utc_date('booked_at') }}) as fact_date from {{ ref('sl_transactions') }}
        union all
        select max({{ utc_date('initiated_at') }}) as fact_date from {{ ref('sl_payments') }}
    )
),

spans as (
    select
        currency_code,
        min(rate_date) as first_rate_date,
        max(rate_date) as last_rate_date
    from rates
    group by currency_code
),

calendar as (
    select
        s.currency_code,
        cast(
            unnest(
                generate_series(
                    s.first_rate_date,
                    greatest(s.last_rate_date, coalesce(f.last_fact_date, s.last_rate_date)),
                    interval 1 day
                )
            ) as date
        ) as calendar_date
    from spans as s
    cross join last_fact as f
),

days as (
    select
        currency_code,
        calendar_date,
        timezone('UTC', cast(calendar_date as timestamp) + interval '23:59:59') as end_of_day
    from calendar
)

select
    d.currency_code,
    d.calendar_date,
    r.rate as fx_rate,
    r.rate_date as fx_rate_date,
    r.published_at as fx_published_at,
    {{ fx_is_carried('r', 'd.calendar_date') }} as fx_is_carried,
    {{ fx_is_provisional('r', 'd.end_of_day') }} as fx_is_provisional
from days as d
asof left join rates as r
    on
        d.currency_code = r.currency_code
        and d.end_of_day >= r.published_at
