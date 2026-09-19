# Metric definitions

Every term used by [business_questions.md](business_questions.md) that could be computed two
defensible ways is pinned here. A model that computes one of these terms implements the rule
on this page; if the rule is wrong, this page changes first and the model follows.

Entries marked **undecided, resolve before M6** depend on a decision that cannot honestly be
made yet. They are listed with what the decision depends on, and they block the marts that
consume them, not the layers below.

All dates are UTC calendar dates. All amounts are EUR equivalents converted as-of, per the
currency conversion rule in [architecture.md](architecture.md), except where a rule states
otherwise, as settlement reconciliation does. Every object named on this page has a row in
[model_inventory.md](model_inventory.md).

## Active account

**Rule.** An account is active in month M if it was open for at least one day in M and
carried at least one customer-initiated posted transaction with a booking timestamp in M.
Customer-initiated excludes interest postings, fee charges, and any other bank-initiated
entry: an account that only accrued a monthly fee is not active. An account opened and closed
inside M can still be active.

**Source columns.** `sl_accounts.opened_date`, `sl_accounts.closed_date`,
`sl_accounts.status`; `fct_transactions.account_sk`, `fct_transactions.booked_at`,
`fct_transactions.is_customer_initiated`; `fct_payments.account_sk`, `fct_payments.booked_at`.

**Used by.** Q1, and as the base of active customer for Q6.

## Active customer

**Rule.** A customer is active in month M if they own at least one account that is active in M
by the definition above. This is a customer-level count: a customer with three active accounts
counts once.

Ownership comes from the account holder bridge (`sl_account_holders` in silver,
`bridge_account_holder` in gold), one row per account and holder, carrying a holder role and
an ownership weighting factor that sums to one per account. A joint account
counts once for each of its holders in customer counts, and is split by the weighting factor
in monetary aggregates, so a joint balance of 1,000 EUR contributes 500 EUR to each of two
holders rather than 1,000 EUR to both. Counting people and dividing money are different
operations on the same bridge, and conflating them double counts every joint account.

The distinction from active account is not cosmetic. Q1 measures product usage at account
level and Q6 measures revenue per person, so the two denominators differ by design and are
never interchangeable.

**Source columns.** `dim_customer.customer_sk`, `bridge_account_holder.account_sk`,
`bridge_account_holder.customer_sk`, `bridge_account_holder.holder_role`,
`bridge_account_holder.ownership_weight`, plus the active account inputs.

**Used by.** Q6, and the denominator of any per-customer measure.

## Customer-initiated transaction

**Rule.** `is_customer_initiated` is true for a transaction the customer caused: card
purchase, card refund, ATM or agent cash withdrawal, agent cash deposit, outgoing transfer,
incoming transfer, standing order execution and direct debit collection.

It is false for everything the bank causes on its own account: interest accrual and posting,
maintenance and account fees, card issuance fees, FX markup postings, chargeback adjustments
raised by the bank, write-offs, and internal reclassification entries between ledger accounts.

The line is drawn by cause, not by sign or by amount. A direct debit is customer-initiated
because the customer signed the mandate; a monthly account fee is not, because it would post
whether the customer touched the account or not. The flag is derived once, in silver, from the
source transaction type, and every downstream activity measure uses it rather than
re-deriving its own list.

**Source columns.** `sl_transactions.transaction_type` with
`ref.transaction_types.is_customer_initiated`, and `sl_payments.payment_type` with
`ref.payment_types.is_customer_initiated`. The flag has one source, the reference table
reached through the type foreign key. An `initiator` column on the transaction itself was
considered and rejected at M2: it would be a second source that can disagree with the first.

**Used by.** Active account, and through it Q1 and Q6.

## Month-over-month growth basis

**Rule.** For a measure X over completed calendar months,
`growth(M) = (X(M) - X(M-1)) / X(M-1)`. M-1 is the immediately preceding calendar month, not
the same month last year and not a trailing 30 days. The current, incomplete month is
excluded from the mart entirely rather than shown as a partial figure. When `X(M-1)` is zero
the result is null, not zero and not infinity. No seasonal adjustment is applied at any point.

