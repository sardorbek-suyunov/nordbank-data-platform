# Data dictionary

Entity-level inventory: what exists, where it comes from, at what grain, and how it loads.
Column-level detail, types and nullability are defined in spec 002 and in the contracts under
`contracts/`.

`Load pattern` describes how the entity reaches bronze. `Grain` is the grain of the source
entity, not of any downstream model.

| Entity | Domain | Grain | Load pattern | Notes |
|---|---|---|---|---|
| `customers` | Customer | One row per customer | Incremental on `updated_at`, soft delete, SCD2 in silver | Identity, KYC status, signup date, residence country, risk band. Source of the PII tokens every other customer-keyed model uses. |
| `customer_addresses` | Customer | One row per customer address version | Incremental on `updated_at`, history retained | Residential and correspondence addresses. Drives country attribution for reporting and cross-border checks. |
| `accounts` | Deposits | One row per account | Incremental on `updated_at`, soft delete | Current and savings accounts: currency, product, opening and closing date, status. Basis of questions 1 and 3. |
| `cards` | Cards | One row per card | Incremental on `updated_at`, soft delete | Issued cards, network, status, linked account. A card is reissued as a new row, not an update, when the PAN changes. |
| `merchants` | Cards | One row per merchant | Incremental on `updated_at` | Acquirer-side merchant identity, MCC, country. Merchant names are dirty by design, to require conformance in silver. |
| `mcc_codes` | Reference | One row per merchant category code | Full refresh, dbt seed | Maps MCC to category and interchange band. Static reference, version controlled as a seed. |
| `transactions` | Cards | One row per card transaction | Incremental append on `created_at`, late arrivals expected | Authorisation and clearing amounts, transaction currency, FX at capture. The highest-volume entity and the partitioning test case. |
| `payments` | Payments | One row per payment instruction | Incremental on `updated_at`, status mutates | SEPA and cross-border transfers: corridor, status, decline reason. Basis of question 13. |
| `loans` | Lending | One row per loan | Incremental on `updated_at`, soft delete | Disbursed loans: product, principal, rate, term, origination vintage. |
| `loan_applications` | Lending | One row per application | Incremental on `updated_at`, status mutates | Application funnel from submission through scoring to decision. Not every application becomes a loan. |
| `loan_installments` | Lending | One row per loan per scheduled installment | Incremental on `updated_at` | Scheduled and actual payment dates and amounts. Source of the delinquency buckets in question 7. |
| `gl_entries` | Finance | One row per ledger line | Append only, immutable at source | Double-entry postings. Debits and credits must balance per posting date, which is the control in question 16. |
| `fraud_alerts` | Fraud | One row per alert | Incremental on `updated_at`, disposition mutates | Detection rule id, triggering transaction, analyst disposition. Disposition arrives days after the alert, which makes precision a moving measure. |
| `login_sessions` | Digital | One row per session | Append only, high volume | Device, IP country, authentication outcome. Feeds account activity and the fraud signals. |
| `products` | Reference | One row per product version | Incremental on `updated_at`, SCD2 in silver | Account, card and loan product catalogue with fees and rates. Product terms change over time and reporting must use the terms in force. |
| `branches` | Reference | One row per branch | Full refresh, small volume | Servicing locations and their country. Small dimension used for attribution. |
| `fx_rates` | Market data | One row per currency pair per rate date | Incremental by date, gaps carried forward | ECB reference rates from the Frankfurter API. Every EUR conversion in the platform resolves here. |
| `sanctions_entities` | Compliance | One row per sanctioned entity per snapshot version | Weekly full refresh, versioned snapshot | OpenSanctions consolidated list. Screening results cite the version they matched against, so past decisions stay explainable. |
