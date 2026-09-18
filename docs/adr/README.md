# docs/adr

Architecture decision records, one file per decision, numbered in the order the decisions
were taken. Filename pattern: `NNNN-short-title.md`.

Format: title, `Status:` line, Context, Decision, Consequences, Alternatives considered.
The `Status:` line is a line and not a heading because CI checks for it.

A record must name at least one rejected alternative with the reason it lost, and at least one
negative consequence of the option chosen. There is no word budget; a record that states
neither is incomplete regardless of length.

Three ways a record changes, and they are not interchangeable:

- **Revised.** While the status is `Accepted`, no implementation depends on it, and the
  milestone that introduced it has not closed, the record may be edited in place. It carries a
  `Revised:` line saying when and what was added.
- **Corrected.** A factual error in a closed record is fixed in place with a `Corrected:` line
  stating what was wrong and what is true. The decision itself has not changed, so a new record
  would misrepresent the history: the choice was made for reasons that still hold, on one fact
  that was wrong.
- **Superseded.** A change to the decision requires a new record that supersedes the old one.
  Both keep their status lines, and the old record is not edited beyond its status.
