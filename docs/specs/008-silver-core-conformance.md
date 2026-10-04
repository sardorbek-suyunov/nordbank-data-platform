# 008 — Silver: Core and Reference Conformance

Status: Approved
Version: 2
Supersedes: version 1 (as drafted for planning; never committed)
Depends on: 000–007, and the FX history fix

## Goal
Build silver for the core banking and reference entities, and for FX rates:
deduplicated, historised where history is read, soft deletes applied, amounts
converted to EUR exactly and with full provenance, and quasi-identifiers
prepared for generalisation. Silver is a pure function of bronze. A final EUR
conversion is never restated by any rebuild.

## Context
Bronze is registry-filtered views (ADR 0018), not deduplicated. Identifiers are
tokens, and pseudonymous keys are clear.

Planning measured the facts this specification rests on:
- no key and `updated_at` pair carries two different states;
- at most one version per key per day;
- 28 of 29 `ref` tables carry a wall-clock seed stamp;
- 99.8 per cent of account changes move only the balance;
- no backdated address changes exist in the data;
- DuckDB 1.5.5 returns DOUBLE from decimal division;
- rates are quoted as currency per euro.

Settlement, sanctions and FRED silver are 009. Entity resolution is 010.

## Scope

### 1. Deduplication
- A version is a key and its `updated_at`. Re-reads of a version collapse
  deterministically, with the earliest batch winning.
- A test asserts that rows sharing key and `updated_at` agree on every contract
  column. Planning measured zero disagreements; the test keeps it so.

### 2. Historisation
**SCD2:**
- `customers`, `cards`, `merchants`, `accounts`, `account_holders`, `loans`,
  `loan_installments`;
- `customer_addresses`, with business validity as in section 3;
- all `ref` tables and `agent_locations`, generated.

**Latest state:** `transactions`, `payments`, `loan_applications`,
`fraud_alerts`, `login_sessions`, `gl_transactions`, `gl_entries`.

**The version rule:** a new version opens when any column in the model's
projection changes, or when no contract column changed at all, which is a
change the contract does not describe (the merchants case).
- A change confined to a measure excluded from the projection opens no
  version.
- A measure is excluded only when no question reads its history from that
  entity.
- `accounts.current_balance_amount` is excluded, and lands in
  `sl_account_balance_observations`: one row per account per observed
  balance, for M7 to reconcile against derived balances.
- `loan_installments.paid_amount` stays in the projection, because Q7 reads
  its history.

**Columns:** `_valid_from`, `_valid_to` and `_is_current`.
- Later versions open at their `updated_at`.
- The first version of every key opens at `1900-01-01T00:00:00Z`.
- The current version closes at a fixed far-future instant, never null.

**What the epoch means:** the first observed state is backdated as the
earliest known state, not the true state. Changes before the first extraction
are invisible, because the historical load writes each entity's state as of
the anchor.

### 3. Two clocks on source-native history
- `customer_addresses` records business validity with an inclusive
  `valid_to_date`. Silver exposes it half-open: from `valid_from_date` to
  `valid_to_date` plus one day.
- A fact joins on business validity, read from the current system-time
  version.
- Consequence, stated: a late correction moves a past fact's geography. A
  bitemporal join is the rejected alternative.
- `ref.interchange_rates` validity is treated the same way.
- A backdated change is tested by a planted fixture, because the data
  contains none.

### 4. Soft deletes
- **SCD2:** `is_deleted` closes the final version, leaving no current version;
  history remains.
- **Latest state:** deleted rows are excluded.
- `ref` rows carry `is_active`, and an inactive row is retained.
- A soft delete is not erasure, which is the vault at M8.
- The rule is stated once, in `architecture.md`. `conventions.md` points to
  it.

### 5. Reference entities
- Every `ref` table has a silver model, generated from the contracts, with a
  `CHECK=1` mode in CI. Gold reads silver only.
- `model_inventory.md` lists them, and corrects `dim_merchant` and
  `dim_currency`, which currently read `ref` directly.

