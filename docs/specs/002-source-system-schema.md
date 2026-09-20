# 002 — Source System Schema

Status: Approved
Version: 2
Supersedes: version 1, 2026-09-18, unimplemented
Depends on: 000, 001

## Goal
The complete relational schema of the simulated core banking system: tables,
constraints, indexes, reference data, and a column-level PII classification, in
the `core` and `ref` schemas provisioned at M1 and in a third schema,
`platform`, that this milestone adds. No generated business data.

## Design rules

These are invariants. A table that breaks one is a defect, not a variation.

1. **Money** is `numeric(18,4)`. **Rates and percentages** are `numeric(18,8)`
   stored as decimal fractions. No `float`, `real` or `double precision`
   anywhere in any of the three schemas.
2. **All timestamps** are `timestamptz`. Business dates that carry no time are
   `date`. No naive timestamps.
3. **Audit columns, by schema.** Every table carries `created_at timestamptz not
   null default now()` and `updated_at timestamptz not null default now()`. The
   third column differs, and the difference carries meaning downstream:

   - **`core`** tables carry `is_deleted boolean not null default false`.
   - **`ref`** tables carry `is_active boolean not null default true` and **no
     `is_deleted`**. Reference rows are deactivated, never deleted. Two
     overlapping flags on the same row would be a defect, not thoroughness.
   - **`platform`** tables carry neither: a row is present or it is not.

   The distinction is a contract, not a naming preference. `is_deleted` on a
   `core` row means the entity is removed from silver. `is_active = false` on a
   `ref` row means the row is **retained**, because a dimension must still
   describe historical facts that reference a retired code: a transaction booked
   under a channel the bank has since withdrawn still needs that channel to have
   a name. This is stated in the silver contract in `docs/architecture.md`.
4. `updated_at` is maintained by a single trigger function `core.set_updated_at()`
   attached to every table in all three schemas with a `before update` trigger.
   It is never set by application code, because watermark extraction correctness
   depends on it and an application that forgets is a silent data loss bug.
   Reference data is extracted by the same watermark path as `core`, so a second
   code path in the extraction layer would be a defect waiting to happen.
5. **Every table in `core`, `ref` and `platform` has an index on `updated_at`.**
   This is the access path the extraction layer uses on every run.
6. Primary keys are `bigint generated always as identity`. Natural business
   identifiers exist as their own columns with unique constraints where the
   domain has one.
7. **Categorical values are lookup tables in `ref` with foreign keys**, not
   Postgres enums and not free text. Reason: enums cannot be altered
   transactionally in older versions, they cannot carry attributes, and a
   lookup table extracts cleanly into a dimension. Every lookup table has a
   stable `code` column, a description, and an `is_active` flag.

   Three qualifications bound the rule, so that it produces tables that earn
   their existence rather than a table per adjective.

   **A categorical value earns a lookup table if either limb is satisfied.**

   *Limb one, the behavioural-attribute test.* The table carries at least one
   attribute beyond `code`, `name` and `is_active` that a model or metric named
   in `docs/metric_definitions.md` or `docs/model_inventory.md` consumes, and
   that attribute adds information the code does not already carry.
   `transaction_types.is_customer_initiated` is the template: it groups eight
   codes into one flag that the active account rule reads. A boolean that is
   true for exactly one code of a two-value domain restates the code and does
   not qualify.

   *Limb two, the open-vocabulary test.* The set of values is open and expected
   to grow with data rather than with schema changes. A vocabulary that grows
   with data must live in data: otherwise every new value is a schema migration,
   in a system whose whole purpose is generating varied data. This limb is
   independent of the first and is the stronger of the two, because it is about
   where a value can be added from, not about what reads it.
   `loan_applications.decision_reason_code` qualifies on this limb alone and on
   no attribute at all.

   A categorical domain satisfying neither limb is enforced by a **check
   constraint against a value list documented in `docs/data_dictionary.md`**, on
   the column that holds it, not by a table.

   **The stopping rule.** A categorical attribute of a `ref` row stays a plain
   column unless it is a join key shared with another table. This is what stops
   the rule recursing into a lookup for every adjective on a lookup.

   The rule in this form is written into `docs/conventions.md`, because it
   governs every schema the project builds from here, not only this one.

8. **Referential integrity is enforced** with foreign keys throughout. A source
   system that permits orphans teaches nothing about handling them; orphans
   will be injected deliberately by the mutation engine at M3 through
   controlled means, not by an absent constraint.
9. **The full card number is never stored.** Cards hold a six-digit BIN and the
   last four digits only. There is no column that could hold a PAN.
10. Check constraints express domain rules that must hold: non-negative
    amounts where the domain requires it, `closed_date >= opened_date`,
    ownership weights in `(0,1]`, status transitions where cheap to express.

    **Documented exception: ledger amounts.** `core.gl_entries.amount` is signed
    (section 4). The non-negative guidance does not apply to it, and the
    exception is recorded in `docs/conventions.md` so that a later reader does
    not "fix" it into a defect.

    **Documented exception: ownership weight.** A holder role that carries no
    ownership, such as an authorised signatory, has a null `ownership_weight`.
    The constraint is therefore `ownership_weight is null or (ownership_weight >
    0 and ownership_weight <= 1)`: every value present is in `(0,1]`, and
    absence is distinguishable from zero.
11. Column names follow `docs/conventions.md`: `_at` for timestamps, `_date`
    for dates, `is_`/`has_` for booleans, `_amount` with a companion
    `_currency_code`, `_id` for keys.

    **`created_at` and `updated_at` are reserved.** No business timestamp on any
    table may take either name. Both are audit columns whose meaning must be
    uniform across every table, because watermark extraction depends on it: a
    table where `created_at` holds a business event time would have its
    `created_at` written by the loader and its `updated_at` written by the
    trigger, so the two would disagree and the column would no longer say when
    the row entered the database. A business timestamp is named for the event:
    `alerted_at`, `booked_at`, `started_at`. This rule is added to
    `docs/conventions.md`.
12. **Every foreign key column is indexed.** Postgres indexes
    the referenced key, not the referencing column. On the `full` profile an
    unindexed `transactions.account_id` makes the M3 load and every source-side
    validation unusable, and it makes a delete or update of the parent scan the
    child table. Either every foreign key column carries an index, or the
    omissions are listed in `docs/data_dictionary.md` with the reason.

## Scope

### 1. Reference schema (`ref`)
Lookup and seed tables, each with `code`, a description, `is_active`, and the
audit columns of design rule 3. Populated by idempotent seed scripts under
`infra/docker/postgres-source/seed/`, re-runnable with no duplication.

