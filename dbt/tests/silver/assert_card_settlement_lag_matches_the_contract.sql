-- Every settlement line was cleared the contract's settlement lag before its settlement date
-- (specification 009 section 6): the lag the reconciliation will put the ledger on the file's
-- date by, read from the contract through `settlement_lag_days`, holds on the data it will be
-- applied to. The history's 9,801 winning lines all clear one day before they settle. Returns
-- each settlement and clearing date pair at another distance, with its count.
select
    settlement_date,
    clearing_date,
    count(*) as line_count
from {{ ref('sl_card_settlements') }}
where settlement_date - clearing_date <> {{ settlement_lag_days() }}
group by settlement_date, clearing_date
