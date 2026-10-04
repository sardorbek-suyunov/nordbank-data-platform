-- Card settlement lines (specification 009 sections 1 and 2): every detail line of each
-- settlement date and sender sequence's winning revision, resolved to the bank's card,
-- transaction and merchant without correction. The winner is chosen once, from lines and
-- trailers together, by the macro `sl_card_settlement_totals` uses too, so the two never mix
-- revisions. No amount is converted: a line has a settlement date and no instant, and the
-- reconciliation compares in the settlement currency. Column names follow the conventions, the
-- contract's in bronze (traceability.md).
with lines as (
    select * from {{ ref('br_cardnet__settlements') }}
),

trailers as (
    select * from {{ ref('br_cardnet__settlement_totals') }}
),

winning as (
    {{ settlement_winning_records('lines', 'lines', 'trailers') }}
),

resolved as (
    {{ settlement_resolution('winning', ref('sl_cards'), ref('sl_transactions')) }}
)

select
    record_type,
    transaction_reference,
    transaction_date,
    clearing_date,
    network,
    card_reference,
    masked_pan,
    merchant_category_code,
    merchant_name,
    presentment,
    settlement_currency as settlement_currency_code,
    settlement_amount,
    settlement_date,
    file_sequence,
    processor_id,
    revision,
    file_created_at,
    _batch_id,
    card_id,
    transaction_id,
    merchant_id
from resolved
