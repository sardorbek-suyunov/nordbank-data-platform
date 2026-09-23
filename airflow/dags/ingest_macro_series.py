"""Land FRED macro series into bronze, when a key is configured (spec 006).

check_key -> discover (nothing to discover) -> open -> extract -> register -> gate.

**Optional, and skipped with a stated reason when there is no key.** FRED requires a free API
key and answers a keyless request with HTTP 400. `check_key` is a short-circuit: without
`FRED_API_KEY` it logs why and skips every task after it, so the run succeeds, says what it did
not do, and fails nothing. Nothing in CI or in any other feed depends on this one.

With a key, each run fetches every configured series in full; a revised period arrives under a
later `realtime_start` and lands as a new row in the run's batch, and silver keeps the history.

**Not scheduled.** A deployed platform would run it at `0 9 15 * *` UTC, the monthly expectation
in `metric_definitions.md`.
"""

from __future__ import annotations

from airflow.sdk import DAG
from nordbank_ops.feeds import phases
from nordbank_ops.feeds.dags import build_feed_dag
from nordbank_ops.feeds.fred import key_absent_reason

ingest_macro_series: DAG = build_feed_dag(
    dag_id="ingest_macro_series",
    system=phases.FRED,
    entities=("series",),
    discover=None,
    open_units=lambda context, _candidates: phases.macro_open(context),
    extract_unit=phases.macro_extract,
    skip_reason=key_absent_reason,
    doc=__doc__,
    tags=("fred",),
)
