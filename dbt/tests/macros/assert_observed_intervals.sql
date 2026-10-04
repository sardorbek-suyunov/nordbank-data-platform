-- A macro series by observed interval (specification 009 section 5), on monthly requests the
-- history never made, because FRED has never landed:
--
-- * 2026-01-01 is observed at 1.0 on 15 July and 15 August, then revised to 1.2 on 15 September
--   and observed at 1.2 again on 15 October; the 15 July request was also re-read by a second
--   batch. Two intervals, the revision the second.
-- * 2026-02-01 is missing (FRED's `.`, landed null) for two requests, then 2.0. A null is a
--   value: two intervals.
-- * 2026-03-01 is revised from 3.0 to 3.1 and back to 3.0. Three intervals.
--
-- Every observation carries `realtime_start` and `realtime_end` equal to its request day, as the
-- current-vintage request returns them. Returns every interval that differs from the expected.
with observations (series_id, observation_date, realtime_start, realtime_end, value, _batch_id) as (
    values
    ('GDP', date '2026-01-01', date '2026-07-15', date '2026-07-15', 1.0, 'b-0715-01'),
    ('GDP', date '2026-01-01', date '2026-07-15', date '2026-07-15', 1.0, 'b-0715-02'),
    ('GDP', date '2026-01-01', date '2026-08-15', date '2026-08-15', 1.0, 'b-0815-01'),
    ('GDP', date '2026-01-01', date '2026-09-15', date '2026-09-15', 1.2, 'b-0915-01'),
    ('GDP', date '2026-01-01', date '2026-10-15', date '2026-10-15', 1.2, 'b-1015-01'),
    ('GDP', date '2026-02-01', date '2026-08-15', date '2026-08-15', null, 'b-0815-01'),
    ('GDP', date '2026-02-01', date '2026-09-15', date '2026-09-15', null, 'b-0915-01'),
    ('GDP', date '2026-02-01', date '2026-10-15', date '2026-10-15', 2.0, 'b-1015-01'),
    ('GDP', date '2026-03-01', date '2026-08-15', date '2026-08-15', 3.0, 'b-0815-01'),
    ('GDP', date '2026-03-01', date '2026-09-15', date '2026-09-15', 3.1, 'b-0915-01'),
    ('GDP', date '2026-03-01', date '2026-10-15', date '2026-10-15', 3.0, 'b-1015-01')
),

expected (series_id, observation_date, value, observed_from_date, observed_to_date, is_latest, _batch_id) as (
    values
    ('GDP', date '2026-01-01', 1.0, date '2026-07-15', date '2026-09-15', false, 'b-0715-01'),
    ('GDP', date '2026-01-01', 1.2, date '2026-09-15', date '9999-12-31', true, 'b-0915-01'),
    ('GDP', date '2026-02-01', null, date '2026-08-15', date '2026-10-15', false, 'b-0815-01'),
    ('GDP', date '2026-02-01', 2.0, date '2026-10-15', date '9999-12-31', true, 'b-1015-01'),
    ('GDP', date '2026-03-01', 3.0, date '2026-08-15', date '2026-09-15', false, 'b-0815-01'),
    ('GDP', date '2026-03-01', 3.1, date '2026-09-15', date '2026-10-15', false, 'b-0915-01'),
    ('GDP', date '2026-03-01', 3.0, date '2026-10-15', date '9999-12-31', true, 'b-1015-01')
),

produced as (
    {{ observed_intervals('observations') }}
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
)

select
    'missing' as problem,
    observation_date,
    observed_from_date
from missing
union all
select
    'unexpected' as problem,
    observation_date,
    observed_from_date
from unexpected
