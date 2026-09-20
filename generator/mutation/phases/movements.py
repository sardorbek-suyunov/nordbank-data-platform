"""The day's movements: what the customers did, what cleared late, and what finally posted.

This phase is the reason the milestone needs a balance fold at all, and it is organised around
three facts that do not fit together comfortably.

**A day is folded in time order, exactly as the historical load folds a month.** The overdraft
decline is only decidable if the balance at the instant of the debit is known, so the day's
events are drawn per account, merged, sorted, and then emitted. Nothing is written until the
order is settled.

**The clearing cycle runs before the day's activity, not after it.** Late arrivals, deferred
postings, reversals and loan collections are applied in the small hours, which is when a real
core system clears them, and the day's decline decisions therefore see the result. That makes
the interaction visible rather than theoretical: an account an offline transaction pushed past
its overdraft limit overnight declines the debits it attempts that afternoon.

**Nothing in the clearing cycle is declined for insufficient funds.** A row inserted on day D
with a business timestamp of D−4 could not have been declined, because no online authorisation
existed to decline it; an authorisation already given is not re-declined when it clears. So
those movements post unconditionally and the balance goes where it goes — past the overdraft
limit if that is where it goes. That is the only mechanism in this source that produces an
unauthorised overdraft, and it is deliberate: the loaded `ci` book contains 72 overdrawn
accounts and not one past its limit, so every instance downstream is attributable to this.

Invariant 4 is unaffected by any of it, because invariant 4 asserts that the stored balance is
the signed sum of posted movements and knows nothing about the limit.

**The posting date is the tick's date, never the business date.** A late arrival's ledger entry
posts to the current open period. Posting it to D−4 would restate totals for a day already
reported, which is the same reproducibility argument that makes FX rates non-restating in
`docs/architecture.md`. Business date, posting date and value date therefore diverge on a late
arrival, and all three are meaningful: `booked_at` is when the customer transacted,
`posting_date` is when the bank recognised it, `value_date` is when the money moved.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from ...entities.ledger import LedgerWriter
from ...entities.risk import AlertWriter, SessionWriter
from ...realism import amounts as amount_model
from ...realism import fraud as fraud_model
from ...realism import presentment, recurrence
from ...realism.calendar import (
    at_time,
    clamp_day_of_month,
    day_weight,
    days_in_month,
    draw_timestamp,
    growth_multiplier,
    is_weekend,
    month_index,
    progress,
)
from ...realism.distributions import (
    bernoulli,
    cents,
    overdispersed_poisson,
    poisson,
    weighted_choice,
    weighted_pick,
)
from ..snapshot import Account, Snapshot
from ..tick import TickContext
from ..writer import apply_updates

# Counterparty names that a sanctions screen at M4 has something to match. The fixture is
# unmistakably synthetic; no real sanctioned individual's name appears in this repository.
_SCREENING = Path(__file__).resolve().parents[2] / "fixtures" / "screening_positives.csv"

# When the overnight clearing cycle runs. Everything it touches is stamped here, which gives
# the day one batch of rows sharing an `updated_at` — the tie M4's `>=` watermark overlap is
# written to survive, and what a nightly batch job actually produces.
CLEARING_HOUR = 4

# When the balance fold is written. One statement for the whole day (spec 004, amended), so
# every account the tick moved shares this instant. `current_balance_amount` is a derived
# aggregate rather than a business event, and giving each account its own instant would cost
# one statement per account: the trigger reads one clock value per statement, by construction.
FOLD_HOUR = 23
FOLD_MINUTE = 45

# Event discriminators, as in the historical pass.
TRANSACTION = 0
PAYMENT = 1

# Statuses a tick advances out of. Named here rather than read from `is_posted`, because the
# set of in-flight statuses is smaller than the set of non-posted ones: `declined`, `reversed`,
# `rejected` and `cancelled` are terminal and a tick never revisits them.
IN_FLIGHT_TRANSACTION = ("pending", "authorised")
IN_FLIGHT_PAYMENT = ("pending", "initiated")

# The credit type a reversal of a debit takes, and the debit type a reversal of a credit takes.
# Both are bank-initiated, because a reversal is the bank's act rather than the customer's.
REVERSAL_OF_DEBIT = "chargeback_adjustment"
REVERSAL_OF_CREDIT = "internal_reclass"


@dataclass
class DayModel:
    """Everything resolved once per tick, so the per-account loop reads and never computes."""

    params: dict
    ref: Any
    gl: dict[str, str]
    direction_of_type: dict[str, str]
    posted_status: dict[str, bool]
    customer_initiated: dict[str, bool]
    payment_direction: dict[str, str]
    payment_posted: dict[str, bool]
    merchant_by_id: dict[int, Any]
    merchant_count: int
    baseline_present: float
    fraud_base: float
    seasonal: float
    growth: float
    month_span: int
    month_index: int
    presentment_progress: float
    day_end: dt.datetime
    month_key: str

    @property
    def transactions(self) -> dict:
        return self.params["transactions"]

    @property
    def amounts(self) -> dict:
        return self.params["amounts"]

    @property
    def payments(self) -> dict:
        return self.params["payments"]

    @property
    def fraud(self) -> dict:
        return self.params["fraud"]

    @property
    def digital(self) -> dict:
        return self.params["digital"]

    @property
    def mutation(self) -> dict:
        return self.params["mutation"]


@dataclass
class Fold:
    """The running balance per account, and what the tick has to write back."""

    opening: dict[int, Decimal]
    balance: dict[int, Decimal]
    touched: set[int] = field(default_factory=set)
    declined_for_funds: int = 0
    unauthorised_overdrafts: set[int] = field(default_factory=set)

    def apply(self, account: Account, signed: Decimal) -> None:
        self.balance[account.account_id] = cents(self.balance[account.account_id] + signed)
        self.touched.add(account.account_id)
        if self.balance[account.account_id] < -account.overdraft_limit:
            self.unauthorised_overdrafts.add(account.account_id)

    def would_breach(self, account: Account, signed: Decimal) -> bool:
        return self.balance[account.account_id] + signed < -account.overdraft_limit

    def deltas(self) -> tuple[list[int], list[Decimal]]:
        ids, amounts = [], []
        for account_id in sorted(self.touched):
            delta = self.balance[account_id] - self.opening[account_id]
            if delta:
                ids.append(account_id)
                amounts.append(delta)
        return ids, amounts


def run(context: TickContext) -> None:
    """Advance the source by one day of movements."""
    model = _model(context)
    snapshot = context.snapshot
    fold = Fold(
        opening={account_id: a.balance for account_id, a in snapshot.accounts.items()},
        balance={account_id: a.balance for account_id, a in snapshot.accounts.items()},
    )

    ledger = LedgerWriter(
        context.writer,
        model.gl,
        first_batch_id=snapshot.next_id["gl_transactions"],
        first_entry_id=snapshot.next_id["gl_entries"],
    )
    alerts = AlertWriter(context.writer, first_id=snapshot.next_id["fraud_alerts"])
    sessions = SessionWriter(
        context.writer,
        context.seed,
        sorted(model.params["customers"]["country_mix"]),
        first_id=snapshot.next_id["login_sessions"],
    )

    # The overnight clearing cycle, in the order a core system runs it: what was already in
    # flight settles, what was taken offline presents, corrections post, loans collect.
    _post_deferred_transactions(context, model, fold, ledger)
    _advance_payments(context, model, fold, ledger)
    _clear_late_arrivals(context, model, fold, ledger, alerts)
    _reverse_recent_postings(context, model, fold, ledger)
    _collect_loan_cash(context, model, fold, ledger)

    # The day itself.
    events: list[tuple] = []
    for account_id in snapshot.open_account_ids:
        _account_day(context, model, snapshot.accounts[account_id], events)
    events.sort(key=lambda item: (item[0], item[1], item[2]))
    _emit_day(context, model, fold, ledger, alerts, sessions, events)

    _background_sessions(context, model, sessions)
    _write_fold(context, fold)

    context.touched_accounts.update(fold.touched)
    context.report.declined_for_funds += fold.declined_for_funds
    context.report.unauthorised_overdrafts = sorted(fold.unauthorised_overdrafts)


# --------------------------------------------------------------------------------------- setup


def _model(context: TickContext) -> DayModel:
    params = context.profile.params
    ref = context.snapshot.ref
    txn = params["transactions"]
    day = context.simulated_date

    merchant_by_id = {merchant.merchant_id: merchant for merchant in context.snapshot.merchants}
    baseline_present = presentment.baseline_present_share(txn)

    # The card-not-present trend is expressed against how far through the history a day sits,
    # and a simulated day past the anchor sits at the end of it. The share therefore holds at
    # its end value rather than continuing to rise, which is the honest reading of a parameter
    # whose two endpoints describe the history and say nothing about what follows it.
    history_start = context.snapshot.history_start
    return DayModel(
        params=params,
        ref=ref,
        gl=params["ledger"]["accounts"],
        direction_of_type={
            code: str(row["direction"]) for code, row in ref.tables["transaction_types"].items()
        },
        posted_status={
            code: bool(row["is_posted"]) for code, row in ref.tables["transaction_statuses"].items()
        },
        customer_initiated={
            code: bool(row["is_customer_initiated"])
            for code, row in ref.tables["transaction_types"].items()
        },
        payment_direction={
            code: str(row["direction"]) for code, row in ref.tables["payment_types"].items()
        },
        payment_posted={
            code: bool(row["is_posted"]) for code, row in ref.tables["payment_statuses"].items()
        },
        merchant_by_id=merchant_by_id,
        merchant_count=max(merchant_by_id, default=0),
        baseline_present=baseline_present,
        fraud_base=fraud_model.calibrate(
            params["fraud"], txn["hour_weights"], 1.0 - baseline_present
        ),
        seasonal=day_weight(txn["month_weights"], 1.0, day),
        growth=growth_multiplier(
            float(params["acquisition"]["monthly_growth_rate"]),
            month_index(history_start, day),
        ),
        month_span=days_in_month(day.year, day.month),
        month_index=month_index(history_start, day),
        presentment_progress=progress(history_start, context.snapshot.anchor_date, day),
        day_end=at_time(day, 23, 59, 59),
        month_key=recurrence.month_key(day),
    )


# ----------------------------------------------------------------------- the clearing cycle


def _clearing_instant(context: TickContext) -> dt.datetime:
    return context.at(CLEARING_HOUR)


def _post_deferred_transactions(
    context: TickContext, model: DayModel, fold: Fold, ledger: LedgerWriter
) -> None:
    """Authorisations taken on an earlier day that clear today.

    An update, not a late arrival: nothing is inserted, `booked_at` does not move, and the row
    is counted under `updated`. What moves is `updated_at`, which is exactly the divergence
    between business time and audit time that silver has to resolve.

    Not declined for funds. The authorisation was given; a clearing run does not take it back.
    """
    horizon = int(model.mutation["deferred_posting"]["max_age_days"])
    hazard = float(model.mutation["deferred_posting"]["transaction_posts"])
    day_start, _ = context.window

    context.cursor.execute(
        """
        select t.transaction_id, t.account_id, t.transaction_amount, t.transaction_currency_code,
               t.transaction_type_code
          from core.transactions t
          join core.accounts a on a.account_id = t.account_id
          join ref.account_statuses s on s.code = a.account_status_code
         where t.transaction_status_code = any(%s)
           and s.is_open and not a.is_deleted and not t.is_deleted
           and t.booked_at >= %s and t.booked_at < %s
         order by t.transaction_id
        """,
        (list(IN_FLIGHT_TRANSACTION), day_start - dt.timedelta(days=horizon), day_start),
    )
    candidates = context.cursor.fetchall()
    if not candidates:
        return

    rng = context.stream("clearing.transactions")
    stamp = _clearing_instant(context)
    chosen: list[int] = []
    for transaction_id, account_id, amount, currency, type_code in candidates:
        if not bernoulli(rng, hazard):
            continue
        account = context.snapshot.accounts.get(account_id)
        if account is None:
            continue
        chosen.append(transaction_id)
        fold.apply(account, amount)
        _post_transaction_ledger(
            ledger,
            model,
            context.simulated_date,
            type_code,
            transaction_id,
            currency,
            amount,
            account_id,
            stamp,
        )

    if not chosen:
        return

    context.set_clock(stamp)
    changed = apply_updates(
        context.cursor,
        "update core.transactions set transaction_status_code = 'posted' "
        "where transaction_id = any(%s::bigint[])",
        chosen,
    )
    context.report.record("transactions", "updated", changed)


def _advance_payments(
    context: TickContext, model: DayModel, fold: Fold, ledger: LedgerWriter
) -> None:
    """Payments that book today, and payments that settle today.

    Two transitions with different consequences. `initiated`/`pending` to `booked` crosses the
    `is_posted` boundary, so it moves the balance and owes the ledger a batch. `booked` to
    `settled` does not cross it: both statuses are posted, so the money has already moved and
    the row's only change is that the scheme confirmed it. The second is the larger population
    and costs the balance nothing, which makes it the cleanest case silver has of a row whose
    business time is days behind its `updated_at`.
    """
    deferred = model.mutation["deferred_posting"]
    horizon = int(deferred["max_age_days"])
    day_start, _ = context.window
    stamp = _clearing_instant(context)

    context.cursor.execute(
        """
        select p.payment_id, p.account_id, p.payment_amount, p.payment_currency_code,
               p.payment_type_code, p.payment_status_code
          from core.payments p
          join core.accounts a on a.account_id = p.account_id
          join ref.account_statuses s on s.code = a.account_status_code
         where s.is_open and not a.is_deleted and not p.is_deleted
           and ((p.payment_status_code = any(%s) and p.initiated_at >= %s and p.initiated_at < %s)
                or (p.payment_status_code = 'booked' and p.booked_at < %s))
         order by p.payment_id
        """,
        (
            list(IN_FLIGHT_PAYMENT),
            day_start - dt.timedelta(days=horizon),
            day_start,
            day_start,
        ),
    )
    candidates = context.cursor.fetchall()
    if not candidates:
        return

    rng = context.stream("clearing.payments")
    booking: list[int] = []
    settling: list[int] = []
    for payment_id, account_id, amount, currency, type_code, status in candidates:
        account = context.snapshot.accounts.get(account_id)
        if account is None:
            continue
        if status == "booked":
            if bernoulli(rng, float(deferred["payment_settles"])):
                settling.append(payment_id)
            continue
        if not bernoulli(rng, float(deferred["payment_books"])):
            continue
        booking.append(payment_id)
        direction = model.payment_direction[type_code]
        signed = -amount if direction == "debit" else amount
        fold.apply(account, signed)
        if direction == "debit":
            ledger.post_customer_debit(
                context.simulated_date,
                type_code,
                "payment",
                payment_id,
                currency,
                amount,
                account_id,
                model.gl["cash"],
                stamp,
            )
        else:
            ledger.post_customer_credit(
                context.simulated_date,
                type_code,
                "payment",
                payment_id,
                currency,
                amount,
                account_id,
                model.gl["cash"],
                stamp,
            )

    if not (booking or settling):
        return

    context.set_clock(stamp)
    changed = 0
    if booking:
        context.cursor.execute(
            "update core.payments set payment_status_code = 'booked', booked_at = %s "
            "where payment_id = any(%s::bigint[])",
            (stamp, booking),
        )
        changed += context.cursor.rowcount
    if settling:
        context.cursor.execute(
            "update core.payments set payment_status_code = 'settled', settled_at = %s "
            "where payment_id = any(%s::bigint[])",
            (stamp, settling),
        )
        changed += context.cursor.rowcount
    context.report.record("payments", "updated", changed)


def _clear_late_arrivals(
    context: TickContext,
    model: DayModel,
    fold: Fold,
    ledger: LedgerWriter,
    alerts: AlertWriter,
) -> None:
    """Offline card transactions presented two to five days after the customer made them.

    The business timestamp is drawn from the intersection of the lag distribution's support
    with the lifetime of every entity the transaction references — the account's open window
    and the card's validity. That is how invariant 1 and invariant 2 are held: an out-of-window
    timestamp is never produced, so none has to be filtered out afterwards. An account or card
    whose intersection is empty is not a candidate and is never drawn from.

    They are card present. An offline authorisation happens at a terminal that could not reach
    the network — a transit gate, an aircraft, an unattended pump — so the card was in the
    customer's hand. That also keeps them out of invariant 13's population, which is right:
    there is no login to precede a tap at a barrier.

    The alert, if one fires, fires at clearing rather than at the transaction. The bank cannot
    detect what it has not been told about yet.
    """
    late = model.mutation["late_arrival"]
    share = float(late["offline_card_share"])
    lag_min, lag_max = int(late["lag_days_min"]), int(late["lag_days_max"])
    weights = list(late["lag_day_weights"])
    lags = list(range(lag_min, lag_max + 1))
    if len(weights) != len(lags):
        raise ValueError(
            f"mutation.late_arrival: {len(weights)} lag weight(s) for {len(lags)} lag day(s) "
            f"between {lag_min} and {lag_max}"
        )

    day = context.simulated_date
    stamp = _clearing_instant(context)
    snapshot = context.snapshot
    rng = context.stream("late_arrivals")

    # The expected count is a share of the day's card purchases, which is the same per-account
    # rate the day itself is drawn from, so the two move together at every profile.
    expected = share * _expected_card_purchases(model, snapshot)
    for _ in range(poisson(rng, expected)):
        account = _draw_account(rng, snapshot)
        if account is None:
            return
        card = _draw_card(rng, snapshot, account, day)
        if card is None:
            continue

        lag = _draw_lag(rng, lags, weights, account, card, day)
        if lag is None:
            continue
        business_day = day - dt.timedelta(days=lag)
        booked_at = draw_timestamp(rng, business_day, model.transactions["hour_weights"])

        merchant_id = _random_merchant(rng, model)
        if merchant_id is None or merchant_id not in model.merchant_by_id:
            continue
        band = model.merchant_by_id[merchant_id].band_code
        band_params = model.amounts["by_band"].get(band, model.amounts["by_band"]["standard"])
        magnitude = amount_model.purchase_amount(rng, band_params)

        transaction_id = snapshot.take_id("transactions")
        signed = -magnitude
        fold.apply(account, signed)

        context.writer.add(
            "transactions",
            (
                transaction_id,
                f"TXN{transaction_id:013d}",
                account.account_id,
                card.card_id,
                merchant_id,
                None,
                "card_purchase",
                "pos",
                "posted",
                "approved",
                booked_at,
                day,
                signed,
                account.currency_code,
                True,
                None,
                None,
                stamp,
                stamp,
                False,
            ),
        )
        context.report.record("transactions", "late_arriving")

        _post_transaction_ledger(
            ledger,
            model,
            day,
            "card_purchase",
            transaction_id,
            account.currency_code,
            signed,
            account.account_id,
            stamp,
        )

        fraud_context = fraud_model.FraudContext(
            card_not_present=False,
            unfamiliar_merchant=True,
            night_hour=booked_at.hour in model.fraud["night_hours"],
            velocity_burst=False,
            new_device=False,
        )
        alerts.consider(
            context.stream("alerts", transaction_id),
            model.fraud,
            transaction_id,
            account.customer_id,
            stamp,
            fraud_model.is_fraudulent(rng, model.fraud, fraud_context, model.fraud_base),
            fraud_context,
            model.day_end,
        )


def _reverse_recent_postings(
    context: TickContext, model: DayModel, fold: Fold, ledger: LedgerWriter
) -> None:
    """A posting is reversed by a movement of its own, never by deleting it.

    Spec 004's amendment: a soft-deleted posted transaction is a fork with no good branch,
    because invariant 4 filters on `is_posted` and not on `is_deleted`. Reversing keeps both
    rows posted, both in the ledger and both in the balance, and it is what a real ledger does.

    The original row is not touched. A reversal is found by following
    `reversal_of_transaction_id`, which nothing in the source populated before this milestone.
    """
    reversal = model.mutation["reversal"]
    horizon = int(reversal["max_age_days"])
    share = float(reversal["share_of_recent_postings"])
    day_start, _ = context.window
    stamp = _clearing_instant(context)

    context.cursor.execute(
        """
        select t.transaction_id, t.account_id, t.transaction_amount, t.transaction_currency_code
          from core.transactions t
          join ref.transaction_statuses ts on ts.code = t.transaction_status_code
          join core.accounts a on a.account_id = t.account_id
          join ref.account_statuses s on s.code = a.account_status_code
         where ts.is_posted and s.is_open and not a.is_deleted and not t.is_deleted
           and t.booked_at >= %s and t.booked_at < %s
           and not exists (select 1 from core.transactions r
                            where r.reversal_of_transaction_id = t.transaction_id)
         order by t.transaction_id
        """,
        (day_start - dt.timedelta(days=horizon), day_start),
    )
    candidates = context.cursor.fetchall()
    if not candidates:
        return

    rng = context.stream("reversals")
    for original_id, account_id, amount, currency in candidates:
        if not bernoulli(rng, share):
            continue
        account = context.snapshot.accounts.get(account_id)
        if account is None:
            continue

        signed = -amount
        type_code = REVERSAL_OF_DEBIT if amount < 0 else REVERSAL_OF_CREDIT
        transaction_id = context.snapshot.take_id("transactions")
        fold.apply(account, signed)

        context.writer.add(
            "transactions",
            (
                transaction_id,
                f"TXN{transaction_id:013d}",
                account_id,
                None,
                None,
                None,
                type_code,
                model.transactions["bank_initiated_channel"],
                "posted",
                None,
                stamp,
                context.simulated_date,
                signed,
                currency,
                None,
                original_id,
                None,
                stamp,
                stamp,
                False,
            ),
        )
        _post_transaction_ledger(
            ledger,
            model,
            context.simulated_date,
            type_code,
            transaction_id,
            currency,
            signed,
            account_id,
            stamp,
        )


def _collect_loan_cash(
    context: TickContext, model: DayModel, fold: Fold, ledger: LedgerWriter
) -> None:
    """Loan disbursements and installment collections the lifecycle phase decided.

    A loan movement is a transaction on the customer's account, not a kind of event of its own,
    because invariant 4 reconciles the balance against transactions and payments alone. The
    ledger separates principal from interest, or Q5's net interest income proxy has no interest
    income to read.
    """
    if not context.cash_events:
        return
    stamp = _clearing_instant(context)
    for event in sorted(context.cash_events, key=lambda item: (item.when, item.account_id)):
        account = context.snapshot.accounts.get(event.account_id)
        if account is None:
            continue
        incoming = event.amount > 0
        type_code = "transfer_in" if incoming else "direct_debit"
        transaction_id = context.snapshot.take_id("transactions")
        fold.apply(account, event.amount)

        context.writer.add(
            "transactions",
            (
                transaction_id,
                f"TXN{transaction_id:013d}",
                event.account_id,
                None,
                None,
                None,
                type_code,
                "api",
                "posted",
                None,
                stamp,
                context.simulated_date,
                event.amount,
                event.currency_code,
                None,
                None,
                None,
                stamp,
                stamp,
                False,
            ),
        )
        _post_loan_ledger(
            ledger, model, context.simulated_date, type_code, transaction_id, event, stamp
        )


# ------------------------------------------------------------------------------- the day


def _account_day(
    context: TickContext, model: DayModel, account: Account, events: list[tuple]
) -> None:
    """One account's movements for one simulated day.

    The per-account rates are the historical load's monthly ones divided by the length of the
    simulated month and carrying the same seasonality, weekend factor and growth trend, so the
    day after the anchor looks like the day before it rather than like a separate simulation.
    """
    if account.status_code == "dormant":
        return

    day = context.simulated_date
    rng = context.stream("movements", account.account_id)
    txn = model.transactions
    amounts = model.amounts

    rate_by_class = txn.get("per_account_month_by_product_class")
    base_rate = (
        float(rate_by_class.get(account.product_class, txn["per_account_month"]))
        if rate_by_class
        else float(txn["per_account_month"])
    )
    expected_month = base_rate * model.seasonal * model.growth
    count = overdispersed_poisson(
        rng, expected_month / model.month_span, float(txn["per_account_month_dispersion"])
    )

    cards = _usable_cards(context.snapshot, account, day)
    has_card = bool(cards)

    familiar = recurrence.familiar_merchants(
        context.streams.stream("recurrence.merchants", account.account_id, model.month_key),
        model.merchant_count,
    )
    mandates = recurrence.mandates(
        context.streams.stream("recurrence.mandates", account.account_id, model.month_key),
        amounts,
        familiar,
        has_card=has_card,
    )
    new_device = bernoulli(
        context.streams.stream("devices", account.customer_id, model.month_key),
        float(model.digital["new_device_monthly_hazard"]),
    )
    weekend_factors = txn["weekend_band_factors"]
    seq = 0

    for _ in range(count):
        if is_weekend(day) and bernoulli(rng, 1.0 - float(txn["weekend_volume_factor"])):
            continue

        type_code = weighted_choice(rng, txn["type_mix"])
        channel_mix = txn["channel_mix_by_type"].get(type_code)
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
            card_id = cards[rng.randrange(len(cards))].card_id
            if type_code == "atm_withdrawal":
                band = "cash"
            else:
                unfamiliar = bernoulli(rng, float(model.fraud["unfamiliar_merchant_share"]))
                merchant_id = (
                    _random_merchant(rng, model)
                    if unfamiliar or not familiar
                    else familiar[rng.randrange(len(familiar))]
                )
                if merchant_id not in model.merchant_by_id:
                    continue
                band = model.merchant_by_id[merchant_id].band_code
                factor = float(weekend_factors.get(band, 1.0))
                if is_weekend(day) and bernoulli(rng, 1.0 - min(1.0, factor)):
                    continue
        elif type_code in ("agent_withdrawal", "agent_deposit"):
            if not context.snapshot.agent_ids:
                continue
            agent_id = context.snapshot.agent_ids[rng.randrange(len(context.snapshot.agent_ids))]
            band = "cash"

        booked_at = draw_timestamp(rng, day, txn["hour_weights"])
        seq += 1

        band_params = amounts["by_band"].get(band, amounts["by_band"]["standard"])
        if band == "cash":
            magnitude = amount_model.cash_amount(rng, amounts, band_params)
        elif type_code in ("transfer_out", "transfer_in", "direct_debit", "standing_order"):
            magnitude = amount_model.transfer_amount(rng, amounts)
        else:
            magnitude = amount_model.purchase_amount(rng, band_params)

        is_card_present = None
        if card_id is not None:
            is_card_present = presentment.decide(
                rng, txn, channel_code, model.presentment_progress, model.baseline_present
            )

        fraud_context = fraud_model.FraudContext(
            card_not_present=is_card_present is False,
            unfamiliar_merchant=merchant_id is not None and merchant_id not in familiar,
            night_hour=booked_at.hour in model.fraud["night_hours"],
            velocity_burst=False,
            new_device=new_device and channel_code in ("ecommerce", "mobile_app", "web"),
        )
        fraudulent = card_id is not None and fraud_model.is_fraudulent(
            rng, model.fraud, fraud_context, model.fraud_base
        )

        value_date = day
        if bernoulli(rng, float(txn["value_date_lag_share"])):
            value_date = day + dt.timedelta(
                days=rng.randrange(1, int(txn["value_date_lag_days_max"]) + 1)
            )

        events.append(
            (
                booked_at,
                account.account_id,
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
                    weighted_choice(rng, txn["status_mix"]),
                    weighted_choice(rng, txn["authorisation_mix"]) if card_id else None,
                    value_date,
                    account.currency_code,
                    False,
                    fraud_context,
                    fraudulent,
                ),
            )
        )

    _regular_credit(context, model, account, day, rng, events)
    _mandates_due(model, account, day, cards, mandates, rng, events)
    _bank_initiated(model, account, day, rng, events)
    _payment_instructions(model, account, day, rng, events)


def _regular_credit(
    context: TickContext, model: DayModel, account: Account, day: dt.date, rng, events: list
) -> None:
    """The salary or pension, on its day, at its amount.

    Derived from the account's own stream rather than read back from the book, so the same
    account receives the same income on both sides of the anchor. See
    `generator/realism/recurrence.py`.
    """
    salary_day, amount = recurrence.regular_credit(
        context.streams.stream("recurrence.credit", account.account_id),
        model.transactions,
        account.product_class,
    )
    if not salary_day:
        return
    if clamp_day_of_month(day.year, day.month, salary_day) != day:
        return
    events.append(
        (
            at_time(day, 5, rng.randrange(60), 0),
            account.account_id,
            900,
            TRANSACTION,
            (
                "transfer_in",
                "api",
                None,
                None,
                None,
                amount,
                None,
                "posted",
                None,
                day,
                account.currency_code,
                True,
                fraud_model.FraudContext(False, False, False, False, False),
                False,
            ),
        )
    )


def _mandates_due(
    model: DayModel,
    account: Account,
    day: dt.date,
    cards: list,
    mandates: list,
    rng,
    events: list,
) -> None:
    """Subscriptions that fall due today, at the same amount they always are."""
    if not cards:
        return
    for ordinal, (day_of_month, amount, merchant_id) in enumerate(mandates):
        if clamp_day_of_month(day.year, day.month, day_of_month) != day:
            continue
        if merchant_id not in model.merchant_by_id:
            continue
        events.append(
            (
                at_time(day, rng.randrange(2, 6), rng.randrange(60), 0),
                account.account_id,
                910 + ordinal,
                TRANSACTION,
                (
                    "card_purchase",
                    "ecommerce",
                    cards[0].card_id,
                    merchant_id,
                    None,
                    amount,
                    False,
                    "posted",
                    "approved",
                    day,
                    account.currency_code,
                    True,
                    fraud_model.FraudContext(False, False, False, False, False),
                    False,
                ),
            )
        )


def _bank_initiated(model: DayModel, account: Account, day: dt.date, rng, events: list) -> None:
    """Fees, interest and the rest, which post whether the customer transacted or not."""
    channel = model.transactions["bank_initiated_channel"]
    for ordinal, (type_code, monthly_rate) in enumerate(
        sorted(model.transactions["bank_initiated_per_account_month"].items())
    ):
        if not bernoulli(rng, min(1.0, float(monthly_rate) / model.month_span)):
            continue
        events.append(
            (
                at_time(day, 2, rng.randrange(60), 0),
                account.account_id,
                920 + ordinal,
                TRANSACTION,
                (
                    type_code,
                    channel,
                    None,
                    None,
                    None,
                    amount_model.fee_amount(rng, model.amounts),
                    None,
                    "posted",
                    None,
                    day,
                    account.currency_code,
                    False,
                    fraud_model.FraudContext(False, False, False, False, False),
                    False,
                ),
            )
        )


def _payment_instructions(
    model: DayModel, account: Account, day: dt.date, rng, events: list
) -> None:
    pay = model.payments
    count = overdispersed_poisson(
        rng,
        float(pay["per_account_month"]) * model.growth / model.month_span,
        float(pay["per_account_month_dispersion"]),
    )
    for ordinal in range(count):
        type_code = weighted_choice(rng, pay["type_mix"])
        cross_border = bernoulli(rng, float(pay["cross_border_share"]))
        events.append(
            (
                draw_timestamp(rng, day, model.transactions["hour_weights"]),
                account.account_id,
                930 + ordinal,
                PAYMENT,
                (
                    type_code,
                    pay["scheme_by_type"][type_code],
                    weighted_choice(rng, pay["status_mix"]),
                    amount_model.payment_amount(rng, pay),
                    weighted_choice(rng, pay["corridor_mix"])
                    if cross_border
                    else account.country_code,
                    _counterparty_name(rng, model),
                    account.currency_code,
                    bernoulli(rng, float(pay["remittance_reference_share"])),
                    rng.randrange(0, int(pay["settlement_lag_days_max"]) + 1),
                    rng.randrange(10, 99),
                ),
            )
        )


def _emit_day(
    context: TickContext,
    model: DayModel,
    fold: Fold,
    ledger: LedgerWriter,
    alerts: AlertWriter,
    sessions: SessionWriter,
    events: list[tuple],
) -> None:
    """Write the day's events in time order, folding the balance as it goes."""
    snapshot = context.snapshot
    for booked_at, account_id, _seq, kind, payload in events:
        account = snapshot.accounts[account_id]
        if kind == TRANSACTION:
            _emit_transaction(
                context, model, fold, ledger, alerts, sessions, account, booked_at, payload
            )
        else:
            _emit_payment(context, model, fold, ledger, account, booked_at, payload)


