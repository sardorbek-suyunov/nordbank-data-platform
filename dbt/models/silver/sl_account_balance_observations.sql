-- Every balance silver observed on an account (specification 008 section 2): the measure
-- `sl_accounts` excludes from its projection, kept as its own fact. One row per account per
-- observation, deduplicated like every other read, the earliest batch winning; the observation
-- that recorded a soft delete is not a balance. M7 reconciles these against the balances it
-- derives from transactions and payments.
with observed as (
    select distinct on (account_id, updated_at)
        account_id,
        updated_at,
        current_balance_amount,
        currency_code,
        is_deleted,
        _batch_id
    from {{ ref('br_corebank__accounts') }}
    order by account_id, updated_at, _batch_id
)

select
    account_id,
    updated_at as observed_at,
    current_balance_amount,
    currency_code,
    _batch_id
from observed
where not is_deleted
