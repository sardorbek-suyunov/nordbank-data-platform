-- Every landed rate is quoted against the euro, as currency per euro, which is what the
-- conversion divides by (specification 008 section 6; planning, T2). A rate quoted against any
-- other base would convert silently wrong. Returns each other base and its count.
select
    base_currency,
    count(*) as rates
from {{ ref('br_ecb__fx_rates') }}
where base_currency is distinct from 'EUR'
group by base_currency