### 6. FX rates and conversion
- **`sl_fx_rates`:** one row per currency per calendar date, from the first
  landed rate date to the later of the last rate date and the last fact date.
  It is gap-filled forward and carries `fx_is_carried` and
  `fx_is_provisional`.
- **Conversion:** at the latest rate whose publication instant is at or
  before the fact's business instant.
  - The business instant is `booked_at` for transactions and `initiated_at`
    for payments.
  - The publication instant is the rate date at 16:00 Europe/Berlin, computed
    by one macro and verified across both daylight-saving changes.
- **Provisional:** `fx_is_provisional` is true exactly when the chosen rate is
  the latest landed for its currency and the business instant falls after the
  publication instant of the next weekday after the chosen rate's date. EUR
  and missing rows are never provisional.
- **The promise:** a final conversion is never restated, and only a
  provisional one may be.
- **Provenance:** `fx_rate`, `fx_rate_date`, `fx_is_carried`,
  `fx_is_missing` and `fx_is_provisional`.
  - EUR converts at 1, with a null `fx_rate_date`, allowed only for EUR.
  - A missing rate gives a null `amount_eur`, never zero and never the
    original amount.
- **Arithmetic:** EUR is the amount divided by the rate, by exact HUGEINT
  integer arithmetic with the rate widened before scaling.
  - Rounding is half away from zero to four decimal places, consistent with
    Regulation (EC) No 1103/97.
  - No floating point in any intermediate expression.
- **Consistency:** `sl_fx_rates` agrees with the conversion rule evaluated at
  23:59:59 UTC for every currency and date, provisional flag included.

### 7. Conformance of values
- Representation is standardised and the business is never corrected.
  - Free-text non-identifier columns gain a normalised column beside the raw
    one: merchant names.
- Nothing is imputed. Expected nulls, contradicted KYC statuses and mod-97
  failures are left for M7.
- Identifier tokens are never normalised.

### 8. Generalisation
- Quasi-identifiers stay in the clear in silver.
- `sl_customer_age_bands` and `sl_customer_tenure_bands`: per customer, the
  half-open intervals during which each band applies, contiguous, derived from
  `date_of_birth` and `signup_date`.
- Postal district is `left(postal_code, 2)`.
- Band edges live in one dbt seed. It duplicates nothing, and the inventory
  records it as the one seed and why.
- **A gold column guard,** built like `bronze_guard`, fails any gold model
  selecting a column classified `quasi-identifier`, reading the
  classifications, not a hand list. It is proven by a `dbt-prove` fixture.

### 9. Corrections
- **Surrogate keys** hash the pseudonymous key, plus `_valid_from` for SCD2.
  Correct `traceability.md` and the `data_dictionary.md` description of
  `customer_reference`.
- **Q5** uses the loan's own rate. Correct `metric_definitions.md` and gap 6.
  Planning measured no numeric effect at `ci`.
- **`traceability.md`:**
  - the gap 10 rows map to the settlement contract columns;
  - platform-table rows are checked against the DDL;
  - line 146's claim that no `ref` table needs a silver model is corrected.
- **The conversion rule and the soft-delete rule** are restated once, in
  `architecture.md`. `conventions.md` points to it.

### 10. Build and orchestration
- Silver models are tables, rebuilt in full. Incremental is decided at M6.
- **Determinism:** two builds from the same bronze are identical by content
  hash per model.
- `transform_silver` is pooled, triggered by `transform_bronze` completing,
  and covered by the backfill's build-once rule and final wait.
- Every model declares and tests its grain. Every structural test states a
  minimum cardinality.

### 11. Documents
- **ADR: conversion at the publication instant.**
  - *Rejected:* as-of the date, which restates on rebuild; freezing converted
    rows, which cannot be reproduced.
  - *Negative consequences:* the nominal 16:00 instant can differ from the
    real publication by minutes; and provisional conversions can restate.
  - Cite Regulation (EC) No 1103/97 for rounding.
- **ADR: historisation.**
  - the version rule and excluded measures;
  - the epoch and what it means;
  - two clocks and the bitemporal alternative;
  - soft deletes;
  - at most one version per day, a property of extraction.
