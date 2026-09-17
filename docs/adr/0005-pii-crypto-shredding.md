# 0005 — PII handling by crypto-shredding

Status: Accepted
Date: 2026-09-17

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

Debugging gets harder. An engineer reading bronze sees tokens, and reconciling a specific
customer against the source system requires a deliberate, logged vault lookup rather than a
glance at the data.

The key cannot be rotated cheaply. Rotation changes every token and therefore every join key
derived from it, which means re-tokenising the vault and rebuilding silver and gold.

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
