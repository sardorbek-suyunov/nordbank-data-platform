# 0014 — The contract in force for a batch is chosen by its interval

Status: Accepted
Date: 2026-09-23

## Context

Specification 005 validated every batch against the contract file currently on disk. That is
correct only while contracts never change, and they change by design: the halt-and-bump
procedure resolves a breaking drift by publishing a new contract version. The first bump,
`payments` version 2 accepting a `remittance_reference` widened from 140 characters to 280,
made the current contract wrong for every day before the widening, because a source narrower
than its contract is a type change like any other. Re-running the acceptance history with the
resolved contract in the tree failed on its first day, and reproducing it meant checking out
the old contract by hand for the first leg. Specification 005 recorded that as an amendment and
gave the fix to specification 006.

The amendment assumed `meta.contract_version` already held what a selection by interval
needs. Planning for 006 measured that it did not, in three ways:

- Its `in_force_from` was written as the register step's clock reading, `now()`: real time, the
  moment the platform first registered a batch under the version. A batch's interval is source
  time, which in this deployment is simulated, so selecting by interval against that column
  compared a simulated day with a wall clock.
- It recorded first sighting, not applicability. Nothing said that version 1 applied from the
  start of history.
- It stored no contract body, only a version number and a fingerprint, so selecting a number
  did not produce the columns to validate against.

A fourth constraint surfaced while implementing. The simulated source's scripted drift fires at
an offset from the seed anchor, so the business day on which `payments` widens depends on which
anchor the history was seeded with, while a contract naturally names an absolute day.

## Decision

A contract carries an authored **`in_force_from`**: the first business day it applies to, in
source time, `null` for a version 1, which applies from the start of history. Superseded
versions are kept on disk, unedited, as `contracts/<source>/history/<entity>.v<N>.yml`. The
loader checks the chain rather than trusting it: versions strictly increase, the current file is
the highest, only the oldest may apply from the start, and each later version takes over
strictly later than the one before.

`meta.contract_version` keeps **both** times, because they answer different questions: the
authored source-time `in_force_from`, and the real-time `first_seen_at`, which is what the old
`in_force_from` actually held. The open step records every version on disk there, selects the
highest version whose `in_force_from` is on or before the batch's interval, and stores it on the
batch; extract and register load that version's body from disk. A recorded version whose file
no longer matches its fingerprint or its date is refused, because an old contract edited in
place would change what a replay accepts and nothing else would notice.

The committed dates are authored against one anchor, `ACCEPTANCE_ANCHOR` in
`generator/drift/timeline.py`, 2026-07-20, and a unit test asserts that every contract version
accepting a scripted drift event takes over on the day that event fires at that anchor.

## Consequences

- Replaying history no longer needs a file checked out by hand. A backfill of the whole
  acceptance history with every bump already committed selects version 1 of `payments` before
  2026-08-26 and version 2 from it, and does not halt.
- The contract tree grows monotonically. A superseded version is never deleted, and
  `contracts-diff` has to know which file is current: it compares only the current version with
  the data dictionary, because a superseded one describes the source as it was and would diverge
  for ever, and it still loads the chain so a broken one fails in CI.
- **The contracts are correct for one anchor.** A history seeded at another anchor fires its
  drift on other days, and the committed contracts then select the wrong version for the days
  in between; the backfill halts there, which is loud but is not the replay this record promises.
  The acceptance procedure therefore seeds at the named anchor rather than at `today − window`.
  A real source has one history, so this is a property of the simulation rather than of the
  design, but it is a real restriction on how the repository can be exercised.
- A warehouse built by specification 005 has the old shape of `meta.contract_version`, and
  `make warehouse-apply` refuses to apply over it rather than skipping the changed definition in
  silence. The migration is `make nuke` and a fresh backfill: the warehouse holds platform state
  that re-ingesting rebuilds.

## Alternatives considered

**Store the contract body in `meta.contract_version`.** The table would then be self-sufficient
and a replay would need nothing from disk. Rejected because the warehouse is not reachable from
the host by design, so validating a contract, diffing it against the dictionary or reviewing a
bump would all need a running stack, and `make test` would stop being able to exercise the
selection. A contract is reviewed in a pull request, which is where a file is and a table row is
not.

**Derive `in_force_from` from the registry: the interval of the first batch registered under a
version.** Anchor-independent, and needs no authored date. Rejected because it describes what
happened in one warehouse rather than what the platform agreed to, and it is empty in a fresh
one, which is exactly the case the amendment described: a whole history replayed into a new
warehouse would have nothing to select from and would fall back to the current file.

**Accept a source narrower than its contract.** Every value a `varchar(140)` holds fits a
`varchar(280)`, so the widening case would pass with no selection at all. Rejected in
specification 005 and again here: it is true for a widened character type and not obviously
true for every type pair the dictionary uses, and a gate right about `varchar` and wrong about
`numeric` is worse than one strict about both.
