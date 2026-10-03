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
