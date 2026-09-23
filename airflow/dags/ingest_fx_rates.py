"""Land the ECB reference rates into bronze, one request per date (spec 006).

discover (nothing to discover) -> open -> extract -> register -> gate. The open step requests
every date after the last one requested through the run's own date, which is one date on a
normal day; extract fetches each through the bounded retry policy and lands a date's rates only
when the response is dated the day asked for. A weekend or holiday answers with an earlier date,
lands nothing, and is recorded as `absent` in `ops.feed_request`: the gap is kept as a gap, and
silver fills it.

**Not scheduled.** A run's logical date is the simulated business day, so a wall-clock schedule
has no meaning against it and the backfill loop drives this DAG. A deployed platform would run
it at `0 17 * * 1-5` UTC in winter and summer alike: the ECB publishes at about 16:00 CET, which
is 15:00 UTC in summer and 14:00 in winter, and `metric_definitions.md` allows three hours from
the publication time in force, so 17:00 UTC is inside the window in both.
"""

from __future__ import annotations

from airflow.sdk import DAG
from nordbank_ops.feeds import phases
from nordbank_ops.feeds.dags import build_feed_dag

# The module-level name is what the DagBag collects, and the `DAG` import keeps the file past the
# DagBag's text heuristic; `ingest_core_banking.py` records both at more length.
ingest_fx_rates: DAG = build_feed_dag(
    dag_id="ingest_fx_rates",
    system=phases.FX,
    entities=("fx_rates",),
    discover=None,
    open_units=lambda context, _candidates: phases.fx_open(context),
    extract_unit=phases.fx_extract,
    doc=__doc__,
    tags=("ecb",),
)
