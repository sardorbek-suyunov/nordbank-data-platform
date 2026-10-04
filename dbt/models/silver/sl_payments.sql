-- Payments, the latest state of each instruction, a soft-deleted one absent (specification 008
-- sections 2, 4 and 6), converted to EUR at the latest rate published at or before
-- `initiated_at`. Not `booked_at`: it is null until a payment books, on 1,074 payments at
-- planning, and the instruction's amount is fixed when it is raised.
--
-- `is_customer_initiated` is the payment type's flag in the version of `sl_payment_types` in force
-- at `initiated_at`.
with payments as (
    {{ latest_state(ref('br_corebank__payments'), ['payment_id']) }}
),

rates as (
    {{ fx_published_rates() }}
)

select
    p.*,
    k.is_customer_initiated,
    {{ fx_provenance('p.payment_amount', 'p.payment_currency_code', 'p.initiated_at', 'r',
                     'payment') }}
from payments as p
left join {{ ref('sl_payment_types') }} as k
    on
        p.payment_type_code = k.code
        and p.initiated_at >= k._valid_from
        and p.initiated_at < k._valid_to
asof left join rates as r
    on
        p.payment_currency_code = r.currency_code
        and p.initiated_at >= r.published_at
