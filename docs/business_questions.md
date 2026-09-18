# Business questions

The acceptance baseline for the gold layer. A gold model exists to answer one or more of
these; a model that answers none of them needs a question added here first, and a question
that no model answers is an open gap.

Grain is the grain the question requires, which is the grain the answering mart declares and
tests. Every term below that could be computed more than one defensible way is pinned in
[metric_definitions.md](metric_definitions.md), which the answering model implements.

## Growth and customer

| # | Question | Persona | Target mart | Grain |
|---|---|---|---|---|
| 1 | Monthly active accounts and month-over-month growth by country and product | Head of Growth | `mart_growth_account_activity` | One row per month, country and product |
| 2 | Cohort retention of customers by signup month over the first 12 months | Head of Growth | `mart_growth_cohort_retention` | One row per signup cohort month and months since signup (0 to 12) |
| 3 | Total customer deposit balance trend by currency and account type | Head of Retail Deposits | `mart_growth_deposit_balances` | One row per month end, currency and account type |

## Revenue

| # | Question | Persona | Target mart | Grain |
|---|---|---|---|---|
| 4 | Interchange revenue by MCC category, country and month | Head of Cards | `mart_revenue_interchange` | One row per month, MCC category and merchant country |
| 5 | Net interest income proxy on the loan book by product and vintage | Head of Lending | `mart_revenue_net_interest` | One row per month, loan product and origination vintage month |
| 6 | Fee and interchange revenue per active customer per month | CFO | `mart_revenue_per_customer` | One row per month and customer |

## Credit risk

| # | Question | Persona | Target mart | Grain |
|---|---|---|---|---|
| 7 | 30/60/90-day delinquency rate by origination vintage | Chief Risk Officer | `mart_credit_delinquency` | One row per reporting month and origination vintage month |
| 8 | Loan approval rate and subsequent default rate by applicant risk band | Head of Credit Risk | `mart_credit_underwriting` | One row per application month and risk band |
| 9 | Loan lifecycle duration: application to disbursement to first payment | Head of Lending Operations | `mart_credit_loan_lifecycle` | One row per loan application |

## Fraud and AML

| # | Question | Persona | Target mart | Grain |
|---|---|---|---|---|
| 10 | Alert precision and false-positive rate by detection rule and month | Head of Fraud | `mart_fraud_alert_performance` | One row per month and detection rule |
| 11 | Customers with multiple sub-threshold cash deposits within a rolling 7-day window (structuring candidates) | Money Laundering Reporting Officer | `mart_aml_structuring_candidates` | One row per customer and window end date that breaches the rule |
| 12 | Customers transacting with counterparties matched to the sanctions list | Money Laundering Reporting Officer | `mart_aml_sanctions_exposure` | One row per customer, matched sanctioned entity and sanctions list version |
| 13 | Cross-border payment volume and decline rate by corridor | Head of Payments | `mart_payments_cross_border` | One row per month, origin country and destination country |
| 19 | Share of transactions preceded by a login from an unrecognised device within 24 hours, by channel and month | Head of Fraud | `mart_fraud_device_risk` | One row per month and channel |

## Treasury and control

| # | Question | Persona | Target mart | Grain |
|---|---|---|---|---|
| 14 | Net FX exposure by currency, daily | Treasurer | `mart_treasury_fx_exposure` | One row per date and currency |
| 15 | Settlement breaks: card network file totals versus internal ledger totals | Head of Financial Control | `mart_control_settlement_reconciliation` | One row per settlement date and card network file |
| 16 | Daily general ledger integrity: total debits equal total credits | Financial Controller | `mart_control_gl_integrity` | One row per posting date |

## Data operations

| # | Question | Persona | Target mart | Grain |
|---|---|---|---|---|
| 17 | Freshness SLA compliance per source per day | Data Platform Owner | `mart_ops_freshness` | One row per date and source |
| 18 | Data quality check pass rate trend by layer and severity | Data Platform Owner | `mart_ops_quality` | One row per date, layer and severity |

## Coverage

Checked in both directions when a milestone closes, rather than argued about per model.

**Nothing unconsumed.** Every source entity in [data_dictionary.md](data_dictionary.md) feeds
at least one silver model. Every silver model either feeds a gold model or is documented in
its schema file as reference-only, with the reason it exists. An entity that reaches the
warehouse and is consumed by nothing is a design defect: either a question is missing, or the
entity should not be ingested. Question 19 exists because `login_sessions` was such an entity,
and the correct response was to state the question it answers rather than to quietly keep
loading it.

A `ref` table is not exempt from this. Exempting reference data would reopen exactly the
orphan problem the rule closes, on the twenty-eight tables least likely to be looked at. The
rule applies in the form the reference layer makes sense in: **a `ref` table either becomes a
conformed dimension in its own right, or is consumed as attributes of one, and either way it
is named in [model_inventory.md](model_inventory.md) and
[data_dictionary.md](data_dictionary.md) with the dimension that consumes it.** Nothing is
unaccounted for, and nothing needs twenty-eight silver models.

**Nothing homeless.** Every object named in [metric_definitions.md](metric_definitions.md) or
in a mart specification appears in [model_inventory.md](model_inventory.md) with a layer, a
grain and the milestone that builds it. A metric that reads from a table nobody has planned is
a rule that cannot be implemented, and it is a defect in the same way that an unconsumed
entity is.