- Known gap: the reference seed's wall-clock stamp.
- `model_inventory.md`, `project_state.md`.

## Out of scope
009 and 010. Gold. `dq` checks. The physical-delete reconciler. Incremental
models.

## Acceptance criteria
1. Every in-scope entity has a silver model with a declared, tested grain.
2. Rows sharing key and `updated_at` agree on every contract column.
3. The version rule holds:
   - merchants' no-visible-change versions appear;
   - balance-only account changes open no version;
   - `sl_account_balance_observations` holds every observed balance.
4. Every fact event resolves to exactly one version of every dimension-bound
   entity, with no gaps or overlaps per key.
5. A planted backdated change joins by business validity.
6. Soft-deleted SCD2 entities have no current version and keep their history.
   Soft-deleted latest-state rows are absent.
7. Every `ref` table has a generated silver model, and `CHECK=1` passes and
   fails on a hand edit.
8. `sl_fx_rates` spans the stated window, with carried and provisional flags.
9. Conversion follows the instant rule. The count differing from the date
   rule is reported.
10. No intermediate expression in conversion is floating point, shown by
    type.
11. 100.00 USD at 1.1426 is 87.5197 EUR. 1.23 at 1.6 is 0.7688, and its
    negative is -0.7688.
12. Missing rates give null `amount_eur`. EUR converts at 1.
13. `sl_fx_rates` agrees with the rule at end of day, provisional flag
    included.
14. Holding back one day's FX, building, landing it and rebuilding moves
    provisional rows only.
15. Band intervals are contiguous per customer, and the gold column guard
    fails its fixture.
16. Two builds from the same bronze are identical.
17. `transform_silver` runs in the pool and follows the build-once rule.
18. The section 9 corrections and section 11 documents are in place.
19. `dbt build --warn-error` passes locally and in CI.
20. All four checks pass; a pull request on `feat/M5-silver-core`.

## Changelog
- The first version opens at the epoch, because the reference seed is stamped
  with wall-clock time and the historical load writes final state. Measured:
  64 per cent of transactions found no account version otherwise.
- A projection-based version rule with excluded measures, because 99.8 per
  cent of account changes move only the balance. The balance becomes its own
  fact.
- Soft-deleted latest-state rows are excluded, not flagged.
- FX history is a prerequisite fix, because 64 per cent of non-EUR
  transactions predated any landed rate.
- Payments convert at `initiated_at`, because `booked_at` is null on 1,074
  of them.
- `fx_is_provisional` is added, because a rebuild before a rate lands would
  otherwise restate.
- Exact integer arithmetic, rounding half away from zero, because decimal
  division returns DOUBLE and was wrong on 3,171 of 200,000 cases.
- Business validity is half-open, and the backdated change is tested by a
  fixture, because the data has none.
- The gold column guard replaces a criterion that contradicted silver keeping
  quasi-identifiers.
- The SCD2 list is fixed from measurement.
- Rules are stated once, in `architecture.md`, with `conventions.md` pointing
  to them.

## Amendments

### 2026-10-03 — rulings on the drafts, before implementation

Three rulings on the macro drafts, recorded here because the delivered macros follow them rather
than the drafts. Generated `ref` models pass `deleted='false'`, and a test shows an inactive `ref`
row current with `is_active` false. `scd2` compares with `is distinct from`, column by column,
generated from the projection's column list, never `hash()`: a collision would merge two real
versions. The provisional flag and the gap fill follow the macros.

### 2026-10-03 — the projection is read from the relation

Section 2 names a projection per model. It is not written down per model: `scd2` reads the bronze
relation's columns when the model runs and takes every one except the key, the audit columns, the
deleted flag and the excluded measures, so a contract change cannot leave a stale list behind.
The only list a model states is its excluded measures, `current_balance_amount` on accounts.

### 2026-10-03 — what a silver model carries