def _emit_transaction(
    context: TickContext,
    model: DayModel,
    fold: Fold,
    ledger: LedgerWriter,
    alerts: AlertWriter,
    sessions: SessionWriter,
    account: Account,
    booked_at: dt.datetime,
    payload: tuple,
) -> None:
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
        fraud_context,
        _fraudulent,
    ) = payload

    direction = model.direction_of_type[type_code]
    posted = model.posted_status.get(status_code, False)
    signed = -magnitude if direction == "debit" else magnitude

    # A debit that would take the account past its overdraft limit is declined for funds. The
    # balance at this instant is known because the day is folded in time order, and it already
    # carries whatever the overnight clearing cycle did to the account.
    if posted and direction == "debit" and fold.would_breach(account, signed):
        posted = False
        status_code = "declined"
        auth_outcome = "declined_funds" if card_id is not None else None
        fold.declined_for_funds += 1

    if posted:
        fold.apply(account, signed)

    transaction_id = context.snapshot.take_id("transactions")
    context.writer.add(
        "transactions",
        (
            transaction_id,
            f"TXN{transaction_id:013d}",
            account.account_id,
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
        ),
    )

    if posted:
        _post_transaction_ledger(
            ledger,
            model,
            context.simulated_date,
            type_code,
            transaction_id,
            currency,
            signed,
            account.account_id,
            booked_at,
        )

    if card_id is not None:
        alerts.consider(
            context.stream("alerts", transaction_id),
            model.fraud,
            transaction_id,
            account.customer_id,
            booked_at,
            _fraudulent,
            fraud_context,
            model.day_end,
        )

    digital = channel_code in ("mobile_app", "web", "ecommerce")
    if (
        digital
        and model.customer_initiated.get(type_code, False)
        and not is_recurring
        and is_card_present is not True
    ):
        session_rng = context.stream("linked_sessions", transaction_id)
        if bernoulli(session_rng, float(model.digital["login_precedes_transaction_share"])):
            # Clamped into the simulated day. A session started before midnight belongs to the
            # previous tick's window, and a row a tick writes outside its own window would make
            # the tick log irreconcilable against it. The 24-hour rule invariant 13 asserts is
            # satisfied either way, because the clamp only ever moves the login closer.
            day_start, _ = context.window
            started = max(
                day_start, booked_at - dt.timedelta(minutes=session_rng.randrange(2, 23 * 60))
            )
            sessions.write(
                session_rng,
                model.digital,
                account.customer_id,
                started,
                session_rng.randrange(max(1, _device_count(context, model, account.customer_id))),
                context.snapshot.customer_country.get(account.customer_id, account.country_code),
                model.day_end,
                linked=True,
            )


