"""Sampling primitives, all taking an explicit random source.

Two rules hold throughout. Every function receives the `random.Random` it draws from, so no
module-level generator exists to be shared by accident. And every function that iterates a
mapping sorts its keys first: the determinism contract forbids depending on dictionary order,
and a mapping assembled from a YAML file or a database query is not something to trust the
ordering of even where CPython would preserve it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal

# Money is DECIMAL(18,4) in the source schema (conventions.md), so every amount is quantised to
# four places the moment it stops being a draw and becomes an amount.
MONEY = Decimal("0.0001")
CENTS = Decimal("0.01")

# Above this mean, a Poisson count is drawn from its normal approximation rather than by Knuth's
# method, which costs one iteration per unit of the mean. At the full profile the generator
# draws tens of millions of these and the difference is minutes. The approximation is documented
# as a deliberate simplification in docs/generator_realism.md.
KNUTH_LIMIT = 12.0


def bernoulli(rng: random.Random, probability: float) -> bool:
    return rng.random() < probability


def weighted_choice(rng: random.Random, mix: Mapping[str, float]) -> str:
    """One outcome from a mapping of outcome to weight. Keys are sorted before drawing."""
    outcomes = sorted(mix)
    if not outcomes:
        raise ValueError("cannot draw from an empty mix")
    weights = [mix[outcome] for outcome in outcomes]
    total = math.fsum(weights)
    if total <= 0:
        raise ValueError("cannot draw from a mix whose weights sum to zero")
    threshold = rng.random() * total
    cumulative = 0.0
    for outcome, weight in zip(outcomes, weights, strict=True):
        cumulative += weight
        if threshold < cumulative:
            return outcome
    return outcomes[-1]


def weighted_pick(rng: random.Random, items: Sequence[object], weights: Sequence[float]) -> object:
    """One item from parallel sequences, where the caller already controls the order."""
    total = math.fsum(weights)
    threshold = rng.random() * total
    cumulative = 0.0
    for item, weight in zip(items, weights, strict=True):
        cumulative += weight
        if threshold < cumulative:
            return item
    return items[-1]


def poisson(rng: random.Random, mean: float) -> int:
    """A Poisson count, by Knuth's method for a small mean and by approximation above."""
    if mean <= 0:
        return 0
    if mean < KNUTH_LIMIT:
        limit = math.exp(-mean)
        count, product = 0, rng.random()
        while product > limit:
            count += 1
            product *= rng.random()
        return count
    # Continuity-corrected normal approximation. The error at mean 12 is under one percent in
    # the body of the distribution, and the counts it produces are never read individually.
    value = rng.gauss(mean, math.sqrt(mean))
    return max(0, int(value + 0.5))


def overdispersed_poisson(rng: random.Random, mean: float, dispersion: float) -> int:
    """A Poisson count whose rate itself varies, so that activity is uneven across accounts.

    A plain Poisson gives every account the same underlying rate, which makes the busiest
    account in a book of 250,000 about twice as active as the median. Real books are far more
    skewed, and the skew is what the activity and dormancy measures are read against. The
    log-normal multiplier carries a minus-half-sigma-squared correction so the mean stays at
    `mean` rather than drifting up with the dispersion.
    """
    if dispersion <= 0:
        return poisson(rng, mean)
    multiplier = math.exp(rng.gauss(0.0, dispersion) - dispersion * dispersion / 2.0)
    return poisson(rng, mean * multiplier)


def bounded_lognormal(
    rng: random.Random, mu: float, sigma: float, low: float, high: float
) -> float:
    """A log-normal draw clamped to a range.

    Clamped rather than resampled: resampling until a draw lands in range is a loop whose
    number of draws depends on the values it rejects, which would make one stream's consumption
    depend on its own parameters. Clamping keeps the draw count fixed at one.
    """
    return min(high, max(low, math.exp(rng.gauss(mu, sigma))))


def triangular_int(rng: random.Random, low: int, mode: int, high: int) -> int:
    return int(round(rng.triangular(low, high, mode)))


def money(value: float | Decimal) -> Decimal:
    """Quantise to the storage scale. Everything downstream of this is Decimal arithmetic."""
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def cents(value: float | Decimal) -> Decimal:
    """Quantise to two places, then to the storage scale, for an amount a customer would see."""
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP).quantize(MONEY)


def first_digit(value: Decimal) -> int | None:
    """The leading significant digit of an amount, or None when there is none.

    Zero has no leading significant digit and is excluded rather than counted as a one, which
    is the usual way a Benford check quietly stops measuring anything.
    """
    for character in str(abs(value)):
        if character.isdigit() and character != "0":
            return int(character)
        if character not in "0.":
            break
    return None


# The expected first-digit distribution, log10(1 + 1/d).
BENFORD = tuple(math.log10(1.0 + 1.0 / digit) for digit in range(1, 10))


def benford_mad(counts: Sequence[int]) -> float:
    """Mean absolute deviation of observed first-digit shares from Benford.

    The conformity measure Benford analysis actually uses, and the one invariant 14
    asserts. Nigrini's published thresholds are 0.006 for close conformity, 0.012 for
    acceptable and 0.015 for marginal.

    It is used in preference to a chi-square critical value because chi-square measures
    significance rather than effect size, and its power grows with the sample. A fixed
    critical value is therefore *stricter* at a larger profile, not scale-invariant: the
    same distribution that passes at ci fails at full for no reason but the row count.
    The chi-square statistic is still computed and reported beside the deviation, so the
    two can be read together.
    """
    total = sum(counts)
    if total == 0:
        return float("inf")
    return math.fsum(
        abs(observed / total - share)
        for observed, share in zip(counts, BENFORD, strict=True)
    ) / len(BENFORD)


def benford_chi_square(counts: Sequence[int]) -> float:
    """Pearson's chi-square of observed first-digit counts against Benford, 8 degrees of freedom.

    A statistic rather than a percentage deviation, because the tolerance has to mean the same
    thing at every profile. A fixed percentage band tight enough to catch a real departure at
    the dev profile fails at ci on sampling noise alone.
    """
    total = sum(counts)
    if total == 0:
        return float("inf")
    statistic = 0.0
    for observed, share in zip(counts, BENFORD, strict=True):
        expected = total * share
        statistic += (observed - expected) ** 2 / expected
    return statistic