**Source columns.** The measure's own columns plus `dim_date.month_start_date`.

**Used by.** Q1.

## Deposit balance

**Rule.** The end-of-day balance on the last calendar day of the month, summed over accounts
whose product class is deposit-taking: current accounts and savings accounts. Loan accounts,
card settlement accounts and internal or suspense accounts are excluded. Overdrawn accounts
are included at their negative balance, so the total is a net customer liability position and
not a sum of positive balances. Accounts closed before the month end contribute nothing.

Whether a movement counts towards a balance is read from the reference layer in both
directions, never decided in a model: `ref.transaction_statuses.is_posted` for transactions and
`ref.payment_statuses.is_posted` for payments. The payment flag was added at M2's data half,
because the balance reconciliation needed a rule for the payment side and settling it in loader
code would have put it where dbt cannot read it.

**Source columns.** `fct_account_balance_daily.balance_amount`,
`fct_account_balance_daily.balance_date`, `dim_account.product_class`,
`dim_account.currency_code`, `dim_account.account_type`,
`ref.transaction_statuses.is_posted`, `ref.payment_statuses.is_posted`.

**Used by.** Q3.

## Interchange rate assumption

**Rule (partially undecided).** Interchange on a settled card transaction is
`transaction_amount_eur * interchange_rate`, where the rate is looked up from the
`interchange_rates` seed keyed on card product class, merchant region and MCC band.

The intra-EEA consumer rates are fixed by regulation and are used as given: 0.20 per cent for
consumer debit and 0.30 per cent for consumer credit. The Interchange Fee Regulation caps do
not vary by presentment, so the same rate applies whether or not the card was presented.

**Inter-regional consumer rates stay null, and the reason is a corridor mismatch rather than
an absent figure.** This entry previously said the rates were decided and would land with a
card-present dimension on the `ref.interchange_rates` key. Working the corridor through at M2
showed that neither half of that was right.

European Commission press release IP/19/2311 of 29 April 2019 made binding the caps Mastercard
and Visa offered in cases AT.40049 and AT.39398: card present 0.20 per cent debit and 0.30 per
cent credit, card not present 1.15 per cent debit and 1.50 per cent credit. Those are real,
citable figures, and their five-year-and-six-month commitment period ended around October 2024,
so they are the last published binding caps rather than current regulation.

They also govern a corridor Nordbank is not in. The commitments cap interchange on cards
**issued outside the EEA and used at EEA merchants** — the inbound corridor, where the issuer
is outside the EEA and the acquirer inside it. Nordbank is an EEA issuer, so it never earns
that interchange. Nordbank's own cards used at non-EEA merchants are the **outbound**
inter-regional corridor, which those commitments do not govern and for which no published
figure exists.

`ref.interchange_rates` is keyed on the merchant region, so the rows those caps would describe
are the `eea` merchant rows, which the Interchange Fee Regulation already fills correctly. The
non-EEA merchant rows describe the outbound corridor. Seeding the 2019 figures into them would
attach a real number to the wrong corridor, which is exactly the false precision the null rates
exist to prevent. **The outbound inter-regional rates therefore stay null.**

The card-present dimension went with them. Its only justification was the inbound presentment
split, and a dimension no rate varies by is ceremony. `core.transactions.is_card_present`
stays, justified on its own terms: fraud concentrates in card not present, and Q10 and Q19
depend on it.

**Commercial card rates remain undecided and stay null.** They are negotiated bilaterally with
no published figure, and inventing one would make Q4 look precise while being arbitrary.

**What Q4 can therefore answer.** Interchange revenue for intra-EEA consumer card volume,
exactly. Everything else resolves to a null rate and fails the gate. The generator holds the
non-EEA merchant share to a few percent, stated in `docs/generator_realism.md`, so the exposure
is bounded and visible rather than dominant.

**Source columns.** `fct_transactions.transaction_amount_eur`, `dim_card.product_class`,
`dim_merchant.country_code`, `dim_merchant.mcc_code`, `ref.interchange_rates.rate`.

A null rate is an error condition, never zero. A transaction that resolves to a null rate
raises a `dq` check of severity `error`, blocks the mart, and Q4 publishes no number for the
affected period. Treating a null as zero would understate interchange revenue silently, which
is the failure mode this page exists to prevent.

