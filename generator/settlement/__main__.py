"""Deliver the clearing files due on one day: `python -m generator.settlement --date D`.

`make generate-settlement-files DATE=...` runs this. On day D the processor delivers the file for
settlement date D, unless that file is one it sends late, and any file for settlement date D-3
that it held back. A file is built in the layout the processor uses on the day it sends it, and
its header says when it was produced, which is that day.

**Corrections.** A processor corrects a file it got wrong. A share of the files that carried a
settlement break is sent again the day after they were first sent, as revision 2 of the same
sequence with the break removed; the platform keeps both, and the highest revision of a
sequence is the one that counts (architecture.md, "Batch arithmetic"). The correction is a new
transmission, so its transit damage is drawn afresh. One correction is scripted to arrive on
the same day as the late file it corrects, the case in which two deliveries for one settlement
date reach one run (`timeline.CORRECTED_ON_ARRIVAL_OFFSET`). Files are written to the inbound
bucket; the manifest of what each contains is written beside them under `_simulation/`, where
the ingestion path never looks.

Idempotent: a file is a pure function of the ledger, the seed and its settlement date, so
delivering the same day twice writes the same bytes to the same key. It reads the source with
the simulator's role, which is the side of the boundary this code is on, and never writes it.

`--dry-run` builds and summarises without delivering, which is how the rates were measured.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for candidate in (ROOT, ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from generator.config import load_profile  # noqa: E402
from generator.rng import SubStreams  # noqa: E402
from generator.settlement import build, timeline  # noqa: E402

PREFIX = "cardnet/"
MANIFEST_PREFIX = "cardnet/_simulation/"

ITEMS_SQL = """
select t.transaction_reference,
       (t.booked_at at time zone 'UTC')::date as transaction_date,
       g.posting_date as clearing_date,
       p.network,
       c.card_reference,
       c.card_bin,
       c.card_last_four,
       m.mcc_code,
       m.merchant_name,
       coalesce(t.is_card_present, false),
       e.entry_currency_code,
       -e.amount as settlement_amount
  from core.transactions t
  join core.cards c on c.card_id = t.card_id
  join ref.card_products p on p.code = c.card_product_code
  join core.gl_transactions g
    on g.source_entity_code = 'transaction' and g.source_entity_id = t.transaction_id
  join core.gl_entries e on e.gl_transaction_id = g.gl_transaction_id and e.gl_account_code = %s
  left join core.merchants m on m.merchant_id = t.merchant_id
 where t.card_id is not null
   and g.posting_date = %s
 order by t.transaction_reference
