"""The lending funnel, the repayment schedule, and how loans go wrong.

Three things have to hold for Q7, Q8 and Q9 to measure anything.

**Approval has to vary by risk band**, or Q8's approval rate is one number repeated five times.
**Default has to vary by risk band too**, and land inside the probability-of-default interval
each band is written for in `ref.risk_bands`, or Q8 cannot compare realised default against the
band's own expectation.

**Delinquency has to emerge over time.** A loan book where defaults are decided at origination
and appear immediately gives Q7 vintage curves that are flat lines at their final value. Real
delinquency seasons: almost nothing goes wrong in the first two months, the hazard peaks
somewhere in the first two years, and it tails off. The seasoning curve in `profiles.yml` is
the cumulative share of a loan's lifetime default hazard that has materialised by month N, and
the month a loan actually defaults is drawn from it.

The schedule is a level-payment annuity, computed in Decimal throughout. Money never touches a
float here: conventions.md prohibits it including in intermediate arithmetic, and an
installment plan is exactly where a half-cent of float error compounds into a schedule that
does not add up to the principal.
"""

from __future__ import annotations

import bisect
import datetime as dt
import random
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from .calendar import add_months
from .distributions import bernoulli, cents, weighted_choice, weighted_pick

# Intermediate precision for the annuity factor. The result is quantised back to the storage
# scale; this only stops the reciprocal losing digits on a 84-month term.
ANNUITY_PRECISION = 28


@dataclass(frozen=True)
class Installment:
    number: int
    due_date: dt.date
    due_amount: Decimal
    principal_component: Decimal
    interest_component: Decimal


def approval_probability(lending: dict[str, Any], risk_band_code: str) -> float:
    return float(lending["approval_rate_by_band"][risk_band_code])


def default_probability(lending: dict[str, Any], risk_band_code: str) -> float:
    return float(lending["default_rate_by_band"][risk_band_code])


def draw_product(rng: random.Random, lending: dict[str, Any]) -> str:
    return weighted_choice(rng, lending["product_mix"])


def draw_requested_amount(rng: random.Random, lending: dict[str, Any]) -> Decimal:
    import math

    raw = math.exp(rng.gauss(lending["requested_amount_mu"], lending["requested_amount_sigma"]))
    bounded = min(float(lending["requested_amount_max"]),
                  max(float(lending["requested_amount_min"]), raw))
    # Loan applications are for round sums. Nobody asks for 8,431.77 euros.
    return cents(int(round(bounded / 100.0)) * 100)


def draw_term_months(rng: random.Random, lending: dict[str, Any]) -> int:
    return int(
        weighted_pick(rng, lending["term_months_choices"], lending["term_months_weights"])
    )


def approved_amount(rng: random.Random, lending: dict[str, Any], requested: Decimal) -> Decimal:
    """What the underwriter agreed to, at most what was asked for.

    Invariant 8 asserts that every disbursed loan is within its approved amount, so the haircut
    is applied here once and the disbursement reads it rather than drawing again.
    """
    haircut = Decimal(str(rng.choice(lending["approved_amount_haircut_choices"])))
    return cents(requested * haircut)


def annuity_payment(principal: Decimal, annual_rate: Decimal, term_months: int) -> Decimal:
    """The level monthly payment for a principal, nominal annual rate and term.

    An interest-free loan is the degenerate case and divides by zero in the general formula, so
    it is handled rather than guarded against downstream.
    """
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if annual_rate == 0:
        return cents(principal / term_months)
    with localcontext() as context:
        context.prec = ANNUITY_PRECISION
        monthly = annual_rate / Decimal(12)
        factor = (Decimal(1) + monthly) ** term_months
        payment = principal * monthly * factor / (factor - Decimal(1))
    return cents(payment)


def schedule(
    principal: Decimal,
    annual_rate: Decimal,
    term_months: int,
    first_due: dt.date,
) -> list[Installment]:
    """The full installment plan.

    The last installment absorbs the rounding, so the principal components sum to exactly the
    principal. Invariant 7 checks the schedule against the loan's principal, rate and term, and
    a plan that is a few cents short would fail it for a reason that has nothing to do with the
    lending model.
    """
    payment = annuity_payment(principal, annual_rate, term_months)
    monthly_rate = annual_rate / Decimal(12)
    outstanding = principal
    out: list[Installment] = []

    for number in range(1, term_months + 1):
        interest = cents(outstanding * monthly_rate)
        if number == term_months:
            principal_part = outstanding
            due = cents(principal_part + interest)
        else:
            principal_part = cents(payment - interest)
            if principal_part > outstanding:
                principal_part = outstanding
            due = cents(principal_part + interest)
        outstanding = cents(outstanding - principal_part)
        out.append(
            Installment(
                number=number,
                due_date=add_months(first_due, number - 1),
                due_amount=due,
                principal_component=principal_part,
                interest_component=interest,
            )
        )
    return out


def seasoned_default_month(
    rng: random.Random, lending: dict[str, Any], term_months: int
) -> int | None:
    """The month after origination in which a defaulting loan stops paying.

    Drawn by inverting the cumulative seasoning curve, so the hazard is front-loaded but not
    immediate and Q7's vintage curves rise over the months after origination instead of
    stepping to their final value at month zero.
    """
    curve = lending["seasoning_curve"]
    target = rng.random()
    index = bisect.bisect_left(curve, target)
    if index >= len(curve):
        index = len(curve) - 1
    month = max(1, index)
    return month if month <= term_months else None


def cures(rng: random.Random, lending: dict[str, Any]) -> bool:
    """Whether a borrower in arrears catches up.

    A cure is visible as a fall in the delinquency rate at the next reporting date, never as a
    retrospective edit of an earlier month. metric_definitions.md is explicit about that, and
    it is the generator's job to produce arrears that later clear rather than arrears that are
    rewritten.
    """
    return bernoulli(rng, float(lending["cure_rate"]))


def pays_late(rng: random.Random, lending: dict[str, Any]) -> bool:
    return bernoulli(rng, float(lending["late_payment_share"]))


def late_days(rng: random.Random, lending: dict[str, Any]) -> int:
    return rng.randint(1, int(lending["late_payment_days_max"]))


def decision_reason(
    rng: random.Random, approved: bool, risk_band_code: str, reasons: list[str]
) -> str:
    """Why the underwriter decided as they did.

    Drawn from the reasons actually seeded in ref.decision_reasons rather than from a list in
    this file, because that table is an open vocabulary and a second copy here would be the
    thing that stops it growing.
    """
    if approved:
        return "within_policy" if "within_policy" in reasons else reasons[0]
    candidates = [
        reason
        for reason in reasons
        if reason
        in ("affordability", "credit_history", "existing_arrears", "fraud_suspicion",
            "documentation", "policy_exclusion")
    ]
    if not candidates:
        return reasons[0]
    # Weaker bands are refused for affordability and history far more often than for policy.
    weights = {reason: 1.0 for reason in candidates}
    if risk_band_code in ("D", "E"):
        for reason in ("affordability", "credit_history", "existing_arrears"):
            if reason in weights:
                weights[reason] *= 3.0
    total = sum(weights.values())
    return weighted_choice(rng, {k: v / total for k, v in weights.items()})
