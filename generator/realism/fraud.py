"""Which transactions are fraudulent, and which of those the bank notices.

Two decisions, kept separate on purpose.

**Fraud** is a property of the transaction. It concentrates in card-not-present, at unfamiliar
merchants, at unusual hours, inside velocity bursts and after a device the customer has not
used before. Each context carries a relative propensity multiplier, and the multipliers are
calibrated against the contexts' own prevalence so that changing where fraud falls does not
change how much of it there is. That separation matters: otherwise raising the card-not-present
multiplier silently raises the fraud rate, and the two would have to be tuned against each
other every time either moved.

**Detection** is a property of the bank, and it is imperfect in both directions. Some fraud is
missed, and some legitimate transactions raise an alert. A detector that fires on exactly the
fraudulent transactions would make Q10 report a precision of 100 percent and a false positive
rate of zero, which would measure the generator rather than the fraud operation.

The alert score distributions overlap for the same reason. A score that separates the classes
perfectly is the same fake one step further in.
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .distributions import bernoulli, weighted_choice


@dataclass(frozen=True)
class FraudContext:
    """The risk context of one card transaction, as the propensity model reads it."""

    card_not_present: bool
    unfamiliar_merchant: bool
    night_hour: bool
    velocity_burst: bool
    new_device: bool


def _expected_multiplier(prevalence: float, multiplier: float) -> float:
    return prevalence * multiplier + (1.0 - prevalence)


def night_hour_prevalence(fraud: dict[str, Any], hour_weights: Sequence[float]) -> float:
    """The share of transactions that land in the hours the model treats as unusual.

    Read off the hour-of-day profile rather than assumed to be five twenty-fourths, because the
    overnight hours are the quietest ones and taking them as uniform would overstate the
    context's prevalence by roughly a factor of four.
    """
    total = sum(hour_weights)
    night = sum(hour_weights[hour] for hour in fraud["night_hours"])
    return night / total if total else 0.0


def calibrate(fraud: dict[str, Any], hour_weights: Sequence[float],
              card_not_present_share: float) -> float:
    """The base probability that makes the mean propensity equal the target fraud rate.

    Computed analytically from the contexts' prevalences, treating them as independent. They
    are not quite independent — card-not-present and new-device co-occur more than chance — so
    the realised rate lands slightly above the target. The band in `profiles.yml` is set wide
    enough to contain that, and the measured rate is reported against the band rather than
    assumed to hit the midpoint.
    """
    expected = (
        _expected_multiplier(card_not_present_share, fraud["card_not_present_multiplier"])
        * _expected_multiplier(
            fraud["unfamiliar_merchant_share"], fraud["unfamiliar_merchant_multiplier"]
        )
        * _expected_multiplier(
            night_hour_prevalence(fraud, hour_weights), fraud["night_hour_multiplier"]
        )
        * _expected_multiplier(fraud["velocity_burst_share"], fraud["velocity_burst_multiplier"])
        * _expected_multiplier(fraud["new_device_context_share"], fraud["new_device_multiplier"])
    )
    return float(fraud["target_rate"]) / expected if expected > 0 else 0.0


def propensity(fraud: dict[str, Any], context: FraudContext, base: float) -> float:
    """The probability that this particular transaction is fraudulent."""
    value = base
    if context.card_not_present:
        value *= fraud["card_not_present_multiplier"]
    if context.unfamiliar_merchant:
        value *= fraud["unfamiliar_merchant_multiplier"]
    if context.night_hour:
        value *= fraud["night_hour_multiplier"]
    if context.velocity_burst:
        value *= fraud["velocity_burst_multiplier"]
    if context.new_device:
        value *= fraud["new_device_multiplier"]
    return min(1.0, value)


def is_fraudulent(rng: random.Random, fraud: dict[str, Any], context: FraudContext,
                  base: float) -> bool:
    return bernoulli(rng, propensity(fraud, context, base))


def raises_alert(rng: random.Random, fraud: dict[str, Any], fraudulent: bool) -> bool:
    """Whether the detector fired.

    On fraud this is recall: the share of real fraud the bank catches. On a legitimate
    transaction it is the false alert rate, which is what puts dismissed alerts in the
    denominator of Q10's precision.
    """
    if fraudulent:
        return bernoulli(rng, float(fraud["detection_recall"]))
    return bernoulli(rng, float(fraud["false_alert_rate"]))


def alert_score(rng: random.Random, fraud: dict[str, Any], fraudulent: bool) -> float:
    """A detector score in [0, 1], from overlapping Beta distributions."""
    if fraudulent:
        alpha, beta = fraud["score_fraud_alpha"], fraud["score_fraud_beta"]
    else:
        alpha, beta = fraud["score_legit_alpha"], fraud["score_legit_beta"]
    return min(1.0, max(0.0, rng.betavariate(alpha, beta)))


def disposition(
    rng: random.Random,
    fraud: dict[str, Any],
    alerted_at: dt.datetime,
    anchor_end: dt.datetime,
    fraudulent: bool,
) -> tuple[str, dt.datetime | None]:
    """The analyst's disposition and when it was reached.

    An alert whose disposition would fall after the anchor has not been dispositioned yet, so
    it sits in the pending backlog. That is not a convenience: dispositions arriving days after
    the alert is exactly why metric_definitions.md attributes precision to the disposition
    month, and a generator that dispositioned everything before the anchor would leave that
    rule with nothing to be right about.
    """
    lag_days = rng.randint(
        int(fraud["disposition_lag_days_min"]), int(fraud["disposition_lag_days_max"])
    )
    dispositioned_at = alerted_at + dt.timedelta(
        days=lag_days, hours=rng.randrange(24), minutes=rng.randrange(60)
    )

    open_mix = {
        "open": float(fraud["open_share"]),
        "investigating": float(fraud["investigating_share"]),
        "escalated": float(fraud["escalated_share"]),
    }
    still_open = sum(open_mix.values())
    if dispositioned_at > anchor_end or bernoulli(rng, still_open):
        # A non-final disposition has no dispositioned_at: the case has not been closed.
        return weighted_choice(rng, {k: v / still_open for k, v in open_mix.items()}), None

    return ("confirmed_fraud" if fraudulent else "dismissed"), dispositioned_at


def rule_for(rng: random.Random, fraud: dict[str, Any], context: FraudContext) -> str:
    """Which detection rule fired.

    Weighted towards the rule that matches the context, so that Q10's precision by rule differs
    between rules instead of being the same number seven times.
    """
    weights = dict(fraud["rule_mix"])
    if context.velocity_burst:
        weights["velocity_card"] = weights.get("velocity_card", 0.0) * 3.0
    if context.card_not_present:
        weights["high_value_cnp"] = weights.get("high_value_cnp", 0.0) * 2.5
    if context.new_device:
        weights["new_device_high_value"] = weights.get("new_device_high_value", 0.0) * 3.0
    if context.unfamiliar_merchant:
        weights["merchant_risk"] = weights.get("merchant_risk", 0.0) * 2.0
    total = sum(weights.values())
    return weighted_choice(rng, {code: weight / total for code, weight in weights.items()})
