"""Compare two acceptance runs from their table dumps, figure by figure.

`make feeds-acceptance RUN=name` dumps the registry, the file identity and sighting tables, the
request log, the contract versions, the drift log and the quarantine index. This reads two such
dumps and prints every figure the acceptance report is built from, side by side, marking the
ones that differ. It explains nothing: a difference is attributed by a person, to a change that
was made between the runs, or reported as a finding.

Runs on the host; it reads only the CSV files.

Usage: `uv run python scripts/compare_runs.py data/acceptance/run2 data/acceptance/run3`.
"""

from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path


def _read(directory: Path, table: str) -> list[dict]:
    with (directory / f"{table}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _quarantine_mode(row: dict) -> str:
    reason, column = row["reason"], row["column_name"]
    if reason.startswith("record has "):
        return "wrong field count"
    if reason.startswith("breaking drift"):
        return "whole file, breaking drift"
    unparseable = reason.startswith("value does not parse")
    if unparseable and column in ("transaction_date", "clearing_date"):
        return "invalid date"
    if unparseable and column == "settlement_amount":
        return "unparseable amount"
    if reason.startswith("null in a non-nullable"):
        return "missing required field"
    return reason


def figures(directory: Path) -> dict[str, object]:
    registry = _read(directory, "ops.batch_registry")
    ingested = _read(directory, "ops.ingested_file")
    sightings = _read(directory, "ops.file_sighting")
    requests = _read(directory, "ops.feed_request")
    versions = _read(directory, "meta.contract_version")
    drift = _read(directory, "meta.schema_drift_log")
    quarantine = _read(directory, "dq.quarantine_log")
    status = {b["batch_id"]: b["status"] for b in registry}

    out: dict[str, object] = {}
    for key, count in sorted(
        collections.Counter((b["source_system"], b["status"]) for b in registry).items()
    ):
        out[f"batches {key[0]} {key[1]}"] = count
    for system in sorted({b["source_system"] for b in registry}):
        registered = [
            b for b in registry if b["source_system"] == system and b["status"] == "registered"
        ]
        for measure in ("rows_read", "rows_landed", "rows_quarantined"):
            out[f"{system} registered {measure}"] = sum(int(b[measure]) for b in registered)
    cardnet = [b for b in registry if b["source_system"] == "cardnet"]
    out["cardnet batches by sequence"] = dict(
        sorted(collections.Counter(int(b["batch_sequence"]) for b in cardnet).items())
    )
    out["cardnet settlement dates"] = len({b["interval_start"][:10] for b in cardnet})
    out["cardnet empty registered settlement batches"] = sum(
        1
        for b in cardnet
        if b["entity"] == "settlements" and b["status"] == "registered" and b["rows_read"] == "0"
    )
    for key, count in sorted(
        collections.Counter(
            (q["entity"], _quarantine_mode(q), status.get(q["batch_id"])) for q in quarantine
        ).items()
    ):
        out[f"quarantine {key[0]} {key[1]} ({key[2]} batch)"] = count
    for key, count in sorted(
        collections.Counter(
            (d["source_system"], d["column_name"], d["drift_kind"]) for d in drift
        ).items()
    ):
        out[f"drift {key[0]} {key[1]} {key[2]}"] = count
    for key, count in sorted(
        collections.Counter((s["source_system"], s["outcome"]) for s in sightings).items()
    ):
        out[f"sightings {key[0]} {key[1]}"] = count
    for key, count in sorted(collections.Counter(f["source_system"] for f in ingested).items()):
        out[f"ingested files {key}"] = count
    for key, count in sorted(
        collections.Counter((r["source_system"], r["outcome"]) for r in requests).items()
    ):
        out[f"requests {key[0]} {key[1]}"] = count
    for v in versions:
        if int(v["contract_version"]) > 1:
            name = f"contract {v['source_system']}.{v['entity']} v{v['contract_version']}"
            out[name] = v["in_force_from"]
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    left, right = (Path(a) for a in argv)
    a, b = figures(left), figures(right)
    width = max(len(k) for k in {*a, *b})
    differing = 0
    print(f"{'figure':{width}}  {left.name:>14}  {right.name:>14}")
    for key in sorted({*a, *b}):
        mark = "" if a.get(key) == b.get(key) else "  <- differs"
        differing += bool(mark)
        print(f"{key:{width}}  {a.get(key, '-')!s:>14}  {b.get(key, '-')!s:>14}{mark}")
    print(f"\n{differing} figure(s) differ of {len({*a, *b})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
