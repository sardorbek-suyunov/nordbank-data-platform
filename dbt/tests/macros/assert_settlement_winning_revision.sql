-- The winning revision (specification 009 sections 1 and 7), on files the history never had:
--
-- * 2026-07-21: revision 2 arrived first and revision 1 after it. Revision 2 wins: arrival
--   order plays no part.
-- * 2026-07-22: a correction whose every detail record was quarantined, so revision 2 landed
--   its trailer and no line. Revision 2 wins, and revision 1's lines do not survive beside it.
-- * 2026-07-23: two sequences, each revision 1. Both add.
-- * 2026-07-24: revisions 1 and 3 of one sequence, no revision 2. Revision 3 wins.
--
-- Returns every line or trailer the macros keep that they should not, or drop that they should
-- keep, counted with duplicates.
with lines (settlement_date, file_sequence, revision, transaction_reference, _batch_id) as (
    values
    (date '2026-07-21', 1, 2, 'R1', 'b-0722-02'),
    (date '2026-07-21', 1, 2, 'R2', 'b-0722-02'),
    (date '2026-07-21', 1, 1, 'R1', 'b-0723-03'),
    (date '2026-07-21', 1, 1, 'R2', 'b-0723-03'),
    (date '2026-07-21', 1, 1, 'R3', 'b-0723-03'),
    (date '2026-07-22', 1, 1, 'R4', 'b-0722-01'),
    (date '2026-07-22', 1, 1, 'R5', 'b-0722-01'),
    (date '2026-07-23', 1, 1, 'R6', 'b-0723-01'),
    (date '2026-07-23', 2, 1, 'R7', 'b-0723-02'),
    (date '2026-07-24', 1, 1, 'R8', 'b-0724-01'),
    (date '2026-07-24', 1, 3, 'R8', 'b-0725-02')
),

trailers (settlement_date, file_sequence, revision, network, settlement_currency) as (
    values
    (date '2026-07-21', 1, 2, 'visa', 'EUR'),
    (date '2026-07-21', 1, 1, 'visa', 'EUR'),
    (date '2026-07-21', 1, 1, 'mastercard', 'EUR'),
    (date '2026-07-22', 1, 1, 'visa', 'EUR'),
    (date '2026-07-22', 1, 2, 'visa', 'EUR'),
    (date '2026-07-23', 1, 1, 'visa', 'EUR'),
    (date '2026-07-23', 2, 1, 'visa', 'EUR'),
    (date '2026-07-24', 1, 1, 'visa', 'EUR'),
    (date '2026-07-24', 1, 3, 'visa', 'EUR')
),

expected_lines (settlement_date, file_sequence, revision, transaction_reference, _batch_id) as (
    values
    (date '2026-07-21', 1, 2, 'R1', 'b-0722-02'),
    (date '2026-07-21', 1, 2, 'R2', 'b-0722-02'),
    (date '2026-07-23', 1, 1, 'R6', 'b-0723-01'),
    (date '2026-07-23', 2, 1, 'R7', 'b-0723-02'),
    (date '2026-07-24', 1, 3, 'R8', 'b-0725-02')
),

expected_trailers (settlement_date, file_sequence, revision, network, settlement_currency) as (
    values
    (date '2026-07-21', 1, 2, 'visa', 'EUR'),
    (date '2026-07-22', 1, 2, 'visa', 'EUR'),
    (date '2026-07-23', 1, 1, 'visa', 'EUR'),
    (date '2026-07-23', 2, 1, 'visa', 'EUR'),
    (date '2026-07-24', 1, 3, 'visa', 'EUR')
),

kept_lines as (
    {{ settlement_winning_records('lines', 'lines', 'trailers') }}
),

kept_trailers as (
    {{ settlement_winning_records('trailers', 'lines', 'trailers') }}
),

-- Counted with duplicates, so a line kept twice is a line kept that should not be.
lines_missing as (
    select * from expected_lines
    except all
    select * from kept_lines
),

lines_kept as (
    select * from kept_lines
    except all
    select * from expected_lines
),

trailers_missing as (
    select * from expected_trailers
    except all
    select * from kept_trailers
),

trailers_kept as (
    select * from kept_trailers
    except all
    select * from expected_trailers
)

select
    'line missing' as problem,
    settlement_date,
    file_sequence,
    revision,
    transaction_reference as record
from lines_missing
union all
select
    'line kept' as problem,
    settlement_date,
    file_sequence,
    revision,
    transaction_reference as record
from lines_kept
union all
select
    'trailer missing' as problem,
    settlement_date,
    file_sequence,
    revision,
    network as record
from trailers_missing
union all
select
    'trailer kept' as problem,
    settlement_date,
    file_sequence,
    revision,
    network as record
from trailers_kept
