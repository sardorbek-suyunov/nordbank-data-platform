-- Transactions, the latest state of each, a soft-deleted one absent (specification 008 sections
-- 2, 4 and 6), converted to EUR at the latest rate published at or before `booked_at`, the
-- instant the transaction hit the account. Planning measured 946 of 2,226 convertible non-EUR
-- transactions choosing another rate date under this rule than under the as-of-date rule.
--
-- `is_customer_initiated` is the transaction type's flag in the version of `sl_transaction_types`
-- in force at `booked_at`: the one source of the flag (traceability gap 1), read as of the fact.
with transactions as (
    {{ latest_state(ref('br_corebank__transactions'), ['transaction_id']) }}
),

rates as (
    {{ fx_published_rates() }}
)

select
    t.*,
    k.is_customer_initiated,
    {{ fx_provenance('t.transaction_amount', 't.transaction_currency_code', 't.booked_at', 'r',
                     'transaction') }}
from transactions as t
left join {{ ref('sl_transaction_types') }} as k
    on
        t.transaction_type_code = k.code
        and t.booked_at >= k._valid_from
        and t.booked_at < k._valid_to
asof left join rates as r
    on
        t.transaction_currency_code = r.currency_code
        and t.booked_at >= r.published_at
