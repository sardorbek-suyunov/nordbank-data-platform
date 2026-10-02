"""The backfill lands the FX history once, before the first day, from the source's own book.

The range comes from the source and the anchor, never from a constant; the history runs before
any day is ticked or ingested; a history already registered is a no-op; and a failed one halts.
"""

from __future__ import annotations

import datetime as dt

import backfill
import pytest

ANCHOR = dt.date(2026, 7, 20)
EARLIEST = dt.date(2024, 1, 24)

RANGES = [
    # (name, earliest business day, expected range)
    ("the ci book", EARLIEST, (dt.date(2024, 1, 23), dt.date(2026, 7, 19))),
    ("a book that starts the day before the anchor", dt.date(2026, 7, 19),
     (dt.date(2026, 7, 18), dt.date(2026, 7, 19))),
    ("a book that starts on the anchor", ANCHOR, (dt.date(2026, 7, 19), dt.date(2026, 7, 19))),
    ("a book that starts after the anchor", dt.date(2026, 7, 21), None),
]  # fmt: skip


def test_the_range_cases_cover_a_book_before_at_and_after_the_anchor() -> None:
    assert len(RANGES) >= 4


@pytest.mark.parametrize(
    ("earliest", "expected"), [c[1:] for c in RANGES], ids=[c[0] for c in RANGES]
)
def test_the_history_runs_from_the_day_before_the_book_to_the_day_before_the_anchor(
    earliest, expected
) -> None:
    assert backfill.history_range(earliest, ANCHOR) == expected


def test_the_earliest_business_day_is_read_from_both_tables_of_the_source(monkeypatch) -> None:
    asked: list[str] = []

    def executor():
        def run(sql):
            asked.append(sql)
            return [("2024-01-24 09:25:00+00",)]

        return run

    monkeypatch.setattr(backfill.db, "executor", executor)
    assert backfill.earliest_business_day() == EARLIEST
    assert len(asked) == 1
    assert "min(booked_at) from core.transactions" in asked[0]
    assert "min(initiated_at) from core.payments" in asked[0]


class Loop:
    """The calls the history makes, recorded."""

    def __init__(self, registered: bool, final: str = "success", batches=None) -> None:
        self.registered = registered
        self.final = final
        self.batches = batches if batches is not None else [
            {"entity": "fx_rates", "interval_start": "2026-07-19T00:00:00+00:00",
             "status": "registered", "failure_reason": None, "delivery": None}
        ]  # fmt: skip
        self.started: list[tuple] = []

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(backfill, "earliest_business_day", lambda: EARLIEST)
        monkeypatch.setattr(backfill, "history_registered", lambda day: self.registered)
        monkeypatch.setattr(backfill, "start_run", self.start_run)
        monkeypatch.setattr(backfill, "wait_for", lambda dag, run_id: self.final)
        monkeypatch.setattr(backfill, "feed_batches", lambda run_id: self.batches)

    def start_run(self, dag, day, conf=None):
        self.started.append((dag, day, conf))
        return f"{dag}__{day}"


def test_the_history_is_one_run_with_its_range_in_the_conf(monkeypatch) -> None:
    loop = Loop(registered=False)
    loop.install(monkeypatch)
    assert backfill.load_fx_history(ANCHOR) == 0
    assert len(loop.started) >= 1
    assert loop.started == [
        (
            backfill.FX_DAG,
            dt.date(2026, 7, 19),
            {"history": {"from": "2024-01-23", "to": "2026-07-19"}},
        )
    ]


def test_a_registered_history_is_not_run_again(monkeypatch) -> None:
    loop = Loop(registered=True)
    loop.install(monkeypatch)
    assert backfill.load_fx_history(ANCHOR) == 0
    assert loop.started == []


def test_a_failed_history_halts_the_backfill(monkeypatch) -> None:
    failed = [
        {"entity": "fx_rates", "interval_start": "2026-07-19T00:00:00+00:00", "status": "failed",
         "failure_reason": "the history request 2024-01-23..2026-07-19 answered HTTP 521",
         "delivery": None}
    ]  # fmt: skip
    loop = Loop(registered=False, final="failed", batches=failed)
    loop.install(monkeypatch)
    assert backfill.load_fx_history(ANCHOR) == 1


def test_the_history_runs_before_any_day_and_a_halt_stops_the_days(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(backfill, "_API", None)
    monkeypatch.setattr(backfill.db, "require_stack", lambda: None)
    monkeypatch.setattr(backfill, "simulation_state", lambda: (ANCHOR, ANCHOR, 0))
    monkeypatch.setattr(backfill, "hold_transforms", lambda: True)
    monkeypatch.setattr(backfill, "release_transforms", lambda prior: order.append("release"))
    monkeypatch.setattr(backfill, "load_fx_history", lambda anchor: order.append("history") or 0)
    monkeypatch.setattr(backfill, "ingest_days", lambda *a: order.append("days") or 0)
    assert backfill.main(["--from", "2026-07-20", "--to", "2026-07-21"]) == 0
    assert order == ["history", "days", "release"]

    order.clear()
    monkeypatch.setattr(backfill, "load_fx_history", lambda anchor: order.append("history") or 1)
    assert backfill.main(["--from", "2026-07-20", "--to", "2026-07-21"]) == 1
    assert order == ["history", "release"]
