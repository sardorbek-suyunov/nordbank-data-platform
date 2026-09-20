"""Scripted schema drift: the source's shape changing under a platform that has to cope.

Spec 004 section 3. A timeline of events, each declaring when it fires, what it does to the
source DDL, what it does to the documented schema, and how to undo it.

**The drift event changes the source DDL and the documented schema together, so that
`make schema-check` stays green.** That is the correct model rather than a convenience: drift
is divergence between the platform's *contract* and reality, not between the source and its own
documentation, and contracts do not exist until M4. The hook for M4 is that the contract in
`contracts/` is versioned separately and lags deliberately.

"Together" cannot mean the tick edits `docs/data_dictionary.md`. That file is committed and is
the parser's input; a tick that rewrote it would make `git status` a function of how many ticks
had been run. Nor can the dictionary ship the post-drift state, because then `schema-check` is
red from `schema-apply` until the event fires. So **the expected schema is the committed
dictionary plus the deltas of the events `platform.drift_log` says have fired**, the timeline
declares each event's DDL and its dictionary delta as one object, and nothing committed is
edited at runtime.
"""

from .timeline import (
    DriftEvent,
    apply_deltas,
    classification_rows,
    due_events,
    event_by_name,
    events,
    fired_names,
    record_fired,
    revert_all,
)

__all__ = [
    "DriftEvent",
    "apply_deltas",
    "classification_rows",
    "due_events",
    "event_by_name",
    "events",
    "fired_names",
    "record_fired",
    "revert_all",
]
