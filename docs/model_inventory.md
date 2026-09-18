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

| Object | Grain | Upstream | Serves | Milestone |
|---|---|---|---|---|
| `seed_mcc_codes` | One row per merchant category code | `mcc_codes` reference data, version controlled | Q4 | M5 |
| `seed_interchange_rates` | One row per card product class, merchant region and MCC band | Regulated EEA rates plus the assumptions recorded in metric definitions | Q4, Q6 | M5 |
| `seed_country_currency` | One row per country | ISO country and currency reference | Q1, Q13, Q14 | M5 |
| `seed_risk_bands` | One row per risk band | Underwriting band thresholds | Q8 | M5 |

Seeds are their own source, so the coverage rule treats them as reference-only by definition.

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
| `sl_gl_entries` | One row per ledger line | `br_corebank__gl_entries` | Q15, Q16 |
| `sl_fraud_alerts` | One row per alert version | `br_corebank__fraud_alerts` | Q10 |
| `sl_login_sessions` | One row per session | `br_corebank__login_sessions` | Q19 |
| `sl_products` | One row per product version (SCD2) | `br_corebank__products` | Q1, Q5 |
| `sl_agent_locations` | One row per agent location version (SCD2) | `br_corebank__agent_locations` | Q11 |
| `sl_fx_rates` | One row per currency per calendar date | `br_ecb__fx_rates`, gap-filled | Every EUR conversion, Q14 |
| `sl_sanctions_entities` | One row per sanctioned entity per list version | `br_opensanctions__entities` | Q12 |
| `sl_card_settlements` | One row per settlement file line | `br_cardnet__settlements` | Q15 |
| `sl_macro_indicators` | One row per series and period | `br_fred__series` | Reference-only at M5; consumer decided with the funding cost assumption before M6 |

`sl_card_settlements` and `sl_macro_indicators` come from sources whose entities are not in the
M0 entity inventory, which lists the core banking entities plus `fx_rates` and
`sanctions_entities`. Their entity-level and column-level definitions arrive with spec 002.

## Gold dimensions

Built at M6. SCD2 dimensions carry `_valid_from`, `_valid_to`, `_is_current`, a surrogate key
and a durable business key, per the conventions.

| Object | Grain | Upstream | Serves |
|---|---|---|---|
| `dim_customer` | One row per customer version | `sl_customers`, `sl_customer_addresses` | Q1, Q2, Q6, Q8, Q11, Q12, Q19 |
| `dim_account` | One row per account version | `sl_accounts`, `sl_products` | Q1, Q3, Q13 |
| `dim_card` | One row per card version | `sl_cards`, `sl_products` | Q4, Q10 |
| `dim_merchant` | One row per merchant version | `sl_merchants`, `seed_mcc_codes` | Q4 |
| `dim_loan_product` | One row per loan product version | `sl_products` | Q5, Q7, Q8 |
| `dim_agent_location` | One row per agent location version | `sl_agent_locations` | Q11 |
| `dim_currency` | One row per currency | `seed_country_currency` | Q3, Q14 |
| `dim_detection_rule` | One row per fraud detection rule version | `sl_fraud_alerts` | Q10 |
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
| `watermarks` | `ops` | One row per source entity | Ingestion DAGs | M4 |
| `freshness_sla` | `ops` | One row per source | Configuration, from `quality/` | M7 |
| `check_results` | `dq` | One row per check run | Quality gates | M7 |
| `quarantine_<entity>` | `bronze` | One row per rejected source row | Ingestion DAGs | M4 |
| `pii_vault` | `meta` | One row per token | Extraction tasks | M4 |
| `contract_versions` | `meta` | One row per contract version | Ingestion DAGs | M4 |
| `erasure_log` | `meta` | One row per erasure request | `gov_erasure` | M8 |

## Derived flags

| Flag | Defined in | Derived in | Serves |
|---|---|---|---|
| `is_customer_initiated` | [metric_definitions.md](metric_definitions.md) | `sl_transactions`, `sl_payments` | Q1, Q6 |
| `fx_is_carried` | [architecture.md](architecture.md) | `sl_fx_rates` | Every converted amount |
| `fx_is_missing` | [architecture.md](architecture.md) | Conversion macro | Every converted amount |
| `_is_current` | [conventions.md](conventions.md) | Every SCD2 silver model | History-aware joins |
