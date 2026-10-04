-- Files of different sequences add (specification 009 section 7, criterion 3): on a settlement
-- date the processor sent two or more sequences for, every sequence it sent is present in the
-- settlement models, lines and trailers alike, rather than one replacing another.
--
-- Live floor: 0, and this test has nothing to check on the history. The simulated processor
-- sends sequence 01 on every date, because the settlement generator hard-codes it (an open item
-- in `project_state.md`), so no date has two sequences. The rule is proven on the two-sequence
-- fixture of `dbt/tests/macros/assert_settlement_winning_revision.sql`, which `make dbt-prove`
-- also shows failing when one sequence's winner is applied to another.
--
-- Returns each settlement date and sequence sent on a multi-sequence date that silver lacks.
with sent as (
    select
        settlement_date,
        file_sequence
    from {{ ref('br_cardnet__settlements') }}
    union
    select
        settlement_date,
        file_sequence
    from {{ ref('br_cardnet__settlement_totals') }}
),

multiple as (
    select settlement_date
    from sent
    group by settlement_date
    having count(*) > 1
),

held as (
    select
        settlement_date,
        file_sequence
    from {{ ref('sl_card_settlements') }}
    union
    select
        settlement_date,
        file_sequence
    from {{ ref('sl_card_settlement_totals') }}
)

select
    sent.settlement_date,
    sent.file_sequence
from sent
inner join multiple on sent.settlement_date = multiple.settlement_date
where not exists (
    select 1
    from held
    where
        held.settlement_date = sent.settlement_date
        and held.file_sequence = sent.file_sequence
)
