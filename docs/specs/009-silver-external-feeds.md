# 009 — Silver: External Feeds

Status: Approved
Version: 2
Supersedes: version 1 (drafted for planning; never committed)
Depends on: 000–008

## Goal
Build silver for the card settlement feed, the sanctions list and the FRED
series: the settlement revision arithmetic applied once, the list's entities
and names per version, and macro observations by observed interval. Each
model has a declared, tested grain, and silver is a pure function of bronze.

## Context
Planning measured, on the 61-day history:
- Settlement batches are keyed on settlement date, with the arrival day as
  the ingest date.
  - Every detail and trailer row carries settlement date, `file_sequence`,
    `revision`, processor and file creation time.
  - Every file is sequence 1; the generator hard-codes it.
  - There are 9 corrections and 6 late files, and no lower revision ever
    arrived after a higher one.
  - Card tokens and transaction references resolve 100 per cent.
  - Refunds are negative on both sides.
- The ledger posts card items to shared account 1000. The settlement accounts
  1200 and 2100 carry no entries. A ledger entry reaches a network only
  through its transaction's card product, and on that path both sides of Q15
  hold the same 644 cells.
- Sanctions: 9 versions, 315 to 339 entities, with ids stable and content
  unchanged between versions.
  - Persons carry `nationality`, not `country`.
  - Birth dates are partial; multi-valued arrays hold at most one element.
  - Every name is already upper-case ASCII.
- FRED bronze is empty. Each run would land the current vintage only, with
  `realtime_start` equal to `realtime_end` equal to the request day.

## Scope

### 1. The winning revision
- One macro decides, once per settlement date and sender `file_sequence`,
  which revision wins: the highest declared revision present in bronze across
  lines and trailers together.
- Both settlement models use it. If each chose its own, a correction whose
  lines were all quarantined would leave revision 1's lines beside revision
  2's trailers.
- Known gap: a correction with zero records is invisible in bronze. Silver
  stays a function of bronze and does not read the registry to find one.

### 2. Settlement lines: `sl_card_settlements`
- Every detail line of each winning revision, with every contract column,
  `clearing_date` included.
- Lines of different sequences add. A no-arrival batch has no rows and
  contributes nothing.
- **Resolution, without correction:**
  - `card_id` comes from the distinct token-to-`card_id` pair. A test asserts
    that resolution adds no rows; a token can have several SCD2 versions.
  - `transaction_id` and `merchant_id` come through `transaction_reference`.
  - An unresolved line is kept with nulls. The history has none, so a fixture
    proves it.
  - `merchant_name` is not back-filled after its retirement.
- No EUR conversion.

### 3. Settlement totals: `sl_card_settlement_totals`
- One row per trailer of each winning revision.
- Grain: settlement date, `file_sequence`, network and settlement currency.
- `file_total_amount` lives here.
- Silver compares nothing. A quarantined line legitimately makes lines and
  trailer differ, and the comparisons are M6's.

### 4. Sanctions: `sl_sanctions_entities` and `sl_sanctions_names`
- **Entities:** one row per entity per list version, keyed by entity id and
  version, with:
  - schema type, topics, countries and nationalities;
  - birth dates as text, because they are partial;
  - first-seen and last-change timestamps;
  - arrays kept as arrays.
- **Names:** one row per entity, version and name, with `name_type` (`name`
  or `alias`) and a normalised name beside the raw one. `caption` is not added
  as a separate name, because it equals the first name.
- **Normalisation** is one macro, extracted from `sl_merchants`, which must
  rebuild with an identical content hash. It normalises representation only:
  case, whitespace and diacritics. Its effect is shown on a fixture with
  diacritics and several names, because the history's names are already
  normalised.
- Matching and screening are 010, as a governance job with vault access
  (ADR 0010).

### 5. FRED: `sl_macro_indicators`
- One row per series, observation period and observed interval.
- Runs of identical values collapse into one interval, with `observed_from`
  and `observed_to` derived in silver, and an `is_latest` flag.