Seeds use `insert ... on conflict (code) do update ... where` the incoming values
are distinct from the stored ones. The `where` clause is load-bearing: a plain
`do update` would move `updated_at` on every row of every re-run, so acceptance
criterion 1 would be false and every extraction run would re-read all reference
data.

Seed files are grouped by domain, not one per table: twenty-something files in
one directory is a directory nobody reads. The grouping is geography, products
and pricing, ledger, status and outcome domains, and risk. Idempotency is a
property of each statement, not of the file, so the grouping costs nothing and
the `on conflict ... do update ... where` construction is used unchanged in every
statement.

#### 1.1 Domain tables

| Table | Contents | Notes |
|---|---|---|
| `currencies` | ISO 4217 code, name, minor unit | Restricted to the currencies the bank operates in; EUR base |
| `countries` | ISO 3166-1 alpha-2, name, region, is_eea, is_sepa | `is_eea` and `is_sepa` drive interchange and cross-border logic |
| `mcc_codes` | MCC, description, category, band | Category and band are what Q4 groups by |
| `account_types` | Code, name, product class, is_deposit_taking | `is_deposit_taking` is what the deposit balance metric filters on |
| `transaction_types` | Code, name, `is_customer_initiated`, `direction` | `is_customer_initiated` is the source of the derived flag in `metric_definitions.md`; it lives here, not in code |
| `channels` | Code, name, is_digital | Q19 groups by channel |
| `payment_schemes` | Code, name, is_sepa, settlement_days | |
| `card_products` | Code, name, product class, network, is_commercial | Product class and commercial flag drive interchange |
| `loan_products` | Code, name, nominal annual rate, term months, is_secured | SCD2 candidate; rates change over time |
| `interchange_rates` | Card product class, merchant region, MCC band, rate, valid from/to | The seed the interchange metric reads; intra-EEA consumer rates are regulatory, others remain undecided per `metric_definitions.md` and are seeded as null with a comment. Section 11 states the contract a null rate carries |
| `gl_accounts` | GL account code, name, type, normal balance side | Required for double-entry |
| `fraud_rules` | Rule id, name, description, is_active | Q10 groups by rule; rules must be an entity, not a string in an alert |

#### 1.2 Shared join-key tables

Three tables exist because a value is a join key shared between two tables, and
a string that must match across tables without a foreign key produces a silent
no-match rather than an error. `interchange_rates` is the case that forces them:
it is keyed on card product class, merchant region and MCC band, and a
mismatched spelling on any of the three returns no rate for a transaction that
has one.

| Table | Referenced by | Prevents |
|---|---|---|
| `card_product_classes` | `card_products.product_class_code`, `interchange_rates.card_product_class_code` | An interchange rate that matches no card |
| `regions` | `countries.region_code`, `interchange_rates.merchant_region_code` | An interchange rate that matches no merchant country |
| `mcc_bands` | `mcc_codes.band_code`, `interchange_rates.mcc_band_code` | An interchange rate that matches no MCC |

#### 1.3 Domain lookups

Each of the following earns a table under design rule 7. Twelve pass on limb one:
the attribute column names the thing beyond `code`, `name` and `is_active` that
the table carries, and the consumer names the model or metric that reads it. One
passes on limb two alone.

| Table | Behavioural attributes | Consumer |
|---|---|---|
| `account_statuses` | `is_open` | Active account rule (Q1); `fct_account_balance_daily` (Q3) |
| `transaction_statuses` | `is_posted` | Active account rule (Q1, Q6); structuring rule (Q11) |
| `payment_statuses` | `is_declined`, `is_final` | `mart_payments_cross_border` decline rate (Q13) |
| `loan_statuses` | `is_open`, `implies_default` | Default rule (Q8); `fct_loan_balance_daily` (Q5, Q7) |
| `loan_application_statuses` | `is_decided`, `is_approved` | Approval rate rule (Q8), which is defined in terms of both |
| `login_outcomes` | `is_successful` | Unrecognised device rule (Q19), which counts successful sessions only |
| `fraud_dispositions` | `is_final`, `is_confirmed_fraud` | Alert precision rule (Q10), which is defined in terms of both |
| `holder_roles` | `is_primary`, `carries_ownership` | `bridge_account_holder` (Q3, Q6, Q11); `carries_ownership` is what makes a null ownership weight legitimate |
| `risk_bands` | `band_ordinal`, `pd_lower_bound`, `pd_upper_bound` | `mart_credit_underwriting` (Q8): ordering bands and comparing realised default rate against expected |
| `payment_types` | `is_customer_initiated`, `direction` | Customer-initiated transaction rule; the derived flag in `sl_payments` |
| `entry_sides` | `sign_multiplier` | `mart_control_gl_integrity` (Q16); the balance trigger in section 4 |
| `gl_account_types` | `normal_side_code` | `mart_control_gl_integrity` (Q16) |

`pd_lower_bound` and `pd_upper_bound` are `numeric(18,8)` decimal fractions, per
design rule 1. `sign_multiplier` is `smallint`, constrained to `-1` or `1`.
`gl_account_types.normal_side_code` is a foreign key to `entry_sides`.

One table earns its place on limb two and carries no behavioural attribute:

| Table | Columns | Why it is a table |
|---|---|---|
| `decision_reasons` | `code`, `name`, `is_active` only | The vocabulary is open. Underwriting reason codes grow as the M3 generator models more decision paths, and under a check constraint every new reason would be a schema migration in the one system whose purpose is generating varied data. No consumer needs an attribute yet, and none is invented for it |

The other four domains that failed limb one were re-tested against limb two and
none qualifies: card status, authorisation outcome, KYC status and address type
are closed domains, fixed by a card lifecycle, an authorisation protocol, a KYC
workflow and a two-value distinction respectively. No mechanism exists by which
generated data creates a new value in any of them. They stay check constraints.

#### 1.4 Value lists enforced by check constraint

These categorical domains satisfy neither limb of design rule 7: no model or
metric named in `metric_definitions.md` or `model_inventory.md` consumes an
attribute of them beyond the code itself, and each is a closed domain. Each is
enforced by a check constraint on the column that holds it, against a value list
documented in `docs/data_dictionary.md`.

