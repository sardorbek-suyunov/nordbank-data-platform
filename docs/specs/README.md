# docs/specs

Specifications, numbered, one per milestone or per coherent unit of work. Filename pattern:
`NNN-short-title.md`. Each carries a `Status:` line (Draft, Approved, Superseded) and a
`Depends on:` line. CI fails if either the status line or the file is missing.

Amendment protocol: an approved specification is never edited in place. A deviation agreed
during implementation is appended to an `## Amendments` section as a dated entry stating
what changed and why. The original scope text stays as written, so the difference between
what was specified and what was delivered stays readable in one file.

Reissue protocol: when a review cycle produces more than five corrections to a specification
that has not been implemented yet, the specification is reissued at the next version rather
than amended. The file is replaced, carries a `Version:` line and a `Supersedes:` line, and
ends with a `## Changelog` section listing what changed from the previous version and why.

The two mechanisms answer different questions. A changelog explains how the target moved
before anything was built against it; a long amendment list on an unbuilt specification only
makes the target hard to read. Amendments exist to record where the delivered system departed
from what was specified, so they start once implementation starts.

One source per fact, and a mechanical check where two representations are unavoidable. A fact
that appears in two places will eventually disagree in two places, and the disagreement is
silent because both copies look authoritative. The first response is to delete one copy:
`platform.column_classifications` is generated from `docs/data_dictionary.md` rather than
seeded beside it, and the four dbt seeds that duplicated `ref` tables were dropped rather than
kept in step. Where neither copy can go — because a document has to state a number for a
reader to review it, and code has to read that number to act on it — the two are not left to
discipline: a check asserts they agree and CI runs it. `make schema-check` is that check for
the dictionary, and the realism document's contract bands are checked against
`generator/profiles.yml` the same way. Neither the direction of generation nor the choice of
which copy survives is the rule; the rule is that no fact is maintained twice by hand.
