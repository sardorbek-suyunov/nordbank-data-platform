-- Merchants, one row per version (specification 008 sections 2 and 7). After the generator's
-- scripted additive drift a merchant can be revised in `merchant_risk_score` alone, a column the
-- contract omits: that observation changes no contract column and opens a version, because it
-- is a real source update the contract does not describe (planning: 59 such versions over 58
-- merchants). The acquirer's name is kept as sent beside one standardised representation.
select
    *,
    regexp_replace(trim(upper(strip_accents(merchant_name))), '\s+', ' ', 'g')
        as merchant_name_normalised
from (
    {{ scd2(ref('br_corebank__merchants'), ['merchant_id']) }}
)