| Column | Why it failed | Value list |
|---|---|---|
| `core.cards.card_status_code` | No named model reads a property of a card status; `dim_card` carries the code as an attribute and computes nothing from it | issued, active, blocked, expired, cancelled, replaced |
| `core.transactions.authorisation_outcome_code` | An `is_approved` grouping would be informative, but no named model or metric consumes it; every rule that filters transactions filters on `transaction_statuses.is_posted` | approved, declined_funds, declined_fraud, referred, timeout |
| `core.customers.kyc_status_code` | No named consumer. The column is classified `sensitive`, so publishing its vocabulary as a joinable dimension is not wanted either | pending, verified, review, rejected, expired |
| `core.customer_addresses.address_type_code` | The only candidate attribute, `is_residential`, is true for exactly one code of a two-value domain, so it restates the code | residential, correspondence |
| `ref.account_types.product_class_code` | Used by one table only, so the stopping rule keeps it a column. It is not a shared join key: `interchange_rates` keys on card product class, not account product class, and the deposit balance filter uses `account_types.is_deposit_taking` rather than the class | current, savings, loan, card_settlement, internal |

### 1a. Platform schema (`platform`)

`platform.column_classifications`, specified in section 5, is the only table in
this schema at M2.

It is not in `ref`. `ref` is the simulated bank's own reference data, and a PII
classification maintained by the data platform is not something a bank's core
system would publish about itself; putting it there would model a fiction. It is
platform metadata **about** a source, co-located with the source so that the
extraction layer at M4 needs one connection rather than two, and in a real
deployment it would live with the platform rather than inside the source system.
The schema name says which of the two it is.

`platform` tables carry `created_at` and `updated_at` with the `set_updated_at`
trigger and the `updated_at` index, and neither `is_deleted` nor `is_active`
(design rule 3).

### 2. Core schema (`core`)
Sixteen entities. Grain and relationships are fixed; column lists are yours to
propose subject to the design rules and section 3.

| Table | Grain | Key relationships |
|---|---|---|
| `customers` | One row per customer | — |
| `customer_addresses` | One row per customer per address version | `customers` |
| `accounts` | One row per account | `account_types`, `currencies`, `countries` |
| `account_holders` | One row per account per holder | `accounts`, `customers`; carries holder role and `ownership_weight` |
| `cards` | One row per card | `accounts`, `card_products` |
| `merchants` | One row per merchant | `mcc_codes`, `countries` |
| `agent_locations` | One row per cash-in/cash-out partner location | `countries` |
| `transactions` | One row per transaction | `accounts`, `cards`, `merchants`, `agent_locations`, `transaction_types`, `channels`, `currencies` |
| `payments` | One row per payment instruction | `accounts`, `payment_schemes`, `countries`, `currencies` |
| `loan_applications` | One row per application | `customers`, `loan_products` |
| `loans` | One row per disbursed loan | `loan_applications`, `customers`, `loan_products` |
| `loan_installments` | One row per loan per installment number | `loans` |
| `gl_transactions` | One row per posting batch (double-entry header) | — |
| `gl_entries` | One row per debit or credit line | `gl_transactions`, `gl_accounts`, `accounts` |
| `fraud_alerts` | One row per alert | `transactions`, `fraud_rules`, `customers` |
| `login_sessions` | One row per login attempt | `customers`, `channels` |

`gl_transactions` is new relative to the entity inventory in
`docs/data_dictionary.md`, which lists fifteen entities that resolve to `core`
tables; `gl_transactions` is the sixteenth. Justify it in the data dictionary:
double-entry integrity is a property of a posting batch, and Q16 cannot be
tested without a header to group by.

`agent_locations` is a `core` table. The entity inventory files it under the
Reference domain, which describes how slowly it changes rather than which schema
holds it; it carries operational rows keyed by a business identifier, not a code
vocabulary. The data dictionary records the move.

### 3. Columns required by existing commitments
Every column named in `docs/metric_definitions.md` and
`docs/model_inventory.md` must have a source column that produces it. At
minimum, and without limiting what else you add:

- `accounts`: opened date, closed date, status, currency, country, account type,
  overdraft limit, current balance
- `account_holders`: holder role, ownership weight
- `transactions`: booked at, value date, amount and currency, transaction type,
  channel, status, authorisation outcome, reversal reference, counterparty
  reference, and a nullable link to each of card, merchant and agent location
- `cards`: BIN, last four, network, product, issued date, expiry, status
- `payments`: initiated at, booked at, settled at, payment type, scheme, status,
  amount and currency, counterparty IBAN, counterparty name, counterparty
  country, remittance reference
- `loan_applications`: applied at, decided at, status, risk band, requested and
  approved amounts, decision reason code
- `loans`: disbursed date, principal, nominal annual rate, term, status,
  written-off date, maturity date
- `loan_installments`: installment number, due date, due amount, paid amount,
  paid at
- `fraud_alerts`: alerted at, rule, score, disposition, dispositioned at,
  analyst reference
- `login_sessions`: started at, device fingerprint, auth outcome, channel, IP
  address, IP country
- `gl_entries`: posting date, GL account, debit or credit side, amount and
  currency, and the account it relates to where applicable

Six of these resolve a gap found while planning against version 1, and the
reason each exists is recorded in `docs/traceability.md`:

| Column | Supplies | Why the source needs it |
|---|---|---|
| `payments.booked_at` | `fct_payments.booked_at` | An outbound payment debits the account when it books and settles with the scheme later; the two are different instants and the active account rule reads the first |
| `payments.payment_type_code` | `sl_payments.payment_type` | `transaction_types` covers card events, not payment instructions |
| `payments.counterparty_name` | `fct_sanctions_screening` (Q12) | A sanctions screen needs a name to match; IBAN and country cannot supply one. Classified `identifier`; section 12 states how screening reaches it |
| `fraud_alerts.alerted_at` | `sl_fraud_alerts.created_at` | Design rule 11 reserves `created_at`. `metric_definitions.md` is amended to the new name |
| `login_sessions.ip_country_code` | Q19 geography | `ip_address` is classified `identifier` and is tokenised at ingest, so the country must be resolved at the source or it is lost |
| `customer_addresses.postal_code` | `dim_customer` (Q1, Q13) | Section 5 states the classification and the generalisation rule |

`direction` is not a column of `core.transactions`. It lives on
`ref.transaction_types` and `transactions` supplies it through the foreign key.
Storing it in both places would allow the two to disagree. `docs/traceability.md`
records the resolution.

Produce `docs/traceability.md`: one row per column referenced in
`metric_definitions.md` or `model_inventory.md`, mapping it to the source table
and column that supplies it, or marking it as derived and naming the layer that
derives it. An unsupplied reference is a defect to fix in this milestone, not a
note for later.

### 4. Double-entry integrity

**The invariant.** Within a `gl_transactions` batch, `gl_entries` amounts sum to
zero **per currency**: `sum(amount) = 0` grouped by
`(gl_transaction_id, currency_code)`. Per batch alone is not enough, because a
EUR debit and a USD credit can sum to zero numerically and mean nothing. A
multi-currency posting balances within each currency, with the FX position
posted to a conversion account.

