"""Transactions, payments, the balance fold, and everything that hangs off a movement.

This is the pass that decides what the bank's customers actually did, and it is organised the
way it is because of spec 003 invariant 4.

**Balances are derived from movements, never drawn.** `accounts.current_balance_amount` is the
opening balance folded over every posted movement on the account. A generator that drew a
closing balance and separately drew transactions would fail invariant 4 on essentially every
account, and no amount of reconciliation afterwards would fix it without rewriting one of the
two.

**The fold has to see an account's movements in time order**, which is why the loop is month by
month on the outside and account by account inside, with the month's events sorted by instant
before anything is written. Three things follow from that ordering, and all three are the
reason for it:

- Ids come out chronological. A transaction id that ran ahead of the clock would be a trap for
  the M3 mutation engine and for anything downstream that assumes the two agree.
- Memory stays bounded. One month of the full profile is a few hundred thousand events; five
  years of them is not something to hold.
- A debit that would breach the overdraft limit can be declined, because the balance at that
  instant is known. Without that the data would be incoherent — accounts drifting arbitrarily
  negative — while still passing every invariant, since nothing else checks it.

The ledger is written inline rather than in a second pass. Every batch balances on its own, so
a day of batches balances, and sorting tens of millions of entries by posting date afterwards
would buy nothing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ..config import RunConfig
from ..realism import amounts as amount_model
from ..realism import fraud as fraud_model
from ..realism import lifecycle, presentment, recurrence
from ..realism.calendar import (
    at_time,
    clamp_day_of_month,
    day_weight,
    days_in_month,
    draw_timestamp,
    growth_multiplier,
    progress,
)
from ..realism.distributions import (
    bernoulli,
    cents,
    first_digit,
    overdispersed_poisson,
    weighted_choice,
)
from ..refdata import RefData
from ..rng import SubStreams
from ..spool import Spool
from .catalogue import AgentBook, MerchantBook
from .deposits import AccountBook, CardBook
from .ledger import LedgerWriter
from .lending import CashEvent
from .people import CustomerBook
from .risk import AlertWriter, SessionWriter

ZERO = Decimal("0.0000")

# Event discriminators, kept as integers because the sort touches every one of them.
TRANSACTION = 0
PAYMENT = 1
LOAN_CASH = 2


@dataclass
class MovementStats:
    """What the run reports and the invariants are measured against."""

    transactions: int = 0
    payments: int = 0
    declined_for_funds: int = 0
    card_transactions: int = 0
    card_not_present: int = 0
    fraudulent: int = 0
    benford_digits: list[int] = field(default_factory=lambda: [0] * 9)
    digital_customer_initiated: int = 0
    digital_with_preceding_login: int = 0


def generate(
    config: RunConfig,
    ref: RefData,
    streams: SubStreams,
    spool: Spool,
    customers: CustomerBook,
    accounts: AccountBook,
    cards: CardBook,
    merchants: MerchantBook,
    agents: AgentBook,
    loan_cash: list[CashEvent],
    loan_hold_until: dict[int, dt.date] | None = None,
) -> MovementStats:
    params = config.profile.params
    txn_params = params["transactions"]
    fraud_params = params["fraud"]
    digital_params = params["digital"]
    attrition_params = params["attrition"]

    stats = MovementStats()
    txn_rows = spool.table("transactions")
    pay_rows = spool.table("payments")
    ledger = LedgerWriter(spool, params["ledger"]["accounts"])
    alerts = AlertWriter(spool)
    sessions = SessionWriter(spool, streams.seed, sorted(params["customers"]["country_mix"]))

    gl = params["ledger"]["accounts"]
    hour_weights = txn_params["hour_weights"]
    month_weights = txn_params["month_weights"]
    weekend_factor = float(txn_params["weekend_volume_factor"])
    anchor_end = at_time(config.anchor, 23, 59, 59)
    months = config.months
    history_start = config.history_start

    baseline_present = presentment.baseline_present_share(txn_params)
    card_not_present_share = 1.0 - baseline_present
    fraud_base = fraud_model.calibrate(fraud_params, hour_weights, card_not_present_share)

    direction_of_type = {
        code: str(row["direction"]) for code, row in ref.tables["transaction_types"].items()
    }
    posted_status = {
        code: bool(row["is_posted"]) for code, row in ref.tables["transaction_statuses"].items()
    }
    payment_direction = {
        code: str(row["direction"]) for code, row in ref.tables["payment_types"].items()
    }
    payment_posted = {
        code: bool(row["is_posted"]) for code, row in ref.tables["payment_statuses"].items()
    }
    # Hoisted out of the inner loop: read once per transaction, tens of millions of times
    # at the full profile.
    customer_initiated = {
        code: bool(row["is_customer_initiated"])
        for code, row in ref.tables["transaction_types"].items()
    }
    screening_names = _screening_names()

    merchant_count = len(merchants)
    agent_count = len(agents)

    # Accounts enter the active set in the month they open and leave it when they close.
    opening_buckets: dict[int, list[int]] = {}
    for account_id in range(1, len(accounts) + 1):
        opened = accounts.opened_date[account_id - 1]
        index = 0
        for position, month in enumerate(months):
            if month <= opened:
                index = position
            else:
                break
        opening_buckets.setdefault(index if opened >= months[0] else 0, []).append(account_id)

    active: list[int] = []
    transaction_id = 0
    payment_id = 0

    loan_cursor = 0
    loan_count = len(loan_cash)

    for month_index, month_start in enumerate(months):
        active.extend(opening_buckets.get(month_index, ()))
        month_end_day = dt.date(
            month_start.year,
            month_start.month,
            days_in_month(month_start.year, month_start.month),
        )
        window_first = max(month_start, history_start)
        window_last = min(month_end_day, config.anchor)
        if window_first > window_last:
            continue

        events: list[tuple] = []
        still_active: list[int] = []
        opened_this_month = set(opening_buckets.get(month_index, ()))

        # Attrition is decided before the month's movements, not after, so that an account
        # closing mid-month does not first acquire transactions dated after it closed. That is
        # what invariant 1 checks, and generating first would fail it on every closure.
        _month_attrition(
            streams=streams,
            accounts=accounts,
            active=active,
            month_index=month_index,
            window_first=window_first,
            window_last=window_last,
            attrition_params=attrition_params,
            loan_hold_until=loan_hold_until or {},
        )

        for account_id in active:
            index = account_id - 1
            closed = accounts.closed_date[index]
            if closed is not None and closed < window_first:
                continue
            if closed is None:
                still_active.append(account_id)
            account_last = min(window_last, closed) if closed is not None else window_last
            account_first = max(window_first, accounts.opened_date[index])
            if account_first > account_last:
                continue

            _account_month(
                config=config,
                ref=ref,
                streams=streams,
                params=params,
                accounts=accounts,
                cards=cards,
                merchants=merchants,
                agents=agents,
                customers=customers,
                account_id=account_id,
                month_index=month_index,
                is_first_month=account_id in opened_this_month,
                window_first=account_first,
                window_last=account_last,
                hour_weights=hour_weights,
                month_weights=month_weights,
                weekend_factor=weekend_factor,
                merchant_count=merchant_count,
                agent_count=agent_count,
                baseline_present=baseline_present,
                fraud_base=fraud_base,
                screening_names=screening_names,
                events=events,
            )

        active = still_active

        # Loan cash flows for this month join the same fold.
        # Loan cash flows are transactions on the customer's account, not a separate
        # kind of movement. A disbursement arrives as a credit and a repayment is
        # collected as a direct debit, which is what a statement shows and what lets
        # invariant 4 reconcile the balance against transactions and payments alone.
        while loan_cursor < loan_count and loan_cash[loan_cursor].when.date() <= window_last:
            event = loan_cash[loan_cursor]
            incoming = event.amount > 0
            events.append(
                (
                    event.when,
                    event.account_id,
                    0,
                    TRANSACTION,
                    (
                        "transfer_in" if incoming else "direct_debit",
                        "api",
                        None,
                        None,
                        None,
                        event.amount if incoming else -event.amount,
                        None,
                        "posted",
                        None,
                        event.when.date(),
                        event.currency_code,
                        True,
                        fraud_model.FraudContext(False, False, False, False, False),
                        False,
                        "standard",
                        event.principal_component,
                    ),
                )
            )
            loan_cursor += 1

        events.sort(key=lambda item: (item[0], item[1], item[2]))

        for booked_at, account_id, _seq, kind, payload in events:
            index = account_id - 1
            if kind == TRANSACTION:
                transaction_id += 1
                _emit_transaction(
                    txn_rows=txn_rows,
                    ledger=ledger,
                    alerts=alerts,
                    sessions=sessions,
                    streams=streams,
                    stats=stats,
                    accounts=accounts,
                    customers=customers,
                    ref=ref,
                    gl=gl,
                    fraud_params=fraud_params,
                    digital_params=digital_params,
                    direction_of_type=direction_of_type,
                    posted_status=posted_status,
                    customer_initiated=customer_initiated,
                    transaction_id=transaction_id,
                    account_id=account_id,
                    booked_at=booked_at,
                    payload=payload,
                    anchor_end=anchor_end,
                )
            elif kind == PAYMENT:
                payment_id += 1
                _emit_payment(
                    pay_rows=pay_rows,
                    ledger=ledger,
                    stats=stats,
                    accounts=accounts,
                    gl=gl,
                    payment_direction=payment_direction,
                    payment_posted=payment_posted,
                    payment_id=payment_id,
                    account_id=account_id,
                    booked_at=booked_at,
                    payload=payload,
                    anchor_end=anchor_end,
                )

        # Background login sessions: the customer opening the app without transacting.
        _month_sessions(
            streams=streams,
            params=params,
            accounts=accounts,
            customers=customers,
            sessions=sessions,
            active=active,
            month_index=month_index,
            window_first=window_first,
            window_last=window_last,
            anchor_end=anchor_end,
            digital_params=digital_params,
        )

    stats.payments = payment_id
    stats.transactions = transaction_id
    return stats


def _screening_names() -> list[str]:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "fixtures" / "screening_positives.csv"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.split(",")[0].strip() for line in lines[1:] if line.strip()]


def _account_month(**kw) -> None:
    """Generate one account's events for one month, appending them to the month's list."""
    config: RunConfig = kw["config"]
    streams: SubStreams = kw["streams"]
    params = kw["params"]
    accounts: AccountBook = kw["accounts"]
    cards: CardBook = kw["cards"]
    merchants: MerchantBook = kw["merchants"]
    account_id: int = kw["account_id"]
    month_index: int = kw["month_index"]
    window_first: dt.date = kw["window_first"]
    window_last: dt.date = kw["window_last"]
    events: list = kw["events"]

    txn_params = params["transactions"]
    amount_params = params["amounts"]
    pay_params = params["payments"]
    fraud_params = params["fraud"]

    index = account_id - 1
    if accounts.status_code[index] == "dormant":
        return

    rng = streams.stream("movements", account_id, month_index)
    product_class = accounts.product_class[index]
    currency = accounts.currency_code[index]
    account_country = accounts.country_code[index]

    rate_by_class = txn_params.get("per_account_month_by_product_class")
    base_rate = (
        float(rate_by_class.get(product_class, txn_params["per_account_month"]))
        if rate_by_class
        else float(txn_params["per_account_month"])
    )
    card_ids = accounts.card_ids[index]
    has_card = bool(card_ids)
    span_days = (window_last - window_first).days + 1
    if span_days <= 0:
        return

    # A monthly rate over a window that is not a whole month is scaled to the days it actually
    # covers. Three windows are short: the month the history starts in, the month it ends in,
    # and the month an account opens in. Without this the rate is spread over the short window
    # instead of the month, which makes those days denser rather than fewer — measured at the
    # `ci` profile, the eighteen days before the anchor ran at 608.7 transactions a day against
    # August's 209.2, a cliff at exactly the date M4 begins extracting from.
    window_fraction = span_days / days_in_month(window_first.year, window_first.month)

    seasonal = day_weight(kw["month_weights"], 1.0, window_first)
    growth = growth_multiplier(float(params["acquisition"]["monthly_growth_rate"]), month_index)
    expected = base_rate * seasonal * growth * window_fraction
    count = overdispersed_poisson(rng, expected, float(txn_params["per_account_month_dispersion"]))

    # The account's regular context for this month, each on its own stream keyed on the account
    # and the calendar month. Addressable rather than drawn in sequence here, because the
    # mutation engine has to derive the same values from the same seed without replaying this
    # function. See generator/realism/recurrence.py.
    month_key = recurrence.month_key(window_first)
    familiar = recurrence.familiar_merchants(
        streams.stream("recurrence.merchants", account_id, month_key), len(merchants)
    )
    mandates = recurrence.mandates(
        streams.stream("recurrence.mandates", account_id, month_key),
        amount_params,
        familiar,
        has_card=has_card,
    )

    weekend_factors = txn_params["weekend_band_factors"]
    new_device_month = bernoulli(
        streams.stream("devices", accounts.customer_id[index], month_key),
        float(params["digital"]["new_device_monthly_hazard"]),
    )

    seq = 0

    # The opening deposit. It is a real movement rather than a starting balance, so that
    # invariant 4 can assert the balance against the transaction history with nothing
    # unexplained on either side.
    if kw["is_first_month"] and accounts.opening_balance[index] > 0:
        seq += 1
        opened = accounts.opened_date[index]
        events.append(
            (
                at_time(opened, 9, rng.randrange(60), 0),
                account_id,
                seq,
                TRANSACTION,
                (
                    "transfer_in",
                    "api",
                    None,
                    None,
                    None,
                    accounts.opening_balance[index],
                    None,
                    "posted",
                    None,
                    opened,
                    currency,
                    True,
                    fraud_model.FraudContext(False, False, False, False, False),
                    False,
                    "standard",
                    None,
                ),
            )
        )

    for _ in range(count):
        day = window_first + dt.timedelta(days=rng.randrange(span_days))
        # Weekend volume is lower. A drawn day that lands at the weekend is sometimes
        # dropped rather than moved to a weekday, so the weekday shape is not distorted
        # by the ones that would have been weekend transactions.
        if day.weekday() >= 5 and bernoulli(rng, 1.0 - kw["weekend_factor"]):
            continue

        type_code = weighted_choice(rng, txn_params["type_mix"])
        channel_mix = txn_params["channel_mix_by_type"].get(type_code)
        if channel_mix is None:
            continue
        channel_code = weighted_choice(rng, channel_mix)

        card_id = None
        merchant_id = None
        agent_id = None
        band = "standard"

        if type_code in ("card_purchase", "card_refund", "atm_withdrawal"):
            if not has_card:
                continue
            card_id = card_ids[rng.randrange(len(card_ids))]
            # `usable_until` rather than the expiry: a card that is blocked or cancelled at the
            # anchor stopped authorising on the day it was blocked, not on the anchor.
            if not (cards.issued_date[card_id - 1] <= day < cards.usable_until[card_id - 1]):
                continue
            if type_code == "atm_withdrawal":
                band = "cash"
            else:
                unfamiliar = bernoulli(rng, float(fraud_params["unfamiliar_merchant_share"]))
                if unfamiliar or not familiar:
                    merchant_id = rng.randrange(1, len(merchants) + 1) if len(merchants) else None
                else:
                    merchant_id = familiar[rng.randrange(len(familiar))]
                if merchant_id is None:
                    continue
                band = merchants.band_code[merchant_id - 1]
                weekend_factor_band = float(weekend_factors.get(band, 1.0))
                if day.weekday() >= 5 and bernoulli(rng, 1.0 - min(1.0, weekend_factor_band)):
                    continue
        elif type_code in ("agent_withdrawal", "agent_deposit"):
            if kw["agent_count"] == 0:
                continue
            agent_id = rng.randrange(1, kw["agent_count"] + 1)
            band = "cash"

        booked_at = draw_timestamp(rng, day, kw["hour_weights"])
        seq += 1

        band_params = amount_params["by_band"].get(band, amount_params["by_band"]["standard"])
        is_recurring = False
        if band == "cash":
            magnitude = amount_model.cash_amount(rng, amount_params, band_params)
        elif type_code in ("transfer_out", "transfer_in", "direct_debit", "standing_order"):
            magnitude = amount_model.transfer_amount(rng, amount_params)
        else:
            magnitude = amount_model.purchase_amount(rng, band_params)

        is_card_present = None
        if card_id is not None:
            is_card_present = presentment.decide(
                rng,
                txn_params,
                channel_code,
                progress(config.history_start, config.anchor, day),
                kw["baseline_present"],
            )

        context = fraud_model.FraudContext(
            card_not_present=is_card_present is False,
            unfamiliar_merchant=merchant_id is not None and merchant_id not in familiar,
            night_hour=booked_at.hour in fraud_params["night_hours"],
            velocity_burst=False,
            new_device=new_device_month and channel_code in ("ecommerce", "mobile_app", "web"),
        )
        fraudulent = card_id is not None and fraud_model.is_fraudulent(
            rng, fraud_params, context, kw["fraud_base"]
        )

        status_code = weighted_choice(rng, txn_params["status_mix"])
        auth_outcome = (
            weighted_choice(rng, txn_params["authorisation_mix"]) if card_id is not None else None
        )
        value_date = day
        if bernoulli(rng, float(txn_params["value_date_lag_share"])):
            value_date = day + dt.timedelta(
                days=rng.randrange(1, int(txn_params["value_date_lag_days_max"]) + 1)
            )
            if value_date > config.anchor:
                value_date = config.anchor

        events.append(
            (
                booked_at,
                account_id,
                seq,
                TRANSACTION,
                (
                    type_code,
                    channel_code,
                    card_id,
                    merchant_id,
                    agent_id,
                    magnitude,
                    is_card_present,
                    status_code,
                    auth_outcome,
                    value_date,
                    currency,
                    is_recurring,
                    context,
                    fraudulent,
                    band,
                    None,
                ),
            )
        )

    # The regular incoming credit, on its own day at its own amount. It is a transfer_in
    # through the internal channel, which is what a salary or pension arrives as.
    salary_day = accounts.salary_day[index]
    if salary_day:
        pay_day = clamp_day_of_month(window_first.year, window_first.month, salary_day)
        if window_first <= pay_day <= window_last:
            seq += 1
            events.append(
                (
                    at_time(pay_day, 5, rng.randrange(60), 0),
                    account_id,
                    seq,
                    TRANSACTION,
                    (
                        "transfer_in",
                        "api",
                        None,
                        None,
                        None,
                        accounts.salary_amount[index],
                        None,
                        "posted",
                        None,
                        pay_day,
                        currency,
                        True,
                        fraud_model.FraudContext(False, False, False, False, False),
                        False,
                        "standard",
                        None,
                    ),
                )
            )

    # Recurring mandates fire once per month on their own day, at the same amount.
    for day_of_month, amount, merchant_id in mandates:
        day = clamp_day_of_month(window_first.year, window_first.month, day_of_month)
        if not (window_first <= day <= window_last):
            continue
        if not has_card:
            continue
        card_id = card_ids[0]
        if not (cards.issued_date[card_id - 1] <= day < cards.usable_until[card_id - 1]):
            continue
        seq += 1
        booked_at = at_time(day, rng.randrange(2, 6), rng.randrange(60), 0)
        context = fraud_model.FraudContext(False, False, False, False, False)
        events.append(
            (
                booked_at,
                account_id,
                seq,
                TRANSACTION,
                (
                    "card_purchase",
                    "ecommerce",
                    card_id,
                    merchant_id,
                    None,
                    amount,
                    False,
                    "posted",
                    "approved",
                    day,
                    currency,
                    True,
                    context,
                    False,
                    "standard",
                    None,
                ),
            )
        )

    # A velocity burst: several card transactions inside a short window, which is the context
    # the velocity_card detection rule exists for.
    burst_probability = (
        float(fraud_params["velocity_burst_share"])
        * expected
        / max(1.0, float(fraud_params["velocity_burst_length"]))
    )
    if has_card and bernoulli(rng, min(1.0, burst_probability)) and len(merchants) > 0:
        day = window_first + dt.timedelta(days=rng.randrange(span_days))
        card_id = card_ids[rng.randrange(len(card_ids))]
        if cards.issued_date[card_id - 1] <= day < cards.usable_until[card_id - 1]:
            start = draw_timestamp(rng, day, kw["hour_weights"])
            for step in range(int(fraud_params["velocity_burst_length"])):
                seq += 1
                merchant_id = rng.randrange(1, len(merchants) + 1)
                band = merchants.band_code[merchant_id - 1]
                band_params = amount_params["by_band"].get(
                    band, amount_params["by_band"]["standard"]
                )
                booked_at = start + dt.timedelta(minutes=step * rng.randrange(2, 8))
                is_card_present = presentment.decide(
                    rng,
                    txn_params,
                    "ecommerce",
                    progress(config.history_start, config.anchor, day),
                    kw["baseline_present"],
                )
                context = fraud_model.FraudContext(
                    card_not_present=is_card_present is False,
                    unfamiliar_merchant=merchant_id not in familiar,
                    night_hour=booked_at.hour in fraud_params["night_hours"],
                    velocity_burst=True,
                    new_device=new_device_month,
                )
                events.append(
                    (
                        booked_at,
                        account_id,
                        seq,
                        TRANSACTION,
                        (
                            "card_purchase",
                            "ecommerce",
                            card_id,
                            merchant_id,
                            None,
                            amount_model.purchase_amount(rng, band_params),
                            is_card_present,
                            "posted",
                            "approved",
                            day,
                            currency,
                            False,
                            context,
                            fraud_model.is_fraudulent(rng, fraud_params, context, kw["fraud_base"]),
                            band,
                            None,
                        ),
                    )
                )

    # Bank-initiated postings, which arrive whether the customer transacted or not.
    bank_channel = txn_params["bank_initiated_channel"]
    for type_code, monthly_rate in sorted(txn_params["bank_initiated_per_account_month"].items()):
        if not bernoulli(rng, min(1.0, float(monthly_rate) * window_fraction)):
            continue
        day = window_first + dt.timedelta(days=rng.randrange(span_days))
        seq += 1
        booked_at = at_time(day, 2, rng.randrange(60), 0)
        amount = amount_model.fee_amount(rng, amount_params)
        events.append(
            (
                booked_at,
                account_id,
                seq,
                TRANSACTION,
                (
                    type_code,
                    bank_channel,
                    None,
                    None,
                    None,
                    amount,
                    None,
                    "posted",
                    None,
                    day,
                    currency,
                    False,
                    fraud_model.FraudContext(False, False, False, False, False),
                    False,
                    "standard",
                    None,
                ),
            )
        )

    # Payment instructions.
    payment_count = overdispersed_poisson(
        rng,
        float(pay_params["per_account_month"]) * growth * window_fraction,
        float(pay_params["per_account_month_dispersion"]),
    )
    for _ in range(payment_count):
        day = window_first + dt.timedelta(days=rng.randrange(span_days))
        seq += 1
        type_code = weighted_choice(rng, pay_params["type_mix"])
        scheme = pay_params["scheme_by_type"][type_code]
        status = weighted_choice(rng, pay_params["status_mix"])
        amount = amount_model.payment_amount(rng, pay_params)
        initiated_at = draw_timestamp(rng, day, kw["hour_weights"])

        cross_border = bernoulli(rng, float(pay_params["cross_border_share"]))
        counterparty_country = (
            weighted_choice(rng, pay_params["corridor_mix"]) if cross_border else account_country
        )
        name = None
        if kw["screening_names"] and bernoulli(rng, float(pay_params["screening_positive_share"])):
            name = kw["screening_names"][rng.randrange(len(kw["screening_names"]))]
        elif bernoulli(rng, 0.94):
            from . import vocabulary as vocab

            name = (
                f"{vocab.GIVEN_NAMES[rng.randrange(len(vocab.GIVEN_NAMES))]} "
                f"{vocab.FAMILY_NAMES[rng.randrange(len(vocab.FAMILY_NAMES))]}"
            )

        events.append(
            (
                initiated_at,
                account_id,
                seq,
                PAYMENT,
                (
                    type_code,
                    scheme,
                    status,
                    amount,
                    counterparty_country,
                    name,
                    currency,
                    bernoulli(rng, float(pay_params["remittance_reference_share"])),
                    rng.randrange(0, int(pay_params["settlement_lag_days_max"]) + 1),
                    rng.randrange(10, 99),
                ),
            )
        )


def _emit_transaction(**kw) -> None:
    accounts: AccountBook = kw["accounts"]
    stats: MovementStats = kw["stats"]
    ledger: LedgerWriter = kw["ledger"]
    gl = kw["gl"]
    account_id: int = kw["account_id"]
    booked_at: dt.datetime = kw["booked_at"]
    transaction_id: int = kw["transaction_id"]
    index = account_id - 1

    (
        type_code,
        channel_code,
        card_id,
        merchant_id,
        agent_id,
        magnitude,
        is_card_present,
        status_code,
        auth_outcome,
        value_date,
        currency,
        is_recurring,
        context,
        fraudulent,
        _band,
        loan_principal,
    ) = kw["payload"]

    direction = kw["direction_of_type"][type_code]
    posted = kw["posted_status"].get(status_code, False)
    signed = -magnitude if direction == "debit" else magnitude

    # A debit that would take the account past its overdraft limit is declined for funds. The
    # balance at this instant is known because the fold is in time order, which is the whole
    # reason the pass is organised month by month.
    if (
        posted
        and direction == "debit"
        and accounts.balance[index] + signed < -accounts.overdraft_limit[index]
    ):
        posted = False
        status_code = "declined"
        auth_outcome = "declined_funds" if card_id is not None else None
        stats.declined_for_funds += 1

    if posted:
        accounts.balance[index] = cents(accounts.balance[index] + signed)
    accounts.updated_at[index] = max(accounts.updated_at[index], booked_at)

    kw["txn_rows"].write(
        (
            transaction_id,
            f"TXN{transaction_id:013d}",
            account_id,
            card_id,
            merchant_id,
            agent_id,
            type_code,
            channel_code,
            status_code,
            auth_outcome,
            booked_at,
            value_date,
            signed,
            currency,
            is_card_present,
            None,
            None,
            booked_at,
            booked_at,
            False,
        )
    )

    if card_id is not None:
        stats.card_transactions += 1
        if is_card_present is False:
            stats.card_not_present += 1
    if fraudulent:
        stats.fraudulent += 1

    if amount_model.is_benford_population(type_code, is_recurring):
        value = first_digit(magnitude)
        if value is not None:
            stats.benford_digits[value - 1] += 1

    if posted:
        if loan_principal is not None:
            # A loan movement. Principal and interest reach different ledger accounts,
            # or Q5's net interest income proxy has no interest income to read.
            if direction == "debit":
                interest = magnitude - loan_principal
                legs = [
                    (gl["customer_deposits"], magnitude, account_id),
                    (gl["loans_advances"], -loan_principal, None),
                ]
                if interest != 0:
                    legs.append((gl["interest_income"], -interest, None))
            else:
                legs = [
                    (gl["loans_advances"], magnitude, None),
                    (gl["customer_deposits"], -magnitude, account_id),
                ]
            ledger.post(
                booked_at.date(),
                type_code,
                "transaction",
                transaction_id,
                currency,
                legs,
                booked_at,
            )
        else:
            contra = _contra_account(gl, type_code, direction)
            if direction == "debit":
                ledger.post_customer_debit(
                    booked_at.date(),
                    type_code,
                    "transaction",
                    transaction_id,
                    currency,
                    magnitude,
                    account_id,
                    contra,
                    booked_at,
                )
            else:
                ledger.post_customer_credit(
                    booked_at.date(),
                    type_code,
                    "transaction",
                    transaction_id,
                    currency,
                    magnitude,
                    account_id,
                    contra,
                    booked_at,
                )

    if card_id is not None:
        kw["alerts"].consider(
            kw["streams"].stream("alerts", transaction_id),
            kw["fraud_params"],
            transaction_id,
            accounts.customer_id[index],
            booked_at,
            fraudulent,
            context,
            kw["anchor_end"],
        )

    # Spec 003 invariant 13, as replaced: a share of customer-initiated digital-channel
    # transactions is preceded by a login within 24 hours. Card-present and recurring
    # transactions are excluded, because neither implies a login.
    # The api channel is machine to machine — standing orders, direct debits, salary credits
    # and loan movements — and implies no customer login, so it is not part of the population
    # the coverage share is measured over. The invariant uses the same definition.
    digital = channel_code in ("mobile_app", "web", "ecommerce")
    if (
        digital
        and kw["customer_initiated"].get(type_code, False)
        and not is_recurring
        and is_card_present is not True
    ):
        stats.digital_customer_initiated += 1
        session_rng = kw["streams"].stream("linked_sessions", transaction_id)
        if bernoulli(session_rng, float(kw["digital_params"]["login_precedes_transaction_share"])):
            stats.digital_with_preceding_login += 1
            started = booked_at - dt.timedelta(minutes=session_rng.randrange(2, 23 * 60))
            customer_id = accounts.customer_id[index]
            kw["sessions"].write(
                session_rng,
                kw["digital_params"],
                customer_id,
                started,
                session_rng.randrange(max(1, kw["customers"].device_count[customer_id - 1])),
                kw["customers"].country_code[customer_id - 1],
                kw["anchor_end"],
                linked=True,
            )


def _contra_account(gl: dict[str, str], type_code: str, direction: str) -> str:
    """Which ledger account faces customer deposits for this kind of movement."""
    if type_code in ("account_fee", "maintenance_fee", "card_issue_fee"):
        return gl["fee_income"]
    if type_code == "fx_markup":
        return gl["fx_income"]
    if type_code in ("interest_posting", "interest_accrual"):
        return gl["interest_expense"]
    if type_code == "chargeback_adjustment":
        return gl["suspense"]
    if type_code == "write_off":
        return gl["suspense"]
    if type_code == "internal_reclass":
        return gl["suspense"]
    return gl["cash"]


def _emit_payment(**kw) -> None:
    accounts: AccountBook = kw["accounts"]
    ledger: LedgerWriter = kw["ledger"]
    gl = kw["gl"]
    account_id: int = kw["account_id"]
    index = account_id - 1
    payment_id: int = kw["payment_id"]
    initiated_at: dt.datetime = kw["booked_at"]

    (
        type_code,
        scheme,
        status,
        amount,
        counterparty_country,
        name,
        currency,
        has_remittance,
        settle_lag,
        iban_check,
    ) = kw["payload"]

    direction = kw["payment_direction"][type_code]
    posted = kw["payment_posted"].get(status, False)

    # A payment whose booking or settlement would fall after the anchor has not reached that
    # stage yet: it is still in flight at the end of the history. Clamping the later timestamp
    # back to the anchor instead would put settlement before booking, which the schema refuses
    # and which would be a lie about what the bank knew when.
    anchor_end: dt.datetime = kw["anchor_end"]
    booked_at = initiated_at + dt.timedelta(minutes=17) if posted else None
    if booked_at is not None and booked_at > anchor_end:
        posted = False
        status = "pending"
        booked_at = None

    settled_at = None
    if posted and status in ("settled", "returned"):
        settled_at = (booked_at or initiated_at) + dt.timedelta(days=settle_lag, minutes=5)
        if settled_at > anchor_end:
            settled_at = None
            status = "booked"

    signed = -amount if direction == "debit" else amount
    if (
        posted
        and direction == "debit"
        and accounts.balance[index] + signed < -accounts.overdraft_limit[index]
    ):
        posted = False
        status = "rejected"
        booked_at = None
        settled_at = None

    if posted:
        accounts.balance[index] = cents(accounts.balance[index] + signed)
    accounts.updated_at[index] = max(accounts.updated_at[index], booked_at or initiated_at)

    counterparty_iban = (
        f"{counterparty_country}{iban_check:02d}{payment_id:016d}" if counterparty_country else None
    )

    kw["pay_rows"].write(
        (
            payment_id,
            f"PAY{payment_id:013d}",
            account_id,
            type_code,
            scheme,
            status,
            initiated_at,
            booked_at,
            settled_at,
            amount,
            currency,
            counterparty_iban,
            name,
            counterparty_country,
            f"INV-{payment_id:09d}" if has_remittance else None,
            initiated_at,
            settled_at or booked_at or initiated_at,
            False,
        )
    )

    if posted:
        when = booked_at or initiated_at
        if direction == "debit":
            ledger.post_customer_debit(
                when.date(),
                type_code,
                "payment",
                payment_id,
                currency,
                amount,
                account_id,
                gl["cash"],
                when,
            )
        else:
            ledger.post_customer_credit(
                when.date(),
                type_code,
                "payment",
                payment_id,
                currency,
                amount,
                account_id,
                gl["cash"],
                when,
            )


def _month_attrition(**kw) -> None:
    """Dormancy, reactivation and closure for the month, decided before any movement."""
    streams: SubStreams = kw["streams"]
    accounts: AccountBook = kw["accounts"]
    attrition_params = kw["attrition_params"]
    month_index: int = kw["month_index"]
    window_first: dt.date = kw["window_first"]
    window_last: dt.date = kw["window_last"]
    span = (window_last - window_first).days + 1
    if span <= 0:
        return

    for account_id in kw["active"]:
        index = account_id - 1
        if accounts.closed_date[index] is not None:
            continue
        rng = streams.stream("lifecycle", account_id, month_index)
        status = accounts.status_code[index]

        if status == "active" and lifecycle.becomes_dormant(rng, attrition_params):
            accounts.status_code[index] = "dormant"
        elif status == "dormant" and lifecycle.reactivates(rng, attrition_params):
            accounts.status_code[index] = "active"
        elif status == "active" and bernoulli(rng, float(attrition_params["frozen_share"])):
            accounts.status_code[index] = "frozen"

        if lifecycle.closes_this_month(rng, attrition_params, month_index):
            close_day = window_first + dt.timedelta(days=rng.randrange(span))
            # An account with a loan still collecting against it does not close. A bank does not
            # let you close the account its direct debit collects a loan from, and the lending
            # pass runs before closures are decided, so without this a repayment lands on a
            # closed account — 1,155 of them at the dev profile, which invariant 1 refused.
            hold = kw["loan_hold_until"].get(account_id)
            if hold is not None and close_day <= hold:
                continue
            # An account does not close inside its first month either. Cards are issued up to ten
            # days after opening, so a closure days after opening leaves a card issued onto a
            # closed account. A cooling-off floor is also what real accounts have, so this is the
            # realistic fix rather than a clamp on the card date.
            if close_day >= accounts.opened_date[index] + dt.timedelta(days=30):
                accounts.closed_date[index] = close_day
                accounts.status_code[index] = "closed"
                accounts.updated_at[index] = max(
                    accounts.updated_at[index], at_time(close_day, 16, 0, 0)
                )


def _month_sessions(**kw) -> None:
    """Background login sessions for every customer with an open account this month."""
    streams: SubStreams = kw["streams"]
    accounts: AccountBook = kw["accounts"]
    customers: CustomerBook = kw["customers"]
    sessions: SessionWriter = kw["sessions"]
    digital_params = kw["digital_params"]
    month_index: int = kw["month_index"]
    window_first: dt.date = kw["window_first"]
    window_last: dt.date = kw["window_last"]
    span = (window_last - window_first).days + 1
    if span <= 0:
        return
    # Scaled to the days the window covers, for the reason `_account_month` states: a whole
    # month of sessions spread over the eighteen days before the anchor made them 2.4 times
    # denser there than in August.
    window_fraction = span / days_in_month(window_first.year, window_first.month)

    seen_customers: set[int] = set()

    for account_id in kw["active"]:
        index = account_id - 1
        customer_id = accounts.customer_id[index]
        if customer_id in seen_customers:
            continue
        seen_customers.add(customer_id)

        session_rng = streams.stream("sessions", customer_id, month_index)
        count = overdispersed_poisson(
            session_rng,
            float(digital_params["background_sessions_per_customer_month"]) * window_fraction,
            float(digital_params["sessions_dispersion"]),
        )
        device_total = max(1, customers.device_count[customer_id - 1])
        new_device = bernoulli(
            streams.stream("devices", customer_id, recurrence.month_key(window_first)),
            float(digital_params["new_device_monthly_hazard"]),
        )
        for _ in range(count):
            day = window_first + dt.timedelta(days=session_rng.randrange(span))
            started = draw_timestamp(session_rng, day, kw["params"]["transactions"]["hour_weights"])
            device_index = (
                device_total + month_index
                if new_device and bernoulli(session_rng, 0.25)
                else session_rng.randrange(device_total)
            )
            sessions.write(
                session_rng,
                digital_params,
                customer_id,
                started,
                device_index,
                customers.country_code[customer_id - 1],
                kw["anchor_end"],
            )