def _emit_payment(
    context: TickContext,
    model: DayModel,
    fold: Fold,
    ledger: LedgerWriter,
    account: Account,
    initiated_at: dt.datetime,
    payload: tuple,
) -> None:
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
    ) = payload

    direction = model.payment_direction[type_code]
    posted = model.payment_posted.get(status, False)

    # A payment whose booking or settlement would fall after the simulated day has not reached
    # that stage yet: it is still in flight when the day ends, and a later tick advances it.
    # Clamping the later timestamp back instead would put settlement before booking, which the
    # schema refuses and which would be a lie about what the bank knew when.
    day_end = model.day_end
    booked_at = initiated_at + dt.timedelta(minutes=17) if posted else None
    if booked_at is not None and booked_at > day_end:
        posted = False
        status = "pending"
        booked_at = None

    settled_at = None
    if posted and status in ("settled", "returned"):
        settled_at = (booked_at or initiated_at) + dt.timedelta(days=settle_lag, minutes=5)
        if settled_at > day_end:
            settled_at = None
            status = "booked"

    signed = -amount if direction == "debit" else amount
    if posted and direction == "debit" and fold.would_breach(account, signed):
        posted = False
        status = "rejected"
        booked_at = None
        settled_at = None
        fold.declined_for_funds += 1

    if posted:
        fold.apply(account, signed)

    payment_id = context.snapshot.take_id("payments")
    context.writer.add(
        "payments",
        (
            payment_id,
            f"PAY{payment_id:013d}",
            account.account_id,
            type_code,
            scheme,
            status,
            initiated_at,
            booked_at,
            settled_at,
            amount,
            currency,
            f"{counterparty_country}{iban_check:02d}{payment_id:016d}"
            if counterparty_country
            else None,
            name,
            counterparty_country,
            f"INV-{payment_id:09d}" if has_remittance else None,
            initiated_at,
            settled_at or booked_at or initiated_at,
            False,
        ),
    )

    if posted:
        when = booked_at or initiated_at
        post = ledger.post_customer_debit if direction == "debit" else ledger.post_customer_credit
        post(
            context.simulated_date,
            type_code,
            "payment",
            payment_id,
            currency,
            amount,
            account.account_id,
            model.gl["cash"],
            when,
        )


