"""The lending funnel: applications, loans, and the installment schedule.

Runs before the movement pass and hands it the cash flows. A disbursement credits the
customer's account and every installment payment debits it, so those movements have to be in
the same chronological fold as the card and payment activity or `accounts.balance` will not
reconcile. Producing them here and merging them there is what keeps invariant 4 true without a
second reconciliation pass.

Three invariants shape the output. Every loan traces to an approved application and is within
its approved amount (invariant 8). Every schedule is consistent with the loan's principal, rate
and term, `paid_amount` never exceeds `due_amount`, and nothing is paid before disbursement
(invariant 7). And a defaulting loan stops paying in a month drawn from the seasoning curve
rather than at origination, so Q7's vintage curves have shape.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ..config import RunConfig
from ..realism import lending as model
from ..realism.calendar import add_months, at_time
from ..realism.distributions import bernoulli, cents, poisson
from ..refdata import RefData
from ..rng import SubStreams
from ..spool import Spool


@dataclass(frozen=True)
class CashEvent:
    """A loan cash flow the movement pass folds into the account balance."""

    when: dt.datetime
    account_id: int
    amount: Decimal  # signed: positive credits the customer, negative debits them
    source_entity_code: str
    source_entity_id: int
    description: str
    currency_code: str
    # An installment repays principal and pays interest, and the ledger has to separate
    # them or Q5's net interest income proxy has no interest income to read. Null for a
    # disbursement, which is principal only.
    principal_component: Decimal | None = None


@dataclass
class LoanBook:
    cash_events: list[CashEvent] = field(default_factory=list)
    loans: int = 0
    applications: int = 0
    installments: int = 0
    approved: int = 0
    decided: int = 0
    defaulted: int = 0


def _first_current_account(
    accounts_customer: list[int], product_class: list[str], customer_id: int,
    index_by_customer: dict[int, list[int]]
) -> int | None:
    for account_id in index_by_customer.get(customer_id, ()):
        if product_class[account_id - 1] == "current":
            return account_id
    return None


def generate(
    config: RunConfig,
    ref: RefData,
    streams: SubStreams,
    spool: Spool,
    customer_signup: list[dt.date],
    customer_risk_band: list[str],
    accounts_customer: list[int],
    accounts_product_class: list[str],
    accounts_currency: list[str],
    accounts_opened: list[dt.date],
) -> LoanBook:
    params = config.profile.params
    lending = params["lending"]
    book = LoanBook()

    application_rows = spool.table("loan_applications")
    loan_rows = spool.table("loans")
    installment_rows = spool.table("loan_installments")

    index_by_customer: dict[int, list[int]] = {}
    for account_id, customer_id in enumerate(accounts_customer, start=1):
        index_by_customer.setdefault(customer_id, []).append(account_id)

    decision_reasons = ref.codes("decision_reasons")
    application_id = 0
    loan_id = 0
    installment_id = 0

    for customer_id, signup in enumerate(customer_signup, start=1):
        rng = streams.stream("lending", customer_id)
        account_id = _first_current_account(
            accounts_customer, accounts_product_class, customer_id, index_by_customer
        )
        if account_id is None:
            continue

        # Applications arrive at a per-year rate over the customer's tenure.
        tenure_days = max(0, (config.anchor - max(signup, config.history_start)).days)
        expected = float(lending["applications_per_customer_year"]) * tenure_days / 365.0
        for _ in range(poisson(rng, expected)):
            application_id += 1
            book.applications += 1

            offset = rng.randrange(tenure_days + 1) if tenure_days > 0 else 0
            applied_day = max(signup, config.history_start) + dt.timedelta(days=offset)
            if applied_day > config.anchor:
                applied_day = config.anchor
            applied_at = at_time(applied_day, rng.randrange(8, 21), rng.randrange(60), 0)

            product_code = model.draw_product(rng, lending)
            product = ref.row("loan_products", product_code)
            requested = model.draw_requested_amount(rng, lending)
            risk_band = customer_risk_band[customer_id - 1]
            currency = accounts_currency[account_id - 1]

            decided_at: dt.datetime | None = None
            approved_amount: Decimal | None = None
            reason: str | None = None

            # Undecided, withdrawn and expired applications are excluded from the approval rate
            # and reported beside it, per metric_definitions.md, so they have to exist.
            roll = rng.random()
            undecided = float(lending["undecided_share"])
            withdrawn = undecided + float(lending["withdrawn_share"])
            expired = withdrawn + float(lending["expired_share"])

            if roll < undecided:
                status = (
                    "referred"
                    if bernoulli(rng, float(lending["referred_share"]))
                    else "scoring"
                )
            elif roll < withdrawn:
                status = "withdrawn"
                reason = "customer_withdrew" if "customer_withdrew" in decision_reasons else None
            elif roll < expired:
                status = "expired"
                reason = (
                    "expired_no_response" if "expired_no_response" in decision_reasons else None
                )
            else:
                lag_hours = rng.randint(
                    int(lending["decision_lag_hours_min"]), int(lending["decision_lag_hours_max"])
                )
                candidate = applied_at + dt.timedelta(hours=lag_hours)
                anchor_end = at_time(config.anchor, 23, 59, 59)
                if candidate > anchor_end:
                    status = "scoring"
                else:
                    decided_at = candidate
                    book.decided += 1
                    approved = bernoulli(rng, model.approval_probability(lending, risk_band))
                    status = "approved" if approved else "rejected"
                    reason = model.decision_reason(rng, approved, risk_band, decision_reasons)
                    if approved:
                        book.approved += 1
                        approved_amount = model.approved_amount(rng, lending, requested)

            application_rows.write((
                application_id,
                f"APP{application_id:010d}",
                customer_id,
                product_code,
                status,
                risk_band if decided_at is not None else None,
                reason,
                applied_at,
                decided_at,
                requested,
                approved_amount,
                currency,
                applied_at,
                decided_at or applied_at,
                False,
            ))

            if status != "approved" or approved_amount is None or approved_amount <= 0:
                continue
            if not bernoulli(rng, float(lending["disbursement_share"])):
                continue

            disbursed_day = (decided_at or applied_at).date() + dt.timedelta(
                days=rng.randint(
                    int(lending["disbursement_lag_days_min"]),
                    int(lending["disbursement_lag_days_max"]),
                )
            )
            if disbursed_day > config.anchor:
                continue
            if disbursed_day < accounts_opened[account_id - 1]:
                disbursed_day = accounts_opened[account_id - 1]

            term_months = model.draw_term_months(rng, lending)
            rate = Decimal(str(product["nominal_annual_rate"]))
            principal = approved_amount
            maturity = add_months(disbursed_day, term_months)
            disbursed_at = at_time(disbursed_day, rng.randrange(9, 18), rng.randrange(60), 0)

            loan_id += 1
            book.loans += 1

            plan = model.schedule(principal, rate, term_months, add_months(disbursed_day, 1))

            # Does this loan go bad, and when.
            defaults = bernoulli(rng, model.default_probability(lending, risk_band))
            default_month = (
                model.seasoned_default_month(rng, lending, term_months) if defaults else None
            )

            status_code = "current"
            written_off: dt.date | None = None
            last_paid_number = 0

            for installment in plan:
                installment_id += 1
                book.installments += 1
                due_date = installment.due_date
                paid_amount = Decimal("0.0000")
                paid_at: dt.datetime | None = None

                stopped = default_month is not None and installment.number >= default_month
                in_future = due_date > config.anchor

                if not in_future and not stopped:
                    if model.pays_late(rng, lending):
                        paid_day = due_date + dt.timedelta(days=model.late_days(rng, lending))
                    else:
                        paid_day = due_date - dt.timedelta(days=rng.randrange(0, 3))
                    if paid_day <= config.anchor:
                        paid_amount = installment.due_amount
                        paid_at = at_time(paid_day, rng.randrange(8, 20), rng.randrange(60), 0)
                        last_paid_number = installment.number
                elif not in_future and stopped and model.cures(rng, lending):
                    # A cure: the arrears clear later, visible as a fall in the rate rather
                    # than as an edit of an earlier month.
                    paid_day = due_date + dt.timedelta(days=rng.randrange(35, 95))
                    if paid_day <= config.anchor:
                        paid_amount = installment.due_amount
                        paid_at = at_time(paid_day, rng.randrange(8, 20), rng.randrange(60), 0)
                        last_paid_number = installment.number

                created = at_time(disbursed_day, 3, 0, 0)
                installment_rows.write((
                    installment_id,
                    loan_id,
                    installment.number,
                    due_date,
                    installment.due_amount,
                    paid_amount,
                    paid_at,
                    currency,
                    created,
                    paid_at or created,
                    False,
                ))

                if paid_at is not None:
                    book.cash_events.append(
                        CashEvent(
                            when=paid_at,
                            account_id=account_id,
                            amount=-installment.due_amount,
                            source_entity_code="loan_installment",
                            source_entity_id=installment_id,
                            description=f"Loan installment {installment.number}",
                            currency_code=currency,
                            principal_component=installment.principal_component,
                        )
                    )

            if default_month is not None:
                default_day = add_months(disbursed_day, default_month)
                if default_day <= config.anchor:
                    book.defaulted += 1
                    status_code = "defaulted"
                    months_in_default = (config.anchor - default_day).days // 30
                    if months_in_default >= int(lending["write_off_months"]) and bernoulli(
                        rng, float(lending["write_off_share"])
                    ):
                        status_code = "written_off"
                        written_off = add_months(default_day, int(lending["write_off_months"]))
                        if written_off > config.anchor:
                            written_off = config.anchor
                else:
                    status_code = "current"
            elif last_paid_number >= term_months:
                status_code = "closed"
            elif bernoulli(rng, float(lending["restructured_share"])):
                status_code = "restructured"

            loan_rows.write((
                loan_id,
                f"LN{loan_id:012d}",
                application_id,
                customer_id,
                product_code,
                status_code,
                disbursed_day,
                maturity,
                written_off,
                principal,
                currency,
                rate,
                term_months,
                disbursed_at,
                at_time(written_off, 2, 0, 0) if written_off else disbursed_at,
                False,
            ))

            book.cash_events.append(
                CashEvent(
                    when=disbursed_at,
                    account_id=account_id,
                    amount=cents(principal),
                    source_entity_code="loan_disbursement",
                    source_entity_id=loan_id,
                    description="Loan disbursement",
                    currency_code=currency,
                )
            )

    book.cash_events.sort(key=lambda event: (event.when, event.account_id, event.source_entity_id))
    return book
