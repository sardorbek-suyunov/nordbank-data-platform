"""Extract the sixteen `core` entities into bronze (spec 005).

Four phases. **Open** allocates a batch per entity in one warehouse transaction. **Extract** is
one mapped task per entity that holds no warehouse pool: it reads Postgres as the read-only
role, validates against the contract, tokenises identifiers, and writes Parquet to the lake.
**Register** is one pooled task on `all_done` that marks the batches that wrote as registered,
advances their watermarks, upserts the vault from its own bounded re-read of the source, loads
the quarantine index and writes the reconciliation rows — all in one transaction. **Gate** is a
terminal task that fails the run if any batch failed.

`all_done` and the separate gate are not a convenience. Under `all_success` a single entity's
breaking drift would stop the register step from running, so the fifteen entities that wrote
cleanly would never be registered and their watermarks would never advance: one entity's drift
would kill the whole pipeline from that day forward. **No partial load is a guarantee at entity
grain, not at run grain.**

**Not scheduled.** The source's clock is simulated, so a wall-clock schedule has no meaning
against it: a day of source data exists when a tick has written it, not when the wall clock
passes midnight. `make backfill` drives this DAG one day at a time, after the tick for that day
and after `ingest_reference_data`. A deployed platform would run it at `0 4 * * *` UTC, after
the source's overnight batch, which leaves two hours before the 06:00 UTC freshness expectation
in `metric_definitions.md` and two more of grace for a retry.
"""

from __future__ import annotations

from airflow.sdk import DAG
from nordbank_ops.ingest import build_ingest_dag

# Two things about this binding are load-bearing and neither is obvious.
#
# The DAG is assigned to a module-level name because that is what the DagBag collects. The
# `@dag` decorator auto-registers only when it is called from the DAG file itself, and here it
# is called from the factory in the plugin tree, so nothing would be found.
#
# The `DAG` import is not decoration either. Before importing a file the DagBag runs a text
# heuristic over it and skips any file that does not mention both a dag and airflow in lower
# case; a file that builds its DAG through a factory mentions neither, and is skipped in
# silence with no import error to explain it.
ingest_core_banking: DAG = build_ingest_dag(
    dag_id="ingest_core_banking", source_schema="core", doc=__doc__
)