def _background_sessions(context: TickContext, model: DayModel, sessions: SessionWriter) -> None:
    """The customer opening the app without transacting."""
    day = context.simulated_date
    seen: set[int] = set()
    for account_id in context.snapshot.open_account_ids:
        customer_id = context.snapshot.accounts[account_id].customer_id
        if customer_id in seen:
            continue
        seen.add(customer_id)

        rng = context.stream("sessions", customer_id)
        count = overdispersed_poisson(
            rng,
            float(model.digital["background_sessions_per_customer_month"]) / model.month_span,
            float(model.digital["sessions_dispersion"]),
        )
        if not count:
            continue
        device_total = max(1, _device_count(context, model, customer_id))
        new_device = bernoulli(
            context.streams.stream("devices", customer_id, model.month_key),
            float(model.digital["new_device_monthly_hazard"]),
        )
        for _ in range(count):
            device_index = (
                device_total + model.month_index
                if new_device and bernoulli(rng, 0.25)
                else rng.randrange(device_total)
            )
            sessions.write(
                rng,
                model.digital,
                customer_id,
                draw_timestamp(rng, day, model.transactions["hour_weights"]),
                device_index,
                context.snapshot.customer_country.get(customer_id, "DE"),
                model.day_end,
            )


def _write_fold(context: TickContext, fold: Fold) -> None:
    """Write every balance the day moved, in one statement (spec 004, amended)."""
    account_ids, deltas = fold.deltas()
    if not account_ids:
        return
    context.set_clock(context.at(FOLD_HOUR, FOLD_MINUTE))
    changed = apply_updates(
        context.cursor,
        """
        update core.accounts a
           set current_balance_amount = a.current_balance_amount + d.delta
          from unnest(%s::bigint[], %s::numeric[]) as d(account_id, delta)
         where a.account_id = d.account_id
        """,
        account_ids,
        deltas,
    )
    context.report.record("accounts", "updated", changed)


