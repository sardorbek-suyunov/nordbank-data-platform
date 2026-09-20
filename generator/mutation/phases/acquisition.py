"""New customers, and everything that arrives with them.

A day's onboarding: customers, their residential and sometimes correspondence address, a first
account with its holder, a card where the product carries one, the opening deposit, and the loan
applications the book generates. It runs after the lifecycle sweep and before movements, because
what it writes must exist before anything can post to it.

**The rate continues the historical trend rather than restating it.** The historical load hands
each month a share of the book by weight — compound growth times damped seasonality — and a tick
evaluates the same expression at its own month and divides by the month's length. So the
acquisition curve does not step at the anchor, and a `dev` book ticked for thirty days keeps
growing at the rate its first thirty-six months grew at.

**The draws are the historical ones, not a second implementation of them.**
`generator/entities/people.py` exposes the per-customer draw and the row builders that the whole
book pass uses, and this phase calls them. The parameters were always single-sourced; sharing
the code makes the *sequence* single-sourced too, which is the part a reviewer cannot check by
reading two files.

**A new account's opening deposit is written here, not folded by the movement phase.** The
movement phase decides against a snapshot taken before this one ran, so it cannot see an account
this tick opened. Writing the deposit and the account's balance together keeps invariant 4 exact
without the movement phase needing to know that acquisition happened.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

from ...entities import people
from ...realism import lending as lending_model
from ...realism.calendar import add_months, at_time, days_in_month, month_index, month_weight
from ...realism.distributions import bernoulli, cents, poisson, weighted_choice
from ..snapshot import Account
from ..tick import TickContext

# The hour an account opens and the hour its opening deposit lands. Inside the phase's window
# (09:00 to 17:00), because onboarding happens in office hours.
OPENING_HOUR = 10
DEPOSIT_HOUR = 11


def run(context: TickContext) -> None:
    for _ in range(_arrivals_today(context)):
        _onboard(context)
    _applications(context)


def _arrivals_today(context: TickContext) -> int:
    """How many customers sign up today, on the curve the historical load is drawn from.

    The load distributes the acquired share of the book across the months of the history by
    weight, where a month's weight is compound growth times seasonality damped by
    `seasonality_weight`. This evaluates the same expression at the simulated month and divides
    by the month's length, so the rate carries on from where the history left it instead of
    being a second number that happens to be close.
    """
    params = context.profile.params
    acquisition = params["acquisition"]
    profile = context.profile
    day = context.simulated_date

    acquired = profile.customers * (1.0 - float(acquisition["initial_customer_share"]))
    history_start = context.snapshot.history_start
    months = max(1, int(profile.history_months))

    base = history_start.replace(day=1)
    total_weight = math.fsum(_month_weight(params, base, index) for index in range(months))
    if total_weight <= 0:
        return 0

    index = month_index(history_start, day)
    monthly = acquired * _month_weight(params, base, index) / total_weight
    daily = monthly / days_in_month(day.year, day.month)
    return poisson(context.stream("acquisition"), daily)


def _month_weight(params: dict, base: dt.date, index: int) -> float:
    """One month's acquisition weight: compound growth times damped seasonality.

    The same expression `generator/realism/lifecycle.acquisition_weights` evaluates over the
    months of the history, at an index that carries on past the anchor. `base` is the first
    month of the history, so the seasonal term reads the calendar month rather than a position.
    """
    acquisition = params["acquisition"]
    damping = float(acquisition["seasonality_weight"])
    growth = (1.0 + float(acquisition["monthly_growth_rate"])) ** index
    month = add_months(base, index)
    seasonal = 1.0 + (month_weight(params["transactions"]["month_weights"], month) - 1.0) * damping
    return growth * seasonal


def _onboard(context: TickContext) -> None:
    """One new customer, their address, their first account, a card and the opening deposit."""
    params = context.profile.params
    day = context.simulated_date
    snapshot = context.snapshot
    customer_id = snapshot.take_id("customers")
    rng = context.stream("acquisition.customer", customer_id)

    drawn = people.draw_customer(rng, params["customers"], customer_id, day)
    stamp = at_time(day, OPENING_HOUR, rng.randrange(60), rng.randrange(60))
    context.writer.add("customers", people.customer_row(customer_id, day, drawn))

    address_id = snapshot.take_id("customer_addresses")
    context.writer.add(
        "customer_addresses",
        people.address_row(rng, address_id, customer_id, drawn.country_code, day, stamp),
    )
    if bernoulli(rng, float(params["customers"]["correspondence_address_share"])):
        address_id = snapshot.take_id("customer_addresses")
        context.writer.add(
            "customer_addresses",
            people.address_row(
                rng,
                address_id,
                customer_id,
                drawn.country_code,
                day,
                stamp,
                address_type="correspondence",
            ),
        )

    account = _open_account(context, rng, customer_id, drawn.country_code, stamp)
    _issue_card(context, rng, account, day, stamp)
    _opening_deposit(context, rng, account)


def _open_account(
    context: TickContext, rng, customer_id: int, country: str, stamp: dt.datetime
) -> Account:
    """The first account, which opens with the customer.

    Only the first. Second and third accounts open fourteen to nine hundred days after signup in
    the historical model, so a customer who joined today does not have one — which is the same
    rule that stopped the load clamping them onto the anchor.
    """
    params = context.profile.params["accounts"]
    day = context.simulated_date
    account_id = context.snapshot.take_id("accounts")
    type_code = weighted_choice(rng, params["type_mix"])
    product_class = str(context.snapshot.ref.row("account_types", type_code)["product_class_code"])

    currency = "EUR"
    if bernoulli(rng, float(params["non_eur_share"])):
        currency = weighted_choice(rng, params["non_eur_mix"])

    overdraft = Decimal("0.0000")
    if product_class == "current" and bernoulli(rng, float(params["overdraft_share"])):
        overdraft = cents(rng.choice(params["overdraft_limit_choices"]))

    opening_balance = cents(
        min(
            250_000.0,
            max(
                0.0,
                math.exp(
                    rng.gauss(
                        float(params["opening_balance_mu"]), float(params["opening_balance_sigma"])
                    )
                ),
            ),
        )
    )

    context.writer.add(
        "accounts",
        (
            account_id,
            f"ACC{account_id:012d}",
            f"{country}{rng.randrange(10, 99):02d}{account_id:016d}",
            type_code,
            currency,
            country,
            "active",
            day,
            None,
            overdraft,
            # The opening deposit is written with the account, so the stored balance is the
            # signed sum of posted movements from the first instant the row exists.
            opening_balance,
            stamp,
            stamp,
            False,
        ),
    )

    holder_id = context.snapshot.take_id("account_holders")
    context.writer.add(
        "account_holders",
        (holder_id, account_id, customer_id, "primary", Decimal("1.00000000"), stamp, stamp, False),
    )

    account = Account(
        account_id=account_id,
        customer_id=customer_id,
        currency_code=currency,
        country_code=country,
        product_class=product_class,
        status_code="active",
        opened_date=day,
        overdraft_limit=overdraft,
        balance=opening_balance,
    )
    context.snapshot.accounts[account_id] = account
    context.snapshot.customer_country[customer_id] = country
    # The guard re-derives the balance of every account the tick touched, and an account opened
    # today with a deposit on it is one of them.
    context.touched_accounts.add(account_id)
    return account


def _issue_card(context: TickContext, rng, account: Account, day: dt.date, stamp) -> None:
    params = context.profile.params["cards"]
    share = float(params["share_by_product_class"].get(account.product_class, 0.0))
    if not bernoulli(rng, share):
        return

    card_id = context.snapshot.take_id("cards")
    validity = int(params["validity_years"])
    expiry = dt.date(day.year + validity, day.month, min(day.day, 28))
    context.writer.add(
        "cards",
        (
            card_id,
            f"NB{card_id:010d}",
            account.account_id,
            weighted_choice(rng, params["product_mix"]),
            f"{400000 + (card_id % 99999):06d}",
            f"{card_id % 10000:04d}",
            "active",
            day,
            expiry,
            stamp,
            stamp,
            False,
        ),
    )


def _opening_deposit(context: TickContext, rng, account: Account) -> None:
    """The deposit that explains the account's opening balance.

    A real movement rather than a starting value, because invariant 4 asserts that the balance
    *is* the signed sum of posted movements. A balance nothing explains would make the invariant
    unsatisfiable, and adding the opening balance to both sides would make it assert nothing.
    """
    if account.balance <= 0:
        return
    day = context.simulated_date
    booked_at = at_time(day, DEPOSIT_HOUR, rng.randrange(60), 0)
    transaction_id = context.snapshot.take_id("transactions")

    context.writer.add(
        "transactions",
        (
            transaction_id,
            f"TXN{transaction_id:013d}",
            account.account_id,
            None,
            None,
            None,
            "transfer_in",
            "api",
            "posted",
            None,
            booked_at,
            day,
            account.balance,
            account.currency_code,
            None,
            None,
            None,
            booked_at,
            booked_at,
            False,
        ),
    )
    context.ledger.post_customer_credit(
        day,
        "transfer_in",
        "transaction",
        transaction_id,
        account.currency_code,
        account.balance,
        account.account_id,
        context.profile.params["ledger"]["accounts"]["cash"],
        booked_at,
    )


def _applications(context: TickContext) -> None:
    """Loan applications from the existing book, all of them still undecided.

    An application arrives; it is not decided in the same instant. The historical load draws a
    decision lag of two to 216 hours, and the lifecycle phase applies that lag on a later tick,
    so an application sits in `scoring` for days exactly as it does in the historical half of
    the book. Writing a decision here would have meant either a decision timestamp before the
    application arrived, which the schema refuses, or a lag the source does not have.
    """
    lending = context.profile.params["lending"]
    day = context.simulated_date

    context.cursor.execute(
        """
        select c.customer_id, c.risk_band_code, a.currency_code
          from core.customers c
          join core.account_holders ah on ah.customer_id = c.customer_id
          join ref.holder_roles hr on hr.code = ah.holder_role_code and hr.is_primary
          join core.accounts a on a.account_id = ah.account_id
          join ref.account_types at on at.code = a.account_type_code
          join ref.account_statuses s on s.code = a.account_status_code
         where s.is_open and at.product_class_code = 'current'
           and not c.is_deleted and not a.is_deleted and not ah.is_deleted
         order by c.customer_id
        """
    )
    candidates = context.cursor.fetchall()
    if not candidates:
        return

    daily_rate = float(lending["applications_per_customer_year"]) / 365.0
    for customer_id, risk_band, currency in candidates:
        rng = context.stream("acquisition.application", customer_id)
        if not bernoulli(rng, daily_rate):
            continue

        application_id = context.snapshot.take_id("loan_applications")
        applied_at = at_time(day, rng.randrange(9, 18), rng.randrange(60), 0)
        context.writer.add(
            "loan_applications",
            (
                application_id,
                f"APP{application_id:012d}",
                customer_id,
                lending_model.draw_product(rng, lending),
                "referred" if bernoulli(rng, float(lending["referred_share"])) else "scoring",
                risk_band,
                None,
                applied_at,
                None,
                lending_model.draw_requested_amount(rng, lending),
                None,
                currency,
                applied_at,
                applied_at,
                False,
            ),
        )
