# Model inventory

Every object the platform plans to build, with its layer, grain, upstream inputs and the
business questions it serves. Nothing here exists yet; each row carries the milestone that
builds it.

The inventory exists so that the coverage rule in
[business_questions.md](business_questions.md) can be checked in both directions: no source
entity without a consumer, and no object referenced in
[metric_definitions.md](metric_definitions.md) without a home. An object named in a metric
definition but absent from this table is a gap, not a detail.

Naming follows [conventions.md](conventions.md).

## Seeds

There are none. The four seeds this table carried at M0 were dropped at M2: `seed_mcc_codes`,
`seed_interchange_rates`, `seed_country_currency` and `seed_risk_bands` all duplicated data
that now lives in the source database's `ref` schema, and two homes for one rate table is two
numbers that can disagree. The reference tables are ingested like any other source entity.

Reference data is version controlled either way; the difference is that it is now version
controlled in one place, `infra/docker/postgres-source/seed/`, and reaches the warehouse
through the same extraction path as everything else.

## Silver

One model per source entity, at entity grain, built at M5 unless noted.

| Object | Grain | Upstream | Serves |
|---|---|---|---|
| `sl_customers` | One row per customer version (SCD2) | `br_corebank__customers` | Q1, Q2, Q6, Q8, Q11, Q12, Q19 |
| `sl_customer_addresses` | One row per customer address version (SCD2) | `br_corebank__customer_addresses` | Q1, Q13 |
| `sl_accounts` | One row per account version (SCD2) | `br_corebank__accounts` | Q1, Q3, Q13 |
| `sl_account_holders` | One row per account and holder version (SCD2) | `br_corebank__account_holders` | Q3, Q6, Q11 |
| `sl_cards` | One row per card version (SCD2) | `br_corebank__cards` | Q4, Q10 |
| `sl_merchants` | One row per merchant version (SCD2) | `br_corebank__merchants` | Q4 |
| `sl_transactions` | One row per transaction | `br_corebank__transactions` | Q1, Q4, Q6, Q10, Q11, Q12, Q15, Q19 |
| `sl_payments` | One row per payment instruction version | `br_corebank__payments` | Q12, Q13 |
| `sl_loans` | One row per loan version (SCD2) | `br_corebank__loans` | Q5, Q7, Q8, Q9 |
| `sl_loan_applications` | One row per application version | `br_corebank__loan_applications` | Q8, Q9 |
| `sl_loan_installments` | One row per loan and installment version | `br_corebank__loan_installments` | Q7, Q8, Q9 |
| `sl_gl_transactions` | One row per posting batch | `br_corebank__gl_transactions` | Q16 |
| `sl_gl_entries` | One row per ledger line | `br_corebank__gl_entries` | Q15, Q16 |
| `sl_fraud_alerts` | One row per alert version | `br_corebank__fraud_alerts` | Q10 |
| `sl_login_sessions` | One row per session | `br_corebank__login_sessions` | Q19 |
| `sl_fraud_rules` | One row per detection rule version (SCD2) | `br_corebank__fraud_rules` | Q10 |
| `sl_account_types` | One row per account type version (SCD2) | `br_corebank__account_types` | Q1, Q3 |
| `sl_card_products` | One row per card product version (SCD2) | `br_corebank__card_products` | Q4 |
| `sl_loan_products` | One row per loan product version (SCD2) | `br_corebank__loan_products` | Q5 |
| `sl_agent_locations` | One row per agent location version (SCD2) | `br_corebank__agent_locations` | Q11 |
| `sl_fx_rates` | One row per currency per calendar date | `br_ecb__fx_rates`, gap-filled | Every EUR conversion, Q14 |
| `sl_sanctions_entities` | One row per sanctioned entity per list version | `br_opensanctions__entities` | Q12 |
| `sl_card_settlements` | One row per settlement file line | `br_cardnet__settlements` | Q15 |
| `sl_macro_indicators` | One row per series and period | `br_fred__series` | Reference-only at M5; consumer decided with the funding cost assumption before M6 |

