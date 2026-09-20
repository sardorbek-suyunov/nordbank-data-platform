"""What an account does regularly: its income, its subscriptions, the merchants it returns to.

These three facts are shared by the historical load and the mutation engine, and that is why
they live here rather than inline in either. The load holds the whole book in memory as it
builds it and could draw them anywhere; a tick starts from a database and can only re-derive
what is a pure function of the run seed and the entity's own key.

**So each is drawn from its own addressable stream**, keyed on the account and — where the fact
is allowed to change from month to month — on the calendar month. The alternative is a fact
drawn in sequence from a stream that something else is also drawing from, which is not
addressable: reproducing it would mean replaying every draw that preceded it. Spec 003's
sub-stream rule already forbids depending on draw order, and this is the case where the cost of
depending on it is not a shifted value but a fact the tick cannot compute at all.

**Why the key is the calendar month and not an index into the history.** The load walks
`config.months` and knows a month by its position; a tick knows a date. `"2026-09"` is the same
string on both sides, where position 41 is meaningful on one side only. Getting this wrong
would not fail: it would give an account one set of familiar merchants up to the anchor and a
different set after it, and the only visible symptom would be the fraud model's
unfamiliar-merchant context stepping at a date nothing else steps at.

The alternative to all of this was to read the facts back out of `core.transactions` — the
salary is the regular credit, the mandates are the repeated amounts. It was rejected because
the query cannot tell a salary from a loan disbursement or an opening deposit: all three are a
`transfer_in` on the `api` channel. Distinguishing them would mean marking them in the source,
and a generator flag in the bank's schema is exactly what `generator/invariants.py` refuses to
add for the recurring population.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from decimal import Decimal
from typing import Any

from .distributions import bernoulli, cents

# How many merchants an account uses regularly. Everything outside the set is an unfamiliar
# merchant, which is one of the contexts fraud concentrates in.
FAMILIAR_MERCHANTS = 8

# The largest number of live mandates one account carries.
MAX_MANDATES = 3

# Regular credits land in the last week of the month, which is when salaries are paid.
CREDIT_DAY_MIN = 24
CREDIT_DAY_MAX = 28

# Upper and lower bounds on a regular credit, in the account's major units. The draw is
# log-normal and unbounded on both sides, and neither an income of nine euro nor one of two
# million is a salary.
CREDIT_MIN = 50.0
CREDIT_MAX = 60_000.0


def month_key(day: dt.date) -> str:
    """The stream coordinate for a fact that may change monthly."""
    return day.strftime("%Y-%m")


def familiar_merchants(rng: random.Random, merchant_count: int) -> list[int]:
    """The small, stable set of merchants an account uses in a given month.

    Drawn with replacement, so an account that happens to draw the same merchant twice simply
    uses it more. Deduplicating would make the set size depend on the draw, and the set size is
    what the unfamiliar-merchant prevalence in `profiles.yml` is calibrated against.
    """
    if merchant_count <= 0:
        return []
    return [rng.randrange(1, merchant_count + 1) for _ in range(FAMILIAR_MERCHANTS)]


def mandates(
    rng: random.Random,
    amount_params: dict[str, Any],
    familiar: list[int],
    *,
    has_card: bool,
) -> list[tuple[int, Decimal, int]]:
    """The account's live subscriptions: (day of month, amount, merchant).

    Each fires once a month on its own day at its own amount, which is what makes the
    subscription population anti-Benford and why invariant 14 excludes it.

    The per-account probability is `recurring_share` times four: the parameter is the share of
    *transactions* that are recurring, and an account that has any mandates contributes several
    transactions a month from them, so the share of *accounts* is the larger number.
    """
    if not has_card:
        return []
    if not bernoulli(rng, float(amount_params["recurring_share"]) * 4):
        return []

    choices = amount_params["recurring_amount_choices"]
    out: list[tuple[int, Decimal, int]] = []
    for _ in range(rng.randrange(1, MAX_MANDATES + 1)):
        out.append(
            (
                rng.randrange(1, 29),
                cents(rng.choice(choices)),
                familiar[rng.randrange(len(familiar))] if familiar else 1,
            )
        )
    return out


def regular_credit(
    rng: random.Random, txn_params: dict[str, Any], product_class: str
) -> tuple[int, Decimal]:
    """The account's income: the day of month it arrives and how much.

    Returns `(0, 0)` for an account that receives none, which is most savings accounts and a
    minority of current ones. Without a regular credit the book drains, which is what the first
    smoke run of the historical load measured and what
    `regular_credit_share_by_product_class` exists to fix.

    A property of the account rather than of the month: the same salary lands on the same day
    for the same amount until something changes it, which is what a statement shows.
    """
    share = float(txn_params["regular_credit_share_by_product_class"].get(product_class, 0.0))
    if not bernoulli(rng, share):
        return 0, Decimal("0.0000")

    day = rng.randrange(CREDIT_DAY_MIN, CREDIT_DAY_MAX + 1)
    amount = cents(
        min(
            CREDIT_MAX,
            max(
                CREDIT_MIN,
                math.exp(
                    rng.gauss(
                        float(txn_params["regular_credit_mu_by_product_class"][product_class]),
                        float(txn_params["regular_credit_sigma"]),
                    )
                ),
            ),
        )
    )
    return day, amount
