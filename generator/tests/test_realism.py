"""The realism models, asserted on statistics rather than on individual values.

Spec 003 section 10 is explicit about that: a test that pins one draw to one number breaks
whenever anything upstream changes and tells you nothing about whether the distribution is the
shape it claims to be. These fix the seed and assert on the shape.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import statistics
from decimal import Decimal

from generator.config import load_profile
from generator.realism import fraud as fraud_model
from generator.realism import lending as lending_model
from generator.realism import presentment
from generator.realism.amounts import cash_amount, purchase_amount
from generator.realism.calendar import (
    day_weight,
    growth_multiplier,
    is_weekend,
    month_weight,
    progress,
)
from generator.realism.distributions import (
    BENFORD,
    benford_mad,
    bounded_lognormal,
    first_digit,
    overdispersed_poisson,
    poisson,
    weighted_choice,
)

PARAMS = load_profile("dev").params
SEED = 20260918


def rng() -> random.Random:
    return random.Random(SEED)


# ---------------------------------------------------------------- distributions


def test_weighted_choice_reproduces_the_mix():
    mix = {"a": 0.5, "b": 0.3, "c": 0.2}
    source = rng()
    draws = [weighted_choice(source, mix) for _ in range(40_000)]
    for outcome, expected in mix.items():
        assert abs(draws.count(outcome) / len(draws) - expected) < 0.01


def test_weighted_choice_does_not_depend_on_mapping_order():
    forward = {"a": 0.5, "b": 0.3, "c": 0.2}
    reversed_order = {"c": 0.2, "b": 0.3, "a": 0.5}
    assert [weighted_choice(random.Random(7), forward) for _ in range(50)] == [
        weighted_choice(random.Random(7), reversed_order) for _ in range(50)
    ]


def test_poisson_has_the_right_mean_in_both_regimes():
    for mean in (3.0, 25.0):
        source = rng()
        draws = [poisson(source, mean) for _ in range(20_000)]
        assert abs(statistics.fmean(draws) - mean) < mean * 0.05


def test_overdispersion_widens_the_spread_without_moving_the_mean():
    """Activity has to be uneven across accounts, or the busiest account is twice the median.

    One source per sample, drawn from repeatedly. Constructing a fresh seeded source inside the
    loop would return the same value every time and compare two constants.
    """
    plain_source = random.Random(1)
    spread_source = random.Random(1)
    plain = [poisson(plain_source, 18.0) for _ in range(40_000)]
    spread = [overdispersed_poisson(spread_source, 18.0, 0.45) for _ in range(40_000)]
    assert abs(statistics.fmean(spread) - statistics.fmean(plain)) < 1.0
    assert statistics.pstdev(spread) > statistics.pstdev(plain) * 1.5


def test_bounded_lognormal_respects_its_bounds_and_costs_one_draw():
    source = rng()
    values = [bounded_lognormal(source, 3.0, 2.0, 5.0, 50.0) for _ in range(5_000)]
    assert min(values) >= 5.0
    assert max(values) <= 50.0

    # Exactly one draw per call, whatever the parameters: a resampling loop would make one
    # stream's consumption depend on its own bounds.
    counter = random.Random(1)
    before = counter.getstate()
    bounded_lognormal(counter, 3.0, 2.0, 5.0, 50.0)
    after_one = counter.getstate()
    counter.setstate(before)
    counter.gauss(0, 1)
    assert counter.getstate() == after_one


def test_first_digit_ignores_leading_zeros_and_has_none_for_zero():
    assert first_digit(Decimal("0.0500")) == 5
    assert first_digit(Decimal("-123.4500")) == 1
    assert first_digit(Decimal("0.0000")) is None


def test_benford_deviation_is_zero_for_a_perfect_distribution():
    counts = [round(share * 1_000_000) for share in BENFORD]
    assert benford_mad(counts) < 1e-6


# ---------------------------------------------------------------- calendar


def test_december_peaks_and_august_troughs():
    weights = PARAMS["transactions"]["month_weights"]
    assert weights.index(max(weights)) == 11
    assert weights.index(min(weights)) == 7


def test_the_weekend_carries_less_volume():
    weights = PARAMS["transactions"]["month_weights"]
    factor = PARAMS["transactions"]["weekend_volume_factor"]
    saturday = dt.date(2026, 3, 21)
    monday = dt.date(2026, 3, 23)
    assert is_weekend(saturday) and not is_weekend(monday)
    assert day_weight(weights, factor, saturday) < day_weight(weights, factor, monday)
    assert month_weight(weights, dt.date(2026, 12, 1)) > month_weight(weights, dt.date(2026, 8, 1))


def test_growth_compounds_so_later_cohorts_are_larger():
    rate = PARAMS["acquisition"]["monthly_growth_rate"]
    assert growth_multiplier(rate, 0) == 1.0
    assert growth_multiplier(rate, 36) > growth_multiplier(rate, 12) > 1.0


def test_progress_runs_from_zero_to_one_across_the_history():
    start, anchor = dt.date(2023, 9, 18), dt.date(2026, 9, 18)
    assert progress(start, anchor, start) == 0.0
    assert progress(start, anchor, anchor) == 1.0
    assert 0.45 < progress(start, anchor, dt.date(2025, 3, 18)) < 0.55


# ---------------------------------------------------------------- amounts


def test_purchase_amounts_sit_inside_the_band_and_are_quantised():
    band = PARAMS["amounts"]["by_band"]["standard"]
    source = rng()
    values = [purchase_amount(source, band) for _ in range(5_000)]
    assert all(Decimal(str(band["min"])) <= value <= Decimal(str(band["max"])) for value in values)
    assert all(value == value.quantize(Decimal("0.0001")) for value in values)
    # Two decimal places: an amount a customer would see.
    assert all(value * 100 == (value * 100).to_integral_value() for value in values)


def test_cash_withdrawals_land_on_note_multiples():
    band = PARAMS["amounts"]["by_band"]["cash"]
    source = rng()
    values = [cash_amount(source, PARAMS["amounts"], band) for _ in range(3_000)]
    multiples = PARAMS["amounts"]["cash_multiples"]
    assert all(any(value % Decimal(multiple) == 0 for multiple in multiples) for value in values)
    assert min(values) >= Decimal(min(multiples))


def test_cash_is_anti_benford_which_is_why_it_is_excluded():
    """The exclusion in invariant 14 is justified by the data, not by convenience."""
    band = PARAMS["amounts"]["by_band"]["cash"]
    source = rng()
    counts = [0] * 9
    for _ in range(20_000):
        digit = first_digit(cash_amount(source, PARAMS["amounts"], band))
        if digit:
            counts[digit - 1] += 1
    assert benford_mad(counts) > 0.02, "cash should deviate far more than the 0.006 limit"


# ---------------------------------------------------------------- presentment


def test_card_not_present_rises_across_the_history():
    txn = PARAMS["transactions"]
    early = presentment.card_not_present_target(txn, 0.0)
    late = presentment.card_not_present_target(txn, 1.0)
    assert early == txn["card_not_present_share_start"]
    assert late == txn["card_not_present_share_end"]
    assert late > early


def test_channel_does_not_decide_presentment_on_its_own():
    """A wallet tap at a terminal is card present; an ecommerce checkout is not.

    Both run on channels a customer would call digital, which is the whole reason the column
    exists rather than being derived.
    """
    shares = PARAMS["transactions"]["card_present_share_by_channel"]
    assert shares["pos"] > 0.9
    assert shares["ecommerce"] < 0.05
    assert 0.2 < shares["mobile_app"] < 0.95


def test_the_presentment_trend_moves_the_realised_share():
    txn = PARAMS["transactions"]
    baseline = presentment.baseline_present_share(txn)
    source = rng()

    def share_at(point: float) -> float:
        present = sum(
            presentment.decide(source, txn, "mobile_app", point, baseline) for _ in range(20_000)
        )
        return present / 20_000

    assert share_at(1.0) < share_at(0.0)


# ---------------------------------------------------------------- fraud


def test_fraud_calibration_hits_the_target_rate():
    fraud = PARAMS["fraud"]
    hours = PARAMS["transactions"]["hour_weights"]
    base = fraud_model.calibrate(fraud, hours, 0.4)
    source = rng()

    night = set(fraud["night_hours"])
    total = 20_000
    fraudulent = 0
    for index in range(total):
        context = fraud_model.FraudContext(
            card_not_present=source.random() < 0.4,
            unfamiliar_merchant=source.random() < fraud["unfamiliar_merchant_share"],
            night_hour=(index % 24) in night,
            velocity_burst=source.random() < fraud["velocity_burst_share"],
            new_device=source.random() < fraud["new_device_context_share"],
        )
        fraudulent += fraud_model.is_fraudulent(source, fraud, context, base)

    rate = fraudulent / total
    low, high = load_profile("dev").band("fraud_rate")
    assert low <= rate <= high, f"calibrated fraud rate {rate} outside {low}..{high}"


def test_fraud_concentrates_where_the_model_says_it_does():
    fraud = PARAMS["fraud"]
    base = fraud_model.calibrate(fraud, PARAMS["transactions"]["hour_weights"], 0.4)
    quiet = fraud_model.FraudContext(False, False, False, False, False)
    risky = fraud_model.FraudContext(True, True, True, False, False)
    assert fraud_model.propensity(fraud, risky, base) > (
        fraud_model.propensity(fraud, quiet, base) * 20
    )


def test_detection_is_imperfect_in_both_directions():
    """A detector that fired on exactly the fraud would make Q10 measure the generator."""
    fraud = PARAMS["fraud"]
    source = rng()
    caught = sum(fraud_model.raises_alert(source, fraud, True) for _ in range(20_000))
    false = sum(fraud_model.raises_alert(source, fraud, False) for _ in range(200_000))
    assert 0.0 < caught / 20_000 < 1.0
    assert false > 0


def test_alert_scores_overlap_between_the_classes():
    fraud = PARAMS["fraud"]
    source = rng()
    genuine = [fraud_model.alert_score(source, fraud, True) for _ in range(5_000)]
    legit = [fraud_model.alert_score(source, fraud, False) for _ in range(5_000)]
    assert statistics.fmean(genuine) > statistics.fmean(legit)
    assert min(genuine) < max(legit), "the score distributions must overlap"


# ---------------------------------------------------------------- lending


def test_approval_and_default_rates_are_ordered_by_risk_band():
    lending = PARAMS["lending"]
    bands = ["A", "B", "C", "D", "E"]
    approvals = [lending["approval_rate_by_band"][band] for band in bands]
    defaults = [lending["default_rate_by_band"][band] for band in bands]
    assert approvals == sorted(approvals, reverse=True)
    assert defaults == sorted(defaults)


def test_default_rates_sit_inside_the_bands_they_are_written_for():
    """Q8 compares realised default against the band's own probability-of-default interval.

    A band whose modelled default rate falls outside its own interval would make that comparison
    meaningless before any data existed.
    """
    bounds = {
        "A": (0.0, 0.005),
        "B": (0.005, 0.015),
        "C": (0.015, 0.040),
        "D": (0.040, 0.100),
        "E": (0.100, 1.000),
    }
    for band, (low, high) in bounds.items():
        rate = PARAMS["lending"]["default_rate_by_band"][band]
        assert low <= rate <= high, f"band {band}: {rate} outside its pd interval"


def test_the_installment_schedule_amortises_the_principal_exactly():
    principal = Decimal("12000.00")
    rate = Decimal("0.0850")
    plan = lending_model.schedule(principal, rate, 24, dt.date(2025, 1, 15))
    assert len(plan) == 24
    assert sum(item.principal_component for item in plan) == principal
    assert all(item.due_amount > 0 for item in plan)
    assert all(item.interest_component >= 0 for item in plan)
    # Interest falls as the principal amortises.
    assert plan[0].interest_component > plan[-1].interest_component


def test_an_interest_free_loan_does_not_divide_by_zero():
    plan = lending_model.schedule(Decimal("1200.00"), Decimal("0"), 12, dt.date(2025, 1, 15))
    assert sum(item.principal_component for item in plan) == Decimal("1200.00")
    assert all(item.interest_component == 0 for item in plan)


def test_total_repayment_stays_below_simple_interest():
    principal = Decimal("20000.00")
    rate = Decimal("0.0925")
    plan = lending_model.schedule(principal, rate, 60, dt.date(2025, 1, 15))
    simple = principal * (1 + rate * 60 / 12)
    assert principal < sum(item.due_amount for item in plan) < simple


def test_delinquency_emerges_over_time_rather_than_at_origination():
    """Q7's vintage curves need shape, which means defaults cannot all land in month one."""
    lending = PARAMS["lending"]
    source = rng()
    months = [lending_model.seasoned_default_month(source, lending, 60) for _ in range(20_000)]
    months = [month for month in months if month is not None]
    assert min(months) >= 1
    early = sum(1 for month in months if month <= 2) / len(months)
    assert early < 0.06, "almost nothing should default in the first two months"
    assert statistics.median(months) > 6


def test_the_seasoning_curve_is_a_cumulative_distribution():
    curve = PARAMS["lending"]["seasoning_curve"]
    assert curve == sorted(curve)
    assert curve[0] >= 0.0 and math.isclose(curve[-1], 1.0, abs_tol=1e-9)
