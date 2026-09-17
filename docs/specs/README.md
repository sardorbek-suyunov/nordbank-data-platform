# docs/specs

Specifications, numbered, one per milestone or per coherent unit of work. Filename pattern:
`NNN-short-title.md`. Each carries a `Status:` line (Draft, Approved, Superseded) and a
`Depends on:` line. CI fails if either the status line or the file is missing.

Amendment protocol: an approved specification is never edited in place. A deviation agreed
during implementation is appended to an `## Amendments` section as a dated entry stating
what changed and why. The original scope text stays as written, so the difference between
what was specified and what was delivered stays readable in one file.
