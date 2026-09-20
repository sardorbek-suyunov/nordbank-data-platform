"""The change classes a tick runs, and the order they run in.

One table, assembled explicitly. A registry populated by decorators at import time would make
the set of phases depend on which modules happened to be imported, and the failure mode of
that is a change class silently not running — which the tick log would faithfully report as
zero rows changed.

The order is a dependency order rather than a preference. Lifecycle changes decide which
accounts, cards and loans are open before movements try to post to them; acquisition adds the
customers and accounts a later movement may touch; dispositions close alerts that earlier ticks
raised; dirt edits rows the earlier phases wrote; deletes remove what nothing references; and
drift changes the shape of the table last, so every row written this tick was written against
one schema.
"""

from __future__ import annotations

from .phases import movements
from .tick import ChangePhase

# Populated as each change class lands. A phase absent from this table is simply not run, and
# `generator/mutation/tick.py` names the full sequence in PHASES.
HANDLERS: dict[str, ChangePhase] = {
    "movements": movements.run,
}
