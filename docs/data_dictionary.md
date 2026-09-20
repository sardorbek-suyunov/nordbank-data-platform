# Data dictionary

The single source of truth for what the source database holds and how every column is
classified. Two things are generated from this file and neither may be edited by hand:
`platform.column_classifications`, loaded by `make schema-apply`, and the comparison
`make schema-check` runs against the live schema. A column that is wrong here is wrong in the
database's own classification table, so this file is code as much as it is documentation.

`Load pattern` describes how an entity reaches bronze. `Grain` is the grain of the source
entity, not of any downstream model.

## How this file is parsed

Every `#### <schema>.<table>` heading starts a column table, and every such table carries
exactly this header:

```
| Column | Type | Nullable | Classification | Description | Consumed by |
```

The parser validates the header before reading a single row and refuses to read a table whose
header has drifted, because reading rows positionally out of a changed table would load
confident nonsense. `Nullable` is `yes` or `no`. `Classification` is one of `identifier`,
`quasi-identifier`, `sensitive` or `non-personal`. `Description` may not be empty, and it is
what the classification table stores as its rationale. A malformed row aborts
`make schema-apply` with the file, the line number and the line, and nothing is loaded.

Anything outside those sections is prose and is ignored by the parser, so the inventories and
value lists below are free to be written for people.

## Entity inventory

| Entity | Schema | Domain | Grain | Load pattern | Notes |
|---|---|---|---|---|---|
| `customers` | `core` | Customer | One row per customer | Incremental on `updated_at`, soft delete, SCD2 in silver | Identity, KYC status, signup date, residence country, risk band. Source of the PII tokens every other customer-keyed model uses. |
| `customer_addresses` | `core` | Customer | One row per customer address version | Incremental on `updated_at`, history retained | Residential and correspondence addresses. Drives country attribution for reporting. |
| `accounts` | `core` | Deposits | One row per account | Incremental on `updated_at`, soft delete | Current and savings accounts: currency, product, opening and closing date, status. Basis of questions 1 and 3. |
| `account_holders` | `core` | Deposits | One row per account and holder | Incremental on `updated_at`, soft delete, SCD2 in silver | Bridge between accounts and customers. Counting people and splitting money both resolve here; without it every joint account is double counted in monetary aggregates. |
| `cards` | `core` | Cards | One row per card | Incremental on `updated_at`, soft delete | Issued cards, network, status, linked account. A card is reissued as a new row, not an update. |
| `merchants` | `core` | Cards | One row per merchant | Incremental on `updated_at` | Acquirer-side merchant identity, MCC, country. Merchant names are dirty by design, to require conformance in silver. |
| `agent_locations` | `core` | Cards | One row per partner agent location | Incremental on `updated_at` | Cash-in and cash-out points in the partner network. The location dimension behind cash transactions, and the geography behind question 11. |
| `transactions` | `core` | Cards | One row per transaction | Incremental on `updated_at`, late arrivals expected | The highest-volume entity and the partitioning test case. |
| `payments` | `core` | Payments | One row per payment instruction | Incremental on `updated_at`, status mutates | SEPA and cross-border transfers: corridor, status, decline reason. Basis of question 13. |
| `loan_applications` | `core` | Lending | One row per application | Incremental on `updated_at`, status mutates | Application funnel from submission through scoring to decision. Not every application becomes a loan. |
| `loans` | `core` | Lending | One row per loan | Incremental on `updated_at`, soft delete | Disbursed loans: product, principal, rate, term, origination vintage. |
| `loan_installments` | `core` | Lending | One row per loan per scheduled installment | Incremental on `updated_at` | Scheduled and actual payment dates and amounts. Source of the delinquency buckets in question 7. |
| `gl_transactions` | `core` | Finance | One row per posting batch | Incremental on `updated_at` | The double-entry header. |
| `gl_entries` | `core` | Finance | One row per ledger line | Incremental on `updated_at` | Double-entry postings. Debits and credits must balance per batch and currency, which is the control in question 16. |
| `fraud_alerts` | `core` | Fraud | One row per alert | Incremental on `updated_at`, disposition mutates | Detection rule, triggering transaction, analyst disposition. Disposition arrives days after the alert, which makes precision a moving measure. |
| `login_sessions` | `core` | Digital | One row per session | Incremental on `updated_at`, high volume | Device fingerprint, IP country, channel and authentication outcome. Source of the unrecognised-device measure in question 19. |
| 29 reference tables | `ref` | Reference | One row per code, or per rate key | Incremental on `updated_at`, deactivation not deletion | Listed below with the dimension that consumes each. |
| `column_classifications` | `platform` | Platform metadata | One row per column in the source database | Generated from this file by `make schema-apply` | Read by the extraction layer at M4 to decide what to tokenise. |
| `simulation_state` | `platform` | Platform metadata | Exactly one row | Written by `make seed` and by every tick | Where the simulation has been advanced to, and the bound invariant 11 asserts audit timestamps against. |
| `tick_log` | `platform` | Platform metadata | One row per tick | Written by every tick | The reconciliation control: what the source says it changed on a given business day. Carries real time, not simulated time. |
| `tick_table_counts` | `platform` | Platform metadata | One row per tick and table | Written by every tick | Per-table counts by operation class, which M4 reconciles bronze against. |
| `tick_deleted_keys` | `platform` | Platform metadata | One row per physically deleted key | Written by a tick that performs a physical delete | The known positive set M7's primary-key reconciliation validates against. |
| `drift_log` | `platform` | Platform metadata | One row per fired drift event | Written by the tick that fires the event | What `make schema-check` adds to the committed dictionary to compute the expected schema. |

### Entity decisions

**`gl_transactions` is new.** Double-entry integrity is a property of a posting batch rather
than of a line, and question 16 cannot be tested without a header to group by. The entity
inventory as written at M0 listed fifteen entities that resolve to `core` tables;
`gl_transactions` is the sixteenth.

**`agent_locations` is a `core` table.** The M0 inventory filed it under the Reference domain,
which describes how slowly it changes rather than which schema holds it. It carries operational
rows keyed by a business identifier, not a code vocabulary, so it belongs in `core`.

**The `products` entity is gone, replaced by three reference tables.** The M0 inventory carried
one `products` entity covering account, card and loan products. They have different attributes
and different consumers: `account_types` carries `is_deposit_taking`, `card_products` carries
the class that drives interchange, and `loan_products` carries a rate and a term. One table
would have been a union of three disjoint column sets with nulls everywhere.