**Signed amounts.** `amount` is `numeric(18,4)`, signed: a debit is positive and
a credit is negative. A check constraint binds the sign to `entry_side_code`
through `ref.entry_sides.sign_multiplier`, so the two can never disagree. The
side is a stored column with a check constraint, **not** a generated column.
Design rule 10 records the exception to its non-negative guidance.

**Why a check constraint cannot express it.** A `check` constraint sees one row.
Balance is a property of a set of rows that does not exist yet while any single
row is being written, so there is no row at which a check constraint could be
true for a batch that will balance and false for one that will not.

**Why the trigger is deferred.** A posting batch is assembled across several
statements: the debit is written, then the credit. An immediate constraint would
reject the batch at the first line, when it is correctly unbalanced, and there is
no ordering of the statements that avoids it. Deferring to commit is what makes
the invariant "no unbalanced batch is ever committed" rather than "no statement
ever leaves the ledger unbalanced", and only the first is true of a real ledger.

Enforce it with a `deferrable initially deferred` constraint trigger in PL/pgSQL
on `gl_entries`. A `dq` test at M7 verifies the same invariant downstream; the
source-side trigger is what makes the invariant true rather than merely observed.

**Bulk load behaviour, measured.** The cost of the trigger under `COPY` was
measured before this specification was reissued, on `postgres-source`
(PostgreSQL 16.15, `mem_limit: 512m`), loading 1,000,000 entry rows in 250,000
four-line batches:

| Path | COPY | COMMIT | Total | After-trigger queue |
|---|---|---|---|---|
| No trigger, baseline | 4.44 s | 0.02 s | 4.46 s | — |
| Deferred per-row constraint trigger, one transaction | 5.27 s | 6.19 s | 11.46 s | 12.58 MB |
| Deferred per-row constraint trigger, ten 100k-row transactions | 5.8 s | 5.8 s | 11.6 s | 1.26 MB peak |
| Statement-level trigger with `referencing new table` | 5.12 s | 0.002 s | 5.12 s | none |

Row-level `after` triggers fire on `COPY`, and deferred events queue at 12.6
bytes per row in `AfterTriggerEvents`, a backend memory context that does not
spill to disk. Extrapolated against the 512 MiB container limit: 10M entries is
about 126 MB of queue, and a single-transaction load crosses the limit somewhere
between 30M and 40M rows, at which point the backend is killed rather than slowed.

**The constraint this places on M3.** The historical load and the mutation engine
commit GL in chunks of whole balanced batches. 100,000 entry rows per transaction
is the measured figure: it costs nothing in throughput against a single large
transaction and bounds the queue at about 1.26 MB. This is a constraint on the
loader, not a property the loader may choose to ignore, and it is recorded in the
ADR rather than in a configuration file.

**The rejected alternative, and why it lost.** The statement-level trigger with a
transition table is 2.2x faster and queues nothing, but Postgres requires a
constraint trigger to be `for each row`, so a statement-level trigger cannot be
deferred. Measured consequence: a legitimate two-statement batch — insert the
debit, insert the credit, commit — is rejected at the first statement with
`gl batch 9100001 does not balance`. It forbids the normal way a posting batch is
assembled, so it cannot be the enforcement mechanism, whatever it costs.

The ADR required by section 9 carries both measurement tables, the memory bound,
the 100,000-row chunk figure with the container limit it was measured at, and the
rejected alternative with this reason.

### 5. PII classification applied
Every column in all three schemas is classified per `docs/pii_classification.md`.
Deliver it as `platform.column_classifications`, seeded from the same source of
truth as the documentation, with columns for schema, table, column,
classification and rationale. Reason for putting it in the database rather than
only in Markdown: the extraction layer at M4 reads it to decide what to
tokenise, so it must be queryable, and a classification that exists only in
prose will drift from the code that depends on it. Section 1a states why the
table is in `platform` and not in `ref`.

Date of birth is `quasi-identifier`, not `identifier`. Full name, email,
phone, national identifier, IBAN, device fingerprint, IP address and payment
counterparty name are `identifier`. Address lines are `identifier`; city,
postal code and country are `quasi-identifier`.

**A fifth class, `pseudonymous_key`.** The four classes in
`docs/pii_classification.md` had no home for a key that identifies a person only
by reference, and the first pass classified all twenty-one of them
`non-personal`, which says the opposite of what is true. An internal customer
number is pseudonymised personal data: inside a platform holding the mapping,
`customer_id = 4711` picks out exactly one person.

It cannot be tokenised either. The key **is** the pseudonym, so hashing it would
produce a different pseudonym of the same personal character while breaking
every join in the platform. The class therefore says what the column is and asks
for nothing to be done to it. What protects the subject is that the identifiers
the key leads to are tokenised and resolvable only through the vault: delete the
vault entries and the key still joins perfectly while resolving to nobody. That
is precisely what makes crypto-shredding sufficient rather than merely
convenient, and the class is how the schema records that the design depends on
it.

The line is drawn at keys to person-bearing entities — `customers`,
`customer_addresses`, `accounts`, `account_holders`, `cards`, `loans`,
`loan_applications` — and every foreign key pointing at one, twenty-one columns
in total. An event key such as `transaction_id` is not one: it identifies an
event, and the event row reaches a person through a column that is classified.
A business key that leaves the platform, such as `customer_reference` or
`account_number`, stays `identifier` and is tokenised, because the surrogate key
carries the join in its place.

`docs/pii_classification.md` currently files IP address and address as
quasi-identifiers. This specification overrides both, and the amendment to that
document is a deliverable of this milestone.

**Postal code.** The full `postal_code` is stored and classified
`quasi-identifier`, and generalised before gold. The district is
`left(postal_code, 2)`. This is a deliberate simplification: EU postcode formats
differ, and a production system would apply a country-specific rule. It is
documented as a simplification with its reason, in the data dictionary and in
`pii_classification.md`; an undocumented one would not be acceptable.

**Source of truth and generation direction.** `docs/data_dictionary.md` is the
single source of truth. `platform.column_classifications` is generated from it during
`make schema-apply`, by parsing the dictionary and loading the table inside one
transaction. No hand-written classification seed file exists, because it would be
the second copy the table exists to prevent. The documentation is not generated
from the database: `description` and the consuming model are prose no catalogue
holds, and generating them would make the documentation unbuildable without a
running stack.

**Parser hardening.** The dictionary parser is on the critical path for
acceptance criteria 6, 10 and 12, so it is specified rather than left to
implementation:

- The column-level table header is fixed and stated in `docs/data_dictionary.md`.
  The parser validates the header before reading any row and refuses to parse a
  table whose header does not match exactly.
