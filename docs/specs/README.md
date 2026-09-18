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
