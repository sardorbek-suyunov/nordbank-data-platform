"""The mutation engine: advance the simulated source system by one business day.

Spec 004. The historical load (spec 003) produces a coherent snapshot; this produces the
*change* that incremental extraction has to cope with, one simulated day at a time — inserts,
in-place updates, soft deletes, late arrivals, plausible dirt and scripted schema drift.

Three properties hold it together, and each costs something in the design:

**A tick is one transaction.** Everything a day changes commits together or not at all, which
is what acceptance criterion 2 is written in terms of. That is why this package reaches the
database with a driver rather than through psql (ADR 0012): a real `rollback()` rather than a
sentinel protocol over a pipe.

**A tick is replayable, not idempotent.** It is a state transition, so running it twice is not
running it once. What is guaranteed is that the same seed, the same starting state and the
same date produce the same result, because the per-tick substream is a keyed hash of the seed
and the date and does not depend on how many ticks preceded it.

**Coherence is maintained, not recomputed.** All fourteen invariants from spec 003 hold after
an arbitrary number of ticks, and a tick cannot afford to recompute them: the full suite costs
about 140 seconds on the `dev` book. So the balance is folded forward rather than re-derived,
the ledger balances per batch so that the daily sum follows for free, and a delta-scoped guard
catches a broken tick before it commits rather than sixty ticks later.
"""

from __future__ import annotations

# Bumped when a change alters what a tick produces, so that a reader can tell "the seed
# changed" from "the engine changed" — the same reason GENERATOR_VERSION exists.
MUTATION_VERSION = "1.0.0"

__all__ = ["MUTATION_VERSION"]
