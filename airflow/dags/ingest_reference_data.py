"""Extract the twenty-nine `ref` entities into bronze (spec 005 section 10).

The same four phases and the same framework as `ingest_core_banking`, over the other source
schema. Reference data is not a special case: the `ref` tables carry `updated_at` and the audit
trigger like everything else, and they go through the same contracts, the same registry and the
same watermarks.

What differs is the traffic. Reference data changes rarely, so **most days most of these
batches are empty**, and an empty registered batch is a correct outcome rather than something
to suppress: it records that the entity was asked and had nothing to say, which is what makes
the freshness measure at M7 able to tell "no change" from "no run". No `ref` column is
classified anything but `non-personal`, so nothing here is tokenised and the vault is untouched.

**It runs ahead of `ingest_core_banking` on every day of the backfill**, so a new reference code
exists in bronze before a `core` fact references it. The ordering lives in the backfill script
rather than in a cross-DAG dependency, because the loop already sequences the tick and the two
ingestions and a second mechanism would be a second place to get the order wrong.

A deployed platform would run it at `0 3 * * *` UTC, an hour ahead of the core banking DAG.
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
ingest_reference_data: DAG = build_ingest_dag(
    dag_id="ingest_reference_data", source_schema="ref", doc=__doc__
)
