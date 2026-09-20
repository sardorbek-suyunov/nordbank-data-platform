"""The per-account regular context, and the property the mutation engine needs from it.

Addressability is the whole point, so that is what is pinned: the same account and the same
calendar month give the same income, the same familiar merchants and the same mandates, derived
from the seed alone, with nothing drawn before them and no book in memory. A tick has only
those two coordinates, so anything that fails here is a fact the tick cannot compute.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from generator.config import load_profile
from generator.realism import recurrence
from generator.rng import SubStreams

PARAMS = load_profile("dev").params
TXN = PARAMS["transactions"]
AMOUNTS = PARAMS["amounts"]


def streams() -> SubStreams:
    return SubStreams(42)


def credit(account_id: int, product_class: str = "current") -> tuple[int, Decimal]:
    return recurrence.regular_credit(
        streams().stream("recurrence.credit", account_id), TXN, product_class
    )


def test_the_month_key_is_the_calendar_month_on_both_sides():
    # The historical load knows a month by its position in the history and a tick knows a date.
    # The key has to be the thing both can compute, or an account's context steps at the anchor.
    assert recurrence.month_key(dt.date(2026, 9, 1)) == "2026-09"
    assert recurrence.month_key(dt.date(2026, 9, 30)) == "2026-09"
    assert recurrence.month_key(dt.date(2026, 10, 1)) == "2026-10"


def test_income_is_a_function_of_the_account_and_the_seed_alone():
    for account_id in (1, 17, 4242):
        assert credit(account_id) == credit(account_id)


def test_income_differs_between_accounts():
    drawn = {credit(account_id) for account_id in range(1, 60)}
    assert len(drawn) > 1


def test_income_that_arrives_lands_in_the_last_week_and_is_plausible():
    paid = [credit(account_id) for account_id in range(1, 400)]
    receiving = [(day, amount) for day, amount in paid if day]
    assert receiving, "no account in four hundred receives a regular credit"
    for day, amount in receiving:
        assert recurrence.CREDIT_DAY_MIN <= day <= recurrence.CREDIT_DAY_MAX
        assert Decimal(str(recurrence.CREDIT_MIN)) <= amount <= Decimal(str(recurrence.CREDIT_MAX))


def test_an_account_receiving_nothing_is_zero_on_both_halves():
    # Not a null and not a sentinel day: the movement pass tests the day for truthiness.
    silent = [credit(account_id) for account_id in range(1, 400) if credit(account_id)[0] == 0]
    assert silent, "every account in four hundred receives a regular credit"
    for day, amount in silent:
        assert (day, amount) == (0, Decimal("0.0000"))


def test_savings_accounts_receive_a_regular_credit_less_often_than_current_ones():
    span = range(1, 900)
    current = sum(1 for account_id in span if credit(account_id, "current")[0])
    savings = sum(1 for account_id in span if credit(account_id, "savings")[0])
    assert savings < current


def test_familiar_merchants_are_stable_for_an_account_and_month():
    first = recurrence.familiar_merchants(
        streams().stream("recurrence.merchants", 7, "2026-09"), 120
    )
    again = recurrence.familiar_merchants(
        streams().stream("recurrence.merchants", 7, "2026-09"), 120
    )
    assert first == again
    assert len(first) == recurrence.FAMILIAR_MERCHANTS
    assert all(1 <= merchant_id <= 120 for merchant_id in first)


def test_familiar_merchants_move_between_months():
    september = recurrence.familiar_merchants(
        streams().stream("recurrence.merchants", 7, "2026-09"), 120
    )
    october = recurrence.familiar_merchants(
        streams().stream("recurrence.merchants", 7, "2026-10"), 120
    )
    assert september != october


def test_no_merchants_means_no_familiar_set_rather_than_an_error():
    assert recurrence.familiar_merchants(streams().stream("recurrence.merchants", 7, "x"), 0) == []


def test_mandates_need_a_card():
    without = recurrence.mandates(
        streams().stream("recurrence.mandates", 7, "2026-09"), AMOUNTS, [1, 2], has_card=False
    )
    assert without == []


def test_mandates_are_stable_and_bounded():
    found = []
    for account_id in range(1, 200):
        drawn = recurrence.mandates(
            streams().stream("recurrence.mandates", account_id, "2026-09"),
            AMOUNTS,
            [3, 9],
            has_card=True,
        )
        repeat = recurrence.mandates(
            streams().stream("recurrence.mandates", account_id, "2026-09"),
            AMOUNTS,
            [3, 9],
            has_card=True,
        )
        assert drawn == repeat
        assert len(drawn) <= recurrence.MAX_MANDATES
        found.extend(drawn)

    assert found, "no account in two hundred carries a mandate"
    prices = {Decimal(str(price)) for price in AMOUNTS["recurring_amount_choices"]}
    for day_of_month, amount, merchant_id in found:
        assert 1 <= day_of_month <= 28
        assert amount.quantize(Decimal("0.01")) in prices
        assert merchant_id in (3, 9)
