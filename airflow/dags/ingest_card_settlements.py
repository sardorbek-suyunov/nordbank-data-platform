"""Land the card processor's clearing files into bronze, by file arrival (spec 006).

wait_for_file -> discover -> open -> extract (one task per new file) -> register -> gate.

The sensor waits for the day's file without holding a worker: it looks once and, if the file is
not there, defers to the triggerer until it arrives or the wait runs out. Running out is not a
failure — the processor may send the day's file late — and the run then records empty batches
for the day. Discovery hashes every file in the inbound prefix, so a late file for an earlier
date is picked up by whichever run sees it, and a file already landed, renamed or not, is
recognised by its checksum and not landed again.

Each new file lands as two entities, its detail records and its trailer totals, in the ingest
partition of the day it arrived, with its settlement date as an attribute of every row.

**Not scheduled**, for the reason every ingestion DAG gives. A deployed platform would run it at
`0 6 * * *` UTC with the sensor's default four-hour wait: the file is expected by 08:00 UTC and
`metric_definitions.md` gives the feed a four-hour grace because a third party produces it.
"""

from __future__ import annotations

from airflow.sdk import DAG
from nordbank_ops.feeds import phases
from nordbank_ops.feeds.dags import build_feed_dag

ingest_card_settlements: DAG = build_feed_dag(
    dag_id="ingest_card_settlements",
    system=phases.CARDNET,
    entities=("settlements", "settlement_totals"),
    discover=phases.settlement_discover,
    open_units=phases.settlement_open_units,
    extract_unit=phases.settlement_extract,
    identifier_values=phases.settlement_identifiers,
    wait_for_file={"prefix_variable": "CARD_SETTLEMENT_PREFIX", "pattern": "NBK_CLR_{day:%Y%m%d}_"},
    doc=__doc__,
    tags=("cardnet",),
)
