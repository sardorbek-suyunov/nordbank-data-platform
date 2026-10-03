{#
  Quasi-identifier generalisation (specification 008 section 8).

  `customer_bands(kind, anchor_column, band_column)`: for every customer, the half-open intervals
  during which each band of `kind` applies, from the anchor date in the customer's latest
  version (`date_of_birth` for age, `signup_date` for tenure) plus each band's lower edge to the
  anchor plus its upper edge, the last band open to 9999-12-31. The edges tile from zero
  (`assert_generalisation_bands_tile_from_zero`), so each customer's intervals are contiguous.

  A 29 February anchor reaches each band on 28 February in a year that has none, which is how
  DuckDB adds years to a date. The interval dates encode the anchor itself, the first one exactly,
  so they are classified as the quasi-identifier they come from: a fact joins on them, and only
  the band reaches gold.
#}
{% macro customer_bands(kind, anchor_column, band_column) -%}
with customers as (
    select distinct on (customer_id) customer_id, {{ anchor_column }} as anchor_date
      from {{ ref('sl_customers') }}
     order by customer_id, _valid_from desc
),

bands as (
    select band, lower_years, upper_years
      from {{ ref('seed_generalisation_bands') }}
     where band_kind = '{{ kind }}'
)

select
    c.customer_id,
    b.band as {{ band_column }},
    cast(c.anchor_date + to_years(b.lower_years) as date) as _valid_from_date,
    coalesce(cast(c.anchor_date + to_years(b.upper_years) as date), date '9999-12-31')
        as _valid_to_date
  from customers c
 cross join bands b
{%- endmacro %}
