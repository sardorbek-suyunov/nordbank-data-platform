-- A `ref` row is deactivated, never deleted, and its model passes deleted='false': the inactive
-- row is a version like any other, current with is_active false, and its history stays
-- (specification 008 section 4). Returns the versions that differ from that.
with fixture (channel_id, code, is_active, created_at, updated_at, _batch_id) as (
    values
    (1, 'BRANCH', true, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', 'b-0720'),
    (1, 'BRANCH', false, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-08-01 09:00:00+00', 'b-0801'),
    (2, 'APP', true, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', 'b-0720')
),

expected (channel_id, code, is_active, _batch_id, _valid_from, _valid_to, _is_current) as (
    values
    (1, 'BRANCH', true, 'b-0720', {{ scd2_epoch() }}, timestamptz '2026-08-01 09:00:00+00', false),
    (1, 'BRANCH', false, 'b-0801', timestamptz '2026-08-01 09:00:00+00', {{ scd2_end() }}, true),
    (2, 'APP', true, 'b-0720', {{ scd2_epoch() }}, {{ scd2_end() }}, true)
),

produced as (
    select * exclude (created_at, updated_at)
    from (
        {{ scd2('fixture', ['channel_id'], deleted='false',
                columns=['channel_id', 'code', 'is_active', 'created_at', 'updated_at', '_batch_id']) }}
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
