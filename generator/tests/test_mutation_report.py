"""The tick report's counting rules, spec 004 section 4.

The report is a reconciliation control, not telemetry: M4 asserts that bronze received exactly
what the source says it changed. So the thing worth testing is the arithmetic that makes two
of the five classes subsets rather than siblings, because adding all five would double count
and the mistake would be invisible until a reconciliation failed for a reason that was not a
reconciliation problem.
"""

from __future__ import annotations

import datetime as dt

import pytest

from generator.mutation.report import TickReport, format_report

DATE = dt.date(2026, 9, 19)


def report() -> TickReport:
    return TickReport(simulated_date=DATE, profile="dev", seed=42)


def test_counts_accumulate_per_table_and_class():
    r = report()
    r.record("transactions", "inserted", 5)
    r.record("transactions", "inserted", 3)
    r.record("customers", "updated")
    assert r.counts["transactions"]["inserted"] == 8
    assert r.total("inserted") == 8
    assert r.total("updated") == 1


def test_an_unknown_class_is_refused_rather_than_silently_counted():
    with pytest.raises(ValueError, match="unknown operation class"):
        report().record("transactions", "amended")


def test_recording_zero_creates_no_entry():
    # So a table nothing happened to does not appear in the log with five zeros.
    r = report()
    r.record("payments", "inserted", 0)
    assert r.counts == {}


def test_late_arrivals_and_soft_deletes_are_subsets_not_extra_rows():
    r = report()
    r.record("transactions", "inserted", 10)
    r.record("transactions", "late_arriving", 4)
    r.record("customers", "updated", 6)
    r.record("customers", "soft_deleted", 2)
    # Ten rows written and six changed. Counting all five classes would say twenty-two.
    assert r.rows_touched == 16


def test_a_physical_delete_records_its_key_for_the_reconciler():
    r = report()
    r.record_delete("customers", 4711)
    r.record_delete("customers", 4712)
    assert r.total("deleted") == 2
    assert r.deleted_keys == [("customers", 4711), ("customers", 4712)]
    assert r.rows_touched == 2


def test_duration_is_zero_until_the_tick_completes():
    r = report()
    assert r.duration_ms == 0
    r.started_at = dt.datetime(2026, 9, 20, 12, 0, 0, tzinfo=dt.UTC)
    assert r.duration_ms == 0
    r.completed_at = r.started_at + dt.timedelta(milliseconds=1500)
    assert r.duration_ms == 1500


def test_the_printed_report_names_every_table_and_a_fired_drift_event():
    r = report()
    r.tick_sequence = 7
    r.record("transactions", "inserted", 2120)
    r.record("customers", "updated", 12)
    r.drift_fired.append("merchants_risk_score_added")
    text = format_report(r)
    assert "tick 7: 2026-09-19" in text
    assert "transactions" in text and "2,120" in text
    assert "drift fired: merchants_risk_score_added" in text
