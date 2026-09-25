"""Demonstrate parking: a refused delivery waits for its contract, then lands (ADR 0016).

Runs inside the scheduler container, against a scratch warehouse, a scratch lake bucket, a
scratch inbound bucket and a scratch copy of the contract tree, so nothing reaches the
acceptance warehouse, the lake or the committed contracts. It drives the real settlement
phases — discover, open, extract, register — as the DAG does.

The scratch contract tree holds `settlements` as it stood before version 2: only version 1,
which requires `merchant_name`. The processor then delivers a file without it, built by the
simulation's own file builder:

1. **Day 1.** The file is new, its batches fail on breaking drift, and nothing is recorded in
   `ops.ingested_file`: it did not land.
2. **Days 2, 3 and 4.** The file is still in the inbound bucket. Each run sights it as
   `parked`, naming the failed batch, and allocates nothing. On day 3 the processor's transfer
   also drops a renamed copy of the same bytes, which is the same parked delivery.
3. **Day 5.** A person publishes version 2, moving version 1 under `history/` and setting
   `in_force_from` to day 1, as the halt message says. The contracts in force for the file
   have changed, so it is sighted `reattempted`, lands under version 2, registers, and gains
   its `ops.ingested_file` row.
4. **Day 6.** Both keys are sighted `already_ingested`.

What each step recorded is printed as JSON, one line per day, and the script exits non-zero if
any of those expectations does not hold.

Usage, from the host: `make parking-demo`.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import random
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

for candidate in ("/opt/airflow/plugins", "/opt/airflow/scripts", "/opt/airflow"):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

SCRATCH_WAREHOUSE = "/tmp/parking_demo.duckdb"
SCRATCH_LAKE = "nordbank-fault-demo"
SCRATCH_INBOUND = "nordbank-parking-inbound"
SCRATCH_CONTRACTS = Path("/tmp/parking_demo_contracts")
FIRST = dt.date(2026, 9, 7)


def _contracts_before_version_2() -> None:
    shutil.rmtree(SCRATCH_CONTRACTS, ignore_errors=True)
    shutil.copytree("/opt/airflow/contracts", SCRATCH_CONTRACTS)
    cardnet = SCRATCH_CONTRACTS / "cardnet"
    (cardnet / "settlements.yml").unlink()
    (cardnet / "history" / "settlements.v1.yml").rename(cardnet / "settlements.yml")


def _publish_version_2(in_force_from: dt.date) -> None:
    cardnet = SCRATCH_CONTRACTS / "cardnet"
    (cardnet / "settlements.yml").rename(cardnet / "history" / "settlements.v1.yml")
    body = Path("/opt/airflow/contracts/cardnet/settlements.yml").read_text(encoding="utf-8")
    committed = "in_force_from: 2026-09-03"
    assert committed in body, "the committed version 2 no longer says what this demo replaces"
    body = body.replace(committed, f"in_force_from: {in_force_from.isoformat()}")
    (cardnet / "settlements.yml").write_text(body, encoding="utf-8")


def _file(settlement: dt.date) -> bytes:
    """A clearing file in the layout without `merchant_name`, by the simulation's builder."""
    from generator.settlement import build, timeline

    columns = tuple(c for c in timeline.BASE_COLUMNS if c != "merchant_name")
    items = [
        build.Item(
            transaction_reference=f"PARKDEMO{n:08d}",
            transaction_date=settlement - dt.timedelta(days=2),
            clearing_date=settlement - dt.timedelta(days=1),
            network="visa" if n % 2 else "mastercard",
            card_reference=f"PD{n:010d}",
            card_bin="400001",
            card_last_four=f"{n:04d}",
            merchant_category_code="5411",
            merchant_name=None,
            is_card_present=bool(n % 3),
            settlement_currency="EUR",
            settlement_amount=decimal.Decimal(f"{12 + n}.5000"),
        )
        for n in range(24)
    ]
    parameters = build.Parameters(
        processor_id="NBKPROC",
        malformed_record_share=0.0,
        malformed_kind_mix={build.UNPARSEABLE_AMOUNT: 1.0},
        break_warn_share=0.0,
        break_error_share=0.0,
        break_warn_relative=(0.0001, 0.0008),
        break_error_relative=(0.005, 0.05),
    )
    return build.build(
        settlement_date=settlement,
        items=items,
        columns=columns,
        parameters=parameters,
        rng=random.Random(7),
        ledger_totals={},
    ).body


def _context(day: dt.date) -> dict:
    class _Ti:
        def __init__(self) -> None:
            self.pushed: dict[tuple[str, str], object] = {}

        def xcom_push(self, key, value, task_id: str = "extract"):
            self.pushed[(task_id, key)] = value

        def xcom_pull(self, task_ids, key: str = "return_value"):
            return self.pushed.get((task_ids, key))

    logical = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    run = SimpleNamespace(logical_date=logical, run_id=f"parking_demo__{day}", conf={})
    return {"dag_run": run, "ti": _Ti()}


