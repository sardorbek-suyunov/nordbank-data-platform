-- The version rule, the clocks, deduplication and soft deletes of `scd2`, on planted rows
-- (specification 008 sections 1, 2 and 4). Returns every expected version the macro did not
-- produce and every version it produced that was not expected.
--
-- `balance` is the excluded measure and `label` the projection, as in `sl_accounts`:
--   1  a balance-only change opens nothing; a label change opens a version
--   2  nothing in the contract changed: a change the contract does not describe, a version
--   3  one version read by two batches: one version, the earliest batch
--   4  deleted: the version closes at the deletion and none is current
--   5  deleted and restored: the restoration opens a version
--   6  a null is a value: null to null is no change, null to 'x' is one, even when the
--      balance moves with it (with `<>` the comparison is null and the version would be lost)
with fixture (account_id, label, balance, created_at, updated_at, is_deleted, _batch_id) as (
    values
    (1, 'a', 10.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', false, 'b-0720'),
    (1, 'a', 25.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-21 09:00:00+00', false, 'b-0721'),
    (1, 'b', 25.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-22 09:00:00+00', false, 'b-0722'),
    (2, 'm', 0.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', false, 'b-0720'),
    (2, 'm', 0.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-23 09:00:00+00', false, 'b-0723'),
    (3, 'c', 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-21 09:00:00+00', false, 'b-0722'),
    (3, 'c', 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-21 09:00:00+00', false, 'b-0721'),
    (4, 'd', 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', false, 'b-0720'),
    (4, 'd', 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-24 09:00:00+00', true, 'b-0724'),
    (4, 'd', 5.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-25 09:00:00+00', true, 'b-0725'),
    (5, 'e', 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', false, 'b-0720'),
    (5, 'e', 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-21 09:00:00+00', true, 'b-0721'),
    (5, 'e', 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-22 09:00:00+00', false, 'b-0722'),
    (6, null, 1.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-20 00:00:00+00', false, 'b-0720'),
    (6, null, 2.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-21 09:00:00+00', false, 'b-0721'),
    (6, 'x', 3.0, timestamptz '2026-07-20 00:00:00+00', timestamptz '2026-07-22 09:00:00+00', false, 'b-0722')
),

expected (account_id, label, _batch_id, _valid_from, _valid_to, _is_current) as (
    values
    (1, 'a', 'b-0720', {{ scd2_epoch() }}, timestamptz '2026-07-22 09:00:00+00', false),
    (1, 'b', 'b-0722', timestamptz '2026-07-22 09:00:00+00', {{ scd2_end() }}, true),
    (2, 'm', 'b-0720', {{ scd2_epoch() }}, timestamptz '2026-07-23 09:00:00+00', false),
    (2, 'm', 'b-0723', timestamptz '2026-07-23 09:00:00+00', {{ scd2_end() }}, true),
    (3, 'c', 'b-0721', {{ scd2_epoch() }}, {{ scd2_end() }}, true),
    (4, 'd', 'b-0720', {{ scd2_epoch() }}, timestamptz '2026-07-24 09:00:00+00', false),
    (5, 'e', 'b-0720', {{ scd2_epoch() }}, timestamptz '2026-07-21 09:00:00+00', false),
    (5, 'e', 'b-0722', timestamptz '2026-07-22 09:00:00+00', {{ scd2_end() }}, true),
    (6, null, 'b-0720', {{ scd2_epoch() }}, timestamptz '2026-07-22 09:00:00+00', false),
    (6, 'x', 'b-0722', timestamptz '2026-07-22 09:00:00+00', {{ scd2_end() }}, true)
),

-- Every column but the audit stamps, so a carried excluded measure or deleted flag would change
-- the column count and fail the comparison.
produced as (
    select * exclude (created_at, updated_at)
    from (
        {{ scd2('fixture', ['account_id'], excluded=['balance'],
                columns=['account_id', 'label', 'balance', 'created_at', 'updated_at',
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