- A parse failure is a hard error that aborts `make schema-apply` before the load
  begins, naming the file, the line number and the offending line. A partial load
  is never written.
- An unknown classification value, a duplicate `(schema, table, column)` row, a
  row with the wrong column count, and a missing header are each errors, not
  warnings.
- The parser is unit-tested against malformed input covering all four cases plus
  a well-formed table, in `make test`, with no database.

**Type canonicalisation.** The drift test compares documented types against live
types. Postgres resolves `decimal` to `numeric` and `varchar` to `character
varying` at parse time, so `format_type(atttypid, atttypmod)` is already
canonical on the database side; the risk is entirely on the documented side.
Each distinct documented type string is canonicalised by asking Postgres to
resolve it, in pure SQL:

```sql
format_type(to_regtype(<documented>)::oid, null)
  || coalesce(substring(<documented> from '\(\s*\d+\s*(?:,\s*\d+\s*)?\)'), '')
```

`to_regtype` resolves the alias and `format_type` prints the canonical base name;
the modifier is carried across from the documented string, because `to_regtype`
discards it. Verified across every alias pair the schema uses: `decimal(18,4)`
and `numeric(18,4)` both canonicalise to `numeric(18,4)`, `varchar(30)` and
`character varying(30)` both to `character varying(30)`, `timestamptz` to
`timestamp with time zone`, `int8` to `bigint`, `int2` to `smallint`, `bool` to
`boolean`, `char(2)` to `character(2)`.

It is pure SQL, read-only and needs no DDL, so `make schema-check` on the host
and the integration test inside the container run the identical mechanism
through different connections, and running it as `nordbank_reader` does not
require the temporary-table privilege that a `create temp table` probe would.
The result is cached per distinct documented string, so the number of round
trips is the number of distinct types and not the number of columns. The
documented string is validated against the allowlist
`^[a-z][a-z ]*(\(\d+(,\d+)?\))?$` before it reaches the query.

A static alias map is rejected: an incomplete map is exactly the false failure
this is meant to prevent. Comparing `information_schema.data_type` is rejected:
it drops the modifier, so `numeric(18,4)` and `numeric(18,8)` compare equal and
design rule 1 stops being testable.

### 6. Grants
Extend the M1 role model to all three schemas:
- `nordbank_app` owns `core`, `ref` and `platform`, and all objects in them.
- `nordbank_reader` has `select` on all tables in all three schemas, including
  default privileges for future tables, and nothing else.
- Prove `nordbank_reader` cannot read `platform.column_classifications`
  differently from any other table — it can read it; classification is not
  secret, the values it protects are.

`platform` is a new schema, so it needs provisioning that `core` and `ref`
received at M1. The M1 init scripts run once, on first start of an empty data
volume, so whatever `platform` needs is added there and takes effect on the next
`make nuke`. If provisioning it requires more than a grant and a default
privilege in those scripts, that is reported rather than worked around.

### 7. ERD and drift control
- `docs/diagrams/erd.md` — two Mermaid `erDiagram` blocks. One diagram of over
  forty tables is unreadable, so the schema is drawn twice at different scopes:
  - **Core.** The sixteen `core` tables with their relationships, and each
    reference table they point at collapsed to its `code` column.
  - **Reference.** The `ref` tables and the shared join keys, showing which
    reference tables point at which others, plus `platform` alongside them.

  Cardinalities must be correct in both, and acceptance criterion 11 applies to
  both.
- `docs/data_dictionary.md` — one file, whatever its size. It is the parser's
  input, and splitting it would make the drift test's job harder for no gain.
  Extended from entity level to column level: table,
  column, type, nullability, classification, description, and the business
  question or model that consumes it where one does. It also carries the value
  lists from section 1.4, the reason for each additional reference table, any
  foreign key left unindexed under design rule 12, and the fixed table header the
  parser validates.
- `scripts/dump_schema.py` — emits the live schema as a normalised, sorted text
  representation on stdout.
- `airflow/tests/test_schema_matches_dictionary.py` — integration test
  asserting the live schema matches the committed data dictionary: no
  undocumented column, no documented column that does not exist, types
  agreeing. This is the control that stops the dictionary rotting.

### 8. Make targets
- `schema-apply` — apply DDL and seeds to a running `postgres-source`,
  idempotently, then load `platform.column_classifications` from the data dictionary.
- `schema-dump` — write the normalised schema representation to stdout, with
  `OUT=<path>` to capture it to a file. Nothing generated is committed: a
  committed schema snapshot is a third artifact that rots unless it is itself
  guarded, and the dictionary is already the committed representation that
  `schema-check` guards.
- `schema-check` — fail if the live schema and the committed dictionary
  disagree, printing the differences.

Wire `schema-check` into `make test-integration`.

Two supporting changes are in scope because the targets do not work without them:

- `docker-compose.yml` mounts `./docs:/opt/airflow/docs:ro` into the Airflow
  services. The drift test runs inside `airflow-scheduler`, which is where
  `make test-integration` runs, and it cannot currently read the dictionary.
- `.github/workflows/stack.yml` runs `make schema-apply` before
  `make test-integration`, or the newly wired `schema-check` runs against an
  empty database.

### 9. Migration approach
DDL lives in numbered, idempotent SQL files under
`infra/docker/postgres-source/schema/`, applied in order and safe to re-apply.
Record an ADR: numbered SQL over a migration framework such as Alembic or
Flyway, on the grounds that the source system is generated and disposable, its
schema is a fixture rather than a production asset that must migrate forward,
and adding a framework would obscure the DDL a reviewer wants to read. State
the negative consequence plainly: no rollback path and no drift detection
against a shared environment, mitigated by `schema-check` and by the fact that
the database is rebuilt rather than migrated.

State the second negative consequence too: `schema-apply` is not purely "run the
files in order", because section 5 makes it run the files and then load one table
from a document. That is the price of having one source of truth for the
classification, and it is paid deliberately.

### 10. Partitioning

`core.transactions` holds tens of millions of rows on the `full` profile, which
makes declarative monthly partitioning on the business timestamp an obvious
candidate. It is **decided against for now**, and the reasoning is recorded
rather than left implicit.

Extraction reads `updated_at`, not the business date (`architecture.md`,
core banking ingestion pattern). Partitioning on `booked_at` would therefore
prune nothing on the access path the platform actually uses: every incremental
run would touch every partition. It would add a partition maintenance job, make
the foreign keys from `fraud_alerts` and `gl_entries` harder to express, and
complicate the DDL a reviewer is meant to read, in exchange for pruning a query
pattern the platform does not run.

