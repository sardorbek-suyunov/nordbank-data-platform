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

**The tables behind the report are dumped with it.** Run 1's warehouse was destroyed by the
`make nuke` that started run 2, and two of its figures could then be reconciled only from logs,
and not all of them at all. `--dump DIR` writes the registry, the file identity and sighting
tables, the request log and the contract versions as CSV, so a later review reconciles from
tables. Those tables hold no identifier, and that is checked rather than assumed: every dumped
file is searched for every vault value, by the PII scan's own matcher, before the dump is kept.

Usage: `make feeds-acceptance [RUN=name]`; with RUN, the dump lands in `data/acceptance/<name>/`.
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
    """Every row under a prefix, with a `json` column decoded to the value it holds.

    Since specification 007 the writer stores a `json` column as JSON text in the Arrow JSON
    type; an object written before it holds the value as a list. Both read back as the value.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows: list[dict] = []
    for item in client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", []):
        body = client.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
        table = pq.read_table(io.BytesIO(body))
        encoded = [f.name for f in table.schema if isinstance(f.type, pa.JsonType)]
        for row in table.to_pylist():
            for name in encoded:
                if row[name] is not None:
                    row[name] = json.loads(row[name])
            rows.append(row)
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


DUMPED = (
    "ops.batch_registry",
    "ops.ingested_file",
    "ops.file_sighting",
    "ops.feed_request",
    "meta.contract_version",
    "meta.schema_drift_log",
    "dq.quarantine_log",
)


def dump_tables(directory: str) -> int:
    """Write the tables a review reconciles from, and refuse if any holds a vault value."""
    import pathlib

    from bronze_pii_scan import Matcher, encodings

    out = pathlib.Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    with warehouse.connect(read_only=True) as connection:
        vault = connection.execute("select token, raw_value from meta.pii_vault").fetchall()
        for table in DUMPED:
            target = out / f"{table}.csv"
            connection.execute(f"copy (select * from {table} order by all) to '{target}' (header)")
            count = connection.execute(f"select count(*) from {table}").fetchone()[0]
            print(f"dump: {table}: {count} row(s) to {target.name}")
    matcher = Matcher([(t, encodings(v)) for t, v in vault if len(v.strip()) >= 6])
    hits = {
        path.name: sorted(matcher.tokens_in(path.read_bytes()))
        for path in sorted(out.glob("*.csv"))
    }
    hits = {name: tokens for name, tokens in hits.items() if tokens}
    print(f"dump: searched {len(DUMPED)} file(s) for {len(vault)} vault value(s): {hits or 'none'}")
    return 1 if hits else 0


def main() -> int:
    if "--dump" in sys.argv:
        return dump_tables(sys.argv[sys.argv.index("--dump") + 1])
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
        # Every clearing file registered after its settlement date: a late file, a correction
        # sent the day after the file it corrects, or a transmission sent again complete.
        late = _rows(
            connection,
            "select b.batch_id, b.ingest_date, cast(b.interval_start as date), b.object_prefix, "
            "b.rows_landed, f.object_key, f.revision "
            "from ops.batch_registry b join ops.ingested_file f using (batch_id) "
            "where b.source_system = 'cardnet' and b.entity = 'settlements' "
            "and b.status = 'registered' and cast(b.interval_start as date) < b.ingest_date "
            "order by b.batch_id",
        )
        kinds = collections.Counter()
        for batch_id, ingest_date, settlement_date, prefix, landed, key, revision in late:
            rows = _parquet(client, lake, prefix)
            attribute = sorted({str(r["settlement_date"]) for r in rows})
            kind = (
                "correction"
                if revision > 1
                else "sent again complete"
                if key.endswith("_RESEND.csv")
                else "late file"
            )
            kinds[kind] += 1
            print(
                f"  {batch_id} ({kind}, {key.rsplit('/', 1)[1]}): settlement date "
                f"{settlement_date}, landed {landed} row(s) in {prefix} (ingest date "
                f"{ingest_date}); settlement_date on its rows: {attribute}"
            )
        print(f"  registered after their settlement date: {dict(kinds)}")

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
        # Evidence of additive drift survives in bronze even though no parsed column carries it:
        # the payload is the line as delivered, so every row of every batch that logged the
        # added field must carry one more field than the contract's column line (R5).
        added = _rows(
            connection,
            "select d.batch_id, b.object_prefix, b.contract_version from meta.schema_drift_log d "
            "join ops.batch_registry b using (batch_id) where d.drift_kind = 'additive' "
            "and d.source_system = 'cardnet' and b.status = 'registered' order by 1",
        )
        expected_fields = {1: 13, 2: 12}
        carrying = 0
        for _batch_id, prefix, version in added:
            payloads = [r["_raw_payload"] for r in _parquet(client, lake, prefix)]
            widths = {len(next(csv.reader(io.StringIO(p)))) for p in payloads}
            carrying += bool(payloads) and widths == {expected_fields[version]}
        print(
            f"  registered batches that logged an additive field: {len(added)} (floor 40); "
            f"whose every payload carries it: {carrying}"
        )

        # --- 6 -----------------------------------------------------------------------------
        heading(6, "FX rates for every publication date; weekends and holidays absent")
        requests = _rows(
            connection,
            "select r.request_key, r.outcome, r.rows_landed from ops.feed_request r "
            "join ops.batch_registry b using (batch_id) where r.source_system = 'ecb' "
            "and b.status = 'registered' order by 1",
        )
        outcomes = collections.Counter(outcome for _k, outcome, _n in requests)
        absent = [key for key, outcome, _n in requests if outcome == "absent_no_publication"]
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

        # --- R15: corrections and the batch arithmetic --------------------------------------
        heading("R15", "corrections: the highest revision of a sequence replaces the others")
        files = _rows(
            connection,
            "select f.business_date, f.file_sequence, f.revision, f.object_key, b.ingest_date, "
            "f.batch_id from ops.ingested_file f join ops.batch_registry b using (batch_id) "
            "where f.source_system = 'cardnet' order by 1, 2, 3",
        )
        first_sent = {
            (day, sequence): ingested
            for day, sequence, revision, _k, ingested, _b in files
            if revision == 1
        }
        corrections = [f for f in files if f[2] > 1]
        of_late = [f for f in corrections if first_sent.get((f[0], f[1]), f[0]) > f[0]]
        same_day = [f for f in corrections if first_sent.get((f[0], f[1])) == f[4]]
        for day, sequence, revision, key, ingested, batch_id in corrections:
            original = first_sent.get((day, sequence))
            print(
                f"  {key.rsplit('/', 1)[1]}: settlement date {day}, sequence {sequence}, revision "
                f"{revision}, ingested {ingested} as {batch_id}; revision 1 ingested {original}"
            )
        print(
            f"  corrections landed: {len(corrections)} (floor 3: "
            f"{'met' if len(corrections) >= 3 else 'NOT MET'}); corrections of a late file: "
            f"{len(of_late)} (floor 1: {'met' if of_late else 'NOT MET'}); arriving in the same "
            f"run as the file they correct: {len(same_day)}"
        )
        print(
            "  the correction rate, 0.25 of break-carrying files, is not asserted at ci: sixty-one "
            "days hold too few break-carrying files for a band to mean anything"
        )
        for day, sequence, _revision, _k, ingested, _batch in same_day:
            pair = [f[5] for f in files if f[0] == day and f[1] == sequence]
            print(
                f"  same run, {ingested}: settlement date {day} sequence {sequence} landed as "
                f"{', '.join(pair)}, distinct batches: {len(set(pair)) == len(pair)}"
            )

        # --- R13: delivery-date selection and parking --------------------------------------
        heading("R13", "a file is read against the contract of its delivery date; refusals park")
        straddling = _rows(
            connection,
            "select f.object_key, f.business_date, b.ingest_date, b.contract_version, b.status, "
            "b.rows_read from ops.ingested_file f join ops.batch_registry b using (batch_id) "
            "where f.source_system = 'cardnet' and f.business_date < date '2026-09-03' "
            "and b.ingest_date >= date '2026-09-03' and b.entity = 'settlements'",
        )
        for key, day, ingested, version, status, read in straddling:
            print(
                f"  straddling late file {key.rsplit('/', 1)[1]}: settlement date {day}, "
                f"delivered and ingested {ingested}, read against settlements version {version}: "
                f"{status}, {read} record(s) read"
            )
        empty = _rows(
            connection,
            "select coalesce(empty_reason, 'delivered'), count(*) from ops.batch_registry "
            "where source_system = 'cardnet' and entity = 'settlements' and status = 'registered' "
            "and rows_read = 0 group by 1 order by 1",
        )
        print(f"  empty settlement batches by reason: {dict(empty)}")
        never_landed = _rows(
            connection,
            "select s.content_checksum, s.object_key, s.outcome, count(*), min(s.ingest_date), "
            "max(s.ingest_date) from ops.file_sighting s "
            "where s.source_system = 'cardnet' and s.content_checksum not in "
            "(select content_checksum from ops.ingested_file) group by all order by 5, 2, 3",
        )
        for checksum, key, outcome, count, first, last in never_landed:
            print(
                f"  never landed {checksum[:19]} {key.rsplit('/', 1)[1]}: {outcome} {count} "
                f"time(s), {first} to {last}"
            )
        refused = _rows(
            connection,
            "select batch_id, ingest_date, contract_version, rows_read, rows_quarantined, "
            "failure_reason from ops.batch_registry where source_system = 'cardnet' "
            "and entity = 'settlements' and status = 'failed' order by batch_id",
        )
        for batch_id, ingested, version, read, quarantined, reason in refused:
            print(
                f"  failed {batch_id}, ingested {ingested}, version {version}, read {read}, "
                f"quarantined {quarantined}: {reason[:110]}"
            )

        # --- 15 ----------------------------------------------------------------------------
        heading(15, "contract of the time: the version in force follows the day of the delivery")
        versions = _rows(
            connection,
            "select source_system, entity, contract_version, min(cast(interval_start as date)), "
            "max(cast(interval_start as date)), count(*) filter (where status = 'registered'), "
            "count(*) filter (where status = 'failed'), min(ingest_date), max(ingest_date) "
            "from ops.batch_registry "
            "where (source_system, entity) in (('corebank','payments'), ('cardnet','settlements')) "
            "group by all order by 1, 2, 3",
        )
        for system, entity, version, first, last, registered, failed_count, seen, until in versions:
            print(
                f"  {system}.{entity} version {version}: intervals {first} to {last}, ingested "
                f"{seen} to {until}, {registered} registered, {failed_count} failed"
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


def severity(file_total: D, ledger_total: D) -> str | None:
    """The severity metric_definitions.md assigns to a difference, implemented here rather than
    imported from the simulation, which labels the breaks it injects with its own copy of the
    rule: a probe that used the injector's function would agree with it by construction.

    Any non-zero difference is a break. `warn` when it is below 0.1 per cent of the absolute
    file total and below 100 units; `error` at or above either threshold.
    """
    difference = abs(file_total - ledger_total)
    if difference == 0:
        return None
    if difference < D("0.001") * abs(file_total) and difference < D("100"):
        return "warn"
    return "error"


def reconcile(*, connection, client, lake: str, inbound: str, totals: list[dict]) -> None:
    # The batch arithmetic for a delivered file (architecture.md): within one of the sender's
    # file sequences the highest revision replaces the others, and sequences add. Summing every
    # batch of a settlement date would count a corrected file twice; that sum is computed too,
    # so the report shows what the rule prevents.
    highest: dict[tuple, int] = {}
    for r in totals:
        key = (r["settlement_date"], r["file_sequence"])
        highest[key] = max(highest.get(key, 0), r["revision"])
    file_side: dict[tuple, D] = {}
    summed: dict[tuple, D] = {}
    for r in totals:
        key = (r["settlement_date"], r["network"], r["settlement_currency"])
        summed[key] = summed.get(key, D(0)) + r["amount_total"]
        if r["revision"] == highest[(r["settlement_date"], r["file_sequence"])]:
            file_side[key] = file_side.get(key, D(0)) + r["amount_total"]
    doubled = sorted(k for k in summed if summed[k] != file_side.get(k))
    print(
        f"  cells a sum of every batch would get wrong (a corrected file counted twice): "
        f"{len(doubled)}, over {len({k[0] for k in doubled})} settlement date(s)"
    )
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

    # What the processor injected, from the manifests the simulation keeps. A break in a file
    # that a correction replaced is expected to be gone after the arithmetic, so only breaks
    # in the highest revision that landed are expected to be detected.
    injected = {}
    corrected_away = 0
    listing = client.list_objects_v2(Bucket=inbound, Prefix="cardnet/_simulation/")
    for item in listing.get("Contents", []):
        manifest = json.loads(client.get_object(Bucket=inbound, Key=item["Key"])["Body"].read())
        day = dt.date.fromisoformat(manifest["settlement_date"])
        revision = manifest.get("revision", 1)
        if manifest.get("transmission") == "cut_off" or revision != highest.get(
            (day, manifest.get("file_sequence", 1)), revision
        ):
            corrected_away += len(manifest["breaks"]) * (manifest.get("transmission") != "cut_off")
            continue
        for entry in manifest["breaks"]:
            key = (day, entry["network"], entry["settlement_currency"])
            injected[key] = (entry["expected_severity"], D(entry["delta"]))
    print(f"  injected breaks removed by a correction that landed: {corrected_away}")
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