**Used by.** Q4, Q6.

## Net interest income proxy

**Rule (partially undecided).** For a loan in month M:

```
interest_accrued = outstanding_principal * nominal_annual_rate * days_in_month / 365
nii_proxy        = interest_accrued - funding_cost
```

The day-count convention is Actual/365 Fixed: the actual number of days in the month over a
fixed 365-day year, so a leap year has 366 days of accrual over a 365-day denominator. It is
named here because the alternatives, 30/360 and Actual/Actual, give different answers on the
same loan book, and a proxy that does not say which one it used cannot be checked.

Outstanding principal is the month-end balance after scheduled repayments. The rate is the
contractual nominal rate in force during M, taken from the SCD2 product dimension as of the
month end. Fee income is excluded: it belongs to Q4 and Q6 and double counting it here would
overstate the loan book.

The funding cost assumption is **undecided, resolve before M6**. The candidate basis is the
ECB deposit facility rate as the funding proxy applied to the funded portion of the loan
book, which requires deciding the funding mix between customer deposits and wholesale
funding, neither of which the source system models today. Until it is decided, Q5 reports
gross interest accrued and states that funding cost is excluded, rather than reporting a
number that silently equals gross.

**Source columns.** `fct_loan_balance_daily.outstanding_principal`, `dim_loan_product.rate`
from `ref.loan_products.nominal_annual_rate`, `dim_date.days_in_month`,
`sl_loans.disbursed_date`. A disbursed loan also carries its own `nominal_annual_rate`, copied
from the product at disbursement, because a loan keeps the terms it was written under.

**Used by.** Q5.

## Origination vintage

**Rule.** The calendar month of the loan disbursement date. Not the application month and not
the approval month: money leaving the bank is what starts the risk. A loan's vintage never
changes, including after restructuring.

**Source columns.** `sl_loans.disbursed_date`.

**Used by.** Q5, Q7.

## Delinquency at 30, 60 and 90 days

**Rule.** Days past due at a reporting date is the reporting date minus the due date of the
oldest installment that is not fully paid, where not fully paid means
`paid_amount < due_amount` as at that reporting date. A loan with no unpaid installment has
zero days past due.

The buckets are cumulative: a loan at 95 days past due appears in 30+, 60+ and 90+. The
headline delinquency rate is value-weighted, `outstanding principal in bucket / total
outstanding principal for the vintage`, with the count-weighted rate reported beside it,
because the two diverge when a few large loans go bad and reporting only one hides that.

A loan that pays its arrears cures and leaves the bucket at the next reporting date. Cures
are visible as a fall in the rate, not as a retrospective edit of earlier months.

**Source columns.** `sl_loan_installments.due_date`, `sl_loan_installments.due_amount`,
`sl_loan_installments.paid_amount`, `fct_loan_balance_daily.outstanding_principal`,
`sl_loans.disbursed_date`.

**Used by.** Q7.

## Default

**Rule.** A loan is in default from the first date it is either 90 or more days past due, or
has been written off or terminated for non-payment by the bank, whichever happens first. The
90-day backstop follows the regulatory convention rather than a project-specific threshold.

Default is absorbing for cohort analysis: a loan that later cures is still counted as ever
defaulted in the vintage and risk-band cohorts, because the question being asked is how often
underwriting decisions go wrong.

**Source columns.** The delinquency inputs, plus `sl_loans.status` and
`sl_loans.written_off_date`.

**Used by.** Q8.

### The default rate must be conditioned on a vintage and a horizon

An overall default rate — defaulted loans over all loans — is not a stable measure and should
not be the one Q7 and Q8 publish. Its denominator changes composition every month as new loans
are written, and a loan that has existed for two months has had almost no opportunity to
default. A book that is growing therefore shows a *falling* default rate with no change in
underwriting at all, and a book that stops lending shows a rising one.

