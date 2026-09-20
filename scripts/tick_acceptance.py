"""The acceptance run for specification 004, and the evidence it produces.

`make tick-acceptance` seeds a profile, advances it a stated number of days asserting the
reconciliation after every tick, and then measures what the run produced against what the
specification asks for. `REPLAY=1` does the whole thing twice and compares a digest of every
table, which is acceptance criterion 4.

**Why this is a script and not a test.** A test answers yes or no. Half of what spec 004 asks
for is a *measurement* reported against a stated distribution — the late-arrival lag, the
disposition lag, the change-class counts, the duplicate similarity — and a threshold on any of
them would either be vacuous at `ci` or flaky at `dev`. The numbers go in the checkpoint and a
reader judges them. What is asserted rather than reported is asserted where it belongs: the
invariants in `make seed-verify`, the reconciliation at each tick, the atomicity in
`make test-integration`.

It reports rather than asserts, with one exception: the reconciliation raises, because a tick
log that disagrees with the database is a broken control rather than a measurement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (str(ROOT), str(ROOT / "scripts")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from source_db_driver import connect  # noqa: E402

from generator import manifest as manifest_module  # noqa: E402
from generator import pipeline, refdata, writer  # noqa: E402
from generator.config import RunConfig, load_profile  # noqa: E402
from generator.mutation import reconcile as reconcile_module  # noqa: E402
from generator.mutation import reset as reset_module  # noqa: E402
from generator.mutation import snapshot as snapshot_module  # noqa: E402
from generator.mutation import tick as tick_module  # noqa: E402
from generator.spool import Spool  # noqa: E402

# The duplicate names this reports carry diacritics and Cyrillic confusables by design, and
# the console this was built on is cp1251. Without this the report crashes on its own evidence.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_ANCHOR = dt.date(2026, 9, 18)
DEFAULT_SEED = 42

# Two names are similar enough to be the same person when they share this much of their
# character trigrams. Computed here rather than in the database: pg_trgm and fuzzystrmatch are
# available and neither is installed, because criterion 11 asks for a report and not for a
# database capability, and `similarity` returns a `real` where design rule 1 prohibits floating
# point in these schemas (spec 004, amended).
SIMILARITY_FLOOR = 0.55

# The tables criterion 3 scans. Every core table a tick can touch.
TABLES = (
    "customers",
    "customer_addresses",
    "accounts",
    "account_holders",
    "cards",
    "merchants",
    "loan_applications",
    "loans",
    "loan_installments",
    "transactions",
    "payments",
    "gl_transactions",
    "gl_entries",
    "fraud_alerts",
    "login_sessions",
)


def trigrams(value: str) -> set[str]:
    padded = f"  {value.strip().lower()} "
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def similarity(left: str, right: str) -> float:
    """Jaccard similarity over character trigrams: what `pg_trgm.similarity` computes."""
    a, b = trigrams(left), trigrams(right)
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def seed_profile(profile_name: str, seed: int, anchor: dt.date) -> RunConfig:
    config = RunConfig(profile=load_profile(profile_name), seed=seed, anchor=anchor)
    ref = refdata.load()
    refdata.validate_against(ref, config.profile.params)
    root = pipeline.spool_root(f"{profile_name}-acceptance")
    shutil.rmtree(root, ignore_errors=True)
    spool = Spool(root)
    pipeline.generate(config, ref, spool)
    reset_module.reset(profile=profile_name, seed=seed, anchor=anchor)
    writer.load(config, spool)
    shutil.rmtree(root, ignore_errors=True)
    return config


def run_ticks(config: RunConfig, ticks: int) -> list[float]:
    """Advance the source `ticks` days, asserting the reconciliation after every one."""
    timings: list[float] = []
    with connect() as connection:
        for _ in range(ticks):
            started = time.perf_counter()
            tick_module.run(connection, requested_date=None, profile=config.profile)
            timings.append(time.perf_counter() - started)

            with connection.cursor() as cursor:
                result = reconcile_module.reconcile_latest(cursor)
            connection.rollback()
            reconcile_module.assert_agrees(result)

        with connection.cursor() as cursor:
            snapshot_module.synchronise_sequences(cursor)
        connection.commit()
    return timings


def query(cursor, sql: str, params: tuple = ()) -> list[tuple]:
    cursor.execute(sql, params)
    return cursor.fetchall()


def report(config: RunConfig, timings: list[float]) -> None:
    anchor = config.anchor
    with connect(autocommit=True) as connection, connection.cursor() as cursor:
        _timing(cursor, config, timings)
        _change_classes(cursor)
        _clock(cursor, anchor)
        _late_arrivals(cursor, anchor)
        _dispositions(cursor, config, anchor)
        _deletes(cursor)
        _duplicates(cursor, anchor)
        _drift(cursor)


def _heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def _timing(cursor, config: RunConfig, timings: list[float]) -> None:
    """Per-tick runtime, from the tick log rather than from this process.

    The log is what a re-run of this report over an already-ticked book can read, and it is the
    truer figure: the wall time this process measures also carries the reconciliation, which an
    operator's `make tick` does not pay.
    """
    _heading(f"Criterion 13: tick runtime, {config.profile.name} profile")
    logged = [int(row[0]) for row in query(cursor, "select duration_ms from platform.tick_log")]
    if not logged:
        print("  no tick has run")
        return
    print(f"  ticks                {len(logged)}")
    print(f"  minimum              {min(logged):,} ms")
    print(f"  median               {statistics.median(sorted(logged)):,.0f} ms")
    print(f"  mean                 {sum(logged) / len(logged):,.0f} ms")
    print(f"  maximum              {max(logged):,} ms")
    print(f"  total                {sum(logged) / 1000:,.1f} s")
    if timings:
        print(
            f"  wall time this run   {sum(timings):,.1f} s for {len(timings)} tick(s), "
            f"reconciliation included"
        )


def _change_classes(cursor) -> None:
    _heading("Criterion 6 and 8: change classes, over the whole run")
    rows = query(
        cursor,
        """
        select c.table_name, sum(c.rows_inserted), sum(c.rows_updated),
               sum(c.rows_soft_deleted), sum(c.rows_late_arriving), sum(c.rows_deleted)
          from platform.tick_table_counts c
         group by c.table_name
         order by c.table_name
        """,
    )
    print(f"  {'table':<22}{'insert':>10}{'update':>10}{'soft del':>10}{'late':>8}{'delete':>8}")
    print(f"  {'-' * 22}{'-' * 9:>10}{'-' * 9:>10}{'-' * 9:>10}{'-' * 7:>8}{'-' * 7:>8}")
    totals = [0, 0, 0, 0, 0]
    for table, *values in rows:
        values = [int(value) for value in values]
        totals = [total + value for total, value in zip(totals, values, strict=True)]
        print(
            f"  {table:<22}"
            + "".join(f"{value:>10,}" for value in values[:3])
            + f"{values[3]:>8,}{values[4]:>8,}"
        )
    print(f"  {'-' * 22}{'-' * 9:>10}{'-' * 9:>10}{'-' * 9:>10}{'-' * 7:>8}{'-' * 7:>8}")
    print(
        f"  {'total':<22}"
        + "".join(f"{value:>10,}" for value in totals[:3])
        + f"{totals[3]:>8,}{totals[4]:>8,}"
    )
    print()
    print(
        f"  soft deletes span {sum(1 for row in rows if int(row[3]) > 0)} entity type(s); "
        f"criterion 8 asks for at least three"
    )


def _clock(cursor, anchor: dt.date) -> None:
    """Every row a tick wrote carries an `updated_at` inside that tick's own simulated day.

    That is the whole of criterion 3, and it catches the wall clock in both directions. The `ci`
    anchor is pinned in the past and sixty ticks run past today, so a row stamped with `now()`
    lands *before* its simulated day; on a profile anchored today it would land after. Neither
    is inside the window, so the window is the check.
    """
    _heading("Criterion 3: no row a tick wrote carries a wall-clock updated_at")
    rows = query(
        cursor,
        """
        select l.simulated_date, sum(c.rows_inserted + c.rows_updated) as logged
          from platform.tick_log l
          join platform.tick_table_counts c on c.tick_log_id = l.tick_log_id
         group by l.simulated_date order by l.simulated_date
        """,
    )
    if not rows:
        print("  no tick has run")
        return

    inside = 0
    for table in TABLES:
        found = query(
            cursor,
            f"""
            select count(*) from core.{table} t
             where t.updated_at >= %s
               and exists (select 1 from platform.tick_log l
                            where t.updated_at >= l.simulated_date::timestamptz
                              and t.updated_at < l.simulated_date::timestamptz
                                                 + interval '1 day')
            """,  # noqa: S608 - fixed table list
            (dt.datetime.combine(anchor + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC),),
        )
        inside += int(found[0][0])

    after_anchor = 0
    for table in TABLES:
        found = query(
            cursor,
            f"select count(*) from core.{table} where updated_at >= %s",  # noqa: S608
            (dt.datetime.combine(anchor + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC),),
        )
        after_anchor += int(found[0][0])

    now = dt.datetime.now(dt.UTC)
    print(f"  rows stamped after the anchor            {after_anchor:,}")
    print(f"  of those, inside some tick's own day     {inside:,}")
    print(f"  outside every tick window                {after_anchor - inside:,}  (expected 0)")
    print(f"  real clock when this ran                 {now.isoformat(timespec='seconds')}")
    print(f"  last tick's simulated day                {rows[-1][0]}")
    print("  every one of the sixty windows is a simulated day, so a row stamped with now()")
    print("  could not be inside any of them.")


def _late_arrivals(cursor, anchor: dt.date) -> None:
    _heading("Criterion 6: late-arrival business-timestamp lag")
    rows = query(
        cursor,
        """
        select (created_at::date - booked_at::date) as lag_days, count(*)
          from core.transactions
         where created_at::date > %s and created_at::date <> booked_at::date
         group by 1 order by 1
        """,
        (anchor,),
    )
    if not rows:
        print("  no late arrival was inserted")
        return
    total = sum(int(count) for _, count in rows)
    weighted = sum(int(lag) * int(count) for lag, count in rows)
    print(f"  {'lag (days)':<12}{'rows':>8}{'share':>9}")
    print(f"  {'-' * 12}{'-' * 7:>8}{'-' * 8:>9}")
    for lag, count in rows:
        print(f"  {int(lag):<12}{int(count):>8,}{int(count) / total:>9.3f}")
    print(f"  {'-' * 12}{'-' * 7:>8}{'-' * 8:>9}")
    print(f"  {'total':<12}{total:>8,}")
    print(f"  mean lag {weighted / total:.2f} days; stated two to five")


def _dispositions(cursor, config: RunConfig, anchor: dt.date) -> None:
    _heading("Criterion 7: fraud alert disposition lag, measured against stated")
    fraud = config.profile.params["fraud"]
    stated_min = int(fraud["disposition_lag_days_min"])
    stated_max = int(fraud["disposition_lag_days_max"])

    for label, predicate in (
        ("alerts raised by a tick", "f.alerted_at::date > %s"),
        ("alerts inherited from the history", "f.alerted_at::date <= %s"),
    ):
        rows = query(
            cursor,
            f"""
            select min(f.dispositioned_at::date - f.alerted_at::date),
                   round(avg(f.dispositioned_at::date - f.alerted_at::date), 2),
                   max(f.dispositioned_at::date - f.alerted_at::date),
                   count(*)
              from core.fraud_alerts f
              join ref.fraud_dispositions d on d.code = f.fraud_disposition_code
             where d.is_final and f.dispositioned_at is not null and {predicate}
            """,  # noqa: S608 - predicate is one of the two literals above
            (anchor,),
        )
        low, mean, high, count = rows[0]
        if not count:
            print(f"  {label}: none dispositioned")
            continue
        print(
            f"  {label}: n={count}, min {low}, mean {mean}, max {high} days "
            f"(stated {stated_min} to {stated_max})"
        )

    rows = query(
        cursor,
        """
        select count(*) filter (where d.is_confirmed_fraud), count(*)
          from core.fraud_alerts f
          join ref.fraud_dispositions d on d.code = f.fraud_disposition_code
         where d.is_final
        """,
    )
    confirmed, decided = int(rows[0][0]), int(rows[0][1])
    low, high = config.profile.band("confirmed_fraud_rate_among_alerts")
    if decided:
        print(
            f"  precision {confirmed / decided:.3f} over {decided} dispositioned alerts "
            f"(band {low} to {high})"
        )


def _deletes(cursor) -> None:
    _heading("Criterion 4 of the rulings: the physical delete and its reconciliation")
    rows = query(
        cursor,
        "select table_name, count(*) from platform.tick_deleted_keys group by 1 order by 1",
    )
    if not rows:
        print("  no row was physically deleted")
        return
    for table, count in rows:
        print(f"  {table}: {int(count)} key(s) recorded in platform.tick_deleted_keys")

    rows = query(
        cursor,
        """
        select count(*) from platform.tick_deleted_keys k
         where k.table_name = 'customers'
           and exists (select 1 from core.customers c where c.customer_id = k.deleted_key)
        """,
    )
    still_present = int(rows[0][0])
    print(f"  of those keys, {still_present} are still in core.customers (expected 0)")

    rows = query(
        cursor,
        """
        select count(*) from platform.tick_deleted_keys k
          join core.account_holders ah on ah.customer_id = k.deleted_key
         where k.table_name = 'customers'
        """,
    )
    print(f"  dependent rows left behind by a purge: {int(rows[0][0])} (expected 0)")
    print("  M7's primary-key reconciliation has this key set as its expected answer, so it")
    print("  can be validated in both directions rather than against a count.")


def _duplicates(cursor, anchor: dt.date) -> None:
    _heading("Criterion 11: duplicate customer records and their similarity")
    rows = query(
        cursor,
        """
        select a.customer_id, a.full_name, b.customer_id, b.full_name
          from core.customers a
          join core.customers b
            on b.date_of_birth = a.date_of_birth
           and b.residence_country_code = a.residence_country_code
           and b.customer_id > a.customer_id
         where a.created_at::date <= %s and b.created_at::date > %s
         order by a.customer_id, b.customer_id
        """,
        (anchor, anchor),
    )
    matched = [
        (left_id, left, right_id, right, similarity(left, right))
        for left_id, left, right_id, right in rows
        if similarity(left, right) >= SIMILARITY_FLOOR
    ]
    print(f"  candidate pairs sharing a date of birth and a country: {len(rows)}")
    print(
        f"  of those, {len(matched)} match on a trigram similarity of at least {SIMILARITY_FLOOR}"
    )
    if matched:
        scores = [score for *_, score in matched]
        print(
            f"  similarity: min {min(scores):.3f}, mean {sum(scores) / len(scores):.3f}, "
            f"max {max(scores):.3f}"
        )
        print("  examples:")
        for left_id, left, right_id, right, score in matched[:5]:
            print(f"    {score:.3f}  {left_id} {left!r}  vs  {right_id} {right!r}")
    print("  Jaccard over character trigrams, which is what pg_trgm.similarity computes.")
    print("  Neither pg_trgm nor fuzzystrmatch is installed: the criterion asks for a report,")
    print("  not a database capability, and similarity returns a real where design rule 1")
    print("  prohibits floating point in these schemas.")


def _drift(cursor) -> None:
    _heading("Criterion 10: scripted drift")
    rows = query(
        cursor,
        "select event_name, drift_type, target_table, target_column, simulated_date, "
        "tick_sequence from platform.drift_log order by drift_log_id",
    )
    if not rows:
        print("  no drift event fired")
        return
    for name, kind, table, column, date, sequence in rows:
        print(f"  {name}: {kind} on core.{table}.{column}, fired {date} at tick {sequence}")
    print("  `make schema-check` computes the expected schema as the dictionary plus these")
    print("  deltas; run it to confirm it is green.")


def replay(profile_name: str, seed: int, anchor: dt.date, ticks: int) -> int:
    """Acceptance criterion 4: the same inputs twice produce the same book."""
    digests = []
    for attempt in (1, 2):
        print(f"\nreplay {attempt} of 2: seeding {profile_name} and running {ticks} tick(s)")
        config = seed_profile(profile_name, seed, anchor)
        run_ticks(config, ticks)
        digests.append(manifest_module.build(config).tables)

    _heading("Criterion 4: replay determinism")
    differences = []
    for table in sorted(set(digests[0]) | set(digests[1])):
        first, second = digests[0].get(table), digests[1].get(table)
        if first is None or second is None or first.digest != second.digest:
            differences.append(table)
        print(
            f"  {table:<22}{first.rows if first else 0:>10,} rows  "
            f"{'identical' if first and second and first.digest == second.digest else 'DIFFERS'}"
        )
    if differences:
        print(f"\n  {len(differences)} table(s) differ between the two runs: {differences}")
        return 1
    print("\n  every table is byte-identical across the two runs")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tick-acceptance", description="Run and report specification 004's acceptance run."
    )
    parser.add_argument("--profile", default=os.environ.get("NORDBANK_ENV", "ci"))
    parser.add_argument("--ticks", type=int, default=60)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--anchor", default=os.environ.get("NORDBANK_ANCHOR_DATE", ""))
    parser.add_argument("--replay", action="store_true", help="run twice and compare digests")
    parser.add_argument("--no-seed", action="store_true", help="tick the book already loaded")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    anchor = dt.date.fromisoformat(args.anchor) if args.anchor else DEFAULT_ANCHOR

    if args.replay:
        return replay(args.profile, args.seed, anchor, args.ticks)

    print(
        f"tick-acceptance: profile {args.profile}, seed {args.seed}, anchor {anchor}, "
        f"{args.ticks} tick(s)"
    )
    if args.no_seed:
        config = RunConfig(profile=load_profile(args.profile), seed=args.seed, anchor=anchor)
    else:
        started = time.perf_counter()
        config = seed_profile(args.profile, args.seed, anchor)
        print(f"tick-acceptance: seeded in {time.perf_counter() - started:.1f} s")

    timings = run_ticks(config, args.ticks)
    report(config, timings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
