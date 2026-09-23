"""Deliver the clearing files due on one day: `python -m generator.settlement --date D`.

`make generate-settlement-files DATE=...` runs this. On day D the processor delivers the file for
settlement date D, unless that file is one it sends late, and any file for settlement date D-3
that it held back. Files are written to the inbound bucket; the manifest of what each contains
is written beside them under `_simulation/`, where the ingestion path never looks.

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


def file_key(settlement_date: dt.date) -> str:
    return f"{PREFIX}NBK_CLR_{settlement_date:%Y%m%d}_01.csv"


def is_late(seed: int, settlement_date: dt.date, share: float) -> bool:
    return SubStreams(seed).stream("settlement.late", settlement_date.isoformat()).random() < share


def due_on(
    day: dt.date, anchor: dt.date, seed: int, share: float, late_by: int
) -> list[tuple[dt.date, bool]]:
    """The settlement dates whose files arrive on `day`, and whether each is late.

    No file is due for a settlement date before the anchor: the processor's history, like the
    bank's, starts where the simulation does.
    """
    due = []
    if day >= anchor and not is_late(seed, day, share):
        due.append((day, False))
    held = day - dt.timedelta(days=late_by)
    if held >= anchor and is_late(seed, held, share):
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


def build_file(cursor, settlement_date: dt.date, *, anchor, seed, section) -> build.Built:
    items = read_items(
        cursor, settlement_date, int(section["settlement_lag_days"]), section["cash_account"]
    )
    ledger: dict[tuple[str, str], decimal.Decimal] = {}
    for item in items:
        key = (item.network, item.settlement_currency)
        ledger[key] = ledger.get(key, decimal.Decimal(0)) + item.settlement_amount
    rng = SubStreams(seed).stream("settlement.file", settlement_date.isoformat())
    built = build.build(
        settlement_date=settlement_date,
        items=items,
        columns=timeline.columns(settlement_date, anchor),
        parameters=build.Parameters.from_profile(section),
        rng=rng,
        ledger_totals=ledger,
    )
    built.manifest["events"] = [e.name for e in timeline.active(settlement_date, anchor)]
    return built


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
            due = due_on(
                day,
                anchor,
                int(seed),
                float(section["late_file_share"]),
                int(section["late_by_days"]),
            )
            if not due:
                print(f"settlement: {day}: no file due")
            for settlement_date, late in due:
                built = build_file(
                    cursor, settlement_date, anchor=anchor, seed=int(seed), section=section
                )
                built.manifest.update({"delivered_on": day.isoformat(), "late": late})
                key = file_key(settlement_date)
                m = built.manifest
                print(
                    f"settlement: {day}: {key}{' (late)' if late else ''}: "
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
