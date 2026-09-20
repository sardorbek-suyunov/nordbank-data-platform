"""The movement phase's two decisions that a database cannot check for us.

**The lag conditioning**, because it is how invariants 1 and 2 are held at the cause. A late
arrival's business timestamp is drawn from the intersection of the lag distribution's support
with the lifetime of every entity the transaction references, so a timestamp outside the
account's open window or the card's validity is never produced and none has to be discarded.
A test that inserted rows and then looked for offenders would be testing the invariant; this
tests the thing that makes the invariant unnecessary.

**The fold arithmetic**, because the overdraft rule has two halves that are easy to conflate.
A same-day debit that would breach the limit is declined and moves nothing. A late arrival or
a deferred posting is not declined at all, and moves the balance wherever it goes — past the
limit if that is where it goes, which is the unauthorised overdraft the milestone exists to
produce.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from generator.config import load_profile
from generator.mutation.phases.movements import Fold, _contra_account, _draw_lag
from generator.mutation.snapshot import Account, Card

DAY = dt.date(2026, 9, 20)
LAGS = [2, 3, 4, 5]
WEIGHTS = [0.44, 0.29, 0.17, 0.10]
PARAMS = load_profile("dev").params


class FixedRng:
    """Draws the first admissible option, so the conditioning is what decides the answer."""

    def random(self) -> float:
        return 0.0

    def randrange(self, *args: int) -> int:
        return args[0] if len(args) > 1 else 0


def account(
    account_id: int = 1,
    opened: dt.date = dt.date(2020, 1, 1),
    limit: str = "500.0000",
    balance: str = "0.0000",
) -> Account:
    return Account(
        account_id=account_id,
        customer_id=account_id,
        currency_code="EUR",
        country_code="DE",
        product_class="current",
        status_code="active",
        opened_date=opened,
        overdraft_limit=Decimal(limit),
        balance=Decimal(balance),
    )


def card(issued: dt.date = dt.date(2024, 1, 1), expiry: dt.date = dt.date(2028, 1, 1)) -> Card:
    return Card(
        card_id=1, account_id=1, status_code="active", issued_date=issued, expiry_date=expiry
    )


# ------------------------------------------------------------------ the lag is conditioned


def test_an_unconstrained_account_can_draw_any_lag_in_the_range():
    drawn = {
        _draw_lag(FixedRngAt(index), LAGS, WEIGHTS, account(), card(), DAY)
        for index in range(len(LAGS))
    }
    assert drawn == set(LAGS)


class FixedRngAt(FixedRng):
    """Picks the option at `index` of whatever weighted list it is given."""

    def __init__(self, index: int) -> None:
        self._index = index

    def random(self) -> float:
        # weighted_pick walks the cumulative weights, so a threshold just past the sum of the
        # first `index` weights selects option `index`.
        return (sum(WEIGHTS[: self._index]) + WEIGHTS[self._index] / 2) / sum(WEIGHTS)


@pytest.mark.parametrize("lag", LAGS)
def test_no_lag_reaches_before_the_account_opened(lag: int):
    # The account opened `lag - 1` days ago, so every lag of `lag` or more is inadmissible.
    opened = DAY - dt.timedelta(days=lag - 1)
    for index in range(len(LAGS)):
        drawn = _draw_lag(FixedRngAt(index), LAGS, WEIGHTS, account(opened=opened), card(), DAY)
        assert drawn is None or DAY - dt.timedelta(days=drawn) >= opened


@pytest.mark.parametrize("lag", LAGS)
def test_no_lag_reaches_before_the_card_was_issued(lag: int):
    issued = DAY - dt.timedelta(days=lag - 1)
    for index in range(len(LAGS)):
        drawn = _draw_lag(FixedRngAt(index), LAGS, WEIGHTS, account(), card(issued=issued), DAY)
        assert drawn is None or DAY - dt.timedelta(days=drawn) >= issued


def test_no_lag_reaches_past_the_card_expiry():
    # Expired three days ago: only lags of four and five land inside the validity window.
    expiry = DAY - dt.timedelta(days=3)
    for index in range(len(LAGS)):
        drawn = _draw_lag(FixedRngAt(index), LAGS, WEIGHTS, account(), card(expiry=expiry), DAY)
        assert drawn is None or DAY - dt.timedelta(days=drawn) < expiry


def test_an_account_with_no_admissible_lag_is_not_a_candidate():
    # Opened yesterday: every lag in the range reaches before it existed.
    assert _draw_lag(FixedRng(), LAGS, WEIGHTS, account(opened=DAY), card(), DAY) is None


def test_a_card_issued_today_is_not_a_candidate():
    assert _draw_lag(FixedRng(), LAGS, WEIGHTS, account(), card(issued=DAY), DAY) is None


def test_mismatched_lag_weights_are_refused_rather_than_silently_truncated():
    # A short weight list would otherwise drop the longest lag and nothing would say so. The
    # phase checks the lengths once per tick and names the parameter; this is the backstop that
    # makes a mismatch impossible to reach silently if that check is ever moved.
    with pytest.raises(ValueError):
        _draw_lag(FixedRng(), LAGS, WEIGHTS[:3], account(), card(), DAY)


# ---------------------------------------------------------------------- the fold arithmetic


def fold(balance: str = "0.0000") -> tuple[Fold, Account]:
    subject = account(balance=balance)
    return (
        Fold(opening={1: subject.balance}, balance={1: subject.balance}),
        subject,
    )


def test_a_debit_inside_the_limit_does_not_breach():
    state, subject = fold("-400.0000")
    assert not state.would_breach(subject, Decimal("-100.0000"))


def test_a_debit_past_the_limit_breaches():
    state, subject = fold("-400.0000")
    assert state.would_breach(subject, Decimal("-100.0001"))


def test_the_limit_is_the_floor_and_not_zero():
    # An overdrawn account inside its arranged limit is not breaching anything.
    state, subject = fold("-100.0000")
    assert not state.would_breach(subject, Decimal("-1.0000"))


def test_a_movement_that_is_not_declined_can_leave_the_account_past_its_limit():
    # This is the late arrival and the deferred posting: no funds check, so the balance goes
    # where it goes. Nothing in the source can stop it, which is the point.
    state, subject = fold("-499.0000")
    state.apply(subject, Decimal("-250.0000"))
    assert state.balance[1] == Decimal("-749.0000")
    assert state.unauthorised_overdrafts == {1}


def test_an_account_inside_its_limit_is_not_reported_as_an_unauthorised_overdraft():
    state, subject = fold("0.0000")
    state.apply(subject, Decimal("-500.0000"))
    assert state.unauthorised_overdrafts == set()


def test_only_accounts_whose_balance_moved_reach_the_update():
    state, subject = fold("100.0000")
    state.apply(subject, Decimal("25.0000"))
    state.apply(subject, Decimal("-25.0000"))
    ids, deltas = state.deltas()
    # Touched, so the guard will look at it; net zero, so there is nothing to write.
    assert state.touched == {1}
    assert (ids, deltas) == ([], [])


def test_the_delta_is_against_the_opening_balance_not_the_last_movement():
    state, subject = fold("100.0000")
    state.apply(subject, Decimal("-10.0000"))
    state.apply(subject, Decimal("-15.0000"))
    assert state.deltas() == ([1], [Decimal("-25.0000")])


def test_amounts_stay_quantised_through_the_fold():
    state, subject = fold("0.0000")
    state.apply(subject, Decimal("0.005"))
    assert state.balance[1] == Decimal("0.0100")


# ------------------------------------------------------------------------- the ledger contra


def test_every_bank_initiated_type_reaches_a_named_ledger_account():
    gl = PARAMS["ledger"]["accounts"]
    for type_code in PARAMS["transactions"]["bank_initiated_per_account_month"]:
        assert _contra_account(gl, type_code) in gl.values()


def test_a_reversal_faces_suspense_rather_than_cash():
    gl = PARAMS["ledger"]["accounts"]
    assert _contra_account(gl, "chargeback_adjustment") == gl["suspense"]
    assert _contra_account(gl, "internal_reclass") == gl["suspense"]


def test_a_customer_movement_faces_cash():
    gl = PARAMS["ledger"]["accounts"]
    assert _contra_account(gl, "card_purchase") == gl["cash"]