# ------------------------------------------------------------------------------- helpers


def _post_transaction_ledger(
    ledger: LedgerWriter,
    model: DayModel,
    posting_date: dt.date,
    type_code: str,
    transaction_id: int,
    currency: str,
    signed: Decimal,
    account_id: int,
    stamp: dt.datetime,
) -> None:
    """One balanced batch for one posted transaction, posted to the current open period."""
    magnitude = abs(signed)
    contra = _contra_account(model.gl, type_code)
    if signed < 0:
        ledger.post_customer_debit(
            posting_date,
            type_code,
            "transaction",
            transaction_id,
            currency,
            magnitude,
            account_id,
            contra,
            stamp,
        )
    else:
        ledger.post_customer_credit(
            posting_date,
            type_code,
            "transaction",
            transaction_id,
            currency,
            magnitude,
            account_id,
            contra,
            stamp,
        )


def _post_loan_ledger(
    ledger: LedgerWriter,
    model: DayModel,
    posting_date: dt.date,
    type_code: str,
    transaction_id: int,
    event: Any,
    stamp: dt.datetime,
) -> None:
    gl = model.gl
    magnitude = abs(event.amount)
    if event.amount < 0:
        principal = event.principal_component or magnitude
        interest = magnitude - principal
        legs = [
            (gl["customer_deposits"], magnitude, event.account_id),
            (gl["loans_advances"], -principal, None),
        ]
        if interest:
            legs.append((gl["interest_income"], -interest, None))
    else:
        legs = [
            (gl["loans_advances"], magnitude, None),
            (gl["customer_deposits"], -magnitude, event.account_id),
        ]
    ledger.post(
        posting_date, type_code, "transaction", transaction_id, event.currency_code, legs, stamp
    )


