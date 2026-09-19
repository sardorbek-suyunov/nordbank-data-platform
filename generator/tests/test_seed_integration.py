"""Spec 003 section 10: load the ci profile, run every invariant, compare the manifest.

These run against the live stack from the host, which is where the Docker socket is. They are
the end-to-end form of acceptance criteria 1, 2, 4, 5 and 13: the other tests check the parts,
these check that the parts together put the right thing in the database.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

import pytest

from generator import invariants, manifest, pipeline, refdata, writer
from generator.config import RunConfig, load_profile
from generator.spool import Spool
from generator.tables import LOAD_ORDER

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

pytestmark = pytest.mark.integration

# Pinned, not defaulted to today. The committed manifest is compared against a regeneration, and
# an anchor that moved every midnight would fail that comparison daily for no reason.
ANCHOR = dt.date(2026, 9, 18)
SEED = 42


@pytest.fixture(scope="module")
def config() -> RunConfig:
    return RunConfig(profile=load_profile("ci"), seed=SEED, anchor=ANCHOR)


@pytest.fixture(scope="module")
def loaded(config: RunConfig):
    """Generate and load the ci profile once for the module."""
    ref = refdata.load()
    refdata.validate_against(ref, config.profile.params)
    root = pipeline.spool_root("ci-test")
    shutil.rmtree(root, ignore_errors=True)
    spool = Spool(root)
    result = pipeline.generate(config, ref, spool)
    report = writer.load(config, spool)
    shutil.rmtree(root, ignore_errors=True)
    return result, report


def test_all_sixteen_core_tables_are_populated(loaded):
    _, report = loaded
    assert set(report.counts) == set(LOAD_ORDER)
    empty = [table for table, count in report.counts.items() if count == 0]
    assert empty == [], f"tables left empty: {empty}"


def test_row_counts_are_inside_the_profile_band(loaded, config):
    """ci is specified as about 500 customers and tens of thousands of transactions."""
    _, report = loaded
    assert 450 <= report.counts["customers"] <= 550
    assert 10_000 <= report.counts["transactions"] < 1_000_000


def test_the_identity_sequences_were_synchronised(loaded):
    """COPY does not advance them, and a miss is invisible until M3's first insert."""
    assert writer.assert_sequences_ahead() == []


def test_every_invariant_passes(loaded, config):
    report = invariants.run(config)
    failures = [f"{r.number}: {r.detail}" for r in report.failures]
    assert failures == [], failures
    assert len(report.results) == 14


def test_invariant_nine_is_suspended_rather_than_asserted_at_ci(loaded, config):
    """Acceptance criterion 5 as amended: the suspension is reported, not silently passed."""
    report = invariants.run(config)
    ninth = next(result for result in report.results if result.number == 9)
    assert ninth.status == invariants.NOT_ASSERTED
    assert "interval half-width" in ninth.detail


def test_no_row_carries_the_load_timestamp(loaded, config):
    """Acceptance criterion 4.

    A historical row stamped with the wall-clock instant of the load would deliver five years of
    history to M4 inside one watermark window.
    """
    import source_db_exec as db

    rows = db.executor()(
        """
        select count(*) from core.transactions
         where updated_at > now() - interval '2 hours'
        """
    )
    assert int(rows[0][0]) == 0


def test_updated_at_spreads_across_the_history(loaded, config):
    """The distribution acceptance criterion 4 asks to be shown, asserted rather than eyeballed."""
    import source_db_exec as db

    rows = db.executor()(
        """
        select count(distinct date_trunc('month', updated_at)) as months,
               min(updated_at)::date, max(updated_at)::date
          from core.transactions
        """
    )
    months = int(rows[0][0])
    assert months >= config.profile.history_months, (
        f"updated_at covers {months} months of a {config.profile.history_months} month history"
    )


def test_the_committed_manifest_matches_a_fresh_regeneration(loaded, config):
    """Acceptance criterion 13, and the reason the manifest is committed at all."""
    fresh = manifest.build(config)
    differences = manifest.compare(fresh)
    assert differences == [], differences


def test_two_loads_of_the_same_inputs_agree(config):
    """Acceptance criterion 2, in the form a test can run repeatedly."""
    ref = refdata.load()

    def load_once() -> dict[str, str]:
        root = pipeline.spool_root("ci-determinism")
        shutil.rmtree(root, ignore_errors=True)
        spool = Spool(root)
        pipeline.generate(config, ref, spool)
        writer.load(config, spool)
        shutil.rmtree(root, ignore_errors=True)
        built = manifest.build(config)
        return {name: f"{d.rows}:{d.digest}" for name, d in built.tables.items()}

    assert load_once() == load_once()


def test_a_different_seed_produces_different_data(config):
    """Acceptance criterion 3, first limb."""
    ref = refdata.load()

    def load_with(seed: int) -> dict[str, str]:
        other = RunConfig(profile=config.profile, seed=seed, anchor=config.anchor)
        root = pipeline.spool_root("ci-seed")
        shutil.rmtree(root, ignore_errors=True)
        spool = Spool(root)
        pipeline.generate(other, ref, spool)
        writer.load(other, spool)
        shutil.rmtree(root, ignore_errors=True)
        built = manifest.build(other)
        return {name: d.digest for name, d in built.tables.items()}

    first = load_with(SEED)
    second = load_with(SEED + 1)
    changed = [table for table in first if first[table] != second[table]]
    assert len(changed) >= 12, f"only {len(changed)} tables changed with the seed"

    # Leave the database holding the committed profile.
    load_with(SEED)
