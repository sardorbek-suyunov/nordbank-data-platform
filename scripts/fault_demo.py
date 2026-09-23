"""Demonstrate retry, backoff and no partial registration against injected faults (criterion 7).

Runs inside the scheduler container. It drives the real FX phases — open, extract, register —
against a local stub that behaves like the Frankfurter API with faults injected, and against a
scratch warehouse file and a scratch bucket, so nothing it does reaches the acceptance
warehouse or the lake. The stub serves the recorded 2026-07-24 response with its `date`
rewritten to the date asked for, so a recovered request lands rows; those rows exist only in
the scratch bucket.

Six scenarios, two for each fault the criterion names:

- **recovers**: the first two attempts at every date meet the fault, the third succeeds. The
  batch registers, and `ops.feed_request` records three attempts per date.
- **exhausts**: a three-date interval whose first two dates answer and whose third meets the
  fault on every attempt. Nothing is written, the batch is failed with the dates that did not
  answer, and the watermark does not move.

The faults: HTTP 429 with `Retry-After: 1`, a response that does not arrive before the request
timeout, and HTTP 503.

Usage, from the host: `make fault-demo`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

for candidate in ("/opt/airflow/plugins", "/opt/airflow/scripts", "/opt/airflow"):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

PORT = 8765
FIXTURE = Path("/opt/airflow/tests/fixtures/feeds/fx_2026-07-24.body")
SCRATCH_WAREHOUSE = "/tmp/fault_demo.duckdb"
SCRATCH_BUCKET = "nordbank-fault-demo"
TIMEOUT = 1.5

_counts: dict[str, int] = {}
_lock = threading.Lock()


class Stub(BaseHTTPRequestHandler):
    """`/<scenario>/v1/<date>`: the scenario decides which attempt at which date fails, and how."""

    def log_message(self, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - the http.server name
        parts = self.path.split("?")[0].strip("/").split("/")
        scenario, date = parts[0], parts[-1]
        fault, mode = scenario.split("-", 1)
        with _lock:
            _counts[self.path] = _counts.get(self.path, 0) + 1
            attempt = _counts[self.path]
        failing = (mode == "recovers" and attempt <= 2) or (
            mode == "exhausts" and date.endswith("26")
        )
        if failing and fault == "ratelimit":
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return
        if failing and fault == "timeout":
            time.sleep(TIMEOUT + 1.0)
        if failing and fault == "servererror":
            self.send_response(503)
            self.end_headers()
            return
        body = json.loads(FIXTURE.read_bytes())
        body["date"] = date
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


def _prepare() -> None:
    from nordbank_ops import clients

    Path(SCRATCH_WAREHOUSE).unlink(missing_ok=True)
    import duckdb

    connection = duckdb.connect(SCRATCH_WAREHOUSE)
    for sql in sorted(Path("/opt/airflow/infra/warehouse/schema").glob("*.sql")):
        code = "\n".join(
            line for line in sql.read_text().splitlines() if not line.strip().startswith("--")
        )
        for statement in code.split(";"):
            if statement.strip():
                connection.execute(statement)
    connection.close()
    client = clients.lake_client()
    buckets = {b["Name"] for b in client.list_buckets()["Buckets"]}
    if SCRATCH_BUCKET not in buckets:
        client.create_bucket(Bucket=SCRATCH_BUCKET)


def _context(day: dt.date, conf: dict) -> dict:
    class _Ti:
        def __init__(self) -> None:
            self.pushed: list = []

        def xcom_push(self, key, value):
            self.pushed.append(value)

        def xcom_pull(self, task_ids, key):
            return list(self.pushed)

    logical = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    run = SimpleNamespace(
        logical_date=logical, run_id=f"fault_demo__{day}__{conf['base_url']}", conf=conf
    )
    return {"dag_run": run, "ti": _Ti()}


def _scenario(fault: str, mode: str, day: dt.date, held: dt.date | None) -> dict:
    from nordbank_ops import registry, warehouse
    from nordbank_ops.feeds import phases

    if held is not None:
        with warehouse.connect(read_only=False) as connection:
            connection.execute(
                """
                insert or replace into ops.extract_watermark
                    (source_system, entity, watermark_at, advanced_by_batch_id, updated_at)
                values ('ecb', 'fx_rates', ?, 'fault-demo-seed', now())
                """,
                [dt.datetime.combine(held, dt.time(), tzinfo=dt.UTC)],
            )
    conf = {
        "base_url": f"http://localhost:{PORT}/{fault}-{mode}/v1",
        "max_attempts": 3 if mode == "recovers" else 4,
        "base_delay": 0.5,
        "max_delay": 4.0,
        "timeout": TIMEOUT,
    }
    context = _context(day, conf)
    started = time.monotonic()
    units = phases.fx_open(context)
    reports = phases.fx_extract(units[0], context)
    context["ti"].xcom_push(key=phases.REPORT_KEY, value=reports)
    summary = phases.register(context, phases.FX)
    elapsed = time.monotonic() - started
    batch_id = reports[0]["batch_id"]
    with warehouse.connect(read_only=True) as connection:
        batch = registry.batch(connection, batch_id)
        requests = connection.execute(
            "select request_key, attempts, final_status, outcome, rows_landed "
            "from ops.feed_request where batch_id = ? order by request_key",
            [batch_id],
        ).fetchall()
        mark = registry.watermark(connection, "ecb", "fx_rates")
    from nordbank_ops import clients

    objects = (
        clients.lake_client()
        .list_objects_v2(Bucket=SCRATCH_BUCKET, Prefix=batch["object_prefix"])
        .get("Contents", [])
    )
    return {
        "scenario": f"{fault}-{mode}",
        "batch_id": batch_id,
        "status": batch["status"],
        "rows_landed": batch["rows_landed"],
        "failure_reason": batch["failure_reason"],
        "requests": [
            {"date": r[0], "attempts": r[1], "final_status": r[2], "outcome": r[3], "rows": r[4]}
            for r in requests
        ],
        "watermark_after": mark.date().isoformat() if mark else None,
        "bronze_objects": len(objects),
        "seconds": round(elapsed, 1),
        "registered": summary["registered"],
    }


def main() -> int:
    os.environ["DUCKDB_PATH"] = SCRATCH_WAREHOUSE
    os.environ["LAKE_BUCKET"] = SCRATCH_BUCKET
    _prepare()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    results = []
    # Each fault gets its own month, so no two scenarios share an interval. The exhausting
    # interval ends on the 26th, which the stub fails on every attempt, and starts after a
    # watermark set three days earlier, so it asks for three dates.
    for month, fault in ((4, "ratelimit"), (5, "timeout"), (6, "servererror")):
        results.append(_scenario(fault, "recovers", dt.date(2026, month, 2), None))
        day = dt.date(2026, month, 26)
        results.append(_scenario(fault, "exhausts", day, day - dt.timedelta(days=3)))
    server.shutdown()
    for result in results:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