`sl_card_settlements` and `sl_macro_indicators` come from sources whose entities are not in the
M0 entity inventory, which lists the core banking entities plus `fx_rates` and
`sanctions_entities`. Their entity-level and column-level definitions arrive at **M4** with the
contracts for the card settlement file and the FRED feed. This table previously said spec 002,
which was wrong: spec 002's scope is the core banking system, and those two are separate
sources with separate contracts.

**The `products` entity is gone.** The M0 inventory carried one `sl_products` model over one
`products` source entity covering account, card and loan products. Spec 002 replaced it with
three reference tables, because the three have different attributes and different consumers and
one table would have been a union of three disjoint column sets. The three silver models above
replace it.

**The remaining reference tables** are consumed as conformed attributes of the dimensions that
use them rather than as silver models of their own. Each is named in
[data_dictionary.md](data_dictionary.md) with the dimension that consumes it, so the coverage
rule is satisfied in both directions without twenty-eight silver models. Whether an individual
reference table materialises as its own silver model or is joined in as attributes of the
dimension that consumes it is an M5 decision; what M2 fixes is that none of them is
unaccounted for.

## Gold dimensions

Built at M6. SCD2 dimensions carry `_valid_from`, `_valid_to`, `_is_current`, a surrogate key
and a durable business key, per the conventions.

| Object | Grain | Upstream | Serves |
|---|---|---|---|
| `dim_customer` | One row per customer version | `sl_customers`, `sl_customer_addresses` | Q1, Q2, Q6, Q8, Q11, Q12, Q19 |
| `dim_account` | One row per account version | `sl_accounts`, `sl_account_types` | Q1, Q3, Q13 |
| `dim_card` | One row per card version | `sl_cards`, `sl_card_products` | Q4, Q10 |
| `dim_merchant` | One row per merchant version | `sl_merchants`, `ref.mcc_codes` attributes | Q4 |
| `dim_loan_product` | One row per loan product version | `sl_loan_products` | Q5, Q7, Q8 |
| `dim_agent_location` | One row per agent location version | `sl_agent_locations` | Q11 |
| `dim_currency` | One row per currency | `ref.currencies` | Q3, Q14 |
| `dim_detection_rule` | One row per fraud detection rule version | `sl_fraud_rules` | Q10 |
| `dim_date` | One row per calendar date | Generated | All time series questions |
| `bridge_account_holder` | One row per account and holder | `sl_account_holders` | Q3, Q6, Q11 |

## Gold facts

Built at M6.

| Object | Grain | Upstream | Serves |
|---|---|---|---|
| `fct_transactions` | One row per transaction | `sl_transactions`, `sl_fx_rates` | Q1, Q4, Q6, Q10, Q11, Q12, Q19 |
| `fct_payments` | One row per payment instruction | `sl_payments`, `sl_fx_rates` | Q12, Q13 |
| `fct_account_balance_daily` | One row per account per day | `sl_accounts`, `sl_transactions` | Q3, Q14 |
| `fct_loan_balance_daily` | One row per loan per day | `sl_loans`, `sl_loan_installments` | Q5, Q7 |
| `fct_loan_installments` | One row per loan and installment | `sl_loan_installments` | Q7, Q8 |
| `fct_loan_applications` | One row per application | `sl_loan_applications`, `sl_loans` | Q8, Q9 |
| `fct_gl_entries` | One row per ledger line | `sl_gl_entries` | Q15, Q16 |
| `fct_fraud_alerts` | One row per alert | `sl_fraud_alerts` | Q10 |
| `fct_login_sessions` | One row per session | `sl_login_sessions` | Q19 |
| `fct_card_settlements` | One row per settlement file line | `sl_card_settlements` | Q15 |
| `fct_fx_rates_daily` | One row per currency per date | `sl_fx_rates` | Q14, and every conversion |
| `fct_sanctions_screening` | One row per screened counterparty, match and list version | `sl_sanctions_entities`, `sl_payments` | Q12 |

## Marts

Built at M6, except the operational marts, which arrive at M7 with the quality gates. Each
mart declares and tests the grain stated in [business_questions.md](business_questions.md).

