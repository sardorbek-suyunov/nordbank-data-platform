"""What changes about an entity that already exists: status, terms, attributes, servicing.

This is the phase M5's SCD2 and deduplication are built for, so breadth matters more than
volume. Six tables move here — `accounts`, `cards`, `customers`, `customer_addresses`, `loans`
and `loan_installments` — and each moves for a reason the source system would recognise rather
than to exercise a code path.

**Nothing in this phase is spread across the day.** `PHASE_CLOCKS` marks it batched, so every
row it touches shares one `updated_at`. That is deliberate twice over. It is what a nightly
sweep produces, and `architecture.md` justifies M4's `>=` watermark overlap partly on the case
where several rows share the exact `updated_at` that became the watermark — a case no test could
exercise if every timestamp this engine produced were distinct.

**Monthly hazards are reused rather than restated.** An account becomes dormant, reactivates,
freezes or closes at the rates in `attrition`, divided by the length of the simulated month. The
alternative is a second set of numbers that happen to look like the first, drifting apart the
first time either is tuned.

**The phase runs before movements, and hands it work rather than doing it.** A repayment is two
facts: the installment is marked paid, which is a loan fact and belongs here, and the money
leaves the account, which is a movement and belongs there. Invariant 4 requires the second to be
a transaction, so this phase appends to `context.cash_events` and the movement phase folds it.

**An account this phase closes leaves the movement phase's candidate set.** It is removed from
the snapshot outright, so no movement can be posted to it and invariant 1 has nothing to catch.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from ...entities import vocabulary as vocab
from ...entities.lending import CashEvent
from ...realism import lending as lending_model
from ...realism.calendar import add_months
from ...realism.distributions import bernoulli, cents, weighted_choice
from ..tick import TickContext
from ..writer import apply_updates

# An account does not close inside its first thirty days: cards are issued up to ten days after
# opening, so a closure days after opening leaves a card issued onto a closed account, which is
# what invariant 1 refuses. The historical load holds the same floor, for the same reason.
COOLING_OFF_DAYS = 30

# Card validity, in years, for a reissue. Read from the profile where the historical load reads
# it, so one change moves both.
CARD_VALIDITY_KEY = "validity_years"


@dataclass
class Sweep:
    """What the phase decided, collected before anything is written."""

    dormant: list[int]
    reactivated: list[int]
    frozen: list[int]
    closed: list[int]
    overdraft_ids: list[int]
    overdraft_limits: list[Decimal]


def run(context: TickContext) -> None:
    daily = _daily_hazards(context)
    _accounts(context, daily)
    _cards(context, daily)
    _customers(context, daily)
    _addresses(context, daily)
    _decide_applications(context)
    _disburse_approved(context)
    _loans(context)


def _daily_hazards(context: TickContext) -> dict[str, float]:
    """Monthly hazards as daily ones, over the length of the simulated month."""
    from ...realism.calendar import days_in_month  # noqa: PLC0415 - one call site

    day = context.simulated_date
    span = days_in_month(day.year, day.month)
    attrition = context.profile.params["attrition"]
    mutation = context.profile.params["mutation"]["lifecycle"]
    return {
        "dormant": float(attrition["monthly_dormancy_hazard"]) / span,
        "reactivate": float(attrition["dormancy_reactivation_hazard"]) / span,
        "frozen": float(attrition["frozen_share"]) / span,
        "close_first_year": float(attrition["monthly_close_hazard_first_year"]) / span,
        "close_thereafter": float(attrition["monthly_close_hazard_thereafter"]) / span,
        "overdraft": float(mutation["overdraft_limit_change_monthly_hazard"]) / span,
        "card_block": float(mutation["card_block_monthly_hazard"]) / span,
        "card_reissue": float(mutation["card_reissue_monthly_hazard"]) / span,
        "risk_band": float(mutation["customer_risk_band_monthly_hazard"]) / span,
        "kyc": float(mutation["customer_kyc_monthly_hazard"]) / span,
        "contact": float(mutation["customer_contact_monthly_hazard"]) / span,
        "surname": float(mutation["customer_surname_monthly_hazard"]) / span,
        "address": float(mutation["address_change_monthly_hazard"]) / span,
        "unblock": float(mutation["blocked_card_replacement_daily_hazard"]),
    }


# --------------------------------------------------------------------------------- accounts


def _accounts(context: TickContext, daily: dict[str, float]) -> None:
    """The overnight status sweep, and the occasional change of terms."""
    day = context.simulated_date
    snapshot = context.snapshot
    limits = context.profile.params["accounts"]["overdraft_limit_choices"]

    collecting = _accounts_with_a_collecting_loan(context)

    sweep = Sweep([], [], [], [], [], [])
    for account_id in snapshot.open_account_ids:
        account = snapshot.accounts[account_id]
        rng = context.stream("lifecycle.account", account_id)

        age = (day - account.opened_date).days
        hazard = daily["close_first_year"] if age // 30 < 12 else daily["close_thereafter"]
        # A bank does not let a customer close the account its direct debit collects a loan
        # from, and the movement phase posts that collection later this tick.
        closable = account_id not in collecting and age >= COOLING_OFF_DAYS

        # One change per account per day. The branches are exclusive rather than independent
        # because two statements touching one row would count it twice in the tick log against
        # a window that holds the row once.
        if account.status_code == "active" and bernoulli(rng, daily["dormant"]):
            sweep.dormant.append(account_id)
        elif account.status_code == "dormant" and bernoulli(rng, daily["reactivate"]):
            sweep.reactivated.append(account_id)
        elif account.status_code == "active" and bernoulli(rng, daily["frozen"]):
            sweep.frozen.append(account_id)
        elif closable and bernoulli(rng, hazard):
            sweep.closed.append(account_id)
        elif account.product_class == "current" and bernoulli(rng, daily["overdraft"]):
            sweep.overdraft_ids.append(account_id)
            sweep.overdraft_limits.append(cents(rng.choice(limits)))

    _apply_statuses(context, sweep, day)

    # Closed accounts leave the movement phase's candidate set, so nothing can post to them.
    for account_id in sweep.closed:
        snapshot.accounts.pop(account_id, None)
    for account_id, status in (
        [(a, "dormant") for a in sweep.dormant]
        + [(a, "active") for a in sweep.reactivated]
        + [(a, "frozen") for a in sweep.frozen]
    ):
        existing = snapshot.accounts.get(account_id)
        if existing is not None:
            snapshot.accounts[account_id] = existing._replace(status_code=status)


def _accounts_with_a_collecting_loan(context: TickContext) -> set[int]:
    context.cursor.execute(
        """
        select distinct ah.account_id
          from core.loans l
          join core.account_holders ah on ah.customer_id = l.customer_id
          join ref.holder_roles hr on hr.code = ah.holder_role_code and hr.is_primary
         where l.loan_status_code in ('current', 'arrears', 'restructured')
           and not l.is_deleted and not ah.is_deleted
        """
    )
    return {row[0] for row in context.cursor.fetchall()}


def _apply_statuses(context: TickContext, sweep: Sweep, day: dt.date) -> None:
    for status, ids in (
        ("dormant", sweep.dormant),
        ("active", sweep.reactivated),
        ("frozen", sweep.frozen),
    ):
        context.report.record_update(
            "accounts",
            apply_updates(
                context.cursor,
                "update core.accounts set account_status_code = %s "
                "where account_id = any(%s::bigint[]) returning account_id",
                ids,
                scalars=(status,),
            ),
        )

    context.report.record_update(
        "accounts",
        apply_updates(
            context.cursor,
            "update core.accounts set account_status_code = 'closed', closed_date = %s "
            "where account_id = any(%s::bigint[]) returning account_id",
            sweep.closed,
            scalars=(day,),
        ),
    )

    context.report.record_update(
        "accounts",
        apply_updates(
            context.cursor,
            """
            update core.accounts a
               set overdraft_limit_amount = d.limit_amount
              from unnest(%s::bigint[], %s::numeric[]) as d(account_id, limit_amount)
             where a.account_id = d.account_id
            returning a.account_id
            """,
            sweep.overdraft_ids,
            sweep.overdraft_limits,
        ),
    )


# ------------------------------------------------------------------------------------ cards


def _cards(context: TickContext, daily: dict[str, float]) -> None:
    """Blocked, replaced, expired and reissued.

    A card is never moved back to `issued`. Invariant 2 treats a transaction on a card whose
    status is `issued` as an offence regardless of when it was booked, so demoting a card that
    has already transacted would make every one of its past transactions an offence at once.
    """
    day = context.simulated_date
    snapshot = context.snapshot
    stamp = context.phase_instant("lifecycle")
    validity = int(context.profile.params["cards"][CARD_VALIDITY_KEY])

    blocking: list[int] = []
    replacing: list[int] = []
    expiring: list[int] = []
    reissue_for: list[int] = []

    for account_id in snapshot.open_account_ids:
        account = snapshot.accounts[account_id]
        rng = context.stream("lifecycle.card", account_id)
        cards = snapshot.cards_of(account_id)
        usable = [
            card
            for card in cards
            if card.status_code == "active" and card.issued_date <= day < card.expiry_date
        ]

        for card in cards:
            if card.status_code == "active" and card.expiry_date <= day:
                expiring.append(card.card_id)
                reissue_for.append(account_id)
            elif card.status_code == "active" and bernoulli(rng, daily["card_block"]):
                blocking.append(card.card_id)
            elif card.status_code == "blocked" and bernoulli(rng, daily["unblock"]):
                # The customer called: the card is superseded and a new one goes out.
                replacing.append(card.card_id)
                reissue_for.append(account_id)

        # An account left with no usable card gets one. This is also what heals the accounts
        # the historical book left carrying nothing but a blocked, cancelled or replaced card.
        if (
            not usable
            and account.product_class == "current"
            and bernoulli(rng, daily["card_reissue"])
        ):
            reissue_for.append(account_id)

    for status, ids in (("blocked", blocking), ("replaced", replacing), ("expired", expiring)):
        context.report.record_update(
            "cards",
            apply_updates(
                context.cursor,
                "update core.cards set card_status_code = %s "
                "where card_id = any(%s::bigint[]) returning card_id",
                ids,
                scalars=(status,),
            ),
        )

    for account_id in sorted(set(reissue_for)):
        _issue_card(context, account_id, day, stamp, validity)


def _issue_card(
    context: TickContext, account_id: int, day: dt.date, stamp: dt.datetime, validity: int
) -> None:
    """Issue a replacement, active from today.

    Active rather than `issued`: a replacement card arrives activated in this model, and a card
    sitting in `issued` would be one invariant 2 forbids transacting on while nothing ever moved
    it on.
    """
    rng = context.stream("lifecycle.reissue", account_id)
    card_id = context.snapshot.take_id("cards")
    product = weighted_choice(rng, context.profile.params["cards"]["product_mix"])
    expiry = dt.date(day.year + validity, day.month, min(day.day, 28))

    context.writer.add(
        "cards",
        (
            card_id,
            f"NB{card_id:010d}",
            account_id,
            product,
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
    context.snapshot.cards_by_account.setdefault(account_id, []).append(
        _card_tuple(card_id, account_id, day, expiry)
    )


def _card_tuple(card_id: int, account_id: int, issued: dt.date, expiry: dt.date):
    from ..snapshot import Card  # noqa: PLC0415 - avoids importing a type for one construction

    return Card(
        card_id=card_id,
        account_id=account_id,
        status_code="active",
        issued_date=issued,
        expiry_date=expiry,
    )


# -------------------------------------------------------------------------------- customers


def _customers(context: TickContext, daily: dict[str, float]) -> None:
    """Risk rating, KYC status, contact details, and the occasional surname change.

    Four attributes rather than one, because M5's SCD2 has to produce a version chain and a
    dimension whose only ever change is one column is not one.
    """
    params = context.profile.params["customers"]
    context.cursor.execute(
        """
        select c.customer_id, c.full_name, c.risk_band_code, c.kyc_status_code
          from core.customers c
         where not c.is_deleted
           and exists (select 1 from core.account_holders ah
                        where ah.customer_id = c.customer_id and not ah.is_deleted)
        """
    )
    candidates = context.cursor.fetchall()

    bands: tuple[list[int], list[str]] = ([], [])
    kyc: tuple[list[int], list[str]] = ([], [])
    emails: tuple[list[int], list[str]] = ([], [])
    phones: tuple[list[int], list[str]] = ([], [])
    names: tuple[list[int], list[str]] = ([], [])

    # One attribute changes per customer per day, drawn from the combined hazard and then
    # allocated. Four independent draws would sometimes pick the same customer twice, which the
    # tick log would count twice against a window that holds the row once.
    weights = {
        "risk_band": daily["risk_band"],
        "kyc": daily["kyc"],
        "contact": daily["contact"],
        "surname": daily["surname"],
    }
    combined = sum(weights.values())

    for customer_id, full_name, risk_band, kyc_status in candidates:
        rng = context.stream("lifecycle.customer", customer_id)
        if not bernoulli(rng, combined):
            continue
        given, _, family = full_name.partition(" ")
        choice = weighted_choice(rng, {key: value / combined for key, value in weights.items()})

        if choice == "risk_band":
            drawn = weighted_choice(rng, params["risk_band_mix"])
            if drawn != risk_band:
                bands[0].append(customer_id)
                bands[1].append(drawn)
        elif choice == "kyc":
            drawn = weighted_choice(rng, params["kyc_status_mix"])
            if drawn != kyc_status:
                kyc[0].append(customer_id)
                kyc[1].append(drawn)
        elif choice == "contact":
            if bernoulli(rng, 0.5):
                emails[0].append(customer_id)
                emails[1].append(
                    f"{given.lower()}.{family.lower()}{customer_id}"
                    f"{rng.randrange(10, 99)}@example.invalid"
                )
            else:
                phones[0].append(customer_id)
                phones[1].append(f"+{rng.randrange(30, 49)}{rng.randrange(100000000, 999999999)}")
        else:
            new_family = vocab.FAMILY_NAMES[rng.randrange(len(vocab.FAMILY_NAMES))]
            names[0].append(customer_id)
            names[1].append(f"{given} {new_family}")

    for column, (ids, values) in (
        ("risk_band_code", bands),
        ("kyc_status_code", kyc),
        ("email", emails),
        ("phone", phones),
        ("full_name", names),
    ):
        context.report.record_update(
            "customers",
            apply_updates(
                context.cursor,
                f"""
                update core.customers c
                   set {column} = d.value
                  from unnest(%s::bigint[], %s::text[]) as d(customer_id, value)
                 where c.customer_id = d.customer_id
                returning c.customer_id
                """,  # noqa: S608 - `column` is one of the five literals above
                ids,
                values,
            ),
        )


def _addresses(context: TickContext, daily: dict[str, float]) -> None:
    """A move: the current address is closed and a new version is inserted.

    One logical change written as an insert and an update, which is the shape M5's SCD2 has to
    recognise. `valid_to_date` on the old row is the day before the new one starts, so the two
    do not overlap and a point-in-time lookup has exactly one answer.
    """
    day = context.simulated_date
    stamp = context.phase_instant("lifecycle")
    context.cursor.execute(
        """
        select a.customer_address_id, a.customer_id, a.country_code
          from core.customer_addresses a
         where a.address_type_code = 'residential'
           and a.valid_to_date is null and not a.is_deleted
        """
    )
    closing: list[int] = []
    for address_id, customer_id, country in context.cursor.fetchall():
        rng = context.stream("lifecycle.address", customer_id)
        if not bernoulli(rng, daily["address"]):
            continue
        closing.append(address_id)

        cities = vocab.cities_for(country)
        digits = vocab.postcode_digits(country)
        new_id = context.snapshot.take_id("customer_addresses")
        context.writer.add(
            "customer_addresses",
            (
                new_id,
                customer_id,
                "residential",
                vocab.street_address(rng.randrange(64), rng.randrange(8), rng.randrange(1, 240)),
                None,
                cities[rng.randrange(len(cities))],
                str(rng.randrange(10 ** (digits - 1), 10**digits)),
                country,
                day,
                None,
                stamp,
                stamp,
                False,
            ),
        )

    context.report.record_update(
        "customer_addresses",
        apply_updates(
            context.cursor,
            "update core.customer_addresses set valid_to_date = %s "
            "where customer_address_id = any(%s::bigint[]) returning customer_address_id",
            closing,
            scalars=(day - dt.timedelta(days=1),),
        ),
    )


# ------------------------------------------------------------------------------------ loans


def _loans(context: TickContext) -> None:
    """Servicing: installments collected, and the loan status that follows from them.

    The money is not moved here. A repayment is a direct debit on the customer's account, and
    invariant 4 reconciles the balance against transactions and payments alone, so the cash
    event goes to the movement phase and the installment record stays here.
    """
    day = context.simulated_date
    lending = context.profile.params["lending"]
    grace = int(lending["late_payment_days_max"])
    late_share = float(lending["late_payment_share"])

    context.cursor.execute(
        """
        select i.loan_installment_id, i.loan_id, i.due_date, i.due_amount,
               i.installment_currency_code, ah.account_id,
               l.principal_amount / greatest(l.term_months, 1) as principal_component
          from core.loan_installments i
          join core.loans l on l.loan_id = i.loan_id
          join core.account_holders ah on ah.customer_id = l.customer_id
          join ref.holder_roles hr on hr.code = ah.holder_role_code and hr.is_primary
          join core.accounts a on a.account_id = ah.account_id
          join ref.account_statuses s on s.code = a.account_status_code
         where i.paid_at is null and i.due_date <= %s
           and l.loan_status_code in ('current', 'arrears', 'restructured')
           and s.is_open and not i.is_deleted and not l.is_deleted
         order by i.loan_installment_id
        """,
        (day,),
    )
    due = context.cursor.fetchall()
    if not due:
        _close_settled_loans(context)
        return

    paid_ids: list[int] = []
    paid_amounts: list[Decimal] = []
    arrears: set[int] = set()

    for installment_id, loan_id, due_date, amount, currency, account_id, principal in due:
        rng = context.stream("lifecycle.installment", installment_id)
        overdue = (day - due_date).days

        if overdue == 0 and bernoulli(rng, late_share):
            # Late but not delinquent: it will be collected inside the same cycle.
            continue
        if overdue > grace:
            arrears.add(loan_id)
            continue
        if overdue > 0 and not bernoulli(rng, 1.0 / max(1, grace - overdue + 1)):
            continue

        paid_ids.append(installment_id)
        paid_amounts.append(amount)
        context.cash_events.append(
            CashEvent(
                when=context.at(6),
                account_id=account_id,
                amount=-amount,
                source_entity_code="transaction",
                source_entity_id=0,
                description="direct_debit",
                currency_code=currency,
                # Principal and interest reach different ledger accounts, or Q5's net interest
                # income proxy has no interest income to read. The split is the loan's own
                # terms rather than a ratio: principal over term, capped at what is being paid.
                principal_component=min(cents(principal), amount),
            )
        )

    context.report.record_update(
        "loan_installments",
        apply_updates(
            context.cursor,
            """
            update core.loan_installments i
               set paid_amount = d.amount, paid_at = %s
              from unnest(%s::bigint[], %s::numeric[]) as d(installment_id, amount)
             where i.loan_installment_id = d.installment_id
            returning i.loan_installment_id
            """,
            paid_ids,
            paid_amounts,
            scalars=(context.at(6),),
        ),
    )

    context.report.record_update(
        "loans",
        apply_updates(
            context.cursor,
            "update core.loans set loan_status_code = 'arrears' "
            "where loan_id = any(%s::bigint[]) and loan_status_code <> 'arrears' "
            "returning loan_id",
            sorted(arrears),
        ),
    )

    _close_settled_loans(context)


def _close_settled_loans(context: TickContext) -> None:
    """A loan whose every installment is paid is closed."""
    context.cursor.execute(
        """
        update core.loans l
           set loan_status_code = 'closed'
         where l.loan_status_code in ('current', 'arrears', 'restructured')
           and not exists (select 1 from core.loan_installments i
                            where i.loan_id = l.loan_id and i.paid_at is null)
        returning l.loan_id
        """
    )
    context.report.record_update("loans", [row[0] for row in context.cursor.fetchall()])


# ---------------------------------------------------------------------------- the lending funnel


# When in the simulated day a decision and a disbursement are stamped. Inside office hours
# rather than at the sweep's own instant, because both are an underwriter's act.
DECISION_HOUR = 11
DISBURSEMENT_HOUR = 14


def _decide_applications(context: TickContext) -> None:
    """Decide the applications whose decision lag has elapsed.

    The lag is two to 216 hours in the historical model — up to nine days — so an application
    written by the acquisition phase sits in `scoring` for a while, which is what gives M4 a row
    that changes status days after it arrived. The lag is drawn from a stream keyed on the
    application, so it is the same number on every replay and does not have to be stored.
    """
    lending = context.profile.params["lending"]
    reasons = context.snapshot.ref.codes("decision_reasons")
    now = context.at(DECISION_HOUR)

    context.cursor.execute(
        """
        select a.loan_application_id,
               -- Nullable on the application; the underwriter scores the customer, so the
               -- customer's current band is the answer when the application does not carry one.
               coalesce(a.risk_band_code, c.risk_band_code) as risk_band_code,
               a.applied_at, a.requested_amount
          from core.loan_applications a
          join core.customers c on c.customer_id = a.customer_id
          join ref.loan_application_statuses s on s.code = a.loan_application_status_code
         where not s.is_decided and not a.is_deleted
         order by a.loan_application_id
        """
    )
    statuses: dict[str, list[int]] = {}
    approvals: tuple[list[int], list] = ([], [])
    decisions: tuple[list[int], list[str]] = ([], [])

    for application_id, risk_band, applied_at, requested in context.cursor.fetchall():
        rng = context.stream("lifecycle.decision", application_id)
        lag = rng.randint(
            int(lending["decision_lag_hours_min"]), int(lending["decision_lag_hours_max"])
        )
        if applied_at + dt.timedelta(hours=lag) > now:
            continue

        roll = rng.random()
        withdrawn = float(lending["withdrawn_share"])
        expired = withdrawn + float(lending["expired_share"])
        if roll < withdrawn:
            statuses.setdefault("withdrawn", []).append(application_id)
            decisions[0].append(application_id)
            decisions[1].append("customer_withdrew" if "customer_withdrew" in reasons else "")
        elif roll < expired:
            statuses.setdefault("expired", []).append(application_id)
            decisions[0].append(application_id)
            decisions[1].append("expired_no_response" if "expired_no_response" in reasons else "")
        elif bernoulli(rng, lending_model.approval_probability(lending, risk_band)):
            approvals[0].append(application_id)
            approvals[1].append(lending_model.approved_amount(rng, lending, requested))
        else:
            statuses.setdefault("rejected", []).append(application_id)
            decisions[0].append(application_id)
            decisions[1].append(lending_model.decision_reason(rng, False, risk_band, reasons))

    for status, ids in sorted(statuses.items()):
        context.report.record_update(
            "loan_applications",
            apply_updates(
                context.cursor,
                "update core.loan_applications set loan_application_status_code = %s, "
                "decided_at = %s where loan_application_id = any(%s::bigint[]) "
                "returning loan_application_id",
                ids,
                scalars=(status, now),
            ),
        )
    context.report.record_update(
        "loan_applications",
        apply_updates(
            context.cursor,
            """
            update core.loan_applications a
               set decision_reason_code = nullif(d.reason, '')
              from unnest(%s::bigint[], %s::text[]) as d(application_id, reason)
             where a.loan_application_id = d.application_id
            returning a.loan_application_id
            """,
            decisions[0],
            decisions[1],
        ),
    )
    context.report.record_update(
        "loan_applications",
        apply_updates(
            context.cursor,
            """
            update core.loan_applications a
               set loan_application_status_code = 'approved', decided_at = %s,
                   approved_amount = d.amount,
                   decision_reason_code = %s
              from unnest(%s::bigint[], %s::numeric[]) as d(application_id, amount)
             where a.loan_application_id = d.application_id
            returning a.loan_application_id
            """,
            approvals[0],
            approvals[1],
            scalars=(now, "within_policy" if "within_policy" in reasons else None),
        ),
    )


def _disburse_approved(context: TickContext) -> None:
    """Draw down the approved applications whose disbursement lag has elapsed.

    Money leaving the bank is what starts the risk, so disbursement lags the decision by one to
    twenty-one days in the historical model and by the same here. Not every approval is drawn
    down; `disbursement_share` decides, and an application that is never drawn stays approved
    with no loan against it, which is what the historical book leaves too.
    """
    lending = context.profile.params["lending"]
    ref = context.snapshot.ref
    day = context.simulated_date
    stamp = context.at(DISBURSEMENT_HOUR)

    context.cursor.execute(
        """
        select a.loan_application_id, a.customer_id, a.loan_product_code, a.approved_amount,
               a.application_currency_code, a.decided_at, ah.account_id
          from core.loan_applications a
          join core.account_holders ah on ah.customer_id = a.customer_id
          join ref.holder_roles hr on hr.code = ah.holder_role_code and hr.is_primary
          join core.accounts ac on ac.account_id = ah.account_id
          join ref.account_statuses s on s.code = ac.account_status_code
         where a.loan_application_status_code = 'approved' and a.approved_amount is not null
           and s.is_open and not a.is_deleted and not ac.is_deleted
           and not exists (select 1 from core.loans l
                            where l.loan_application_id = a.loan_application_id)
         order by a.loan_application_id
        """
    )
    for (
        application_id,
        customer_id,
        product_code,
        approved,
        currency,
        decided_at,
        account_id,
    ) in context.cursor.fetchall():
        rng = context.stream("lifecycle.disbursement", application_id)
        if not bernoulli(rng, float(lending["disbursement_share"])):
            continue
        lag = rng.randint(
            int(lending["disbursement_lag_days_min"]), int(lending["disbursement_lag_days_max"])
        )
        if decided_at.date() + dt.timedelta(days=lag) > day:
            continue

        product = ref.row("loan_products", product_code)
        term = int(product["term_months"])
        rate = Decimal(str(product["nominal_annual_rate"]))
        loan_id = context.snapshot.take_id("loans")

        context.writer.add(
            "loans",
            (
                loan_id,
                f"LN{loan_id:013d}",
                application_id,
                customer_id,
                product_code,
                "current",
                day,
                add_months(day, term),
                None,
                approved,
                currency,
                rate,
                term,
                stamp,
                stamp,
                False,
            ),
        )
        for installment in lending_model.schedule(approved, rate, term, add_months(day, 1)):
            installment_id = context.snapshot.take_id("loan_installments")
            context.writer.add(
                "loan_installments",
                (
                    installment_id,
                    loan_id,
                    installment.number,
                    installment.due_date,
                    installment.due_amount,
                    Decimal("0.0000"),
                    None,
                    currency,
                    stamp,
                    stamp,
                    False,
                ),
            )

        context.cash_events.append(
            CashEvent(
                when=stamp,
                account_id=account_id,
                amount=approved,
                source_entity_code="transaction",
                source_entity_id=0,
                description="transfer_in",
                currency_code=currency,
                principal_component=None,
            )
        )
