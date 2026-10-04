-- Card settlement totals (specification 009 sections 1 and 3): one row per trailer of each
-- settlement date and sender sequence's winning revision, chosen by the macro the lines use.
-- The processor's count and total cover every detail record it wrote, so a quarantined line
-- makes the lines and the trailer differ, legitimately; silver compares nothing. Column
-- names follow the conventions, the contract's in bronze (traceability.md).
with lines as (
    select * from {{ ref('br_cardnet__settlements') }}
),

trailers as (
    select * from {{ ref('br_cardnet__settlement_totals') }}
),

winning as (
    {{ settlement_winning_records('trailers', 'lines', 'trailers') }}
)

select
    record_type,
    network,
    settlement_currency as settlement_currency_code,
    record_count,
    amount_total as file_total_amount,
    settlement_date,
    file_sequence,
    processor_id,
    revision,
    file_created_at,
    _batch_id
from winning