**The condition that reverses the decision:** M3 measuring `full` profile load
times or autovacuum times on `core.transactions` that are unacceptable. If that
happens, partitioning is reconsidered on the evidence, and the candidate key is
`booked_at` monthly with the extraction index kept on `updated_at`.

This reasoning is carried into the M2 checkpoint when M2 closes.

### 11. Interchange rates: the contract a null rate carries

`interchange_rates` is seeded with the regulated intra-EEA consumer rates and
with nulls for the commercial and inter-regional rates that
`metric_definitions.md` marks undecided. The downstream contract is stated now,
so that it is not decided by accident at M6:

**A null interchange rate is an error condition, never zero.** A transaction that
resolves to a null rate makes Q4 fail loudly: the `dq` check is severity `error`,
it blocks the mart, and `mart_revenue_interchange` does not publish a number for
the affected period. Treating a null as zero would understate interchange revenue
silently, which is the failure mode the whole metric definitions document exists
to prevent.

### 12. Sanctions screening and the vault

`payments.counterparty_name` is classified `identifier` and is therefore
tokenised at ingest, which appears to make Q12 impossible. The resolution is an
architecture decision recorded by this milestone, not a problem deferred to M4:

- Sanctions screening runs as a **governance-domain job with vault access**. It
  resolves tokens to names through the vault, screens against a named list
  version, and persists only the token, the matched entity, the list version and
  the match score. The raw name never leaves the vault and never reaches silver
  or gold.
- This is what makes **historical re-screening** possible when the list changes.
  Screening at ingest time would prevent it, and re-screening the book against an
  updated list is how AML actually works.
- **Erasure therefore also destroys the ability to re-screen that subject.** That
  is correct behaviour, and it is stated rather than discovered.
- The vault's concentration of risk, already documented in ADR 0005, becomes
  load-bearing for a second reason: it is now the only path to a sanctions screen
  as well as the only path to erasure.

This is recorded in `architecture.md` and added to the consequences of ADR 0005.

### 13. Document amendments delivered by this milestone

The specification is not complete until the documents it contradicts are
corrected. Each of these is a defect found while planning against version 1.

| Document | Amendment |
|---|---|
| `conventions.md` | `_amount` takes a `_currency_code` companion, not `_currency`; the signed ledger amount exception; `created_at` and `updated_at` reserved for audit columns; the two-limb lookup-table rule from design rule 7; the `is_deleted` versus `is_active` distinction between `core` and `ref`; the four required checks are `lint`, `dags`, `docs` and `stack`, not two |
| `pii_classification.md` | IP address and address lines move to `identifier`; city, postal code and country are `quasi-identifier`; the `left(postal_code, 2)` district simplification and its reason |
| `metric_definitions.md` | `sl_transactions.initiator` becomes `transactions.transaction_type_code` with `ref.transaction_types.is_customer_initiated`; `sl_fraud_alerts.created_at` becomes `alerted_at` |
| `model_inventory.md` | The `products` entity splits into `account_types`, `card_products` and `loan_products`, and `sl_products` goes with it; the four dbt seeds that duplicate `ref` tables are dropped; `dim_detection_rule` reads `sl_fraud_rules`; `br_corebank__gl_transactions` and `sl_gl_transactions` are added; the `sl_card_settlements` and `sl_macro_indicators` column definitions move to M4 with their contracts; every `ref` table is named with the dimension that consumes it |
| `data_dictionary.md` | Column level for all three schemas; the `products` entity removed; `agent_locations` filed under `core`; the transactions and `gl_entries` load patterns corrected to the `updated_at` watermark |
| `business_questions.md` | The coverage rule keeps its two directions and gains a clause: a `ref` table either becomes a conformed dimension in its own right or is consumed as attributes of one, and either way it is named in `model_inventory.md` with the dimension that consumes it. No `ref` table is exempt, and none requires a silver model of its own |
| `architecture.md` | Section 12; and the silver contract states the `is_deleted` versus `is_active` distinction from design rule 3, so that a retired reference code is retained rather than removed |
| `adr/0005-pii-crypto-shredding.md` | Section 12, as added consequences |
| `project_state.md`, `README.md` | The M2 schema half delivered, the data half pending |

## Out of scope
Generated business data, the historical load, the mutation engine, extraction
logic, contracts, dbt, and anything in the warehouse. Reference seeds are in
scope; business rows are not.

## Acceptance criteria

The count stays at fourteen. The additions in this version strengthen the text of
existing criteria rather than adding new ones, so that the reporting contract
against this specification does not move.

1. `make schema-apply` on a freshly nuked stack creates all three schemas
   complete, and running it twice changes nothing. Prove idempotency: identical
   `schema-dump` output, identical row counts, and an unmoved `max(updated_at)`
   on every reference table and on `platform.column_classifications`.
2. Every table has `created_at`, `updated_at`, an `updated_at` index and a
   working `set_updated_at` trigger; every `core` table has `is_deleted` and
   every `ref` table has `is_active` and no `is_deleted`, per design rule 3.
   Prove the trigger fires by updating a row and observing `updated_at` change
   without it being named in the statement.
3. No `float`, `real` or `double precision` column exists in any of the three
   schemas. Prove it with a catalogue query.
4. No column in any of the three schemas can hold a full card number; cards
   store BIN and last four only.

   As literally worded this criterion is unprovable: any sufficiently wide text
   column could hold a PAN. The bar is therefore these three checks, stated here
   so the criterion cannot be quietly weakened later:
   - No column in any schema is named `pan`, `card_number` or
     `primary_account_number`.
   - Every character column on `core.cards` is length-bounded, and none admits
     thirteen or more characters.
   - `card_bin` is `character(6)` and `card_last_four` is `character(4)`, each
     with a digit-only check constraint.
5. An unbalanced `gl_entries` batch cannot be committed. Demonstrate the
   rejection and the error message, for a batch unbalanced in one currency as
   well as a batch unbalanced overall.
6. `platform.column_classifications` covers every column in all three schemas,
   with no unclassified column and no classification for a column that does not
   exist. Prove both directions with a query.
7. `docs/traceability.md` accounts for every column referenced in
   `metric_definitions.md` and `model_inventory.md`. Report any reference you
   could not supply and what you changed to supply it.
8. `nordbank_reader` can `select` from every table and cannot `insert`,
   `update`, `delete` or `create`. Prove all four refusals.
9. Reference seeds are populated and idempotent; re-running the seed produces
   identical row counts and no duplicates.
10. `make schema-check` passes, and demonstrably fails if a column is added to
    the database without updating the dictionary. The parser refuses malformed
    input rather than loading partially, proven by its unit tests.
