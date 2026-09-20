"""The lifecycle phase's rate conversion, which is where the tick and the history could diverge.

A tick applies a monthly hazard over one day. If it used its own numbers rather than the ones
`attrition` already states, the two halves of the book would age accounts at rates that looked
alike and were not, and nothing would fail — the survival curve would simply be wrong in a way
only a chart shows. So the conversion is what is pinned, against the profile itself.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from generator.config import load_profile
from generator.mutation.phases.lifecycle import COOLING_OFF_DAYS, _daily_hazards

PROFILE = load_profile("dev")
ATTRITION = PROFILE.params["attrition"]
MUTATION = PROFILE.params["mutation"]["lifecycle"]


def hazards(day: dt.date) -> dict[str, float]:
    return _daily_hazards(SimpleNamespace(simulated_date=day, profile=PROFILE))


def test_a_monthly_hazard_becomes_a_daily_one_over_the_months_own_length():
    september = hazards(dt.date(2026, 9, 15))
    february = hazards(dt.date(2026, 2, 15))
    assert september["dormant"] == float(ATTRITION["monthly_dormancy_hazard"]) / 30
    assert february["dormant"] == float(ATTRITION["monthly_dormancy_hazard"]) / 28


def test_thirty_days_of_a_daily_hazard_recover_the_monthly_one():
    daily = hazards(dt.date(2026, 9, 15))
    for key, monthly in (
        ("dormant", ATTRITION["monthly_dormancy_hazard"]),
        ("reactivate", ATTRITION["dormancy_reactivation_hazard"]),
        ("close_first_year", ATTRITION["monthly_close_hazard_first_year"]),
        ("close_thereafter", ATTRITION["monthly_close_hazard_thereafter"]),
    ):
        assert daily[key] * 30 == float(monthly)


def test_the_tick_reads_attrition_rather_than_restating_it():
    # The point of the conversion: a second set of numbers for the same behaviour would drift
    # from the first the moment either was tuned.
    daily = hazards(dt.date(2026, 6, 1))
    assert daily["frozen"] == float(ATTRITION["frozen_share"]) / 30
    assert daily["close_thereafter"] < daily["close_first_year"]


def test_the_replacement_hazard_is_daily_already_and_is_not_divided():
    # A blocked card's clock starts at the block, not at the month, so this one is stated daily.
    assert hazards(dt.date(2026, 2, 15))["unblock"] == float(
        MUTATION["blocked_card_replacement_daily_hazard"]
    )


def test_every_hazard_the_phase_reads_is_a_probability():
    for day in (dt.date(2026, 2, 15), dt.date(2026, 7, 31), dt.date(2026, 12, 1)):
        for name, value in hazards(day).items():
            assert 0.0 <= value <= 1.0, name


def test_the_cooling_off_floor_outlasts_the_card_issue_window():
    # Cards are issued up to ten days after the account opens, so a closure inside that window
    # would leave a card issued onto a closed account, which invariant 1 refuses.
    assert COOLING_OFF_DAYS > 10
