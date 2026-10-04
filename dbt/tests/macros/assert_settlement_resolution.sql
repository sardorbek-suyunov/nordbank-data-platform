-- A line resolves to its card, transaction and merchant without adding a row or losing one
-- (specification 009 section 2). Card 10 has three versions under one token, as five of the
-- history's cards do; joined to every version, its line would be repeated three times. A line
-- whose token and reference the bank does not hold is kept with nulls; the history has none.
-- Returns every resolved line that differs from the expected, counted with duplicates.
with cards (card_id, card_reference, _valid_from) as (
    values
    (10, 'token-a', timestamptz '1900-01-01 00:00:00+00'),
    (10, 'token-a', timestamptz '2026-07-21 09:00:00+00'),
    (10, 'token-a', timestamptz '2026-07-22 09:00:00+00'),
    (11, 'token-b', timestamptz '1900-01-01 00:00:00+00')
),

transactions (transaction_id, transaction_reference, merchant_id) as (
    values
    (100, 'REF-1', 5),
    (101, 'REF-2', null)
),

lines (transaction_reference, card_reference) as (
    values
    ('REF-1', 'token-a'),
    ('REF-2', 'token-b'),
    ('REF-9', 'token-z')
),

expected (transaction_reference, card_reference, card_id, transaction_id, merchant_id) as (
    values
    ('REF-1', 'token-a', 10, 100, 5),
    ('REF-2', 'token-b', 11, 101, null),
    ('REF-9', 'token-z', null, null, null)
),

resolved as (
    {{ settlement_resolution('lines', 'cards', 'transactions') }}
),

missing as (
    select * from expected
    except all
    select * from resolved
),

unexpected as (
    select * from resolved
    except all
    select * from expected
)

select
    'missing' as problem,
    transaction_reference
from missing
union all
select
    'unexpected' as problem,
    transaction_reference
from unexpected