**Load patterns are stated as `updated_at` throughout.** The M0 inventory described
`transactions` as incremental on `created_at` and `gl_entries` as append-only. Design rule 3
gives every table an `updated_at` maintained by a trigger, and `architecture.md` specifies one
watermark path for this source, so there is no second pattern.

## Reference tables and the dimensions that consume them

No reference table is exempt from the coverage rule in `business_questions.md`. Each either
becomes a conformed dimension in its own right, or is consumed as attributes of one.

| Table | Why it exists | Consumed by |
|---|---|---|
| `currencies` | Named in spec 002 section 1 | `dim_currency` |
| `countries` | Named in spec 002 section 1 | Attributes of `dim_account`, `dim_merchant`, `dim_customer`, `dim_agent_location` |
| `mcc_codes` | Named in spec 002 section 1 | Attributes of `dim_merchant` |
| `account_types` | Named in spec 002 section 1 | Attributes of `dim_account` |
| `transaction_types` | Named in spec 002 section 1 | Attributes of `fct_transactions`, and the source of `is_customer_initiated` |
| `channels` | Named in spec 002 section 1 | Attributes of `fct_transactions` and `fct_login_sessions` |
| `payment_schemes` | Named in spec 002 section 1 | Attributes of `fct_payments` |
| `card_products` | Named in spec 002 section 1 | Attributes of `dim_card` |
| `loan_products` | Named in spec 002 section 1 | `dim_loan_product` |
| `interchange_rates` | Named in spec 002 section 1 | `mart_revenue_interchange` |
| `gl_accounts` | Named in spec 002 section 1 | Attributes of `fct_gl_entries` |
| `fraud_rules` | Named in spec 002 section 1 | `dim_detection_rule` |
| `card_product_classes` | Shared join key: `card_products` and `interchange_rates` both key on it, and a string that must match across tables without a foreign key produces a silent no-match | Attributes of `dim_card` |
| `regions` | Shared join key: `countries` and `interchange_rates` both key on it | Attributes of `dim_merchant` |
| `mcc_bands` | Shared join key: `mcc_codes` and `interchange_rates` both key on it | Attributes of `dim_merchant` |
| `account_statuses` | Behavioural attribute `is_open`, which groups five statuses into the flag the activity rules read | Attributes of `dim_account` |
| `transaction_statuses` | Behavioural attribute `is_posted` | Attributes of `fct_transactions` |
| `payment_statuses` | Behavioural attributes `is_declined` and `is_final` | Attributes of `fct_payments` |
| `loan_statuses` | Behavioural attributes `is_open` and `implies_default` | Attributes of `dim_loan_product` consumers and `fct_loan_balance_daily` |
| `loan_application_statuses` | Behavioural attributes `is_decided` and `is_approved`, which the approval rate is literally defined in terms of | Attributes of `fct_loan_applications` |
| `login_outcomes` | Behavioural attribute `is_successful` | Attributes of `fct_login_sessions` |
| `fraud_dispositions` | Behavioural attributes `is_final` and `is_confirmed_fraud`, which alert precision is literally defined in terms of | Attributes of `fct_fraud_alerts` |
| `holder_roles` | Behavioural attributes `is_primary` and `carries_ownership` | `bridge_account_holder` |
| `risk_bands` | Behavioural attributes `band_ordinal`, `pd_lower_bound` and `pd_upper_bound` | `mart_credit_underwriting` |
| `payment_types` | Behavioural attributes `is_customer_initiated` and `direction` | Attributes of `fct_payments` |
| `entry_sides` | Behavioural attribute `sign_multiplier` | Attributes of `fct_gl_entries` |
| `gl_account_types` | Behavioural attribute `normal_side_code` | Attributes of `fct_gl_entries` |
| `gl_source_entities` | Open vocabulary: the kinds of business event that produce a posting batch grow as the bank models more postings, and under a check constraint every new one would be a schema migration. It carries no attribute beyond code, name and is_active, and none is invented for it | Attributes of `fct_gl_entries` |
| `decision_reasons` | Open vocabulary: underwriting reason codes grow as the generator models more decision paths, and under a check constraint every new reason would be a schema migration. It carries no attribute beyond code, name and is_active, and none is invented for it | Attributes of `fct_loan_applications` |

## Value lists enforced by a check constraint

These categorical domains satisfy neither limb of design rule 7: no named model or metric
consumes an attribute of them beyond the code, and each is a closed domain that generated data
cannot extend. They are check constraints on the column that holds them, not tables.

| Column | Permitted values |
|---|---|
| `core.cards.card_status_code` | `issued`, `active`, `blocked`, `expired`, `cancelled`, `replaced` |
| `core.transactions.authorisation_outcome_code` | `approved`, `declined_funds`, `declined_fraud`, `referred`, `timeout` |
| `core.customers.kyc_status_code` | `pending`, `verified`, `review`, `rejected`, `expired` |
| `core.customer_addresses.address_type_code` | `residential`, `correspondence` |
| `ref.account_types.product_class_code` | `current`, `savings`, `loan`, `card_settlement`, `internal` |
| `ref.transaction_types.direction`, `ref.payment_types.direction` | `debit`, `credit` |

## Exceptions

**`ref.interchange_rates` has no `code` column.** Design rule 7 requires one of every lookup
table, and this is not a lookup of codes: it is a rate table keyed on the composite
(card product class, merchant region, MCC band, valid from). Its seed upserts on that composite
rather than on a code.

**No foreign key is left unindexed.** Design rule 12 allows omissions to be listed here with a
reason. There are none: every foreign key constraint in the three schemas carries an index on
its referencing columns, created by the catalogue loop in
`infra/docker/postgres-source/schema/50_audit_and_indexes.sql`, or by a composite index whose
leading columns are the constraint's, which the loop recognises and leaves alone.
`core.gl_transactions.source_entity_code` is the one served that way, by
`ix_gl_transactions_source`.

**`core.gl_transactions.source_entity_id` carries no foreign key, and that is the one exception
to design rule 8.** It is a polymorphic reference: the table it points into is named by
`source_entity_code`, so no single constraint can express it. Four nullable foreign keys, one
per possible target, would read as stricter while being unable to say that exactly one of them
must be set, and would need a fifth the next time the bank posts from a new kind of event.
Referential integrity at this join is therefore replaced by spec 003 invariant 6 and by a data
quality test at M7, and the loss is stated here rather than discovered in the DDL.

**`core.cards` character columns are narrower than elsewhere.** Every one is twelve characters
or fewer, including the two code columns, so that no column on that table can hold a thirteen
to nineteen digit card number. This is what acceptance criterion 4 is measured by, and it is
why `ref.card_products.code` is `varchar(12)` while every other reference code is `varchar(40)`.

