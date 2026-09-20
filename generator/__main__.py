"""Command line entry point.

    python -m generator --profile dev --seed 42 --anchor 2026-09-18
    python -m generator --verify
    python -m generator --manifest [--check]

Each of the three inputs falls back to its environment variable, and the environment falls back
to a default. `NORDBANK_ANCHOR_DATE` defaults to the real current date, so local data always
looks current; the `ci` profile pins it explicitly, because a committed manifest whose anchor
moved every midnight would fail the determinism check every day for no reason.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
import time

from . import GENERATOR_VERSION
from .config import ConfigError, load_profile
from .config import RunConfig as _RunConfig
from .invariants import format_report
from .invariants import run as run_invariants
from .manifest import build as build_manifest
from .manifest import compare as compare_manifest
from .manifest import format_manifest
from .manifest import write as write_manifest
from .mutation.reset import reset as reset_simulation
from .pipeline import generate, spool_root
from .refdata import RefDataError, validate_against
from .refdata import load as load_refdata
from .spool import Spool
from .writer import assert_sequences_ahead
from .writer import load as load_into_database

DEFAULT_SEED = 42


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m generator",
        description="Generate and load the Nordbank synthetic source data.",
    )
    parser.add_argument("--profile", default=os.environ.get("NORDBANK_ENV", "dev"))
    parser.add_argument(
        "--seed", type=int, default=int(os.environ.get("NORDBANK_SEED", DEFAULT_SEED))
    )
    parser.add_argument("--anchor", default=os.environ.get("NORDBANK_ANCHOR_DATE", ""))
    parser.add_argument(
        "--verify",
        action="store_true",
        help="run the invariants against the loaded database and exit",
    )
    parser.add_argument("--manifest", action="store_true", help="write the run manifest and exit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="with --manifest, compare against the committed one and fail on a difference",
    )
    parser.add_argument(
        "--keep-spool",
        action="store_true",
        help="leave the generated CSV files in place after loading",
    )
    return parser.parse_args(argv)


def _config(args: argparse.Namespace) -> _RunConfig:
    profile = load_profile(args.profile)
    anchor = dt.date.fromisoformat(args.anchor) if args.anchor else dt.date.today()
    if anchor > dt.date.today():
        # core.customers carries `check (date_of_birth < current_date)`, which is evaluated
        # against the real clock at insert time, so an anchor in the future is not loadable.
        raise SystemExit(
            f"generator: anchor {anchor} is in the future; the source schema validates dates of "
            f"birth against the real current date, so history has to end today or earlier"
        )
    return _RunConfig(profile=profile, seed=args.seed, anchor=anchor)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        config = _config(args)
    except ConfigError as error:
        print(f"generator: {error}", file=sys.stderr)
        return 1

    if args.verify:
        report = run_invariants(config)
        print(format_report(report, config))
        return 0 if report.passed else 1

    if args.manifest:
        manifest = build_manifest(config)
        if args.check:
            differences = compare_manifest(manifest)
            if differences:
                print(
                    "seed-manifest: the committed manifest and a fresh one disagree",
                    file=sys.stderr,
                )
                for difference in differences:
                    print(f"  {difference}", file=sys.stderr)
                print(
                    "\nIf the change to the generator was intended, regenerate the manifest with "
                    "`make seed-manifest` and commit it with the change that caused it.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"seed-manifest: the committed {config.profile.name} manifest matches a fresh "
                f"regeneration ({manifest.total_rows:,} rows across "
                f"{len(manifest.tables)} tables)"
            )
            return 0
        target = write_manifest(manifest)
        print(format_manifest(manifest))
        relative = target.relative_to(target.parent.parent.parent).as_posix()
        print(f"\nseed-manifest: wrote {relative}")
        return 0

    print(
        f"seed: profile {config.profile.name}, seed {config.seed}, anchor {config.anchor}, "
        f"generator {GENERATOR_VERSION}"
    )
    print(
        f"seed: history {config.history_start} to {config.anchor}, "
        f"{config.profile.history_months} months, {config.profile.customers:,} customers"
    )

    try:
        ref = load_refdata()
        validate_against(ref, config.profile.params)
    except RefDataError as error:
        print(f"seed: {error}", file=sys.stderr)
        return 1

    root = spool_root(config.profile.name)
    shutil.rmtree(root, ignore_errors=True)
    started = time.perf_counter()
    spool = Spool(root)
    result = generate(config, ref, spool)
    print(f"seed: generated {sum(result.counts.values()):,} rows in {result.seconds:.2f} s")

    # The simulation is reset before the new book is loaded, not after: reverting a drift event
    # drops a column, and doing that to a table that has just been filled would be a schema
    # change on live data for no reason. Spec 004 section 7, as amended.
    reset_report = reset_simulation(
        profile=config.profile.name, seed=config.seed, anchor=config.anchor
    )

    report = load_into_database(config, spool)
    faults = assert_sequences_ahead()
    if faults:
        print("seed: identity sequences were not synchronised:", file=sys.stderr)
        for fault in faults:
            print(f"  {fault}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - started
    print()
    print(f"  {'table':<22} {'rows':>12}")
    print(f"  {'-' * 22} {'-' * 12}")
    for table, count in sorted(report.counts.items()):
        print(f"  {table:<22} {count:>12,}")
    print(f"  {'-' * 22} {'-' * 12}")
    print(f"  {'total':<22} {report.rows:>12,}")
    print()
    if report.foreign_keys_repaired:
        print(
            f"seed: repaired {report.foreign_keys_repaired} foreign key(s) a previous load "
            f"left dropped"
        )
    print(
        f"seed: loaded in {report.total_seconds:.2f} s "
        f"({report.ledger_chunks} ledger chunk(s), "
        f"{report.foreign_keys_restored} foreign keys revalidated)"
    )
    if reset_report.reverted:
        print(
            f"seed: reverted {len(reset_report.reverted)} fired drift event(s): "
            f"{', '.join(reset_report.reverted)}"
        )
    print(
        f"seed: simulation reset to {reset_report.anchor}"
        + (f", {reset_report.ticks_cleared} tick(s) cleared" if reset_report.ticks_cleared else "")
    )
    print(f"seed: {elapsed:.2f} s total")

    if not args.keep_spool:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"seed: spool kept at {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
