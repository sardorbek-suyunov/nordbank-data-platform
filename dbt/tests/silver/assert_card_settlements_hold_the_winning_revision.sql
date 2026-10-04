-- The settlement models hold exactly the records of each settlement date and sequence's highest
-- revision in bronze (specification 009 sections 1 to 3, criterion 1), restated here without the
-- settlement macros: the highest revision declared across lines and trailers together, every
-- line and trailer of it, and nothing of a lower one. Counted with duplicates, so a line that
-- resolution repeated, a card joined to each of its versions, is a line too many and fails here.
-- A winning declaration that two batches landed, which ingestion refuses, would double its
-- records and is reported as such. Returns each record missing or unexpected, by its key.
with declared as (
    select
        'line' as record,
        settlement_date,
        file_sequence,
        revision,
        _batch_id,
        transaction_reference as record_key
    from {{ ref('br_cardnet__settlements') }}
    union all
    select
        'trailer' as record,
        settlement_date,
        file_sequence,
        revision,
        _batch_id,
        network || '/' || settlement_currency as record_key
    from {{ ref('br_cardnet__settlement_totals') }}
),

judged as (
    select
        *,
        max(revision) over (partition by settlement_date, file_sequence) as highest
    from declared
),

expected as (
    select
        record,
        settlement_date,
        file_sequence,
        revision,
        _batch_id,
        record_key
    from judged
    where revision = highest
),

produced as (
    select
        'line' as record,
        settlement_date,
        file_sequence,
        revision,
        _batch_id,
        transaction_reference as record_key
    from {{ ref('sl_card_settlements') }}
    union all
    select
        'trailer' as record,
        settlement_date,
        file_sequence,
        revision,
        _batch_id,
        network || '/' || settlement_currency_code as record_key
    from {{ ref('sl_card_settlement_totals') }}
),

missing as (
    select * from expected
    except all
    select * from produced
),

unexpected as (
    select * from produced
    except all
    select * from expected
),

-- Per record type: the lines and the trailers of one file land in two batches, one per entity.
split as (
    select
        record,
        settlement_date,
        file_sequence,
        revision
    from expected
    group by record, settlement_date, file_sequence, revision
    having count(distinct _batch_id) > 1
)

select
    'missing' as problem,
    record,
    settlement_date,
    file_sequence,
    revision,
    record_key
from missing
union all
select
    'unexpected' as problem,
    record,
    settlement_date,
    file_sequence,
    revision,
    record_key
from unexpected
union all
select
    'a winning declaration landed by two batches' as problem,
    record,
    settlement_date,
    file_sequence,
    revision,
    null as record_key
from split