## Columns

#### core.account_holders

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `account_holder_id` | bigint | no | `pseudonymous_key` | Surrogate primary key of one account-and-holder pairing, which is a relationship between an account and a person. A pseudonymous key: retained unchanged, never tokenised. | - |
| `account_id` | bigint | no | `pseudonymous_key` | Foreign key to core.accounts.account_id, which resolves to the people who hold the account. A pseudonymous key: retained unchanged, never tokenised. | - |
| `customer_id` | bigint | no | `pseudonymous_key` | Foreign key to core.customers.customer_id, and therefore a reference to a person. A pseudonymous key: retained unchanged in every layer because it is the join path, and never tokenised, because tokenising the pseudonym would break every join without protecting anything the vault does not already protect. | - |
| `holder_role_code` | character varying(40) | no | `non-personal` | Whether this holder is the primary holder, a joint holder or an authorised signatory. | bridge_account_holder, Q3, Q6, Q11 |
| `ownership_weight` | numeric(18,8) | yes | `non-personal` | Share of the balance attributable to this holder, in (0,1]. Null for a role that carries no ownership. The rule that weights sum to one per account is a set property, checked in dq at M7. | bridge_account_holder, Q3, Q6, Q11 |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.accounts

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `account_id` | bigint | no | `pseudonymous_key` | Surrogate primary key, and the join path from any account-keyed row to the people who hold it. A pseudonymous key: retained unchanged, never tokenised. | - |
| `account_number` | character varying(40) | no | `identifier` | Internal account number. | - |
| `iban` | character varying(34) | yes | `identifier` | International bank account number. Null for internal accounts, which have none. | - |
| `account_type_code` | character varying(40) | no | `non-personal` | The product this account is. | dim_account, Q3 |
| `currency_code` | character(3) | no | `non-personal` | Currency the account is denominated in. The companion of both amount columns on this table. | dim_account, Q3 |
| `country_code` | character(2) | no | `non-personal` | Country the account is held in. The cross-border rule compares this against the counterparty country, not the customer's residence. | Cross-border rule, Q13 |
| `account_status_code` | character varying(40) | no | `non-personal` | Current account status. | Active account rule, Q1 |
| `opened_date` | date | no | `non-personal` | Date the account opened. An account attribute rather than a person attribute; the customer-level tenure basis is customers.signup_date. | Active account rule, Q1 |
| `closed_date` | date | yes | `non-personal` | Date the account closed. Null while open, and never earlier than opened_date. | Active account rule, Q1; deposit balance, Q3 |
| `overdraft_limit_amount` | numeric(18,4) | no | `non-personal` | Agreed overdraft facility, zero where none is granted. | - |
| `current_balance_amount` | numeric(18,4) | no | `non-personal` | Current balance, signed. An overdrawn account carries a negative balance, and the deposit balance metric sums it as a net liability position. | fct_account_balance_daily, Q3 |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.agent_locations

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `agent_location_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `agent_location_reference` | character varying(40) | no | `non-personal` | Partner network reference for the location. | - |
| `partner_name` | character varying(200) | no | `non-personal` | Name of the post office or retail partner operating the location. | - |
| `address_line` | character varying(200) | yes | `non-personal` | Street address of the location. | - |
| `city` | character varying(120) | yes | `non-personal` | City or town of the location. | - |
| `postal_code` | character varying(20) | yes | `non-personal` | Postal code of the location. | - |
| `country_code` | character(2) | no | `non-personal` | Country the location is in. | - |
| `active_from_date` | date | no | `non-personal` | First date the location accepted Nordbank cash. | - |
| `active_to_date` | date | yes | `non-personal` | Last date the location accepted Nordbank cash. Null while active. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.cards

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `card_id` | bigint | no | `pseudonymous_key` | Surrogate primary key of a card, which belongs to an account and through it to a person. A pseudonymous key: retained unchanged, never tokenised. | - |
| `card_reference` | character(12) | no | `identifier` | Issuer reference for the card. A reissue is a new card row with a new reference, not an update. | - |
| `account_id` | bigint | no | `pseudonymous_key` | Foreign key to core.accounts.account_id, which resolves to the people who hold the account. A pseudonymous key: retained unchanged, never tokenised. | - |
| `card_product_code` | character varying(12) | no | `non-personal` | The card product. Referenced against a code held to twelve characters, so this table cannot hold a card number. | Interchange rule, Q4 |
| `card_bin` | character(6) | no | `non-personal` | The six-digit bank identification number, the leading digits of the card number. | - |
| `card_last_four` | character(4) | no | `non-personal` | The last four digits of the card number. | - |
| `card_status_code` | character varying(12) | no | `non-personal` | Current card status. Closed value list, enforced by a check constraint: issued, active, blocked, expired, cancelled, replaced. | - |
| `issued_date` | date | no | `non-personal` | Date the card was issued. | - |
| `expiry_date` | date | no | `non-personal` | Date the card expires, always after the issue date. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.customer_addresses

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `customer_address_id` | bigint | no | `pseudonymous_key` | Surrogate primary key of one address version of a person. A pseudonymous key: retained unchanged because it is the join path, never tokenised. | - |
| `customer_id` | bigint | no | `pseudonymous_key` | Foreign key to core.customers.customer_id, and therefore a reference to a person. A pseudonymous key: retained unchanged in every layer because it is the join path, and never tokenised, because tokenising the pseudonym would break every join without protecting anything the vault does not already protect. | - |
| `address_type_code` | character varying(40) | no | `non-personal` | Whether this is the residential or the correspondence address. Closed two-value list, enforced by a check constraint. | - |
| `address_line_1` | character varying(200) | no | `identifier` | First line of the street address. | - |
| `address_line_2` | character varying(200) | yes | `identifier` | Second line of the street address, where there is one. | - |
| `city` | character varying(120) | no | `quasi-identifier` | City or town. | - |
| `postal_code` | character varying(20) | no | `quasi-identifier` | Full postal code. Generalised to left(postal_code, 2) before gold, which is a deliberate simplification: EU postcode formats differ and a production system would apply a country-specific rule. | - |
| `country_code` | character(2) | no | `quasi-identifier` | Country of the address. | - |
| `valid_from_date` | date | no | `quasi-identifier` | First date this address version applies. | - |
| `valid_to_date` | date | yes | `quasi-identifier` | Last date this address version applies. Null while it is the current version. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.customers

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `customer_id` | bigint | no | `pseudonymous_key` | Surrogate primary key, and the platform join path to a person. A pseudonymous key: it is the pseudonym rather than the identifier, so it is never tokenised, and its personal character is neutralised by shredding the vault mappings of the identifiers on this row. | - |
| `customer_reference` | character varying(40) | no | `identifier` | Bank-assigned customer number. The business key every customer-keyed model joins on, and a token after ingest. | - |
| `full_name` | character varying(200) | no | `identifier` | Legal name as captured at onboarding. | - |
| `email` | character varying(320) | yes | `identifier` | Contact email address. | - |
| `phone` | character varying(40) | yes | `identifier` | Contact telephone number in international format. | - |
| `national_identifier` | character varying(60) | yes | `identifier` | National identity number as captured for KYC. | - |
| `date_of_birth` | date | no | `quasi-identifier` | Date of birth. A quasi-identifier, not an identifier: hashing it would destroy the order and distance that age banding needs, and thirty thousand plausible values make the hash weak anyway. | - |
| `kyc_status_code` | character varying(40) | no | `sensitive` | Where the customer stands in identity verification. Closed value list, enforced by a check constraint: pending, verified, review, rejected, expired. | - |
| `risk_band_code` | character varying(40) | yes | `sensitive` | Underwriting risk band the customer currently sits in. | - |
| `signup_date` | date | no | `quasi-identifier` | The date the customer relationship opened. The tenure band before gold is derived from it. | - |
| `residence_country_code` | character(2) | no | `quasi-identifier` | Country of residence as declared at onboarding. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.fraud_alerts

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `fraud_alert_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `alert_reference` | character varying(40) | no | `non-personal` | Reference the alert is known by. | - |
| `transaction_id` | bigint | no | `non-personal` | Foreign key to core.transactions.transaction_id. | - |
| `customer_id` | bigint | no | `pseudonymous_key` | Foreign key to core.customers.customer_id, and therefore a reference to a person. A pseudonymous key: retained unchanged in every layer because it is the join path, and never tokenised, because tokenising the pseudonym would break every join without protecting anything the vault does not already protect. | - |
| `fraud_rule_code` | character varying(40) | no | `non-personal` | The detection rule that fired. | Alert precision, Q10 |
| `fraud_disposition_code` | character varying(40) | no | `sensitive` | What the analyst concluded. Only final dispositions count towards precision; the rest are the pending backlog. | Alert precision, Q10 |
| `alerted_at` | timestamp with time zone | no | `non-personal` | When the rule fired. Not named created_at, which is reserved for the audit column: the loader writes this at a historical instant and the two would otherwise disagree on every backfilled row. | Alert precision, Q10 |
| `dispositioned_at` | timestamp with time zone | yes | `non-personal` | When the analyst closed the case. Null while open, and alerts are attributed to the month of this date rather than the month they fired. | Alert precision, Q10 |
| `alert_score` | numeric(18,8) | no | `sensitive` | Model score for the alert, as a decimal fraction between zero and one. | - |
| `analyst_reference` | character varying(40) | yes | `identifier` | The analyst who dispositioned the alert. Personal data about a member of staff, so it is an identifier like any other. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.gl_entries

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `gl_entry_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `gl_transaction_id` | bigint | no | `non-personal` | Foreign key to core.gl_transactions.gl_transaction_id. | - |
| `posting_date` | date | no | `non-personal` | The ledger date, held equal to the batch header by the composite foreign key on (gl_transaction_id, posting_date). | GL integrity, Q16; settlement reconciliation, Q15 |
| `gl_account_code` | character varying(40) | no | `non-personal` | The general ledger account posted to. | GL integrity, Q16 |
| `entry_side_code` | character(1) | no | `non-personal` | Debit or credit. Bound to the sign of amount by a check constraint so the two can never disagree. | GL integrity, Q16 |
| `amount` | numeric(18,4) | no | `non-personal` | Signed amount: debit positive, credit negative, so a balanced batch sums to zero per currency. This is the documented exception to the non-negative amount guidance. | GL integrity, Q16; settlement reconciliation, Q15 |
| `entry_currency_code` | character(3) | no | `non-personal` | Currency of amount. Balance is enforced per currency, not across them. | GL integrity, Q16; settlement reconciliation, Q15 |
| `account_id` | bigint | yes | `pseudonymous_key` | Foreign key to core.accounts.account_id, which resolves to the people who hold the account. A pseudonymous key: retained unchanged, never tokenised. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.gl_transactions

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `gl_transaction_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `gl_transaction_reference` | character varying(40) | no | `non-personal` | Reference the posting batch is known by. | - |
| `posting_date` | date | no | `non-personal` | The ledger date the batch posts to. Authoritative: gl_entries carries the same date and a composite foreign key keeps the two equal. | GL integrity, Q16 |
| `description` | character varying(200) | yes | `non-personal` | What the batch represents. | - |
| `source_entity_code` | character varying(40) | yes | `non-personal` | Which kind of business event produced this batch, from ref.gl_source_entities. The vocabulary half of a polymorphic reference: it names the core table source_entity_id points into. | fct_gl_entries, Q16, spec 003 invariant 6 |
| `source_entity_id` | bigint | yes | `pseudonymous_key` | The key of the event that produced this batch, in the table source_entity_code names. Polymorphic, so it carries no foreign key and referential integrity at this join is replaced by spec 003 invariant 6 and a dq test at M7. Classified as a pseudonymous key because it can reference core.loans, which is person-bearing; a column carries one classification and this one takes the more protective of the two it could hold. | fct_gl_entries, Q16, spec 003 invariant 6 |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.loan_applications

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `loan_application_id` | bigint | no | `pseudonymous_key` | Surrogate primary key of an application made by one person. A pseudonymous key: retained unchanged, never tokenised. | - |
| `application_reference` | character varying(40) | no | `non-personal` | Reference the application is known by. | - |
| `customer_id` | bigint | no | `pseudonymous_key` | Foreign key to core.customers.customer_id, and therefore a reference to a person. A pseudonymous key: retained unchanged in every layer because it is the join path, and never tokenised, because tokenising the pseudonym would break every join without protecting anything the vault does not already protect. | - |
| `loan_product_code` | character varying(40) | no | `non-personal` | The product applied for. | - |
| `loan_application_status_code` | character varying(40) | no | `non-personal` | Where the application is in the funnel. Approved and rejected are the decided statuses. | Approval rate, Q8 |
| `risk_band_code` | character varying(40) | yes | `sensitive` | Risk band assigned at decision. Null until decided, and never restated to the customer's band today. | Approval rate, Q8 |
| `decision_reason_code` | character varying(40) | yes | `sensitive` | Why the decision went the way it did. An open vocabulary held in ref.decision_reasons, so a new reason is a seed row rather than a migration. | - |
| `applied_at` | timestamp with time zone | no | `non-personal` | When the application was submitted. | mart_credit_loan_lifecycle, Q9 |
| `decided_at` | timestamp with time zone | yes | `non-personal` | When the decision was taken. Null while undecided; applications are attributed to the month of this date. | Approval rate, Q8 |
| `requested_amount` | numeric(18,4) | no | `non-personal` | Amount the customer asked for. | - |
| `approved_amount` | numeric(18,4) | yes | `non-personal` | Amount approved, which can be less than requested. Null unless approved. | - |
| `application_currency_code` | character(3) | no | `non-personal` | Currency of the requested and approved amounts. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.loan_installments

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `loan_installment_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `loan_id` | bigint | no | `pseudonymous_key` | Foreign key to core.loans.loan_id, which resolves to the person who holds the loan. A pseudonymous key: retained unchanged, never tokenised. | - |
| `installment_number` | smallint | no | `non-personal` | Position in the repayment schedule, starting at one. | - |
| `due_date` | date | no | `non-personal` | Date the installment falls due. Days past due is measured from the oldest unpaid one. | Delinquency rule, Q7 |
| `due_amount` | numeric(18,4) | no | `non-personal` | Amount contractually due. | Delinquency rule, Q7 |
| `paid_amount` | numeric(18,4) | no | `non-personal` | Amount paid so far. An installment is not fully paid while this is below due_amount. | Delinquency rule, Q7 |
| `paid_at` | timestamp with time zone | yes | `non-personal` | When the installment was settled. Null while unpaid. | mart_credit_loan_lifecycle, Q9 |
| `installment_currency_code` | character(3) | no | `non-personal` | Currency of the due and paid amounts. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.loans

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `loan_id` | bigint | no | `pseudonymous_key` | Surrogate primary key of a contract held by one person. A pseudonymous key: retained unchanged, never tokenised. | - |
| `loan_reference` | character varying(40) | no | `non-personal` | Reference the loan is known by. | - |
| `loan_application_id` | bigint | no | `pseudonymous_key` | Foreign key to core.loan_applications.loan_application_id, which resolves to the person who applied. A pseudonymous key: retained unchanged, never tokenised. | - |
| `customer_id` | bigint | no | `pseudonymous_key` | Foreign key to core.customers.customer_id, and therefore a reference to a person. A pseudonymous key: retained unchanged in every layer because it is the join path, and never tokenised, because tokenising the pseudonym would break every join without protecting anything the vault does not already protect. | - |
| `loan_product_code` | character varying(40) | no | `non-personal` | The product the loan was written under. | - |
| `loan_status_code` | character varying(40) | no | `non-personal` | Current loan status. | Default rule, Q8 |
| `disbursed_date` | date | no | `non-personal` | Date the money left the bank. The origination vintage is the month of this date, and it never changes, including after restructuring. | Origination vintage, Q5, Q7 |
| `maturity_date` | date | no | `non-personal` | Contractual final repayment date. | - |
| `written_off_date` | date | yes | `non-personal` | Date the bank wrote the loan off. Null unless written off. | Default rule, Q8 |
| `principal_amount` | numeric(18,4) | no | `non-personal` | Amount disbursed. | fct_loan_balance_daily, Q5, Q7 |
| `loan_currency_code` | character(3) | no | `non-personal` | Currency of principal_amount. | - |
| `nominal_annual_rate` | numeric(18,8) | no | `non-personal` | Contractual nominal annual rate as a decimal fraction, copied from the product at disbursement. A disbursed loan keeps the terms it was written under. | Net interest income proxy, Q5 |
| `term_months` | smallint | no | `non-personal` | Contractual term in months. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.login_sessions

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `login_session_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `session_reference` | character varying(40) | no | `non-personal` | Reference the session is known by. | - |
| `customer_id` | bigint | no | `pseudonymous_key` | Foreign key to core.customers.customer_id, and therefore a reference to a person. A pseudonymous key: retained unchanged in every layer because it is the join path, and never tokenised, because tokenising the pseudonym would break every join without protecting anything the vault does not already protect. | - |
| `channel_code` | character varying(40) | no | `non-personal` | The channel the login came through. | Unrecognised device, Q19 |
| `login_outcome_code` | character varying(40) | no | `non-personal` | Whether the login succeeded, and how it failed if it did not. The unrecognised device rule looks only at successful sessions. | Unrecognised device, Q19 |
| `started_at` | timestamp with time zone | no | `non-personal` | When the login attempt started. | Unrecognised device, Q19 |
| `ended_at` | timestamp with time zone | yes | `non-personal` | When the session ended. Null while open. | - |
| `device_fingerprint` | character varying(128) | no | `identifier` | Device fingerprint. A device is unrecognised when this has not appeared in the customer's successful sessions in the trailing ninety days. | Unrecognised device, Q19 |
| `ip_address` | character varying(45) | no | `identifier` | Source address, held as text rather than inet because a keyed hash replaces it at ingest and a hash is not an inet. | - |
| `ip_country_code` | character(2) | yes | `quasi-identifier` | Country the address resolves to, resolved here because the address itself is tokenised and the geography cannot be recovered from a hash. | mart_fraud_device_risk, Q19 |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.merchants

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `merchant_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `merchant_reference` | character varying(40) | no | `non-personal` | Acquirer-side merchant identifier. | - |
| `merchant_name` | character varying(200) | no | `non-personal` | Merchant name as the acquirer sent it. Dirty by design: casing, punctuation and trailing location noise are left alone so conformance has something real to do in silver. | - |
| `mcc_code` | character(4) | no | `non-personal` | Merchant category code. | Interchange rule, Q4 |
| `country_code` | character(2) | no | `non-personal` | Country the merchant is acquired in. | Interchange rule, Q4 |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.payments

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `payment_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `payment_reference` | character varying(40) | no | `non-personal` | Reference the payment instruction is known by. | - |
| `account_id` | bigint | no | `pseudonymous_key` | Foreign key to core.accounts.account_id, which resolves to the people who hold the account. A pseudonymous key: retained unchanged, never tokenised. | - |
| `payment_type_code` | character varying(40) | no | `non-personal` | What kind of payment instruction this is. | Customer-initiated rule, Q1, Q6 |
| `payment_scheme_code` | character varying(40) | no | `non-personal` | The scheme the payment is sent over. | Cross-border rule, Q13 |
| `payment_status_code` | character varying(40) | no | `non-personal` | Current status of the instruction. | Cross-border rule, Q13 |
| `initiated_at` | timestamp with time zone | no | `non-personal` | When the customer or the scheme raised the instruction. | - |
| `booked_at` | timestamp with time zone | yes | `non-personal` | When the account was debited or credited. Earlier than settlement for an outbound SEPA payment, and null until the payment books. | Active account rule, Q1 |
| `settled_at` | timestamp with time zone | yes | `non-personal` | When the scheme settled. Null until then, and never before booked_at. | - |
| `payment_amount` | numeric(18,4) | no | `non-personal` | Amount in the payment currency, always positive; direction comes from the payment type. | - |
| `payment_currency_code` | character(3) | no | `non-personal` | Currency of payment_amount. | - |
| `counterparty_iban` | character varying(34) | yes | `identifier` | Counterparty account number. | - |
| `counterparty_name` | character varying(200) | yes | `identifier` | Counterparty name. What a sanctions screen matches on, through the vault rather than in silver. | fct_sanctions_screening, Q12 |
| `counterparty_country_code` | character(2) | yes | `non-personal` | Country of the counterparty institution. Cross-border is this against the originating account's country. | Cross-border rule, Q13 |
| `remittance_reference` | character varying(140) | yes | `quasi-identifier` | Free text the payer attached. Classified quasi-identifier because its content is unknown and may carry personal detail. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### core.transactions

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `transaction_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `transaction_reference` | character varying(40) | no | `non-personal` | Reference the transaction is known by outside the platform. | - |
| `account_id` | bigint | no | `pseudonymous_key` | Foreign key to core.accounts.account_id, which resolves to the people who hold the account. A pseudonymous key: retained unchanged, never tokenised. | - |
| `card_id` | bigint | yes | `pseudonymous_key` | Foreign key to core.cards.card_id, which resolves through the account to a person. A pseudonymous key: retained unchanged, never tokenised. | - |
| `merchant_id` | bigint | yes | `non-personal` | The merchant, where there is one. Null for cash and transfers. | - |
| `agent_location_id` | bigint | yes | `non-personal` | The partner location, for a cash-in or cash-out. Null otherwise. | - |
| `transaction_type_code` | character varying(40) | no | `non-personal` | What kind of transaction this is. The customer-initiated flag is derived from this through ref.transaction_types, not from a list held in code. | Customer-initiated rule, Q1, Q6, Q11 |
| `channel_code` | character varying(40) | no | `non-personal` | The channel the transaction came through. | Structuring, Q11; Q19 |
| `transaction_status_code` | character varying(40) | no | `non-personal` | Current status. Posted and settled are the statuses that count as posted. | Active account rule, Q1 |
| `authorisation_outcome_code` | character varying(40) | yes | `non-personal` | Outcome of the card authorisation. Null for anything not authorised through the card rails. Closed value list, enforced by a check constraint: approved, declined_funds, declined_fraud, referred, timeout. | - |
| `booked_at` | timestamp with time zone | no | `non-personal` | When the transaction hit the account. | Active account rule, Q1; structuring, Q11 |
| `value_date` | date | no | `non-personal` | The date the transaction takes effect for interest, which can differ from the booking date. | - |
| `transaction_amount` | numeric(18,4) | no | `non-personal` | Amount in the transaction currency, signed by direction. | fct_transactions, Q4, Q6, Q11 |
| `transaction_currency_code` | character(3) | no | `non-personal` | Currency of transaction_amount. | fct_transactions, Q4, Q6, Q11 |
| `is_card_present` | boolean | yes | `non-personal` | Whether the card was physically presented. Decided per card authorisation, not derived from the channel: a mobile wallet tap at a terminal is card present and an ecommerce purchase from the same handset is not. Null exactly when card_id is null, because presentment is meaningless without a card authorisation. | fct_transactions, Q10, Q19 |
| `reversal_of_transaction_id` | bigint | yes | `non-personal` | The transaction this one reverses. Null unless this is a reversal, and never itself. | - |
| `counterparty_reference` | character varying(140) | yes | `identifier` | Counterparty account or descriptor as the scheme sent it. Classified identifier because it can carry an IBAN. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
| `is_deleted` | boolean | no | `non-personal` | Soft delete flag. A deleted entity is removed from silver; the row itself stays. | - |

#### platform.column_classifications

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `column_classification_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `schema_name` | character varying(63) | no | `non-personal` | Schema the classified column belongs to. | - |
| `table_name` | character varying(63) | no | `non-personal` | Table the classified column belongs to. | - |
| `column_name` | character varying(63) | no | `non-personal` | The classified column. | - |
| `classification` | character varying(20) | no | `non-personal` | One of identifier, quasi-identifier, sensitive or non-personal. | - |
| `rationale` | text | no | `non-personal` | Why the column carries that classification, taken from its description in the data dictionary. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### platform.drift_log

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `drift_log_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `event_name` | character varying(100) | no | `non-personal` | The drift event from the timeline in generator/drift/, unique so an event cannot fire twice. | schema-check |
| `drift_type` | character varying(40) | no | `non-personal` | What kind of change the event made. Closed value list, enforced by a check constraint: column_added, type_widened. | schema-check |
| `target_schema` | character varying(63) | no | `non-personal` | Schema the event changed. | schema-check |
| `target_table` | character varying(63) | no | `non-personal` | Table the event changed. | schema-check |
| `target_column` | character varying(63) | no | `non-personal` | Column the event added or retyped. | schema-check |
| `simulated_date` | date | no | `non-personal` | The simulated date the event was scheduled for and fired on. | - |
| `tick_sequence` | integer | no | `non-personal` | The tick that applied it. | - |
| `applied_at` | timestamp with time zone | no | `non-personal` | Real time the DDL ran. A platform table records when it actually happened, not the date it simulates. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### platform.simulation_state

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `simulation_state_id` | bigint | no | `non-personal` | Surrogate primary key. The table holds exactly one row, enforced by a unique index on a constant. | - |
| `profile` | character varying(20) | no | `non-personal` | The profile the source was seeded at, one of ci, dev or full. | - |
| `seed` | bigint | no | `non-personal` | The run seed the source was generated under, and the seed every tick derives its substream from. | - |
| `anchor_date` | date | no | `non-personal` | The date the historical load ended on. make seed resets simulated_date to it. | - |
| `simulated_date` | date | no | `non-personal` | The date the source has been advanced to, and the upper bound invariant 11 asserts audit timestamps against. Equal to anchor_date before the first tick. | Invariant 11 |
| `tick_sequence` | integer | no | `non-personal` | Ticks completed since the last seed. Zero before the first tick. | - |
| `last_tick_completed_at` | timestamp with time zone | yes | `non-personal` | Real time the last tick committed. Null before the first tick. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### platform.tick_deleted_keys

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `tick_deleted_key_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `tick_log_id` | bigint | no | `non-personal` | The tick that performed the delete. | - |
| `table_name` | character varying(63) | no | `non-personal` | The core table the row was removed from. | M7 reconciliation |
| `deleted_key` | bigint | no | `pseudonymous_key` | The primary key that no longer exists in the source, so that M7's primary-key reconciliation has a known positive set rather than a count. Classified as a pseudonymous key because the only physical delete the source performs is of a dependent-free duplicate customer, so the value is a customer_id: retained unchanged, never tokenised. | M7 reconciliation |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### platform.tick_log

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `tick_log_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `tick_sequence` | integer | no | `non-personal` | Position of this tick in the sequence since the last seed, unique so a sequence number cannot be reused. | - |
| `simulated_date` | date | no | `non-personal` | The business day this tick advanced the source to. Its change window is this date to the next, and acceptance criterion 9 reconciles the counts against rows whose updated_at falls inside it. | M4 reconciliation |
| `profile` | character varying(20) | no | `non-personal` | The profile in force when the tick ran. | - |
| `seed` | bigint | no | `non-personal` | The run seed the tick derived its substream from, so a tick can be replayed from the log alone. | - |
| `started_at` | timestamp with time zone | no | `non-personal` | Real time the tick began. A platform table records when it actually happened, not the date it simulates. | - |
| `completed_at` | timestamp with time zone | no | `non-personal` | Real time the tick committed. | - |
| `duration_ms` | integer | no | `non-personal` | How long the tick took, in milliseconds, which is what acceptance criterion 13 is measured from. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### platform.tick_table_counts

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `tick_table_count_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `tick_log_id` | bigint | no | `non-personal` | The tick these counts belong to. | M4 reconciliation |
| `table_name` | character varying(63) | no | `non-personal` | The core table the counts describe. | M4 reconciliation |
| `rows_inserted` | integer | no | `non-personal` | Rows the tick inserted into this table. | M4 reconciliation |
| `rows_updated` | integer | no | `non-personal` | Rows the tick changed in place. | M4 reconciliation |
| `rows_soft_deleted` | integer | no | `non-personal` | Rows the tick marked is_deleted. A subset of rows_updated, since setting the flag is an update. | M4 reconciliation |
| `rows_late_arriving` | integer | no | `non-personal` | Rows inserted by this tick whose business timestamp is earlier than its simulated date. A subset of rows_inserted rather than a fourth disjoint class, because the row is both. | M4 reconciliation |
| `rows_deleted` | integer | no | `non-personal` | Rows the tick physically removed. The only physical delete the source performs is of a dependent-free duplicate customer record, so that M7's reconciliation control has a real instance of the condition it detects. | M7 reconciliation |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.account_statuses

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `account_status_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_open` | boolean | no | `non-personal` | Whether an account in this status is open. Groups five statuses into the flag the activity rules read. | Active account rule, Q1 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.account_types

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `account_type_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `product_class_code` | character varying(40) | no | `non-personal` | Product class of the account type. Closed value list, enforced by a check constraint: current, savings, loan, card_settlement, internal. | Deposit balance, Q3 |
| `is_deposit_taking` | boolean | no | `non-personal` | Whether balances on this account type count towards customer deposits. What the deposit balance metric filters on. | Deposit balance, Q3 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.card_product_classes

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `card_product_class_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.card_products

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `card_product_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(12) | no | `non-personal` | Product code, held to twelve characters because core.cards references it. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `product_class_code` | character varying(40) | no | `non-personal` | The class that drives the interchange rate. | Interchange rule, Q4 |
| `network` | character varying(40) | no | `non-personal` | Card network the product is issued on. | - |
| `is_commercial` | boolean | no | `non-personal` | Whether the product is a commercial card, which falls outside the regulated caps. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.channels

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `channel_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_digital` | boolean | no | `non-personal` | Whether the channel is a digital one. | mart_fraud_device_risk, Q19 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.countries

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `country_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character(2) | no | `non-personal` | ISO 3166-1 alpha-2 country code. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `region_code` | character varying(40) | no | `non-personal` | The region used for interchange pricing. | Interchange rule, Q4 |
| `is_eea` | boolean | no | `non-personal` | Whether the country is in the European Economic Area, which is what the regulated interchange cap turns on. | Interchange rule, Q4; cross-border, Q13 |
| `is_sepa` | boolean | no | `non-personal` | Whether the country is a SEPA participant. Every EEA state is; the converse does not hold. | Cross-border rule, Q13 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.currencies

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `currency_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character(3) | no | `non-personal` | ISO 4217 alphabetic currency code. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `minor_unit` | smallint | no | `non-personal` | ISO 4217 exponent: the number of decimals the currency is quoted in. Not the storage scale, which is always four. | Presentation rounding |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.decision_reasons

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `decision_reason_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.entry_sides

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `entry_side_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character(1) | no | `non-personal` | D for debit, C for credit. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `sign_multiplier` | smallint | no | `non-personal` | Plus one for a debit, minus one for a credit. The same fact the check constraint on core.gl_entries enforces row by row. | GL integrity, Q16 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.fraud_dispositions

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `fraud_disposition_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_final` | boolean | no | `non-personal` | Whether the analyst has closed the case. Alerts that are not final are the pending backlog. | Alert precision, Q10 |
| `is_confirmed_fraud` | boolean | no | `non-personal` | Whether the case was confirmed as fraud. The numerator of alert precision, and never true unless is_final is. | Alert precision, Q10 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.fraud_rules

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `fraud_rule_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Detection rule identifier. Q10 groups by it, which is why a rule is an entity rather than a string on an alert. | dim_detection_rule, Q10 |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.gl_account_types

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `gl_account_type_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `normal_side_code` | character(1) | no | `non-personal` | The side this account type normally carries a balance on. A function of the type, so gl_accounts does not store a second copy. | GL integrity, Q16 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.gl_accounts

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `gl_account_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | General ledger account code. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `gl_account_type_code` | character varying(40) | no | `non-personal` | The account type, which is also where the normal balance side comes from. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.gl_source_entities

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `gl_source_entity_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code naming the kind of business event that produced a posting batch, and the vocabulary half of the polymorphic reference on core.gl_transactions. | fct_gl_entries, Q16, spec 003 invariant 6 |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. Each row names the core table its source_entity_id points into, because a polymorphic reference that does not say where it points is unreadable. Two codes are seeded, because the M2 load produces two kinds of posting; the vocabulary grows with the postings rather than ahead of them. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.holder_roles

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `holder_role_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_primary` | boolean | no | `non-personal` | Whether the role is the primary holder. | bridge_account_holder, Q3, Q6 |
| `carries_ownership` | boolean | no | `non-personal` | Whether the role owns part of the balance. False is what makes a null ownership_weight legitimate. | bridge_account_holder, Q3, Q6 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.interchange_rates

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `interchange_rate_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `card_product_class_code` | character varying(40) | no | `non-personal` | Card product class the rate applies to. | - |
| `merchant_region_code` | character varying(40) | no | `non-personal` | Merchant region the rate applies to. | - |
| `mcc_band_code` | character varying(40) | no | `non-personal` | MCC band the rate applies to. | - |
| `rate` | numeric(18,8) | yes | `non-personal` | Interchange rate as a decimal fraction. Null where the rate is undecided, which is an error condition at M6 and never treated as zero. | Interchange rule, Q4, Q6 |
| `valid_from_date` | date | no | `non-personal` | First date the rate applies. | - |
| `valid_to_date` | date | yes | `non-personal` | Last date the rate applies. Null while current. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.loan_application_statuses

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `loan_application_status_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_decided` | boolean | no | `non-personal` | Whether the application has been decided. Decided is the denominator of the approval rate. | Approval rate, Q8 |
| `is_approved` | boolean | no | `non-personal` | Whether the application was approved. The numerator of the approval rate, and never true unless is_decided is. | Approval rate, Q8 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.loan_products

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `loan_product_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `nominal_annual_rate` | numeric(18,8) | no | `non-personal` | Current nominal annual rate as a decimal fraction. A disbursed loan copies it rather than following it. | Net interest income proxy, Q5 |
| `term_months` | smallint | no | `non-personal` | Contractual term in months. | - |
| `is_secured` | boolean | no | `non-personal` | Whether the product is secured on an asset. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.loan_statuses

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `loan_status_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_open` | boolean | no | `non-personal` | Whether the loan is still outstanding. | fct_loan_balance_daily, Q5, Q7 |
| `implies_default` | boolean | no | `non-personal` | Whether the status alone puts the loan in default, without reference to days past due. | Default rule, Q8 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.login_outcomes

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `login_outcome_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_successful` | boolean | no | `non-personal` | Whether the login succeeded. The unrecognised device rule counts successful sessions only. | Unrecognised device, Q19 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.mcc_bands

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `mcc_band_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.mcc_codes

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `mcc_code_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character(4) | no | `non-personal` | Four-digit merchant category code. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `category` | character varying(80) | no | `non-personal` | Grouping Q4 reports interchange revenue by. | Interchange revenue, Q4 |
| `band_code` | character varying(40) | no | `non-personal` | Interchange pricing band for the category. | Interchange rule, Q4 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.payment_schemes

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `payment_scheme_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_sepa` | boolean | no | `non-personal` | Whether the scheme is a SEPA retail scheme. | Cross-border rule, Q13 |
| `settlement_days` | smallint | no | `non-personal` | Business days from instruction to settlement under the scheme. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.payment_statuses

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `payment_status_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_declined` | boolean | no | `non-personal` | Whether the status means the payment was refused. The numerator of the decline rate. | mart_payments_cross_border, Q13 |
| `is_final` | boolean | no | `non-personal` | Whether the status is terminal. | mart_payments_cross_border, Q13 |
| `is_posted` | boolean | no | `non-personal` | Whether the payment's amount is reflected in the account balance. The mirror of ref.transaction_statuses.is_posted and the payment half of the balance reconciliation. A returned payment is posted: it did debit the account, and the return arrives as its own scheme_return row. | fct_account_balance_daily, Q3, spec 003 invariant 4 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.payment_types

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `payment_type_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_customer_initiated` | boolean | no | `non-personal` | Whether the customer caused the payment. A direct debit is, because the customer signed the mandate. | Customer-initiated rule, Q1, Q6 |
| `direction` | character varying(10) | no | `non-personal` | Debit or credit against the customer account. | fct_payments |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.regions

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `region_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.risk_bands

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `risk_band_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `band_ordinal` | smallint | no | `non-personal` | Rank of the band, lowest risk first. What orders the bands in a report. | mart_credit_underwriting, Q8 |
| `pd_lower_bound` | numeric(18,8) | no | `non-personal` | Lower bound of the probability of default range, inclusive, as a decimal fraction. | mart_credit_underwriting, Q8 |
| `pd_upper_bound` | numeric(18,8) | no | `non-personal` | Upper bound of the probability of default range, exclusive, as a decimal fraction. | mart_credit_underwriting, Q8 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.transaction_statuses

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `transaction_status_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_posted` | boolean | no | `non-personal` | Whether a transaction in this status has posted. Only posted transactions make an account active. | Active account rule, Q1 |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |

#### ref.transaction_types

| Column | Type | Nullable | Classification | Description | Consumed by |
|---|---|---|---|---|---|
| `transaction_type_id` | bigint | no | `non-personal` | Surrogate primary key. | - |
| `code` | character varying(40) | no | `non-personal` | Stable business code. What core tables reference. | - |
| `name` | character varying(120) | no | `non-personal` | Human readable name. | - |
| `is_customer_initiated` | boolean | no | `non-personal` | Whether the customer caused the transaction. The single source of the derived flag; it lives here, not in code. | Customer-initiated rule, Q1, Q6 |
| `direction` | character varying(10) | no | `non-personal` | Debit or credit. A property of the type, so core.transactions does not carry a second copy. | fct_transactions |
| `description` | text | yes | `non-personal` | Free text note on the row, where one is useful. | - |
| `is_active` | boolean | no | `non-personal` | Whether the code is currently in use. An inactive row is retained, because a dimension must still describe facts that reference a retired code. | - |
| `created_at` | timestamp with time zone | no | `non-personal` | When the row was inserted. An audit column: no business timestamp may take this name. | - |
| `updated_at` | timestamp with time zone | no | `non-personal` | When the row last changed, set by the core.set_updated_at trigger and never by application code. The watermark the extraction layer reads. | - |