11. Both Mermaid ERDs render on GitHub and their cardinalities match the foreign
    keys actually created.
12. `docs/data_dictionary.md` is column-level for all tables in all three
    schemas, in one file.
13. All existing checks still pass: `make test`, `test-dags`,
    `test-integration`, and both CI workflows.
14. Delivered as a pull request on `feat/M2-source-schema` with all four required
    checks green: `lint`, `dags`, `docs` and `stack`.

## Changelog

Version 2 replaces version 1 under the reissue protocol in `docs/specs/README.md`:
the review of version 1 produced more than five corrections to a specification
that had not been implemented, so the file is replaced rather than amended.

**Ledger.**
- Section 4: the balance invariant is per `(gl_transaction_id, currency_code)`
  rather than per batch. A batch balancing across currencies is arithmetic
  without meaning.
- Section 4: `amount` is signed, debit positive and credit negative, with a check
  constraint binding the sign to the side, and explicitly not a generated column.
  Design rule 10 records the exception, and `conventions.md` records it too so a
  later reader does not undo it.
- Section 4: the trigger design is unchanged, and now carries the measurements
  that justify it, the memory bound that constrains it, and the chunked-commit
  requirement it places on M3. Version 1 asserted the approach; version 2 has
  measured it.

**Reference tables.**
- Design rule 7 gains a two-limb test and the stopping rule, so that "categorical
  values are lookup tables" produces tables that earn their existence. Version 1
  stated the rule without a bound, which would have produced a lookup for every
  adjective. Limb one is the behavioural-attribute test; limb two is the
  open-vocabulary test, which is independent of it and stronger, because it is
  about where a value can be added from rather than about what reads it.
- Section 1 is restructured into domain tables, shared join keys, domain lookups
  that earn a table, value lists that do not, and the classifications table.
  Thirteen of seventeen candidate lookups earn a table: twelve on limb one and
  `decision_reasons` on limb two alone, with no invented attribute. Four are
  check constraints; a fifth column, `account_types.product_class`, was demoted
  by the stopping rule because it is not a shared join key. Three shared join
  keys, not four.

**Schema-wide rules.**
- Design rule 3 is rewritten by schema rather than extended wholesale: `core`
  carries `is_deleted`, `ref` carries `is_active` and no `is_deleted`, and
  `platform` carries neither. Extending `is_deleted` to `ref` would have given
  reference tables two overlapping flags. The distinction is a downstream
  contract, stated here and in the silver contract in `architecture.md`: a
  deleted `core` row leaves silver, an inactive `ref` row is retained, because a
  dimension must still describe facts that reference a retired code.
- Design rules 4 and 5 extend from `core` to `ref` and `platform`. One watermark
  path, one extraction code path.
- Design rule 11 reserves `created_at` and `updated_at` for audit columns on
  every table, generalising the `fraud_alerts.alerted_at` fix into a convention.
- Design rule 12 is new: every foreign key column is indexed, or the omission is
  documented with its reason.
- Design rule 10 gains the ownership-weight exception, so that a holder role
  carrying no ownership is expressible without violating the `(0,1]` range.

**Columns.**
- Section 3 adds `payments.booked_at`, `payments.payment_type_code`,
  `payments.counterparty_name`, `fraud_alerts.alerted_at`,
  `login_sessions.ip_country_code` and `customer_addresses.postal_code`, each
  closing a traceability gap found against version 1.
- Section 3 removes `direction` from the `transactions` column list: it lives on
  `ref.transaction_types` and arrives through the foreign key.

**Classification and drift.**
- Section 5 names the direction of generation, hardens the parser with a
  validated header, hard failures and unit tests against malformed input, and
  specifies and caches the type canonicalisation. Version 1 said the table was
  "seeded from the same source of truth" without saying which document that was
  or which way the generation ran.
- Section 5 canonicalises types with `to_regtype` and `format_type` in pure SQL
  rather than by reading a driver's cursor description, which was the mechanism
  proposed during review. Both ask Postgres to resolve the alias, which is the
  point; the SQL form additionally runs unchanged through `psql` and through a
  driver, so `make schema-check` and the integration test share one mechanism,
  and it needs no temporary table, so it does not soften acceptance criterion 8.
- Section 5 adds the postal code decision and its simplification.
- The classification table moves out of `ref` into a new `platform` schema, with
  section 1a stating why: `ref` is the simulated bank's own reference data, and a
  classification maintained by the data platform is not something a bank's core
  system would publish about itself. It is co-located with the source so the
  extraction layer needs one connection, not two.

**New sections.**
- Section 1a introduces the `platform` schema.
- Section 10 records the partitioning decision, its reasoning and the condition
  that would reverse it.
- Section 11 states that a null interchange rate is an error at M6, never zero.
- Section 12 records sanctions screening as a governance job with vault access,
  the historical re-screening it enables, and the erasure consequence.
- Section 13 lists the document amendments this milestone owes, which version 1
  left implicit.

**Corrections.**
- Section 2: `gl_transactions` is the sixteenth `core` table, not "a seventeenth
  table relative to the current inventory". `agent_locations` is recorded as a
  `core` table against the entity inventory, which files it under Reference.
- Section 7: the ERD splits into two diagrams, because one diagram of over forty
  tables is unreadable. `docs/data_dictionary.md` stays one file whatever its
  size, because it is the parser's input.
- Section 8: `schema-dump` writes to stdout with `OUT=` to capture, and the two
  supporting changes to `docker-compose.yml` and `stack.yml` are named in scope.
- Section 1: seed files are grouped by domain rather than one per table, since
  idempotency is a property of each statement and a directory of twenty-something
  files is one nobody reads.
- Acceptance criterion 4 states the three checks that operationalise it, and says
  that the literal wording is unprovable.
- Acceptance criterion 14 requires four checks, not two. The repository has four:
  `lint`, `dags`, `docs` and `stack`.

## Amendments

Appended after implementation, by the milestone that found the deviation. The scope text above
is left as issued; the protocol is in `docs/specs/README.md`.

### 2026-09-18 — Design rule 4 is refined: the loader sets `updated_at` on insert

Design rule 4 says `updated_at` is maintained by `core.set_updated_at()` and "is never set by
application code". That is correct for every `UPDATE` and wrong for an `INSERT`, and spec 003
found the gap while specifying the historical load.

The trigger is created `before update` only, in
`infra/docker/postgres-source/schema/50_audit_and_indexes.sql`. An insert therefore never
fires it, and a loader that stays silent falls through to the column default of `now()`. For
the historical load that would stamp five years of simulated history with the wall-clock
instant of the load, delivering the entire source to M4 inside one watermark window and
leaving incremental extraction with nothing to demonstrate.

