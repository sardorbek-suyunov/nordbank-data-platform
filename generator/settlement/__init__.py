"""The card processor: clearing files built from the source ledger (spec 006 section 2).

Simulation infrastructure, on the far side of the boundary M3 drew. It stands in for the bank's
card processor, which in a real deployment would send these files itself, and it is not part of
the platform: it reads the source with the simulator's own role, writes to the inbound bucket
that represents the processor's side, and the ingestion path knows nothing about how a file was
made — only the specification in `contracts/cardnet/README.md`.

A file for settlement date S covers the card items whose ledger posting date is S minus the
network's settlement lag. Presenting on the posting date rather than the day the cardholder
transacted is deliberate: a late offline item posts to the period open when it arrives, and a
processor clears it when it is presented, which is the same day, so the file and the ledger
agree on every item that was not deliberately broken.

Defects are drawn at the rates in `generator/profiles.yml`, justified in
`docs/generator_realism.md`, from a stream keyed on the seed and the settlement date, so a file
is a pure function of the ledger, the seed and its date: regenerating it produces the same
bytes. Every defect is written to a manifest under `_simulation/`, which the ingestion path
never lists, so the acceptance evidence can compare what was detected with what was injected.
"""
