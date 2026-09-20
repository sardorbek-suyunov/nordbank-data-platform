"""Spec 004's acceptance criteria that need a live database and a running engine.

These run against the stack from the host, and they leave the source where they found it: the
module reseeds once, so a failure does not strand a half-ticked book for the next test.

Criterion 2 is the one that cannot be checked any other way. "A tick is one transaction" is a
claim about what happens when a tick fails, and the only way to know is to fail one.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

import pytest

from generator import pipeline, refdata, writer
from generator.config import RunConfig, load_profile
from generator.mutation import reconcile as reconcile_module
from generator.mutation import reset as reset_module
from generator.mutation import state as state_module
from generator.mutation import tick as tick_module
from generator.spool import Spool
from generator.tables import LOAD_ORDER

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from source_db_driver import connect  # noqa: E402

pytestmark = pytest.mark.integration

ANCHOR = dt.date(2026, 9, 18)
SEED = 42


@pytest.fixture(scope="module")
def profile():
    return load_profile("ci")


@pytest.fixture(scope="module")
def seeded(profile):
    """A freshly seeded `ci` book with the simulation pointed at the anchor."""
    config = RunConfig(profile=profile, seed=SEED, anchor=ANCHOR)
    ref = refdata.load()
    refdata.validate_against(ref, config.profile.params)
    root = pipeline.spool_root("tick-test")
    shutil.rmtree(root, ignore_errors=True)
    spool = Spool(root)
    pipeline.generate(config, ref, spool)
    reset_module.reset(profile="ci", seed=SEED, anchor=ANCHOR)
    writer.load(config, spool)
    shutil.rmtree(root, ignore_errors=True)
    return config


def counts(cursor) -> dict[str, int]:
    out = {}
    for table in LOAD_ORDER:
        cursor.execute(f"select count(*) from core.{table}")  # noqa: S608 - fixed table list
        out[table] = int(cursor.fetchone()[0])
    return out


def test_a_tick_advances_the_date_by_exactly_one_day(seeded, profile):
    with connect() as connection:
        with connection.cursor() as cursor:
            before = state_module.read(cursor)
        connection.rollback()

        report = tick_module.run(connection, requested_date=None, profile=profile)

        with connection.cursor() as cursor:
            after = state_module.read(cursor)
        connection.rollback()

    assert report.simulated_date == before.simulated_date + dt.timedelta(days=1)
    assert after.simulated_date == report.simulated_date
    assert after.tick_sequence == before.tick_sequence + 1


def test_a_tick_reconciles_against_its_own_window(seeded, profile):
    with connect() as connection:
        tick_module.run(connection, requested_date=None, profile=profile)
        with connection.cursor() as cursor:
            result = reconcile_module.reconcile_latest(cursor)
        connection.rollback()

    assert result is not None
    assert result.agrees, reconcile_module.format_reconciliation(result)
    assert result.logged > 0, "a tick that changed nothing proves nothing"


@pytest.mark.parametrize("phase", ["lifecycle", "acquisition", "movements", "dirt"])
def test_an_induced_failure_leaves_the_source_untouched(seeded, profile, phase):
    """Acceptance criterion 2, for a failure in each half of the tick.

    The hook has no command line flag and no environment variable behind it, so it cannot be
    reached from an operator's shell by accident. Failing after `movements` is the interesting
    case: by then the tick has copied thousands of rows and folded hundreds of balances.
    """
    with connect() as connection:
        with connection.cursor() as cursor:
            before_state = state_module.read(cursor)
            before_counts = counts(cursor)
        connection.rollback()

        with pytest.raises(tick_module.InducedFailureError):
            tick_module.run(connection, requested_date=None, profile=profile, fail_after=phase)
        connection.rollback()

        with connection.cursor() as cursor:
            after_state = state_module.read(cursor)
            after_counts = counts(cursor)
        connection.rollback()

    assert after_state == before_state, "the simulated date moved"
    assert after_counts == before_counts, "rows survived a failed tick"


def test_the_failure_hook_refuses_a_phase_that_does_not_exist(seeded, profile):
    with connect() as connection, pytest.raises(ValueError, match="unknown phase"):
        tick_module.run(connection, requested_date=None, profile=profile, fail_after="nonsense")


def test_no_row_a_tick_writes_carries_the_wall_clock(seeded, profile):
    """Acceptance criterion 3, for one tick. The sixty-tick distribution is reported separately.

    The tick's own simulated day is in the future of the real clock at the `ci` anchor, so a row
    stamped with `now()` is not merely in the wrong place — it is on the wrong side of today.
    """
    with connect() as connection:
        report = tick_module.run(connection, requested_date=None, profile=profile)
        start, end = (
            dt.datetime.combine(report.simulated_date, dt.time(), tzinfo=dt.UTC),
            dt.datetime.combine(
                report.simulated_date + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC
            ),
        )
        with connection.cursor() as cursor:
            outside = 0
            for table in LOAD_ORDER:
                cursor.execute(
                    f"select count(*) from core.{table} "  # noqa: S608 - fixed table list
                    f"where updated_at >= %s and (updated_at < %s or updated_at >= %s)",
                    (start, start, end),
                )
                outside += int(cursor.fetchone()[0])
        connection.rollback()

    assert outside == 0, f"{outside} row(s) carry an updated_at outside the tick's own day"


def test_the_platform_tables_carry_real_time_while_core_carries_simulated(seeded, profile):
    """The standing ruling: `core` and `ref` carry the simulated clock, `platform` real time."""
    with connect() as connection:
        report = tick_module.run(connection, requested_date=None, profile=profile)
        with connection.cursor() as cursor:
            cursor.execute(
                "select started_at, completed_at, created_at from platform.tick_log "
                "where simulated_date = %s",
                (report.simulated_date,),
            )
            started_at, completed_at, created_at = cursor.fetchone()
        connection.rollback()

    simulated_day = report.simulated_date
    for stamp in (started_at, completed_at, created_at):
        assert stamp.date() != simulated_day, (
            "a platform row carries the simulated date; the clock was not cleared before it"
        )
    assert completed_at >= started_at
