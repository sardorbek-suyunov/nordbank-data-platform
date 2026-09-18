"""When things happen: seasonality, the working week, the hour of day, and the growth trend.

Every timestamp the generator produces comes from here, derived from the anchor date and the
seed. Nothing reads the clock, which is what makes a run reproducible and what keeps five years
of simulated history out of a single watermark window.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Sequence

from .distributions import weighted_pick

UTC = dt.UTC
HOURS = tuple(range(24))


def add_months(value: dt.date, months: int) -> dt.date:
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(value.day, days_in_month(year, month))
    return dt.date(year, month, day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day


def month_index(start: dt.date, value: dt.date) -> int:
    """Whole months from `start` to `value`, so that a cohort's age is an integer."""
    return (value.year - start.year) * 12 + (value.month - start.month)


def is_weekend(day: dt.date) -> bool:
    return day.weekday() >= 5


def month_weight(month_weights: Sequence[float], day: dt.date) -> float:
    """The seasonal multiplier for the month `day` falls in. December peaks, August troughs."""
    return month_weights[day.month - 1]


def day_weight(month_weights: Sequence[float], weekend_factor: float, day: dt.date) -> float:
    weight = month_weight(month_weights, day)
    return weight * weekend_factor if is_weekend(day) else weight


def growth_multiplier(monthly_growth_rate: float, months_elapsed: int) -> float:
    """Compound growth since the start of the history.

    Applied to acquisition and to per-account activity, so that later months are busier than
    earlier ones and Q1's month-over-month growth measures a trend rather than noise.
    """
    return (1.0 + monthly_growth_rate) ** months_elapsed


def progress(start: dt.date, anchor: dt.date, day: dt.date) -> float:
    """How far through the history `day` sits, in [0, 1].

    The card-not-present trend and anything else that moves across the history is expressed
    against this rather than against an absolute date, so the same parameters describe a six
    month profile and a five year one.
    """
    span = (anchor - start).days
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (day - start).days / span))


def draw_hour(rng: random.Random, hour_weights: Sequence[float]) -> int:
    """An hour of day from the bimodal profile: a lunchtime peak and a larger evening one."""
    return int(weighted_pick(rng, HOURS, hour_weights))


def draw_timestamp(rng: random.Random, day: dt.date, hour_weights: Sequence[float]) -> dt.datetime:
    """A UTC instant on `day`, with the hour drawn from the profile and the rest uniform."""
    return dt.datetime(
        day.year,
        day.month,
        day.day,
        draw_hour(rng, hour_weights),
        rng.randrange(60),
        rng.randrange(60),
        tzinfo=UTC,
    )


def at_time(day: dt.date, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


def days_of_month(month_start: dt.date, first: dt.date, last: dt.date) -> list[dt.date]:
    """Every day of `month_start`'s month that falls within [first, last]."""
    count = days_in_month(month_start.year, month_start.month)
    out = []
    for offset in range(count):
        day = month_start + dt.timedelta(days=offset)
        if day.month != month_start.month:
            break
        if first <= day <= last:
            out.append(day)
    return out


def clamp_day_of_month(year: int, month: int, day: int) -> dt.date:
    """A date for a recurring payment, clamped when the month is short.

    A subscription billed on the 31st is billed on the 30th in November and the 28th in
    February, which is what a real mandate does and what keeps the recurring amount landing on
    a date that exists.
    """
    return dt.date(year, month, min(day, days_in_month(year, month)))