| Object | Question | Milestone |
|---|---|---|
| `mart_growth_account_activity` | Q1 | M6 |
| `mart_growth_cohort_retention` | Q2 | M6 |
| `mart_growth_deposit_balances` | Q3 | M6 |
| `mart_revenue_interchange` | Q4 | M6 |
| `mart_revenue_net_interest` | Q5 | M6 |
| `mart_revenue_per_customer` | Q6 | M6 |
| `mart_credit_delinquency` | Q7 | M6 |
| `mart_credit_underwriting` | Q8 | M6 |
| `mart_credit_loan_lifecycle` | Q9 | M6 |
| `mart_fraud_alert_performance` | Q10 | M6 |
| `mart_aml_structuring_candidates` | Q11 | M6 |
| `mart_aml_sanctions_exposure` | Q12 | M6 |
| `mart_payments_cross_border` | Q13 | M6 |
| `mart_fraud_device_risk` | Q19 | M6 |
| `mart_treasury_fx_exposure` | Q14 | M6 |
| `mart_control_settlement_reconciliation` | Q15 | M7 |
| `mart_control_gl_integrity` | Q16 | M7 |
| `mart_ops_freshness` | Q17 | M7 |
| `mart_ops_quality` | Q18 | M7 |

## Platform tables

Not dbt models. They are written by the orchestration and quality layers and read by the
operational marts, which is the exception to gold reading only silver.

| Object | Schema | Grain | Written by | Milestone |
|---|---|---|---|---|
| `batch_registry` | `ops` | One row per extraction batch | Ingestion DAGs | M4 |
| `extract_watermark` | `ops` | One row per source system and entity | Ingestion DAGs | M4 |
| `source_reconciliation` | `ops` | One row per source system, entity and source day | Ingestion DAGs | M4 |
| `task_failure` | `ops` | One row per failed task instance | `on_failure_callback` | M4 |
| `freshness_sla` | `ops` | One row per source | Configuration, from `quality/` | M7 |
| `check_results` | `dq` | One row per check run | Quality gates | M7 |
| `quarantine_log` | `dq` | One row per rejected source record | Ingestion DAGs | M4 |
| `pii_vault` | `meta` | One row per distinct identifier value, keyed on its token | Extraction tasks | M4 |
| `contract_version` | `meta` | One row per entity and contract version | Ingestion DAGs | M4 |
| `schema_drift_log` | `meta` | One row per drift observation | Ingestion DAGs | M4 |
| `erasure_log` | `meta` | One row per erasure request | `gov_erasure` | M8 |

Four of these names moved at M4 and the specification's names won, so this table is the one
that changed. `ops.watermarks` became `ops.extract_watermark`; `meta.contract_versions` became
`meta.contract_version`; and sixteen `bronze.quarantine_<entity>` tables became one
`dq.quarantine_log`, because `dq` is where quality results live by `conventions.md` and
sixteen tables of one shape is sixteen places for a schema to drift apart. `meta.pii_vault` is
keyed on the token rather than on the token with its entity and column: one raw value occurs
in more than one entity — measured, 949 payments in the `ci` book carry a `counterparty_name`
equal to some customer's `full_name` — and keying on the triple would hold two rows for one
person.

**One consequence of additive drift belongs to silver and is recorded here because this is
where its models are declared.** After the generator's scripted additive event fires, a
`core.merchants` row can be revised in `merchant_risk_score` alone, which is the column the
contract omits. `br_corebank__merchants` therefore receives a version in which every
contract-described column equals its predecessor's. `sl_merchants` must expect a version with
no visible differences: it is a real source update, not a duplicate, and deduplicating it away
would lose the fact that the row moved.

## Derived flags

| Flag | Defined in | Derived in | Serves |
|---|---|---|---|
| `is_customer_initiated` | `ref.transaction_types`, `ref.payment_types` | `sl_transactions`, `sl_payments` | Q1, Q6 |
| `fx_is_carried` | [architecture.md](architecture.md) | `sl_fx_rates` | Every converted amount |
| `fx_is_missing` | [architecture.md](architecture.md) | Conversion macro | Every converted amount |
| `_is_current` | [conventions.md](conventions.md) | Every SCD2 silver model | History-aware joins |
