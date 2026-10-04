{#
  Conversion to EUR (specification 008 section 6).

  `fx_publication_instant(rate_date)`: the nominal instant a rate is published, 16:00 in
  Europe/Berlin on its date, with the daylight-saving offset in force that day: 14:00 UTC in
  summer and 15:00 UTC in winter. One macro, so the conversion, `sl_fx_rates` and M7's freshness
  window cannot disagree about it.

  `fx_to_eur(amount, rate)`: an amount in a currency, divided by that currency's rate per euro,
  rounded half away from zero to four places, in exact integer arithmetic. DuckDB 1.5.5 returns
  DOUBLE from any division of decimals, from a decimal divided by an integer and from a HUGEINT
  divided by a HUGEINT with `/` (planning measured 3,171 wrong of 200,000 cases, every one an
  exact half), so nothing here divides with `/`:

  - `fx_minor_units`: the amount in units of 0.0001, and `fx_rate_units`: the rate in units of
    0.00000001, both HUGEINT. Each operand is widened to DECIMAL(38, s) before it is scaled:
    scaling a DECIMAL(18,8) rate by 10^8 in place overflows for a rate above 100, such as JPY's.
  - `fx_quotient_units`: the EUR amount in units of 0.0001 is a * 10^8 / r. With n = |a| * 10^8,
    the quotient rounded half away from zero is (2n + r) // 2r, an integer division of HUGEINTs
    (`fx_rounding_numerator` over `fx_rounding_denominator`), and the sign of a is put back.
  - `fx_units_to_eur`: back to DECIMAL by multiplying by 0.0001 as a DECIMAL(38,4)
    (`fx_scaled_units`), never by dividing by 10000: planning found the division path DOUBLE,
    and wrong on 12,345,678,901,234.5678 at 1.6. Then cast to the platform's DECIMAL(18,4).

  Every step is its own macro so `assert_fx_conversion_is_exact` can check the type of each one,
  not only of the result. A rate of zero fails the division, loudly; the feed never lands one.
#}

{% macro fx_publication_instant(rate_date) -%}
timezone('Europe/Berlin', cast({{ rate_date }} as timestamp) + interval 16 hour)
{%- endmacro %}

{% macro fx_minor_units(amount) -%}
cast(cast({{ amount }} as decimal(38,4)) * 10000 as hugeint)
{%- endmacro %}

{% macro fx_rate_units(rate) -%}
cast(cast({{ rate }} as decimal(38,8)) * 100000000 as hugeint)
{%- endmacro %}

{% macro fx_rounding_numerator(amount, rate) -%}
(2 * abs({{ fx_minor_units(amount) }}) * cast(100000000 as hugeint) + {{ fx_rate_units(rate) }})
{%- endmacro %}

{% macro fx_rounding_denominator(rate) -%}
(2 * {{ fx_rate_units(rate) }})
{%- endmacro %}

{% macro fx_quotient_units(amount, rate) -%}
(sign({{ fx_minor_units(amount) }})
  * ({{ fx_rounding_numerator(amount, rate) }} // {{ fx_rounding_denominator(rate) }}))
{%- endmacro %}

{% macro fx_scaled_units(units) -%}
(cast({{ units }} as decimal(38,0)) * cast(0.0001 as decimal(38,4)))
{%- endmacro %}

{% macro fx_units_to_eur(units) -%}
cast({{ fx_scaled_units(units) }} as decimal(18,4))
{%- endmacro %}

{% macro fx_to_eur(amount, rate) -%}
{{ fx_units_to_eur(fx_quotient_units(amount, rate)) }}
{%- endmacro %}

{#
  The rule a fact is converted by (specification 008 section 6): the latest rate whose
  publication instant is at or before the fact's business instant.

  `fx_published_rates()`: every landed rate, one per currency and rate date (re-reads collapse to
  the earliest batch), with its nominal publication instant, the publication instant of the next
  weekday after its date, and whether its date is the feed's latest publication: the greatest rate
  date landed for any currency. A fact joins it
  with `asof left join ... on currency and instant >= published_at`; `sl_fx_rates` joins it the
  same way at 23:59:59 UTC of each calendar date, so the two cannot disagree.

  `fx_next_weekday(d)`: Monday after a Friday or a Saturday, otherwise the next day. No holiday
  calendar: a TARGET holiday is a weekday on which the expected publication does not come.

  `fx_provenance(amount, currency, instant, rate, prefix)`: the converted amount and its
  provenance from the joined rate `rate`:
  - EUR converts at 1, with a null rate date, never carried, missing or provisional;
  - no rate at or before the instant: a null amount, never zero and never the original amount,
    and `fx_is_missing`;
  - `fx_is_carried`: the rate's date is not the fact's UTC business date, so the rate was carried
    to it: across a weekend or a holiday, or from the day before when the fact precedes the
    day's publication;
  - `fx_is_provisional`: the rate's date is the feed's latest publication date, across every
    currency, and the instant is after the publication instant of the next weekday after it, so
    the next publication has not landed yet and a rebuild may restate the row. Once any later
    publication has landed the conversion is final, even for a currency that publication does
    not carry: a TARGET holiday, or a currency the ECB has stopped quoting (BGN after 2025). Every
    final conversion is never restated by any rebuild.
#}

{% macro fx_next_weekday(rate_date) -%}
(cast({{ rate_date }} as date)
 + case dayofweek(cast({{ rate_date }} as date)) when 5 then 3 when 6 then 2 else 1 end)
{%- endmacro %}

{% macro utc_date(instant) -%}
cast(timezone('UTC', {{ instant }}) as date)
{%- endmacro %}

{% macro fx_published_rates() -%}
select
    quote_currency as currency_code,
    rate_date,
    rate,
    {{ fx_publication_instant('rate_date') }} as published_at,
    {{ fx_publication_instant(fx_next_weekday('rate_date')) }} as next_published_at,
    rate_date = max(rate_date) over () as is_latest_publication
from (
    select distinct on (quote_currency, rate_date) quote_currency, rate_date, rate
    from {{ ref('br_ecb__fx_rates') }}
    order by quote_currency, rate_date, _batch_id
)
{%- endmacro %}

{% macro fx_provenance(amount, currency, instant, rate, prefix) -%}
case
    when {{ currency }} = 'EUR' then cast({{ amount }} as decimal(18,4))
    when {{ rate }}.rate is null then null
    else {{ fx_to_eur(amount, rate ~ '.rate') }}
end as {{ prefix }}_amount_eur,
case when {{ currency }} = 'EUR' then cast(1 as decimal(18,8)) else {{ rate }}.rate end as fx_rate,
case when {{ currency }} = 'EUR' then null else {{ rate }}.rate_date end as fx_rate_date,
coalesce({{ currency }} <> 'EUR' and {{ fx_is_carried(rate, utc_date(instant)) }}, false)
    as fx_is_carried,
({{ currency }} <> 'EUR' and {{ rate }}.rate is null) as fx_is_missing,
coalesce({{ currency }} <> 'EUR' and {{ fx_is_provisional(rate, instant) }}, false)
    as fx_is_provisional
{%- endmacro %}

{#- The two flags, shared by every converted fact and by `sl_fx_rates`; null without a rate. -#}
{% macro fx_is_carried(rate, on_date) -%}
({{ rate }}.rate_date <> {{ on_date }})
{%- endmacro %}

{% macro fx_is_provisional(rate, instant) -%}
({{ rate }}.is_latest_publication and {{ instant }} > {{ rate }}.next_published_at)
{%- endmacro %}
