-- `sl_fx_rates` agrees with the conversion rule evaluated at 23:59:59 UTC for every currency and
-- date, the provisional flag included (specification 008 section 6, criterion 13).
--
-- The rule is restated here without the macros the model and the facts share: the rate in force
-- is the one with the greatest date whose 16:00 Europe/Berlin publication is at or before the
-- end of the day; it is carried when its date is not the day; it is provisional when its date is
-- the latest publication date landed for any currency and the end of the day is after 16:00
-- Europe/Berlin on the next weekday after its date. Returns every row that disagrees.
with landed as (
    select distinct
        quote_currency as currency_code,
        rate_date,
        rate
    from {{ ref('br_ecb__fx_rates') }}
),

latest as (
    select max(rate_date) as latest_rate_date
    from landed
),

expected as (
    select
        s.currency_code,
        s.calendar_date,
        (
            select max(l.rate_date)
            from landed as l
            where
                l.currency_code = s.currency_code
                and timezone('Europe/Berlin', l.rate_date + interval 16 hour)
                <= timezone('UTC', s.calendar_date + interval '23:59:59')
        ) as rate_date
    from {{ ref('sl_fx_rates') }} as s
),

judged as (
    select
        e.currency_code,
        e.calendar_date,
        e.rate_date as expected_rate_date,
        s.fx_rate_date,
        l.rate as expected_rate,
        s.fx_rate,
        s.fx_is_carried,
        s.fx_is_provisional,
        (e.rate_date <> e.calendar_date) as expected_carried,
        (
            e.rate_date = t.latest_rate_date
            and timezone('UTC', e.calendar_date + interval '23:59:59')
            > timezone(
                'Europe/Berlin',
                e.rate_date
                + case dayofweek(e.rate_date) when 5 then 3 when 6 then 2 else 1 end
                + interval 16 hour
            )
        ) as expected_provisional
    from expected as e
    inner join {{ ref('sl_fx_rates') }} as s
        on e.currency_code = s.currency_code and e.calendar_date = s.calendar_date
    left join landed as l
        on e.currency_code = l.currency_code and e.rate_date = l.rate_date
    cross join latest as t
)

select *
from judged
where
    expected_rate_date is distinct from fx_rate_date
    or expected_rate is distinct from fx_rate
    or expected_carried is distinct from fx_is_carried
    or expected_provisional is distinct from fx_is_provisional