"""


def file_key(settlement_date: dt.date, revision: int = 1) -> str:
    base = f"{PREFIX}NBK_CLR_{settlement_date:%Y%m%d}_01"
    return f"{base}.csv" if revision == 1 else f"{base}_R{revision}.csv"


@dataclass(frozen=True)
class Delivery:
    """One file the processor sends on a day."""

    settlement_date: dt.date
    late: bool
    revision: int = 1


def is_corrected(seed: int, settlement_date: dt.date, share: float) -> bool:
    drawn = SubStreams(seed).stream("settlement.correction", settlement_date.isoformat()).random()
    return drawn < share


def is_late(seed: int, settlement_date: dt.date, share: float, anchor: dt.date) -> bool:
    drawn = SubStreams(seed).stream("settlement.late", settlement_date.isoformat()).random()
    return drawn < share or timeline.held_late(settlement_date, anchor)


def due_on(
    day: dt.date, anchor: dt.date, seed: int, share: float, late_by: int
) -> list[tuple[dt.date, bool]]:
    """The settlement dates whose files arrive on `day`, and whether each is late.

    No file is due for a settlement date before the anchor: the processor's history, like the
    bank's, starts where the simulation does.
    """
    due = []
    if day >= anchor and not is_late(seed, day, share, anchor):
        due.append((day, False))
    held = day - dt.timedelta(days=late_by)
    if held >= anchor and is_late(seed, held, share, anchor):
        due.append((held, True))
    return sorted(due)


def read_items(cursor, settlement_date: dt.date, lag: int, cash_account: str) -> list[build.Item]:
    cursor.execute(ITEMS_SQL, (cash_account, settlement_date - dt.timedelta(days=lag)))
    return [
        build.Item(
            transaction_reference=row[0],
            transaction_date=row[1],
            clearing_date=row[2],
            network=row[3],
            card_reference=row[4],
            card_bin=row[5],
            card_last_four=row[6],
            merchant_category_code=row[7],
            merchant_name=row[8],
            is_card_present=bool(row[9]),
            settlement_currency=row[10],
            settlement_amount=decimal.Decimal(row[11]),
        )
        for row in cursor.fetchall()
    ]


def inbound_client():
    import boto3
    from env_file import read_dotenv

    values = read_dotenv(ROOT / ".env")
    return boto3.client(
        "s3",
        endpoint_url=f"http://localhost:{values.get('MINIO_API_PORT', '9000')}",
        aws_access_key_id=values["MINIO_ROOT_USER"],
        aws_secret_access_key=values["MINIO_ROOT_PASSWORD"],
        region_name="us-east-1",
    ), values.get("INBOUND_BUCKET", "nordbank-inbound")


def build_file(
    cursor,
    settlement_date: dt.date,
    *,
    delivered_on: dt.date,
    anchor,
    seed,
    section,
    revision: int = 1,
) -> build.Built:
    """One file, as sent on `delivered_on`. A revision above 1 is a correction: the same items
    with no break, and its own draw of transit damage, because it is a new transmission."""
    items = read_items(
        cursor, settlement_date, int(section["settlement_lag_days"]), section["cash_account"]
    )
    ledger: dict[tuple[str, str], decimal.Decimal] = {}
    for item in items:
        key = (item.network, item.settlement_currency)
        ledger[key] = ledger.get(key, decimal.Decimal(0)) + item.settlement_amount
    parameters = build.Parameters.from_profile(section)
    stream = "settlement.file"
    if revision > 1:
        parameters.break_warn_share = parameters.break_error_share = 0.0
        stream = f"settlement.file.revision{revision}"
    rng = SubStreams(seed).stream(stream, settlement_date.isoformat())
    built = build.build(
        settlement_date=settlement_date,
        items=items,
        columns=timeline.columns(delivered_on, anchor),
        parameters=parameters,
        rng=rng,
        ledger_totals=ledger,
        created_at=dt.datetime.combine(delivered_on, dt.time(5), tzinfo=dt.UTC),
        revision=revision,
    )
    built.manifest["events"] = [e.name for e in timeline.active(delivered_on, anchor)]
    return built


def plan(cursor, day: dt.date, *, anchor, seed: int, section) -> list[Delivery]:
    """Everything the processor sends on `day`: the files due, and the corrections of files
    that carried a break, sent the day after them or, in the scripted case, with them."""
    share = float(section["late_file_share"])
    late_by = int(section["late_by_days"])
    lag = int(section["correction_lag_days"])
    correction_share = float(section["correction_share"])

    out = [Delivery(s, late) for s, late in due_on(day, anchor, seed, share, late_by)]

    def carried_a_break(settlement_date: dt.date, sent: dt.date) -> bool:
        first = build_file(
            cursor, settlement_date, delivered_on=sent, anchor=anchor, seed=seed, section=section
        )
        return bool(first.manifest["breaks"])

    sent_before = day - dt.timedelta(days=lag)
    for settlement_date, late in due_on(sent_before, anchor, seed, share, late_by):
        if timeline.corrected_on_arrival(settlement_date, anchor):
            continue
        if is_corrected(seed, settlement_date, correction_share) and carried_a_break(
            settlement_date, sent_before
        ):
            out.append(Delivery(settlement_date, late, revision=2))
    for delivery in list(out):
        if delivery.revision == 1 and timeline.corrected_on_arrival(
            delivery.settlement_date, anchor
        ):
            out.append(Delivery(delivery.settlement_date, delivery.late, revision=2))
    return sorted(out, key=lambda d: (d.settlement_date, d.revision))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m generator.settlement")
    parser.add_argument("--date", required=True, help="the day the files are delivered on")
    parser.add_argument("--to", help="deliver every day from --date to this date, dry runs only")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)

    from source_db_driver import connect

    first = dt.date.fromisoformat(arguments.date)
    last = dt.date.fromisoformat(arguments.to) if arguments.to else first
    if arguments.to and not arguments.dry_run:
        raise SystemExit("settlement: --to is for dry runs; deliveries go one day at a time")

    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("select profile, seed, anchor_date from platform.simulation_state")
        state = cursor.fetchone()
        if state is None:
            raise SystemExit("settlement: the source has no simulation state; run `make seed`")
        profile, seed, anchor = state
        section = load_profile(profile).section("settlement")

        client = bucket = None
        if not arguments.dry_run:
            client, bucket = inbound_client()

        day = first
        while day <= last:
            due = plan(cursor, day, anchor=anchor, seed=int(seed), section=section)
            if not due:
                print(f"settlement: {day}: no file due")
            for delivery in due:
                built = build_file(
                    cursor,
                    delivery.settlement_date,
                    delivered_on=day,
                    anchor=anchor,
                    seed=int(seed),
                    section=section,
                    revision=delivery.revision,
                )
                built.manifest.update({"delivered_on": day.isoformat(), "late": delivery.late})
                key = file_key(delivery.settlement_date, delivery.revision)
                m = built.manifest
                kind = " (late)" if delivery.late else ""
                if delivery.revision > 1:
                    kind += f" (correction, revision {delivery.revision})"
                print(
                    f"settlement: {day}: {key}{kind}: "
                    f"{m['detail_records']} record(s), {len(m['breaks'])} break(s), "
                    f"{len(m['malformed'])} malformed, {m['cells']} cell(s), "
                    f"events {','.join(m['events']) or 'none'}"
                )
                if client is not None:
                    client.put_object(Bucket=bucket, Key=key, Body=built.body)
                    manifest_key = MANIFEST_PREFIX + Path(key).with_suffix(".json").name
                    client.put_object(
                        Bucket=bucket,
                        Key=manifest_key,
                        Body=json.dumps(built.manifest, indent=2, sort_keys=True).encode("utf-8"),
                    )
            day += dt.timedelta(days=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
