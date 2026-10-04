{#
  A macro series' values by the interval the platform observed each (specification 009
  section 5).

  The feed asks FRED for the current vintage only, so every observation it lands carries
  `realtime_start` and `realtime_end` equal to the day of the request: they say when the platform
  asked, not when FRED first published the value or stopped publishing it. Each run lands every
  period again, revised or not. Silver collapses each period's run of identical values, across
  consecutive requests, into one interval:

  * `observed_from_date` is the request day the value was first observed;
  * `observed_to_date` is the request day a different value was first observed, exclusive, or
    9999-12-31 while it is the latest;
  * `is_latest` marks the period's last interval.

  Values compare with `is distinct from`, so a missing observation, which FRED sends as `.` and
  the contract lands as null, is a value: null, then a number, is a revision. A value that is
  revised and later revised back is three intervals. The interval is the platform's observation,
  not FRED's real-time vintage, which a request for the full real-time history would give (an
  open item, decided with the funding cost).

  A period re-read by two batches on one request day keeps the earliest batch's observation.
  `observations` is a relation or CTE name with the bronze columns.
#}
{% macro observed_intervals(observations) -%}
with observed as (
    select distinct on (series_id, observation_date, realtime_start)
        series_id,
        observation_date,
        realtime_start,
        value,
        _batch_id
    from {{ observations }}
    order by series_id, observation_date, realtime_start, _batch_id
),

ordered as (
    select
        *,
        row_number() over w as _position,
        lag(value) over w as _previous_value
    from observed
    window w as (partition by series_id, observation_date order by realtime_start)
),

marked as (
    select
        *,
        cast(_position = 1 or "value" is distinct from _previous_value as integer) as _opens
    from ordered
),

runs as (
    select
        *,
        sum(_opens) over (
            partition by series_id, observation_date
            order by realtime_start
            rows between unbounded preceding and current row
        ) as _run
    from marked
),

intervals as (
    select
        series_id,
        observation_date,
        _run,
        min(realtime_start) as observed_from_date,
        arg_min(value, realtime_start) as value,
        arg_min(_batch_id, realtime_start) as _batch_id
    from runs
    group by series_id, observation_date, _run
)

select
    series_id,
    observation_date,
    value,
    observed_from_date,
    coalesce(
        lead(observed_from_date) over w, date '9999-12-31'
    ) as observed_to_date,
    (lead(observed_from_date) over w is null) as is_latest,
    _batch_id
from intervals
window w as (partition by series_id, observation_date order by observed_from_date)
{%- endmacro %}
