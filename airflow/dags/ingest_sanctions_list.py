"""Land the sanctions list snapshot into bronze (spec 006, ADR 0015).

discover -> open -> extract -> register -> gate. Discovery reads the publisher's latest index
and hashes the entities file it names; open lands it as a new batch only if that content has
not landed before, so a new version string over unchanged content is a no-op, recorded as a
sighting of the batch it already landed as.

The list's shape is the real OpenSanctions feed's and its content is synthetic: every name in
it is a marker name, and the real list is never downloaded or landed (ADR 0015).

**Not scheduled.** A deployed platform would run it at `0 9 * * 1` UTC. That weekly cadence is
the platform's choice, and it is what the freshness SLA in `metric_definitions.md` measures; the
real publisher exports four times a day, which the contract records as the publisher's
behaviour and not as the platform's.
"""

from __future__ import annotations

from airflow.sdk import DAG
from nordbank_ops.feeds import phases
from nordbank_ops.feeds.dags import build_feed_dag

ingest_sanctions_list: DAG = build_feed_dag(
    dag_id="ingest_sanctions_list",
    system=phases.SANCTIONS,
    entities=("entities",),
    discover=phases.sanctions_discover,
    open_units=phases.sanctions_open,
    extract_unit=phases.sanctions_extract,
    doc=__doc__,
    tags=("opensanctions",),
)
