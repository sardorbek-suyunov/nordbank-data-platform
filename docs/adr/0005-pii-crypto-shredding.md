# 0005 — PII handling by crypto-shredding

Status: Accepted
Date: 2026-09-17
Revised: 2026-09-18, during the M0 review, to add the payload fidelity and quarantine
consequences. The decision itself is unchanged.
Consequence added: 2026-09-18, discovered by ADR 0010, which decided that sanctions screening
resolves tokens through the vault. The vault is therefore also the only path to a sanctions
screen, and erasure also destroys the ability to re-screen a subject. The decision, its
context and its alternatives are unchanged.

## Context

Two requirements point in opposite directions. Bronze is immutable: a partition is written
once, never edited, and that property is what makes every downstream model rebuildable and
every past load auditable. GDPR erasure requires that a subject's personal data stops being
personal data on request, within a month, permanently.

If bronze holds raw identifiers, erasure means editing bronze, and bronze stops being an
immutable record. If bronze is never edited, and holds raw identifiers, the platform cannot
honour an erasure request. The platform needs both properties, so the personal data cannot be
what is stored.

This record refines the PII placement described in ADR 0001, which located tokenisation in
silver. The rest of ADR 0001 stands.

## Decision

Crypto-shredding.

Direct identifiers are tokenised in the extraction task, before anything is written to the
lake, so no raw identifier is ever persisted in the lake or in bronze. The token is a keyed
hash, so it is stable and joinable.

The reversible mapping from token to raw value lives in one vault table in the `meta` schema.
That vault is the only place a raw identifier exists after ingestion, and access to it is
separate from access to the warehouse.

Erasure deletes the subject's vault entries. Bronze stays byte-identical and fully
auditable, while the tokens belonging to that subject become permanently unresolvable to a
person by anyone, including the operator. A governance DAG performs the erasure and records
what it did in `meta`: the request, the timestamp and the tokens affected, which is evidence
of compliance that does not retain what was erased.

## Consequences

Erasure is irreversible. There is no archived copy of a deleted vault row, and a mistaken
erasure cannot be undone by reloading, because a legitimate erasure implies the source no
longer holds the subject either.

Aggregates already published are not retracted. A total that included the subject before
erasure keeps its value, and the platform does not rewrite gold or past reports to make them
disagree with themselves.

The vault concentrates risk. Everything tokenisation protects now depends on one table and
one key. Losing the key destroys the ability to resolve any token, including for legitimate
operational use; leaking it undoes the protection for every subject at once.

Bronze is structurally faithful rather than byte-identical. The payload kept for API and
file sources preserves the shape, the field names and every non-identifier value the source
sent, but identifier fields carry tokens, so reconstructing an original record requires the
vault. A byte-identical copy was the alternative, and it would have put cleartext identifiers
in a place erasure cannot reach, which is the whole failure this record exists to avoid.

Quarantine is inside the erasure scope for the same reason. A row that fails its cast is
quarantined with the token for identifier columns, never the cleartext value, because
quarantine is a mutable side table that the vault would otherwise not cover.

Debugging gets harder. An engineer reading bronze sees tokens, and reconciling a specific
customer against the source system requires a deliberate, logged vault lookup rather than a
glance at the data.

The key cannot be rotated cheaply. Rotation changes every token and therefore every join key
derived from it, which means re-tokenising the vault and rebuilding silver and gold.

**Added at M2: the vault is the only path to a sanctions screen, as well as the only path to
erasure.** A payment counterparty name is an identifier and is tokenised like any other, so
screening cannot happen in silver against a hash. It runs instead as a governance-domain job
that resolves tokens through the vault, matches them against a named list version, and
persists only the token, the matched entity, the list version and the score. The raw name
never leaves the vault.

This is what makes historical re-screening possible. Screening once at ingest would answer the
question only for the list as it stood that day, and re-screening the book when the list
changes is how the control is actually operated.

Two consequences follow. First, **erasure also destroys the ability to re-screen that
subject**: their tokens no longer resolve, so no future list version can be matched against
them. That is correct behaviour rather than a defect, and it is recorded here so that it is
not discovered during an investigation. Second, the concentration of risk described above is
now larger than this record originally stated: the vault was a single point of failure for
erasure, and it is now a single point of failure for a compliance control as well. Access to
`meta` is restricted accordingly, and separately from access to the warehouse.

## Alternatives considered

**Rewrite bronze partitions on erasure.** Read the affected partitions, drop or mask the
subject's rows, write them back. Rejected because it destroys immutability for every
consumer, not just for the erased subject: a partition that can be rewritten cannot be relied
on for reproducibility or for audit, the rewrite itself is an expensive scan over history,
and a failure mid-rewrite leaves a partition in a state no contract describes.

**Hold raw PII in bronze and control access instead.** Keep the data as it arrived and
restrict who can read it. Rejected because access control is not erasure: the data still
exists, so the obligation is not met, and the risk is deferred rather than removed. It also
makes bronze the most sensitive store in the platform, when bronze is the layer that most
needs to be freely readable by engineers debugging a pipeline.

**Encrypt the identifier columns in place with a per-subject key.** Functionally close to the
chosen approach, and rejected mainly for mechanics: encrypted values are not stable join
keys, and the per-subject key store would be the same vault with extra steps.
