# 0010 — Sanctions screening through the vault

Status: Accepted
Date: 2026-09-18

## Context

Question 12 asks which customers transact with counterparties on the sanctions list. Answering
it needs a counterparty name to match against, so `core.payments.counterparty_name` was added
at M2.

A name is an `identifier` under [pii_classification.md](pii_classification.md), so it is
tokenised in the extraction task before anything is written to the lake (ADR 0005). Silver
therefore holds a keyed hash, and a sanctions list holds names. Matching a hash against a name
returns nothing, so the question appears unanswerable by construction.

Something has to give: either the name is not an identifier, or it is not tokenised, or the
screen does not happen in silver.

## Decision

The screen does not happen in silver.

Sanctions screening runs as a **governance-domain job with vault access**, alongside the
erasure workflow rather than alongside the transformations. It resolves tokens to names through
the vault in `meta`, screens them against a named sanctions list version, and persists only the
token, the matched entity, the list version and the match score. The raw name never leaves the
vault, and never reaches silver, gold or an export.

`fct_sanctions_screening` is therefore fed by that job rather than by a join between
`sl_payments` and `sl_sanctions_entities`.

## Consequences

**Erasing a subject also destroys the ability to re-screen them.** Their tokens no longer
resolve to a name, so no future list version can be matched against them. This is correct
behaviour — the subject has exercised a right to erasure, and what remains is a hash with no
surviving key material — but it is a real capability lost at a real moment. It is recorded here
and in the erasure log rather than left to be discovered during an investigation.

**The vault becomes load-bearing for a second reason.** ADR 0005 already noted it as a
concentration of risk, because everything tokenisation protects depends on it. It is now also
the single point of failure for a compliance control: if the vault is unavailable, screening
cannot run at all. Access to `meta` is restricted accordingly, and separately from access to
the warehouse.

**Screening is slower and more restricted than a join.** It cannot run inside the normal dbt
transformation graph, it needs credentials nothing else in the transformation layer holds, and
every run is a deliberate, logged vault read rather than a query anyone can write. That is the
intended cost of keeping cleartext names in exactly one place.

**Fuzzy matching stays possible, which a token-only design would have prevented.** Sanctions
screening in practice is not exact string equality; it is transliteration, name order and
edit distance. Those work on the resolved name inside the governance job, and would have been
impossible against a hash.

## Alternatives considered

**Screen at ingest, before tokenisation.** Rejected, and it is the obvious answer. The
extraction task already holds the cleartext, so screening there costs nothing extra and needs
no vault read. It fails on the question actually being asked: a sanctions list changes weekly,
and compliance asks whether anyone the bank has already paid appears on the list **as it stands
today**. Screening once at ingest answers that only for the list as it stood on the day of the
payment, and re-screening the book when the list changes is how the control is really operated.
An ingest-time screen would make historical re-screening impossible, which is worse than making
it expensive.

**Classify the counterparty name as a quasi-identifier and keep it in the clear.** Rejected. It
would make screening a simple join, and it would put the cleartext names of people outside the
bank into silver, gold and every export, where erasure cannot reach them. A quasi-identifier is
retained in the clear because something downstream must *interpret* it — an age band, a region.
Nothing interprets a counterparty name; it is matched. That is the test ADR 0005 sets, and a
name fails it.

**Keep a second, un-tokenised copy of the name in `meta` outside the vault.** Rejected as the
worst of both: it is a cleartext store of personal data with none of the vault's access
controls, and erasure would have to remember to reach it. The vault already is that store, with
the controls, and adding a second one is how a subject survives their own erasure.
