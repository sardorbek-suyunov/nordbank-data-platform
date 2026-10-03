-- The EUR conversion is exact, rounds half away from zero, and is never floating point
-- (specification 008 section 6, criteria 10 and 11).
--
-- Every case runs through each step of `fx_to_eur` separately, and a row is returned when a
-- step's type is not the exact type it must be, or when the result differs from the value
-- computed independently with Python's decimal module at 28 digits, half away from zero.
-- 1.23 at 1.6 and its negative are exact halves, where floating point rounds the wrong way;
-- 12,345,678,901,234.5678 at 1.6 is where scaling back by division instead of multiplication
-- was wrong in the last digit; 10,000 at 161.42 needs the rate widened before it is scaled.
with cases (case_name, amount, rate, expected) as (
    values
    ('the known value', 100.00, 1.1426, 87.5197),
    ('an exact half', 1.23, 1.6, 0.7688),
    ('an exact half, negative', -1.23, 1.6, -0.7688),
    ('a large amount', 12345678901234.5678, 1.6, 7716049313271.6049),
    ('a large amount, negative', -12345678901234.5678, 1.6, -7716049313271.6049),
    ('a rate above 100', 10000.00, 161.42, 61.9502),
    ('the largest amount', 99999999999999.9999, 1.6, 62499999999999.9999),
    ('zero', 0, 1.1426, 0),
    ('below the last place', 0.0001, 185.54, 0),
    ('half the last place', 0.0002, 4, 0.0001),
    ('half the last place, negative', -0.0002, 4, -0.0001),
    ('a rate below one', 250.00, 0.84888, 294.5057)
),

typed as (
    select
        case_name,
        cast(expected as decimal(18, 4)) as expected,
        cast(amount as decimal(18, 4)) as amount,
        cast(rate as decimal(18, 8)) as rate
    from cases
),

steps as (
    select
        case_name,
        expected,
        typeof({{ fx_minor_units('amount') }}) as minor_units_type,
        typeof({{ fx_rate_units('rate') }}) as rate_units_type,
        typeof({{ fx_rounding_numerator('amount', 'rate') }}) as numerator_type,
        typeof({{ fx_rounding_denominator('rate') }}) as denominator_type,
        typeof({{ fx_quotient_units('amount', 'rate') }}) as quotient_type,
        typeof({{ fx_scaled_units(fx_quotient_units('amount', 'rate')) }}) as scaled_type,
        typeof({{ fx_to_eur('amount', 'rate') }}) as result_type,
        {{ fx_to_eur('amount', 'rate') }} as result
    from typed
)

select *
from steps
where
    minor_units_type <> 'HUGEINT'
    or rate_units_type <> 'HUGEINT'
    or numerator_type <> 'HUGEINT'
    or denominator_type <> 'HUGEINT'
    or quotient_type <> 'HUGEINT'
    or scaled_type <> 'DECIMAL(38,4)'
    or result_type <> 'DECIMAL(18,4)'
    or result is distinct from expected
