# docs/adr

Architecture decision records, one file per decision, numbered in the order the decisions
were taken. Filename pattern: `NNNN-short-title.md`.

Format: title, `Status:` line, Context, Decision, Consequences, Alternatives considered.
The `Status:` line is a line and not a heading because CI checks for it.

A record is not rewritten once accepted. A decision that no longer holds is superseded by a
later record, and both keep their status lines.