def _contra_account(gl: dict[str, str], type_code: str) -> str:
    """Which ledger account faces customer deposits for this kind of movement."""
    if type_code in ("account_fee", "maintenance_fee", "card_issue_fee"):
        return gl["fee_income"]
    if type_code == "fx_markup":
        return gl["fx_income"]
    if type_code in ("interest_posting", "interest_accrual"):
        return gl["interest_expense"]
    if type_code in ("chargeback_adjustment", "write_off", "internal_reclass"):
        return gl["suspense"]
    return gl["cash"]


def _expected_card_purchases(model: DayModel, snapshot: Snapshot) -> float:
    """The day's expected card purchase count, from the same rates the day is drawn from."""
    share = float(model.transactions["type_mix"].get("card_purchase", 0.0))
    rate_by_class = model.transactions.get("per_account_month_by_product_class") or {}
    total = 0.0
    for account in snapshot.accounts.values():
        if account.status_code == "dormant":
            continue
        rate = float(
            rate_by_class.get(account.product_class, model.transactions["per_account_month"])
        )
        total += rate
    return total * model.seasonal * model.growth * share / model.month_span


def _draw_account(rng, snapshot: Snapshot) -> Account | None:
    ids = snapshot.open_account_ids
    if not ids:
        return None
    return snapshot.accounts[ids[rng.randrange(len(ids))]]


