# contracts

Data contracts for every ingested source: expected schema, types, nullability, primary key,
and the agreed behaviour when the source drifts.

A contract is the authority for what bronze accepts. Records that violate it are quarantined
with the reason attached, not dropped and not silently coerced.

## Versions, and the contract of the time

A contract is versioned, and a batch is validated against the version in force for its
interval rather than against whatever file is current (specification 006 section 5).

- The current version of an entity's contract is `<source>/<entity>.yml`.
- Every superseded version is kept, unedited, as `<source>/history/<entity>.v<N>.yml`. It is
  never deleted and never edited in place: the open step records each version's fingerprint in
  `meta.contract_version` and refuses a recorded version whose file has changed.
- `in_force_from` is the first business day a version applies to, in source time. A version 1
  says `null`, meaning from the start of history; every later version names a day, and each
  takes over strictly later than the one before it.

To accept a breaking change: move the current file into `history/` under its version number,
write the next version with `in_force_from` set to the day the source changed, and commit both.
A replay of an earlier day then selects the earlier version with no file edited.

**The dates are authored against one anchor.** The simulated source's scripted drift fires at an
offset from the seed anchor, while a contract names an absolute day. The committed contracts are
written for `ACCEPTANCE_ANCHOR` in `generator/drift/timeline.py`, 2026-07-20, and
`airflow/tests/test_contract_history.py` asserts that every version accepting a scripted event
takes over on the day that event fires at that anchor. A history seeded at a different anchor
fires its drift on different days, and those contracts are then wrong for it by construction.

## Layout

`corebank/` holds the forty-five relational contracts, bootstrapped once from
`docs/data_dictionary.md` and hand-authored since; `make contracts-diff` compares the current
versions against the dictionary.
