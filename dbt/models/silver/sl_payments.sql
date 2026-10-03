-- Payments, the latest state of each instruction, a soft-deleted one absent (specification 008
-- sections 2, 4 and 6), converted to EUR at the latest rate published at or before
-- `initiated_at`. Not `booked_at`: it is null until a payment books, on 1,074 payments at
-- planning, and the instruction's amount is fixed when it is raised.
with payments as (
    {{ latest_state(ref('br_corebank__payments'), ['payment_id']) }}
),

rates as (
    {{ fx_published_rates() }}
)

select
    p.*,
    {{ fx_provenance('p.payment_amount', 'p.payment_currency_code', 'p.initiated_at', 'r',
                     'payment') }}
from payments as p
asof left join rates as r
    on
        p.payment_currency_code = r.currency_code
        and p.initiated_at >= r.published_at