- **The interval is when the platform observed the value. It is not FRED's
  real-time vintage,** and it is named so it cannot be mistaken for one.
- The model builds typed and empty live. Its tests are proven on `dbt-prove`
  fixtures that include a revision. The live floor applies only once a FRED
  batch is registered, and the test says so.

### 6. Corrections and records
- **`business_questions.md` Q15:** grain is settlement date, network and
  settlement currency, summed over the sender's sequences.
- **`metric_definitions.md` settlement source columns:**
  - `file_total_amount` from `sl_card_settlement_totals`;
  - the ledger side through `sl_gl_entries` on account 1000, then
    `sl_gl_transactions`, `sl_transactions`, `sl_cards` and
    `sl_card_products.network`;
  - settlement date as the posting date plus the contract's settlement lag.
- **The settlement lag has one source, in the contract.** Add a
  machine-readable copy if none exists, and have the README point to it.
- **`model_inventory.md`:** correct `sl_card_settlement_totals`' grain, add
  `sl_sanctions_names`, and update the FRED model's grain and columns.
- **`architecture.md`:** FRED silver keeps observed intervals, not only the
  latest vintage.
- **`project_state.md`:**
  - Known gap, owned by the source: account 1200 claims to be reconciled in
    Q15 but carries no entries. Posting card items through a settlement
    payable is the real fix, decided at M6.
  - Open item: the settlement generator should emit a second sequence on
    some days.
  - Open item: request FRED's full real-time history, decided with the
    funding cost.
  - Sanctions screening is built in 010 as a governance job with vault
    access.

### 7. Tests and orchestration
- Every model declares and tests its grain, and every structural test states
  a minimum cardinality.
- **Revision:** the kept revision is the highest in bronze for its date and
  sequence, and the model holds exactly that revision's lines and trailers.
  `dbt-prove` fixtures cover:
  - a lower revision arriving after a higher one;
  - a correction whose lines were all quarantined.
- **Additivity:** a two-sequence fixture shows both sequences present. The
  live floor is 0, and the test states why.
- **Determinism:** two builds from the same bronze are identical by content
  hash. `sl_merchants`' hash is unchanged by the macro extraction.
- The models join `transform_silver`.

## Out of scope
Matching and screening (010). The reconciliation mart (M6). `dq` checks. EUR
conversion of settlement amounts. Changes to the ledger or settlement
generators.

## Acceptance criteria
1. `sl_card_settlements` and `sl_card_settlement_totals` hold exactly the
   records of each date and sequence's highest revision in bronze: 9,801
   lines and 644 totals on the history.
2. The revision fixtures hold: a lower revision after a higher one is
   excluded, and an all-quarantined correction leaves no revision 1 lines.
3. Two-sequence additivity holds on its fixture, and the live floor states
   its reason.
4. Card and transaction resolution are reported, resolution adds no rows,
   and an unresolved line is kept, on a fixture.
5. `sl_sanctions_entities` holds 2,943 rows and `sl_sanctions_names` 4,022
   on the history, with every landed version present.
6. Name normalisation is one macro, used by merchants and sanctions.
   `sl_merchants`' hash is unchanged, and the effect is shown on a fixture.
7. `sl_macro_indicators` builds typed and empty live, and its interval tests
   pass and fail correctly on fixtures with a revision.
8. The section 6 corrections and records are made, including the
   machine-readable settlement lag.
9. Two builds are identical.
10. `dbt build --warn-error` passes locally and in CI.
11. All four checks pass; a pull request on `feat/M5-silver-feeds`.

## Changelog
- **The late file:** it lands under its settlement date's batch, with the
  arrival day as the ingest date. Version 1 said the reverse.
- **One winning-revision macro** shared by lines and totals, because separate
  choices can mix revisions.
- **Every contract column kept on lines,** because `clearing_date` is what
  the ledger posts on.
- **Card resolution through the distinct token-to-id pair, with a no-row-added
  test,** because 5 tokens have 3 versions each.
- **`file_sequence` in the totals grain.**
- **The Q15 ledger path is stated,** with the lag from one source, because no
  account attributes card items to a network.