The specification is silent on it. A model that mirrors an entity carries its key, its
projection, the version's `created_at` and `updated_at` and the earliest `_batch_id` that read it.
It does not carry `is_deleted`, false on every row silver keeps; an excluded measure, wrong on
every version but the one that opened it; or the audit columns that describe a read. Every
silver contract is enforced, and the properties of every mirroring model are generated by
`make silver-generate`, hand-written SQL included, which goes beyond section 5's thirty generated
models: one source for columns, types, descriptions and classifications.

### 2026-10-03 — `fx_is_carried` on a fact, and `sl_fx_rates` per currency

Section 6 defines `fx_is_carried` on `sl_fx_rates` and not on a fact. On a fact it means the
rate's date is not the business instant's UTC date, which is what `sl_fx_rates` says at 23:59:59
and keeps the two in agreement; a fact before the day's publication is therefore carried, 3,968 of
6,163 non-EUR transactions on the sixty-one-day history. `sl_fx_rates` spans each currency from
its own first landed rate rather than from one first date for all; every currency's first date is
2024-01-23 here, so nothing differs.

### 2026-10-03 — the resolution test, and a gap it refuses

Criterion 4 is `silver_resolves_one_version`, generated on 29 dimension columns across the seven
latest-state facts, each resolved at the fact's business instant (the start of the posting date,
UTC, for the ledger). Its first version counted versions per value and instant, so two facts
sharing both read as an overlap; real data caught it, 1,530 `api` transactions, and it now counts
per fact. `silver_scd2_intervals` fails on a gap between versions, so a key soft-deleted and later
restored would fail it: planning measured no restoration, and the first one is a decision.

### 2026-10-03 — choices section 8 left open

The band edges: age under 18, 18-24, 25-34, 35-44, 45-54, 55-64, 65+; tenure under one year,
1-2, 2-5, 5+ years. A band model reads the customer's latest version. Its interval dates encode
the date of birth or signup date, the first exactly, and are classified quasi-identifier, so gold
joins on them and cannot select them. `postal_district` is classified non-personal, being the
generalisation. The gold column guard compares a gold model's declared columns with its parents'
quasi-identifier columns and requires every gold contract enforced; it cannot see a column
renamed on the way, which needs column-level lineage.

### 2026-10-03 — `make dbt-build` builds everything

It built bronze only. Criterion 19 asks for `dbt build --warn-error` locally and in CI, so it is
now the whole project: bronze, the seed, silver and every test, the macro tests included.

### 2026-10-03 — bronze builds only the tests that read bronze alone

The stack job's first run on the branch failed at the backfill's bronze build: `transform_bronze`
selects `path:models/bronze`, and dbt's default eager indirect selection took in the 86 silver
tests that read bronze beside a silver model, which error while silver does not exist. The
throwaway stack hid it, its silver having been built already. The bronze build now passes
`--indirect-selection cautious`; silver's build keeps the default and runs them. Reproduced on a
copy of the throwaway warehouse with silver dropped: 40 errors before, 320 nodes passing after.

### 2026-10-04 — provisional against the feed's latest publication

Section 6's provisional rule read "the latest landed for its currency", and on the history it left
261 BGN rows of `sl_fx_rates` provisional for good: BGN's last rate is 2025-12-31, Bulgaria having
adopted the euro, so no later BGN rate will ever land. Ruled on review: a conversion is
provisional when the chosen rate's date is the latest publication date landed for the feed,
across every currency, and the instant is after the next weekday's publication; once any later
publication has landed, a currency missing from it is final, a holiday gap or a discontinued
currency alike. ADR 0019 is revised in place, its milestone being open, and states
`fx_is_carried` exactly. Re-measured on the throwaway stack: BGN provisional rows 0, and none in
`sl_fx_rates`; the rule restated in `assert_fx_rates_agree_with_the_conversion_rule` agrees on
every row, and the per-currency rule fails it on exactly the 261 BGN rows. Criterion 14 again:
with 2026-09-18's batch held back, 20 transactions and 4 payments provisional and 29
`sl_fx_rates` rows; landed and rebuilt, exactly those moved, and none of the 58,501 final facts
or 29,071 final rate rows.