def _usable_cards(snapshot: Snapshot, account: Account, day: dt.date) -> list:
    """Cards that may carry a transaction today.

    `active` only. Invariant 2 treats a transaction on a card whose status is `issued` as an
    offence regardless of its date, so a card that has not been activated never transacts, and
    a tick never moves a card back to `issued`.
    """
    return [
        card
        for card in snapshot.cards_of(account.account_id)
        if card.status_code == "active" and card.issued_date <= day < card.expiry_date
    ]


def _draw_card(rng, snapshot: Snapshot, account: Account, day: dt.date):
    cards = _usable_cards(snapshot, account, day)
    return cards[rng.randrange(len(cards))] if cards else None


def _draw_lag(
    rng, lags: list[int], weights: list[float], account: Account, card, day: dt.date
) -> int | None:
    """A lag whose business date falls inside the lifetime of every entity it references.

    The draw is conditioned rather than filtered: the weights of the lags that would put the
    transaction outside the account's open window or the card's validity are zeroed before
    anything is drawn, and an entity with no admissible lag is not a candidate at all. That is
    how invariants 1 and 2 are held at the cause instead of by discarding rows afterwards.
    """
    admissible = []
    admissible_weights = []
    for lag, weight in zip(lags, weights, strict=True):
        business_day = day - dt.timedelta(days=lag)
        if business_day < account.opened_date:
            continue
        if not (card.issued_date <= business_day < card.expiry_date):
            continue
        admissible.append(lag)
        admissible_weights.append(weight)
    if not admissible:
        return None
    return int(weighted_pick(rng, admissible, admissible_weights))