- **Sanctions entities gain nationalities, partial birth dates and first and
  last timestamps; names gain `name_type` and drop `caption`.**
- **No-op snapshots leave the silver scope,** because they are a bronze
  property.
- **FRED intervals are named for platform observation,** because the request
  returns the current vintage only.
- **Revision and additivity are proven on fixtures where the history has no
  case;** the revision test asserts the highest revision, not merely one.
- **Screening is placed in 010 as a governance job,** per ADR 0010.

## Amendments

**2026-10-04, `normalise_name` collapses whitespace before it trims.** The merchants expression
extracted into the macro trimmed first, and `trim` removes spaces only, so a name that began with
a tab or ended with a line feed kept a space at its edge once the tab became one. Extracted
unchanged in one commit, corrected in the next, with a fixture case and a planted defect each
way. No merchant name on the history has edge whitespace, 0 of 179, and `sl_merchants`' content
hash is identical to the pre-009 build after both commits, as are all fifty silver tables.

**2026-10-04, a letter with no decomposed form is kept.** `strip_accents` turns `É` into `E` but
leaves `Ł`, `Ø` and `ß`, which are letters of their own. The fixture states it; transliteration
is a matching rule and 010's. Recorded as a known gap in `project_state.md`.

**2026-10-04, `sl_sanctions_names` keeps the grain entity, version and name.** A value an entity
states twice, or as both a name and an alias, is one row, typed `name`. None does on the history;
the fixture has both.

**2026-10-04, the column choices the scope left open.** `sl_card_settlements` carries
`record_type` too, every contract column being its rule. `sl_card_settlement_totals` carries the
trailer's `amount_total` as `file_total_amount`, the name section 3 gives it, and keeps
`record_count`. `sl_sanctions_entities` also carries the publisher's `caption`, `datasets` and
`referents`, and not `names` or `aliases`, which are `sl_sanctions_names`'. Every multi-valued
property is `VARCHAR[]`. `sl_macro_indicators` does not carry `realtime_start` or `realtime_end`,
both the request day.

**2026-10-04, where the settlement lag lives and how SQL reads it.** In the settlements
contract's `source_of_truth`, as `settlement_lag_days: 1`, beside the document it restates. The
fingerprint covers columns, keys and format, not `source_of_truth`, so the contract keeps its
version and fingerprint, `sha256:b65fcabb18f53562` before and after, and no warehouse that
recorded version 2 refuses it; the superseded version 1 is not edited. The silver generator
copies it into `sl_card_settlements`' properties, the `settlement_lag_days` macro reads it from
there, and M6's reconciliation calls the macro. A live test applies it to every line, 9,801 at
one day, and a unit test holds it equal to the simulated processor's profile.

**2026-10-04, the generator states the feed models.** `scripts/silver_generate.py` gains a feed
family: for each of the five models, the bronze columns it carries, renamed or retyped where
silver changes them, the columns it adds, its grain and its floor. Their properties are
generated from that and the contracts, as the mirroring models' are, and their SQL is written by
hand. Floors sit below the stack job's week: 700 lines of 926, 45 totals of 60, 300 entities of
315, 400 names of 431.

**2026-10-04, the FRED floor is a test of its own.** `silver_min_rows_once_registered` applies
a floor of 1 only once the registry holds a registered batch of `fred.series`; the registry is
an argument, so `make dbt-prove` proves it on three fixtures, unlanded and empty, landed and
empty, landed with rows. The interval rules are `silver_observed_intervals`, proven on six.

**2026-10-04, a vault value in a fixture.** The first name fixtures used a screening fixture
name, which is also a payment counterparty the vault holds, and the documentation site
publishes every test's SQL: `make docs-scan` found it. The fixtures use a list-only synthetic
name, and the scan is clean.

**2026-10-04, two tests say more than the scope asked.** The revision test also reports a
winning declaration that two batches landed, which ingestion refuses and which would double
its records. The additivity test is a singular test whose comment states its live floor of zero
and why, since a singular test has no floor argument.