**The measure to use is a fixed-horizon rate on seasoned vintages: the twelve-month default
rate, over loans originated at least twelve months before the reporting date.** Every loan in
that denominator has had the same opportunity to default, so two vintages are comparable to each
other and a change in the rate is a change in credit quality rather than in the growth rate.
The horizon is twelve months because that is where the seasoning curve has delivered most of its
hazard; a longer horizon is more complete and excludes more recent vintages, which is the
trade-off to state if it is ever changed.

The overall rate is kept as a loose sanity check on the generated book — it appears as the
`default_rate_overall` band in `docs/generator_realism.md` — and is explicitly not the measure
the marts report. This is a forward note: **Q7 and Q8 implement the vintage-conditioned measure
at M6**, and `mart_credit_delinquency` and `mart_credit_underwriting` declare it.

## Approval rate

**Rule.** `approved applications / decided applications`, where decided is approved plus
rejected. Applications that were withdrawn by the customer or expired without a decision are
excluded from both numerator and denominator, and their count is reported beside the rate so
that a rising withdrawal rate cannot hide inside an unchanged approval rate. Applications are
attributed to the month of the decision, and the risk band is the band assigned at decision
time, not the customer's band today.

**Source columns.** `sl_loan_applications.status`, `sl_loan_applications.decided_at`,
`sl_loan_applications.risk_band`.

**Used by.** Q8.

## Alert precision and false positive rate

**Rule.** Over alerts that reached a final analyst disposition in month M, attributed to the
month of disposition rather than the month the alert fired:

```
precision      = confirmed_fraud / (confirmed_fraud + dismissed)
false_positive = dismissed / (confirmed_fraud + dismissed)
```

Alerts still open at the end of M are excluded and counted separately as a pending backlog.
Attributing by disposition month is deliberate: dispositions arrive days after the alert, and
attributing by alert date would force last month's published precision to change every time
an analyst closes an old case.

**Recall is not reported, and that is a property of the domain rather than a limitation of the
platform.** Precision and the false positive rate are computable because their denominator is
the set of alerts, which the bank has. Recall's denominator is *all fraud*, which no bank has:
it includes fraud that was never detected, never reported by the customer, and never
distinguished from a legitimate transaction. A recall figure could only be produced by assuming
the answer to the question it claims to measure.

This is why Q10 measures precision and the false positive rate and makes no claim about recall.
It also means the synthetic source cannot be used to smuggle the number back in: the generator
knows which transactions it made fraudulent, but that label is never written to the database —
only the alert and its disposition are — so what a model or a mart sees is exactly what a bank
sees. Any future measure that needs a fraud denominator has to say what it is assuming.

**Source columns.** `sl_fraud_alerts.rule_id`, `sl_fraud_alerts.disposition`,
`sl_fraud_alerts.dispositioned_at`, `sl_fraud_alerts.alerted_at`. The alert instant is
`alerted_at` and not `created_at`: from M2, `created_at` and `updated_at` are reserved for
audit columns on every table, because the loader writes a historical alert time and a
trigger writes the audit time, so one column cannot honestly be both.

**Used by.** Q10.

## Structuring threshold and window

**Rule.** A customer is a structuring candidate on date D when, within the rolling 7 calendar
day window ending on D, they made at least three cash deposits, each individually below EUR
10,000, whose total is EUR 10,000 or more. The individual-deposit condition is what makes it
structuring rather than a large legitimate deposit: the pattern of interest is deliberately
staying under the reporting threshold.

The window is evaluated on every date, so one customer can appear on several consecutive
dates for the same cluster of deposits. That is intended; the mart is a candidate list for
investigation, not a count of distinct events.

Only cash deposits count. Incoming transfers and card refunds are excluded.

**Source columns.** `fct_transactions.transaction_type`, `fct_transactions.channel`,
`fct_transactions.transaction_amount_eur`, `fct_transactions.booked_at`,
`dim_customer.customer_sk`.

**Used by.** Q11.

## Cross-border

**Rule.** A payment is cross-border when the counterparty institution country differs from
the country of the originating account. The originating account's country is used, not the
customer's country of residence, because the account is what the payment leaves. The corridor
is the ordered pair `origin account country -> counterparty country`, so a corridor is
directional and inbound and outbound are never netted.

Both SEPA and non-SEPA payments are in scope, flagged by scheme, since a cross-border SEPA
payment is still cross-border.