def _random_merchant(rng, model: DayModel) -> int | None:
    if not model.merchant_count:
        return None
    return rng.randrange(1, model.merchant_count + 1)


def _counterparty_name(rng, model: DayModel) -> str | None:
    from ...entities import vocabulary as vocab  # noqa: PLC0415 - a large module, used once

    names = _screening_names()
    if names and bernoulli(rng, float(model.payments["screening_positive_share"])):
        return names[rng.randrange(len(names))]
    if bernoulli(rng, 0.94):
        return (
            f"{vocab.GIVEN_NAMES[rng.randrange(len(vocab.GIVEN_NAMES))]} "
            f"{vocab.FAMILY_NAMES[rng.randrange(len(vocab.FAMILY_NAMES))]}"
        )
    return None


@cache
def _screening_names() -> tuple[str, ...]:
    if not _SCREENING.exists():
        return ()
    lines = _SCREENING.read_text(encoding="utf-8").splitlines()
    return tuple(line.split(",")[0].strip() for line in lines[1:] if line.strip())


def _device_count(context: TickContext, model: DayModel, customer_id: int) -> int:
    """Re-derived rather than counted from the book: spec 003, amended 2026-09-20.

    Counting `distinct device_fingerprint` per customer measures 3.0 s on the `dev` book
    against 71 ms for every identity high-water mark in the schema, and the value is a pure
    function of the seed and the customer, so reading it back buys nothing.
    """
    from ...realism.lifecycle import device_count  # noqa: PLC0415 - one call site

    return device_count(context.streams.stream("devices", customer_id), model.digital)
