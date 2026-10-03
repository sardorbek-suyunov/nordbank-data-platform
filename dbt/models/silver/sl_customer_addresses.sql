-- Customer addresses, one row per system-time version (specification 008 sections 2, 3 and 8).
--
-- Two clocks: `_valid_from` and `_valid_to` say when silver learned each state; the business
-- validity the source records, `valid_from_date` to an inclusive `valid_to_date`, is exposed
-- half-open beside it. A fact joins on business validity, read from the current system-time
-- version (`business_valid_on`), so a correction recorded late moves a past fact's geography;
-- a bitemporal join is the rejected alternative.
--
-- The postal code is a quasi-identifier and stays in the clear here; `postal_district` is its
-- generalisation, the one that may reach gold.
select
    *,
    valid_from_date as _business_valid_from_date,
    {{ business_valid_to('valid_to_date') }} as _business_valid_to_date,
    left(postal_code, 2) as postal_district
from (
    {{ scd2(ref('br_corebank__customer_addresses'), ['customer_address_id']) }}
)
