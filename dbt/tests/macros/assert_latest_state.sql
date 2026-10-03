-- `latest_state` keeps one row per key: its latest observation, the earliest batch of a re-read,
-- and nothing for a key whose latest observation is deleted (specification 008 sections 1, 2
-- and 4). Returns every row that differs from that.
with fixture (transaction_id, status, created_at, updated_at, is_deleted, _batch_id) as (
    values
    (1, 'PENDING', timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', false, 'b-0720'),
    (1, 'POSTED', timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-21 09:00:00+00', false, 'b-0722'),
    (1, 'POSTED', timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-21 09:00:00+00', false, 'b-0721'),
    (2, 'DECLINED', timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', false, 'b-0720'),
    (2, 'DECLINED', timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-22 09:00:00+00', true, 'b-0722'),
    (3, 'POSTED', timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', true, 'b-0720'),
    (3, 'POSTED', timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-23 09:00:00+00', false, 'b-0723')
),

expected (transaction_id, status, updated_at, _batch_id) as (
    values
    (1, 'POSTED', timestamptz '2026-07-21 09:00:00+00', 'b-0721'),
    (3, 'POSTED', timestamptz '2026-07-23 09:00:00+00', 'b-0723')
),

produced as (
    select * exclude (created_at)
    from (
        {{ latest_state('fixture', ['transaction_id'],
                        columns=['transaction_id', 'status', 'created_at', 'updated_at',
                                 'is_deleted', '_batch_id']) }}
    )
)

-- `*` on purpose: the comparison covers every column the macro produces, so a column it should
-- not carry changes the column count and fails the test.
-- noqa: disable=AM04
(
    select
        'missing' as problem,
        *
    from expected
    except
    select
        'missing' as problem,
        *
    from produced
)
union all
(
    select
        'unexpected' as problem,
        *
    from produced
    except
    select
        'unexpected' as problem,
        *
    from expected
)
