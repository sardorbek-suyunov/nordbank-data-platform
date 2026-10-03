-- Transactions, the latest state of each, a soft-deleted one absent (specification 008 sections
-- 2, 4 and 6), converted to EUR at the latest rate published at or before `booked_at`, the
-- instant the transaction hit the account. Planning measured 946 of 2,226 convertible non-EUR
-- transactions choosing another rate date under this rule than under the as-of-date rule.
with transactions as (
    {{ latest_state(ref('br_corebank__transactions'), ['transaction_id']) }}
),

rates as (
    {{ fx_published_rates() }}
)

select
    t.*,
    {{ fx_provenance('t.transaction_amount', 't.transaction_currency_code', 't.booked_at', 'r',
                     'transaction') }}
from transactions as t
asof left join rates as r
    on
        t.transaction_currency_code = r.currency_code
        and t.booked_at >= r.published_at