def _run(day: dt.date) -> dict:
    """One settlement run for one day, as the DAG runs it, with the sensor having found a file."""
    from nordbank_ops import warehouse
    from nordbank_ops.feeds import phases

    context = _context(day)
    candidates = phases.settlement_discover(context)
    units = phases.settlement_open(context, candidates, {"found": True})
    context["ti"].xcom_push(key="return_value", value=units, task_id="open_batches")
    reports = []
    for unit in units:
        reports.extend(phases.settlement_extract(unit, context))
    context["ti"].xcom_push(key=phases.REPORT_KEY, value=reports)
    phases.register(context, phases.CARDNET, phases.settlement_identifiers)
    with warehouse.connect(read_only=True) as connection:
        sightings = connection.execute(
            "select object_key, outcome, batch_id from ops.file_sighting where ingest_date = ? "
            "order by object_key",
            [day],
        ).fetchall()
        batches = connection.execute(
            "select batch_id, contract_version, status, rows_read, rows_landed, "
            "rows_quarantined, failure_reason from ops.batch_registry "
            "where entity = 'settlements' order by batch_id"
        ).fetchall()
        ingested = connection.execute(
            "select object_key, batch_id from ops.ingested_file order by 1"
        ).fetchall()
    return {
        "day": day.isoformat(),
        "allocated": len(units),
        "sightings": [
            {"key": k.rsplit("/", 1)[1], "outcome": o, "batch": b} for k, o, b in sightings
        ],
        "settlement_batches": [
            {
                "batch": b,
                "version": v,
                "status": s,
                "read": r,
                "landed": n,
                "quarantined": q,
                "reason": f,
            }
            for b, v, s, r, n, q, f in batches
        ],
        "ingested_file": [{"key": k.rsplit("/", 1)[1], "batch": b} for k, b in ingested],
    }


def main() -> int:
    os.environ["DUCKDB_PATH"] = SCRATCH_WAREHOUSE
    os.environ["LAKE_BUCKET"] = SCRATCH_LAKE
    os.environ["INBOUND_BUCKET"] = SCRATCH_INBOUND

    import fault_demo
    from nordbank_ops import clients

    fault_demo.SCRATCH_WAREHOUSE = SCRATCH_WAREHOUSE
    fault_demo.SCRATCH_BUCKET = SCRATCH_LAKE
    fault_demo._prepare()
    _contracts_before_version_2()
    import nordbank_ops.ingest

    nordbank_ops.ingest.contract_root = lambda: SCRATCH_CONTRACTS

    client = clients.lake_client()
    if SCRATCH_INBOUND not in {b["Name"] for b in client.list_buckets()["Buckets"]}:
        client.create_bucket(Bucket=SCRATCH_INBOUND)
    for item in client.list_objects_v2(Bucket=SCRATCH_INBOUND).get("Contents", []):
        client.delete_object(Bucket=SCRATCH_INBOUND, Key=item["Key"])
    body = _file(FIRST)
    key = f"cardnet/NBK_CLR_{FIRST:%Y%m%d}_01.csv"
    client.put_object(Bucket=SCRATCH_INBOUND, Key=key, Body=body)

    days = [FIRST + dt.timedelta(days=n) for n in range(6)]
    results = []
    for n, day in enumerate(days):
        if n == 2:
            copy = f"cardnet/NBK_CLR_{FIRST:%Y%m%d}_01_COPY.csv"
            client.put_object(Bucket=SCRATCH_INBOUND, Key=copy, Body=body)
        if n == 4:
            _publish_version_2(FIRST)
        results.append(_run(day))
    for result in results:
        print(json.dumps(result))

    first, *parked, landed, after = results[0], *results[1:4], results[4], results[5]
    expectations = {
        "day 1 fails on breaking drift and nothing is ingested": (
            first["settlement_batches"][0]["status"] == "failed"
            and first["settlement_batches"][0]["reason"].startswith("breaking drift")
            and first["ingested_file"] == []
        ),
        "days 2 to 4 allocate nothing and sight the file parked": all(
            r["allocated"] == 0
            and r["sightings"]
            and all(s["outcome"] == "parked" for s in r["sightings"])
            for r in parked
        ),
        "the registry does not grow while parked": all(
            len(r["settlement_batches"]) == 1 for r in parked
        ),
        "after the bump the file is reattempted and lands under version 2": (
            landed["sightings"][0]["outcome"] == "reattempted"
            and landed["settlement_batches"][-1]["status"] == "registered"
            and landed["settlement_batches"][-1]["version"] == 2
            and len(landed["ingested_file"]) == 1
        ),
        "afterwards both keys are already ingested": (
            {s["outcome"] for s in after["sightings"]} == {"already_ingested"}
            and len(after["sightings"]) == 2
        ),
    }
    for claim, held in expectations.items():
        print(f"parking-demo: {'holds' if held else 'DOES NOT HOLD'}: {claim}")
    return 0 if all(expectations.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