**Source columns.** `sl_payments.counterparty_country_code`, `dim_account.country_code`,
`sl_payments.scheme`, `sl_payments.status`.

**Used by.** Q13.

## Settlement break and materiality

**Rule.** Reconciliation compares the card network file total against the internal ledger
total for the same settlement date, network and settlement currency, at source precision, in
the settlement currency. Both sides are exact decimals, so the expected difference is exactly
zero and any non-zero difference is a break. There is no detection tolerance.

EUR conversion plays no part in detection. Converting first would introduce rounding that
manufactures breaks in one direction and hides them in the other, and it would compare a
number the network sent with a number the platform computed. The EUR equivalent is computed
afterwards, for reporting the size of a break in a common currency and for ranking breaks
across networks.

Materiality governs the response, not the detection:

| Condition, evaluated in the settlement currency | Severity |
|---|---|
| Difference is exactly zero | No break |
| Difference is below 0.1 per cent of the file total and below 100 units of the settlement currency | `warn`, recorded and reviewed |
| Difference is at or above either threshold | `error`, fails the reconciliation gate and escalates |

The relative term keeps a large file from being waved through on an absolute number. The
absolute term keeps a tiny file from escalating over a rounding difference. Both are expressed
in the currency the comparison happened in, so the thresholds do not move with the exchange
rate.

**Source columns.** `sl_card_settlements.file_total_amount`,
`sl_card_settlements.settlement_currency`, `sl_card_settlements.settlement_date`,
`sl_card_settlements.network`, `fct_gl_entries.amount`, `fct_gl_entries.currency_code`,
`fct_gl_entries.posting_date`.

**Used by.** Q15.

## Freshness SLA per source

**Rule.** A source is fresh on date D when its most recent successful batch landed within the
window below, measured from the batch's end time recorded in `ops`. Compliance is the share
of days on which every expected batch for that source was fresh.

| Source | Expected by | Window |
|---|---|---|
| Core banking | 06:00 UTC daily | 24 hours plus a 2 hour grace |
| ECB FX rates | Around 16:00 CET on ECB working days, which is 15:00 UTC in summer and 14:00 UTC in winter | 3 hours after the publication time in force on that date; the window is computed from CET and never from a fixed UTC hour |
| Card settlement files | 08:00 UTC daily | 24 hours plus a 4 hour grace, because the file is produced by a third party |
| Sanctions list | 09:00 UTC each Monday | 8 days |
| FRED macro series | 09:00 UTC on the 15th of the month | 35 days |

**Source columns.** `ops.batch_registry.source`, `ops.batch_registry.ended_at`,
`ops.batch_registry.status`, `ops.freshness_sla.window_hours`.

**Used by.** Q17.

## Quality check severity

**Rule.** Severity is a property of the check, declared once where the check is defined, and
is never decided per run.

| Severity | Meaning | Effect |
|---|---|---|
| `error` | The data is wrong in a way that makes downstream numbers wrong | Fails the gate, blocks promotion of that layer, pages the owner |
| `warn` | The data is suspicious but usable | Recorded in `dq`, does not block, reviewed in the trend |
| `info` | A measurement kept for trend only, such as a row count drift | Recorded in `dq`, never blocks |

Pass rate is `passing checks / executed checks` per layer and severity per day. A check that
errored before it could evaluate counts as executed and failing, not as absent, so an
outage cannot look like a clean day.

**Source columns.** `dq.check_results.check_name`, `dq.check_results.severity`,
`dq.check_results.status`, `dq.check_results.layer`, `dq.check_results.executed_at`.

**Used by.** Q18.

## Unrecognised device

**Rule.** A device is unrecognised for a customer when its fingerprint does not appear in
that customer's successful login sessions in the trailing 90 days before the session in
question. A customer's first ever login is therefore always from an unrecognised device, and
is flagged as such rather than excluded.

Channel is the session channel: mobile app, web, or API.

**Source columns.** `sl_login_sessions.device_fingerprint`,
`sl_login_sessions.customer_id`, `sl_login_sessions.started_at`,
`sl_login_sessions.auth_outcome`, `sl_login_sessions.channel`.

**Used by.** Q19.