The rule as refined by spec 003: no code path may set `updated_at` on an `UPDATE`, and every
`INSERT` must set it to the row's true last-change time in simulated history. Watermark
correctness, which is what design rule 4 exists to protect, is what requires the refinement
rather than what argues against it.

Design rule 6 is unchanged. Spec 003's identity probe established that `COPY` accepts explicit
values into a `bigint generated always as identity` column while `INSERT` still refuses them,
so the loader needs nothing relaxed.

### 2026-09-18 — `ref.payment_statuses` gains `is_posted`, and `core.gl_transactions` gains a source reference

Two columns are added to the schema this specification delivered, both by spec 003 and both
because an invariant it defines had no way to read what it needed.

`ref.payment_statuses.is_posted` mirrors `ref.transaction_statuses.is_posted`. Spec 003's
invariant 4 reconciles `accounts.current_balance_amount` against posted transactions and
payments, and the payment side had no reference flag saying whether the account moved. It
passes design rule 7's limb one with that invariant and the deposit balance metric as named
consumers.

`core.gl_transactions.source_entity_code` and `.source_entity_id` carry a polymorphic
reference to the business event that produced the posting batch, with
`ref.gl_source_entities` as the vocabulary. Spec 003's invariant 6 requires every monetary
event to have GL entries, and nothing in the ledger pointed back at the event. The reference
sits on the batch rather than the line because a business event produces a batch and the
entries are its lines. A polymorphic reference cannot carry a foreign key, so referential
integrity at that join is replaced by invariant 6 and a data quality test at M7. Design rule 8
is therefore not universal, and the exception is recorded here rather than left to be
discovered in the DDL.

### 2026-09-20 — Design rule 4 reads a simulation clock, and `platform` is exempt from it

Spec 004 advances the source one simulated day at a time. An `UPDATE` during a tick fires
`core.set_updated_at()`, which sets `now()`, so every mutated historical row would carry the
real wall clock and the whole milestone would collapse into one watermark window.

`core.set_updated_at()` therefore reads a transaction-scoped custom setting and falls back to
`now()`:

```sql
new.updated_at := coalesce(
    nullif(current_setting('nordbank.sim_now', true), '')::timestamptz,
    now()
);
```

Design rule 4 holds unchanged in substance: the trigger still owns `updated_at` on every
`UPDATE`, and no code path assigns it directly. What changed is where the trigger reads the
time from.

**The `nullif` is load-bearing, not defensive, and it was measured.** A custom setting that has
never been set reads as `NULL`, but one set with `SET LOCAL` reads as the **empty string** for
the rest of that session once the transaction ends. Without the `nullif`, the second tick on a
reused connection would evaluate `''::timestamptz` and raise. Measured on PostgreSQL 16.15:
after commit, `current_setting('nordbank.sim_now', true)` returns `''`, `is null` is false and
`= ''` is true.

**`SET LOCAL`, not `SET`, and the reason is leakage.** A plain `SET` survives commit and would
backdate every later write on that connection; `SET LOCAL` is discarded at transaction end,
including on an abort. Cross-session isolation was measured directly: while one backend held an
open transaction with the setting applied, a second backend read the fallback. Through a driver
the statement form is `select set_config('nordbank.sim_now', <value>, true)`, because `SET`
accepts no bind parameter.

**The simulated clock is not visible in the catalogue, and the earlier claim that it was is
withdrawn.** Placeholder settings are excluded from `pg_settings` and from `SHOW ALL`, and
`SHOW nordbank.sim_now` errors with `unrecognized configuration parameter` in a session that
has not set it. Only `current_setting(..., true)` sees it. The authoritative record of the
simulated date is `platform.simulation_state`, which is a table, and that is where an operator
looks.

**`core` and `ref` carry simulated time; `platform` carries real time.** The trigger is
attached to all three schemas, so a tick that left the clock set would backdate its own
bookkeeping. `platform.tick_log` is a reconciliation control that M7 reads, and a control that
lies about when it ran is useless. The tick therefore clears the setting with
`set_config('nordbank.sim_now', '', true)` before writing any `platform` row, and the `nullif`
makes the fallback to `now()` work for the second time.

Clearing is not scoped to the following statement: it holds for the remainder of the
transaction. This is an ordering requirement rather than a toggle. A tick sets the clock once,
performs every `core` and `ref` write, clears the clock once, and writes its `platform` rows
last.

**The negative consequence, stated as spec 004 requires.** Any write in the tick's transaction
is backdated, not only the ones intended to be. Measured: with the clock set, an unrelated
`UPDATE` on `core.merchants` in the same transaction took the simulated timestamp. Confining
the setting to `SET LOCAL` in the tick's own transaction is what bounds the blast radius, and
the ordering rule above is what keeps `platform` out of it.

**Cost: none measurable.** A/B/A at 50,000 updated rows on `core.transactions`, PostgreSQL
16.15: `now()` 1569, 1447, 1428 ms; simulation clock 1409, 1434, 1429 ms. The setting lookup
disappears into the roughly 29 microseconds per row the heap and index writes cost.

### 2026-09-20 — Access-path indexes, where a foreign key index is not the index the query needs

Design rule 12 gives every foreign key column an index, and the catalogue loop in
`50_audit_and_indexes.sql` enforces it. That serves the constraint. It does not serve a query
that filters on the key *and* a range of another column, because the second predicate is
evaluated after every row for that key has been read, and it serves nothing at all for a column
that is not a key.

M3's mutation engine is where that became load-bearing, and four indexes are added with it. Each
is justified by a query that exists rather than by one that might:

- `core.login_sessions (customer_id, started_at)`. Spec 003 invariant 13 and question 19 both
  ask the same question — this customer's sessions in a window around an instant — and the
  foreign key index answers it by reading every session that customer ever had. Measured on the
  `dev` book's 1.5 million sessions, invariant 13 over the whole book goes from 52.8 s to 6.7 s.
  The index is leading on `customer_id`, so it also serves the foreign key and the catalogue loop
  leaves the single-column one out.
- `core.transactions (booked_at)`, `core.payments (initiated_at)` and `core.payments (booked_at)`.
  The mutation engine's clearing cycle reads recent business history on every tick and filters on
  a business timestamp over a window of days. None of those columns is a foreign key, so each
  query scanned the whole table: a seven-day window over 2.2 million transactions went from
  1,227 ms to 25 ms, and over 470,000 payments from 1,230 ms to 16 ms.

`updated_at` is indexed and is not a substitute for the second group. It says when the bank last
touched a row; these questions are about when the customer did, and on a late-arriving item those
are different days by construction.
