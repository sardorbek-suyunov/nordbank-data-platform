# 0020 — Silver historises by a projection-based version rule, from the epoch

Status: Accepted
Date: 2026-10-03
Revised: 2026-10-04, before its milestone closed and on review of specification 008: a key
restored after a soft delete opens a new version, and the gap between the delete and the restore
is the only gap the interval test allows.

## Context

Bronze holds every extraction of every row (ADR 0018): a key recurs once per batch that read it,
and nothing is deduplicated. Silver makes it one row per version for the entities whose history
a question reads, and one row per entity for the rest. Specification 008's planning measured the
`ci` book on the throwaway stack with sixty-one days of history before deciding how:

- **No key and `updated_at` pair carries two different states** across 45 entities, so a
  re-read is the same version read twice, never a conflict.
- **At most one version per key per day.** Ticks stamp event instants and fixed 23:45 and 02:00
  stamps, and extraction reads one state per key per watermark window. Silver cannot see more
  than one change a day because bronze never holds more; that is a property of extraction, not
  of the rule.
- **99.8 per cent of account changes move only the balance**: 13,441 of 13,469. A version per
  change would make `sl_accounts` a balance history with the account's attributes repeated.
- **A change the contract cannot see.** After the scripted additive drift, a merchant can be
  revised in `merchant_risk_score` alone, a column the contract omits: 59 such observations over
  58 merchants, every contract column equal to the one before and a new `updated_at`.
- **The first observed state is not the first state.** The historical load writes each entity as
  it stands at the anchor, and 28 of the 29 `ref` tables are stamped with the wall-clock time the
  seed ran. A first version opened at its `updated_at` leaves 64 per cent of transactions without
  an account version and every transaction without a reference version.
- **Two clocks on one table.** `customer_addresses` records business validity, `valid_from_date`
  to an inclusive `valid_to_date`, beside the system time `updated_at`. No backdated address
  exists in the data; 17 closures are recorded the day after their inclusive last day.

## Decision

- **A version is a key and its `updated_at`.** Re-reads collapse to the earliest batch, and a test
  asserts on every entity that re-reads agree on every contract column.
- **The version rule.** An observation opens a version when a column of the model's projection is
  distinct from the previous observation's, compared column by column with `is distinct from`
  so a null is a value, or when no contract column changed at all, which is a change the contract
  does not describe. A change confined to a measure excluded from the projection opens none.
- **A measure is excluded only when no question reads its history from that entity.**
  `accounts.current_balance_amount` is excluded and is not carried; every observed balance lands
  in `sl_account_balance_observations`, one row per account per observation, for M7's
  reconciliation. `loan_installments.paid_amount` stays in the projection, because Q7 reads its
  history.
- **The epoch.** A key's first version opens at 1900-01-01T00:00:00Z, later versions at their
  `updated_at`, and the current version closes at 9999-12-31, never null. The first observed state
  is backdated as the earliest known state, not the true state: changes before the first
  extraction are invisible, because the historical load writes final state.
- **Two clocks.** Silver keeps system time as SCD2 and exposes business validity half-open beside
  it, from `valid_from_date` to `valid_to_date` plus one day. A fact joins on business validity,
  read from the current system-time version, so a correction recorded late moves a past fact to
  the corrected row. `ref.interchange_rates` is treated the same way.
- **Soft deletes.** In an SCD2 model a soft delete closes the final version at the instant it was
  recorded and leaves no current version; the history stays. A later observation that is not deleted
  restores the key and opens a new version. A latest-state model excludes a
  deleted row. A `ref` row is deactivated, never deleted, and is retained as a version, current
  with `is_active` false. A soft delete is not erasure, which is the vault's, at M8.

## Consequences

- `sl_accounts` holds 721 versions over 690 accounts on the history instead of about 14,000;
  `sl_account_balance_observations` holds all 14,159 observed balances.
- **The epoch is a claim about knowledge, not about the past.** An account's first version is
  valid from 1900, which says "the earliest state we know", and a reader who takes `_valid_from`
  for an opening date is wrong. The business dates that matter, `opened_date`, `signup_date`,
  `issued_date`, are columns of their own.
- **A late correction moves a past fact's geography.** Joining on business validity from the
  current system-time version answers "where do we now believe the customer lived then", so a
  report rerun after a backdated correction changes. A bitemporal join would keep the old answer
  and was rejected below.
- **An excluded measure has one home.** Asking for an account's balance as of a date means
  `sl_account_balance_observations`, never `sl_accounts`.
- **A restoration after a soft delete leaves a gap** between the deletion and the restoration:
  the restore opens a new version, and between the two the key has none, so a fact dated in the
  gap resolves to nothing. The interval test allows a gap only there, directly after a version a
  soft delete closed, read from bronze, and fails any other. Planning measured no restoration.
- **At most one version per day** is inherited from extraction. If extraction ever read more often
  than daily, silver would see more versions without any change here.

## Alternatives considered

**A version for every change of every contract column.** The simplest rule and the textbook SCD2.
Rejected by measurement: 99.8 per cent of account versions would differ only in the balance, so
`sl_accounts` would be a balance history in disguise and every account-level join would carry it.

**Compare a hash of the projection.** One expression instead of one per column. Rejected: a
collision would silently merge two real versions, which is what the rule exists to prevent, and
a hash of a row with nulls depends on how nulls are hashed.

**Drop an observation that changes no contract column, as a duplicate.** It looks like a
re-read. Rejected: it has a new `updated_at`, so the source changed; deduplicating it away loses
the fact that the row moved, and the merchants case is exactly a change the contract cannot see.

**Open the first version at its first `updated_at`, or at `created_at`.** Truthful about when
silver first saw a row. Rejected by measurement: at `updated_at`, 64 per cent of transactions
resolve to no account version and every transaction to no reference version; at `created_at`,
551 transactions still precede their account, because the historical load writes final state.

**A bitemporal join, the business validity as known at the fact's system time.** Reproduces a
past report exactly. Rejected for 008: no backdated change exists in the data, the join doubles
the conditions on every address-bound fact, and the question a report asks of a past fact's
geography is where the customer lived, which the latest knowledge answers best. The consequence
is stated above and tested on a planted backdated change.
