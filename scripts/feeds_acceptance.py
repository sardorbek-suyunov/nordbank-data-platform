"""Report specification 006's evidence against what the backfill produced.

Runs inside a container, after the backfill, because the warehouse is on a named volume and
excludes readers while a writer holds it. It reads the registry, the bronze and quarantine
objects, the inbound bucket's deliveries and the processor's injection manifests, and the source
ledger through the read-only role, and prints one section per acceptance criterion it can speak
to. Criteria that are evidenced elsewhere — the offline suite, the fault demonstration, the
assets in Airflow, the checks on the pull request — are named with where their evidence is.

The settlement reconciliation here is an acceptance probe, not the metric: `metric_definitions.md`
defines settlement breaks over `sl_card_settlements` and `fct_gl_entries`, which are silver and
gold and do not exist yet. It applies the same rule to the processor's trailer totals as landed
in bronze and to the ledger as the source holds it.

Usage: `make feeds-acceptance`.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import decimal
import io
import json
import sys

sys.path.insert(0, "/opt/airflow/plugins")
sys.path.insert(0, "/opt/airflow/scripts")
sys.path.insert(0, "/opt/airflow")

from nordbank_ops import clients, warehouse  # noqa: E402

FEEDS = ("ecb", "cardnet", "opensanctions", "fred")
D = decimal.Decimal


def heading(number: int | str, title: str) -> None:
    print(f"\n=== criterion {number}: {title}")


def _parquet(client, bucket: str, prefix: str) -> list[dict]:
    import pyarrow.parquet as pq

    rows: list[dict] = []
    for item in client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", []):
        body = client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
        rows.extend(pq.read_table(io.BytesIO(body)).to_pylist())
    return rows


def _rows(connection, sql: str, parameters=()) -> list[tuple]:
    return connection.execute(sql, list(parameters)).fetchall()


def _landed(connection, client, bucket, system: str, entity: str) -> list[dict]:
    prefixes = _rows(
        connection,
        "select object_prefix from ops.batch_registry where source_system = ? and entity = ? "
        "and status = 'registered' order by batch_id",
        (system, entity),
    )
    rows: list[dict] = []
    for (prefix,) in prefixes:
        rows.extend(_parquet(client, bucket, prefix))
    return rows


def main() -> int:
    client = clients.lake_client()
    lake = clients.lake_bucket()
    import os

    inbound = os.environ["INBOUND_BUCKET"]

    with warehouse.connect(read_only=True) as connection:
        window = _rows(
            connection,
            "select min(ingest_date), max(ingest_date) from ops.batch_registry "
            "where source_system = 'corebank'",
        )[0]
        print(f"acceptance window, by ingest date: {window[0]} to {window[1]}")

        # --- 17 ----------------------------------------------------------------------------
        heading(17, "every batch ends registered or explicitly failed")
        by_status = _rows(
            connection,
            "select source_system, status, count(*) from ops.batch_registry "
            "group by all order by 1, 2",
        )
        for system, status, count in by_status:
            print(f"  {system:14} {status:11} {count}")
        stranded = sum(count for _s, status, count in by_status if status in ("open", "written"))
        failed = _rows(
            connection,
            "select source_system, entity, batch_id, failure_reason from ops.batch_registry "
            "where status = 'failed' order by batch_id",
        )
        for row in failed:
            print(f"  failed: {row[0]}.{row[1]} {row[2]}: {row[3][:150]}")
        print(f"  open or written: {stranded}")

        # --- 4 -----------------------------------------------------------------------------
        heading(4, "malformed records quarantine individually; landed + quarantined = read")
        identity = _rows(
            connection,
            "select source_system, count(*), sum(rows_read), sum(rows_landed), "
            "sum(rows_quarantined), "
            "count(*) filter (where rows_read <> rows_landed + rows_quarantined) "
            "from ops.batch_registry "
            "where source_system in ('ecb', 'cardnet', 'opensanctions', 'fred') "
            "and status = 'registered' group by 1 order by 1",
        )
        for system, batches, read, landed, quarantined, broken in identity:
            print(
                f"  {system:14} {batches} registered batch(es): read {read}, landed {landed}, "
                f"quarantined {quarantined}; batches where landed + quarantined <> read: {broken}"
            )
        modes = _rows(
            connection,
            """
            select case when q.reason like 'record has %' then 'wrong field count'
                        when q.reason like 'breaking drift%' then 'whole file, breaking drift'
                        when q.column_name in ('transaction_date', 'clearing_date')
                             and q.reason like 'value does not parse%' then 'invalid date'
                        when q.column_name = 'settlement_amount'
                             and q.reason like 'value does not parse%' then 'unparseable amount'
                        when q.reason like 'null in a non-nullable%' then 'missing required field'
                        else q.reason end as mode,
                   b.status, count(*)
              from dq.quarantine_log q join ops.batch_registry b using (batch_id)
             where q.source_system = 'cardnet'
             group by all order by 2 desc, 3 desc
            """,
        )
        for mode, status, count in modes:
            print(f"  quarantined, {status:10} batch: {mode:28} {count}")

        # --- 3 -----------------------------------------------------------------------------
        heading(3, "a late file lands in the partition of its arrival")
        late = _rows(
            connection,
            "select batch_id, ingest_date, cast(interval_start as date), object_prefix, "
            "rows_landed "
            "from ops.batch_registry where source_system = 'cardnet' and entity = 'settlements' "
            "and status = 'registered' and cast(interval_start as date) < ingest_date "
            "order by batch_id",
        )
        for batch_id, ingest_date, settlement_date, prefix, landed in late:
            rows = _parquet(client, lake, prefix)
            attribute = sorted({str(r["settlement_date"]) for r in rows})
            print(
                f"  {batch_id}: settlement date {settlement_date}, landed {landed} row(s) in "
                f"{prefix} (ingest date {ingest_date}); settlement_date on its rows: {attribute}"
            )
        print(f"  late files: {len(late)}")

        # --- 5 -----------------------------------------------------------------------------
        heading(5, "additive drift lands and logs; a removed column fails the whole file")
        drift = _rows(
            connection,
            "select d.entity, d.column_name, d.drift_kind, d.action_taken, count(*), "
            "min(b.ingest_date), max(b.ingest_date) from meta.schema_drift_log d "
            "join ops.batch_registry b using (batch_id) "
            "where d.source_system in ('ecb', 'cardnet', 'opensanctions', 'fred') "
            "group by all order by 6",
        )
        for entity, column, kind, action, count, first, last in drift:
            print(f"  {entity}.{column}: {kind}, {action}, {count} batch(es), {first} to {last}")

        # --- 6 -----------------------------------------------------------------------------
        heading(6, "FX rates for every publication date; weekends and holidays absent")
        requests = _rows(
            connection,
            "select r.request_key, r.outcome, r.rows_landed from ops.feed_request r "
            "join ops.batch_registry b using (batch_id) where r.source_system = 'ecb' "
            "and b.status = 'registered' order by 1",
        )
        outcomes = collections.Counter(outcome for _k, outcome, _n in requests)
        absent = [key for key, outcome, _n in requests if outcome == "absent"]
        weekdays = collections.Counter(dt.date.fromisoformat(k).strftime("%a") for k in absent)
        print(f"  requests: {dict(outcomes)}; absent dates by weekday: {dict(weekdays)}")
        rates = _landed(connection, client, lake, "ecb", "fx_rates")
        landed_dates = {r["rate_date"] for r in rates}
        requested_landed = {dt.date.fromisoformat(k) for k, o, _n in requests if o == "landed"}
        fabricated = sorted(d for d in landed_dates if d.weekday() >= 5 or d.isoformat() in absent)
        mismatched = sorted(landed_dates ^ requested_landed)
        payload_dates = {json.loads(r["_raw_payload"])["date"] for r in rates}
        print(
            f"  landed {len(rates)} rate row(s) over {len(landed_dates)} date(s); rows on a "
            f"weekend or an absent date: {len(fabricated)}; landed dates differing from dates "
            f"answered for themselves: {len(mismatched)}; payload dates equal landed dates: "
            f"{payload_dates == {d.isoformat() for d in landed_dates}}"
        )

        # --- 11 ----------------------------------------------------------------------------
        heading(11, "a card identifier resolves to one token through both sources")
        settlements = _landed(connection, client, lake, "cardnet", "settlements")
        cards = _landed(connection, client, lake, "corebank", "cards")
        file_tokens = {r["card_reference"] for r in settlements}
        core_tokens = {r["card_reference"] for r in cards}
        from_file = _rows(
            connection,
            "select count(*) from meta.pii_vault where first_seen_source_system = 'cardnet'",
        )[0][0]
        print(
            f"  distinct card tokens in settlements: {len(file_tokens)}; also in core.cards "
            f"bronze: {len(file_tokens & core_tokens)}; in settlements only: "
            f"{len(file_tokens - core_tokens)}"
        )
        print(f"  vault rows first seen through the settlement feed: {from_file}")
        from nordbank_ops.tokenise import Tokeniser

        tokeniser = Tokeniser.from_environment()
        sample = _rows(
            connection,
            "select token, raw_value, first_seen_entity from meta.pii_vault "
            "where first_seen_column = 'card_reference' order by token",
        )
        resolved = sum(1 for token, raw, _e in sample if token in file_tokens)
        rehash = all(tokeniser.token(raw) == token for token, raw, _e in sample)
        print(
            f"  card_reference vault rows: {len(sample)}, of which {resolved} are tokens the file "
            f"carried; every one re-tokenises to itself: {rehash}"
        )

        # --- 12 ----------------------------------------------------------------------------
        heading(12, "_raw_payload present and structurally faithful")
        checks = {}
        missing = sum(1 for r in settlements if not r.get("_raw_payload"))
        faithful = 0
        for r in settlements:
            fields = next(csv.reader(io.StringIO(r["_raw_payload"])))
            if (
                fields[1] == r["transaction_reference"]
                and r["card_reference"] in fields
                and D(fields[fields.index(r["settlement_currency"]) + 1]) == r["settlement_amount"]
            ):
                faithful += 1
        checks["cardnet.settlements"] = (len(settlements), missing, faithful)
        totals = _landed(connection, client, lake, "cardnet", "settlement_totals")
        checks["cardnet.settlement_totals"] = (
            len(totals),
            sum(1 for r in totals if not r.get("_raw_payload")),
            sum(
                1
                for r in totals
                if r["_raw_payload"].split(",")[1:3] == [r["network"], r["settlement_currency"]]
            ),
        )

        def _rate_agrees(row: dict) -> bool:
            served = json.loads(row["_raw_payload"], parse_float=D)
            return D(str(served["rates"][row["quote_currency"]])) == row["rate"]

        checks["ecb.fx_rates"] = (
            len(rates),
            sum(1 for r in rates if not r.get("_raw_payload")),
            sum(1 for r in rates if _rate_agrees(r)),
        )
        entities = _landed(connection, client, lake, "opensanctions", "entities")
        checks["opensanctions.entities"] = (
            len(entities),
            sum(1 for r in entities if not r.get("_raw_payload")),
            sum(1 for r in entities if json.loads(r["_raw_payload"])["id"] == r["entity_id"]),
        )
        for name, (count, absent_payloads, agree) in checks.items():
            print(
                f"  {name}: {count} row(s), {absent_payloads} without a payload, {agree} whose "
                f"payload agrees with the parsed columns"
            )
        cleartext = sum(
            1 for r in settlements for t in (r["_raw_payload"],) if any(
                raw in t for _tok, raw, _e in sample
            )
        )  # fmt: skip
        print(f"  settlement payloads containing a cleartext card reference: {cleartext}")

        # --- 13, 14, 1 ---------------------------------------------------------------------
        heading(13, "the sanctions snapshot is versioned and the fixture matchable")
        snapshots = _rows(
            connection,
            "select b.batch_id, b.ingest_date, f.publisher_version, f.business_date, "
            "b.rows_landed, f.content_checksum from ops.batch_registry b "
            "join ops.ingested_file f using (batch_id) "
            "where b.source_system = 'opensanctions' order by b.batch_id",
        )
        for batch_id, ingest_date, version, published, landed, checksum in snapshots:
            print(
                f"  {batch_id}: version {version}, published {published}, landed {landed}, "
                f"ingested {ingest_date}, {checksum[:19]}"
            )
        from generator.sanctions.build import FIXTURE, is_marker_name

        with FIXTURE.open(encoding="utf-8", newline="") as handle:
            fixture = {row["counterparty_name"] for row in csv.DictReader(handle)}
        latest = max(snapshots, key=lambda s: s[0])[0] if snapshots else None
        latest_names = {
            name for r in entities if r["_batch_id"] == latest for name in (r["names"] or [])
        }
        print(
            f"  fixture names in the latest landed snapshot: {len(fixture & latest_names)} of "
            f"{len(fixture)}"
        )

        heading(14, "no real sanctioned individual's name in any landed object")
        names = [
            name
            for r in entities
            for name in [r["caption"], *(r["names"] or []), *(r["aliases"] or [])]
        ]
        payload_names = [
            name
            for r in entities
            for entity in (json.loads(r["_raw_payload"]),)
            for name in [entity["caption"], *entity["properties"].get("name", []),
                         *entity["properties"].get("alias", [])]
        ]  # fmt: skip
        quarantined = _rows(
            connection,
            "select count(*) from dq.quarantine_log where source_system = 'opensanctions'",
        )[0][0]
        print(
            f"  names in landed sanctions rows: {len(names)}, marker names: "
            f"{sum(1 for n in names if is_marker_name(n))}; names in their payloads: "
            f"{len(payload_names)}, marker names: "
            f"{sum(1 for n in payload_names if is_marker_name(n))}; "
            f"quarantined sanctions records: {quarantined}"
        )

        heading(1, "a snapshot re-landed is a no-op; a new version is a new batch")
        sightings = _rows(
            connection,
            "select s.outcome, count(*), count(distinct s.object_key) from ops.file_sighting s "
            "where s.source_system = 'opensanctions' group by 1 order by 1",
        )
        for outcome, count, keys in sightings:
            print(f"  sightings {outcome}: {count} across {keys} object key(s)")
        distinct_content = len({s[5] for s in snapshots})
        print(
            f"  registered snapshot batches: {len(snapshots)}, distinct contents: "
            f"{distinct_content}"
        )

        heading(2, "file ingestion is idempotent by checksum")
        # Discovery sees every file in the inbound prefix on every run, so a file delivered on
        # day D is sighted again, and recognised, on every later day. Summarised, not listed.
        summary = _rows(
            connection,
            "select outcome, count(*), count(distinct object_key), "
            "count(distinct content_checksum) "
            "from ops.file_sighting where source_system = 'cardnet' group by 1 order by 1",
        )
        for outcome, count, keys, checksums in summary:
            print(
                f"  sightings {outcome}: {count} across {keys} object key(s) and {checksums} "
                f"distinct content(s)"
            )
        renamed = _rows(
            connection,
            "select s.object_key, f.object_key, s.batch_id from ops.file_sighting s "
            "join ops.ingested_file f using (content_checksum) "
            "where s.source_system = 'cardnet' and s.object_key <> f.object_key "
            "group by all order by 1",
        )
        for key, original, batch_id in renamed:
            print(f"  {key}: recognised as {original}, already landed as {batch_id}")
        counts = _rows(
            connection,
            "select count(*), count(distinct content_checksum) from ops.ingested_file "
            "where source_system = 'cardnet'",
        )[0]
        print(f"  ingested clearing files: {counts[0]}, distinct checksums: {counts[1]}")

        # --- 15 ----------------------------------------------------------------------------
        heading(15, "contract of the time: the version in force follows the interval")
        versions = _rows(
            connection,
            "select source_system, entity, contract_version, min(cast(interval_start as date)), "
            "max(cast(interval_start as date)), count(*) filter (where status = 'registered'), "
            "count(*) filter (where status = 'failed') from ops.batch_registry "
            "where (source_system, entity) in (('corebank','payments'), ('cardnet','settlements')) "
            "group by all order by 1, 2, 3",
        )
        for system, entity, version, first, last, registered, failed_count in versions:
            print(
                f"  {system}.{entity} version {version}: intervals {first} to {last}, "
                f"{registered} registered, {failed_count} failed"
            )
        recorded = _rows(
            connection,
            "select source_system, entity, contract_version, in_force_from "
            "from meta.contract_version "
            "where contract_version > 1 order by 1, 2",
        )
        for row in recorded:
            print(
                f"  meta.contract_version: {row[0]}.{row[1]} version {row[2]} "
                f"in force from {row[3]}"
            )

    # --- 10 --------------------------------------------------------------------------------
    heading(10, "settlement totals reconcile against the ledger, with the injected breaks")
    reconcile(connection=None, client=client, lake=lake, inbound=inbound, totals=totals)
    return 0


def reconcile(*, connection, client, lake: str, inbound: str, totals: list[dict]) -> None:
    from generator.settlement.build import severity

    file_side: dict[tuple, D] = {}
    for r in totals:
        key = (r["settlement_date"], r["network"], r["settlement_currency"])
        file_side[key] = r["amount_total"]
    with clients.source_cursor() as cursor:
        cursor.execute(
            """
            select g.posting_date + 1, p.network, e.entry_currency_code, -sum(e.amount)
              from core.transactions t
              join core.cards c on c.card_id = t.card_id
              join ref.card_products p on p.code = c.card_product_code
              join core.gl_transactions g
                on g.source_entity_code = 'transaction' and g.source_entity_id = t.transaction_id
              join core.gl_entries e
                on e.gl_transaction_id = g.gl_transaction_id and e.gl_account_code = '1000'
             where t.card_id is not null
               and g.posting_date + 1 between %s and %s
             group by 1, 2, 3
            """,
            (min(k[0] for k in file_side), max(k[0] for k in file_side)),
        )
        ledger = {(row[0], row[1], row[2]): D(row[3]) for row in cursor.fetchall()}

    detected = {}
    for key in sorted(set(file_side) | set(ledger)):
        file_total = file_side.get(key, D(0))
        ledger_total = ledger.get(key, D(0))
        verdict = severity(file_total, ledger_total)
        if verdict:
            detected[key] = (verdict, file_total - ledger_total)
    cells = len(set(file_side) | set(ledger))
    by_severity = collections.Counter(v for v, _d in detected.values())
    print(
        f"  cells compared (settlement date, network, currency): {cells}; zero difference: "
        f"{cells - len(detected)}; breaks: {dict(by_severity)}"
    )

    injected = {}
    listing = client.list_objects_v2(Bucket=inbound, Prefix="cardnet/_simulation/")
    for item in listing.get("Contents", []):
        manifest = json.loads(client.get_object(Bucket=inbound, Key=item["Key"])["Body"].read())
        day = dt.date.fromisoformat(manifest["settlement_date"])
        for entry in manifest["breaks"]:
            key = (day, entry["network"], entry["settlement_currency"])
            injected[key] = (entry["expected_severity"], D(entry["delta"]))
    in_window = {k: v for k, v in injected.items() if k[0] in {c[0] for c in file_side}}
    agree = [k for k in in_window if k in detected and detected[k] == in_window[k]]
    missed = [k for k in in_window if k not in detected]
    wrong = [k for k in in_window if k in detected and detected[k] != in_window[k]]
    unexplained = [k for k in detected if k not in in_window]
    print(
        f"  injected breaks in the window: {len(in_window)}; detected at the expected severity "
        f"and amount: {len(agree)}; missed: {len(missed)}; detected differently: {len(wrong)}; "
        f"detected but not injected: {len(unexplained)}"
    )
    for key in sorted(detected):
        verdict, difference = detected[key]
        mark = "injected" if key in in_window else "NOT INJECTED"
        print(f"    {key[0]} {key[1]:10} {key[2]} {verdict:5} difference {difference:>12} ({mark})")
    for key in unexplained + missed + wrong:
        print(f"  unexplained: {key} detected {detected.get(key)} injected {in_window.get(key)}")


if __name__ == "__main__":
    raise SystemExit(main())
