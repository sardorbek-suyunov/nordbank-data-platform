"""The reconciliation's arithmetic, which is the half that does not need a database.

What is worth pinning here is that a disagreement is loud and names the table. The control
exists so M4 can assert that bronze received exactly what the source says changed; a
reconciliation that returned a number nobody read would be worse than none, because an untested
control reads as a working one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from generator.mutation.reconcile import (
    ReconciliationError,
    TableReconciliation,
    TickReconciliation,
    assert_agrees,
    format_reconciliation,
)

DATE = dt.date(2026, 9, 19)


def result(*tables: tuple[str, int, int]) -> TickReconciliation:
    return TickReconciliation(
        tick_sequence=1,
        simulated_date=DATE,
        tables=[TableReconciliation(name, logged, in_window) for name, logged, in_window in tables],
    )


def test_a_tick_that_agrees_passes_through():
    reconciliation = result(("transactions", 241, 241), ("accounts", 237, 237))
    assert reconciliation.agrees
    assert assert_agrees(reconciliation) is reconciliation


def test_a_tick_with_nothing_in_it_agrees():
    assert result().agrees


def test_a_disagreement_raises_and_names_every_table_that_differs():
    reconciliation = result(
        ("transactions", 241, 241), ("accounts", 237, 230), ("payments", 80, 81)
    )
    assert not reconciliation.agrees
    with pytest.raises(ReconciliationError) as error:
        assert_agrees(reconciliation)
    message = str(error.value)
    assert "core.accounts: log says 237, window holds 230" in message
    assert "core.payments: log says 80, window holds 81" in message
    assert "core.transactions" not in message


def test_the_difference_is_signed_towards_the_window():
    # Positive means the window holds rows the log did not claim, which is a change class that
    # wrote without counting. Negative means the log counted rows that are not there.
    assert TableReconciliation("accounts", 10, 12).difference == 2
    assert TableReconciliation("accounts", 12, 10).difference == -2


def test_the_totals_sum_both_sides():
    reconciliation = result(("transactions", 241, 241), ("accounts", 237, 237))
    assert reconciliation.logged == 478
    assert reconciliation.in_window == 478


def test_the_report_names_the_tick_and_every_table():
    text = format_reconciliation(result(("transactions", 241, 241), ("accounts", 237, 230)))
    assert "tick 1 (2026-09-19)" in text
    assert "transactions" in text
    assert "accounts" in text
    assert "1 table(s) disagree" in text
