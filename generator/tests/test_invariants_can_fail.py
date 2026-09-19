"""Spec 003 acceptance criterion 6: every invariant is proven capable of failing.

A check that cannot fail is worse than no check, because it reports a pass. Each test here
breaks the data in the specific way its invariant exists to catch, runs that invariant's own
query against the broken state, and asserts it reports the breakage.

**Nothing is committed.** The breakage and the check run inside one transaction that ends in
`rollback`, so the database is never left holding a deliberately broken row even if a test
fails partway through. That is why these reuse the query text from `generator.invariants`
rather than calling `invariants.run()`, which opens a session of its own and could not see an
uncommitted change.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

from generator.config import RunConfig, load_profile
from generator.invariants import _queries

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

pytestmark = pytest.mark.integration

ANCHOR = dt.date(2026, 9, 18)


@pytest.fixture(scope="module")
def config() -> RunConfig:
    return RunConfig(profile=load_profile("ci"), seed=42, anchor=ANCHOR)


@pytest.fixture(scope="module")
def query_text(config: RunConfig) -> dict[str, str]:
    return dict(_queries(config))


def run_broken(query: str, breakage: str) -> list[tuple[str, ...]]:
    """Apply `breakage`, run `query`, and roll back. The rollback is unconditional."""
    import source_db_exec as db

    script = (
        "begin;\n"
        + breakage.strip().rstrip(";")
        + ";\n"
        + "\\echo @@result@@\n"
        + query.strip().rstrip(";")
        + ";\n"
        + "rollback;\n"
    )
    completed = db._run(
        db._psql_command(False, ["-A", "-t", "-F", db.FIELD_SEPARATOR]), stdin=script
    )
    if completed.returncode != 0:
        raise AssertionError(
            "the breakage could not be applied:\n"
            + (completed.stdout + completed.stderr).strip()[:1200]
        )
    rows, seen = [], False
    for line in completed.stdout.splitlines():
        if line.startswith("@@result@@"):
            seen = True
            continue
        if seen and line.strip():
            rows.append(tuple(line.split(db.FIELD_SEPARATOR)))
    return rows


def offenders(rows: list[tuple[str, ...]]) -> int:
    return int(rows[0][0]) if rows else 0


def test_1_catches_a_transaction_after_the_account_closed(query_text):
    rows = run_broken(
        query_text["1"],
        """
        update core.accounts set closed_date = opened_date + 1
         where account_id = (select account_id from core.transactions
                              order by transaction_id limit 1)
        """,
    )
    assert offenders(rows) > 0


def test_2_catches_a_transaction_after_the_card_expired(query_text):
    rows = run_broken(
        query_text["2"],
        """
        update core.cards set expiry_date = issued_date + 1
         where card_id = (select card_id from core.transactions
                           where card_id is not null order by transaction_id limit 1)
        """,
    )
    assert offenders(rows) > 0


def test_3_catches_ownership_weights_that_do_not_sum_to_one(query_text):
    rows = run_broken(
        query_text["3"],
        """
        update core.account_holders set ownership_weight = 0.5
         where account_holder_id = (select min(account_holder_id) from core.account_holders
                                     where ownership_weight = 1)
        """,
    )
    assert offenders(rows) > 0


def test_3_catches_an_account_with_no_primary_holder(query_text):
    rows = run_broken(
        query_text["3"],
        """
        update core.account_holders set holder_role_code = 'joint', ownership_weight = 1
         where holder_role_code = 'primary'
           and account_holder_id = (select min(account_holder_id) from core.account_holders
                                     where holder_role_code = 'primary')
        """,
    )
    assert offenders(rows) > 0


def test_4_catches_a_balance_that_does_not_reconcile(query_text):
    rows = run_broken(
        query_text["4"],
        """
        update core.accounts
           set current_balance_amount = current_balance_amount + 0.01
         where account_id = (select min(account_id) from core.accounts)
        """,
    )
    assert offenders(rows) > 0


def test_5_catches_a_day_whose_debits_and_credits_differ(query_text):
    rows = run_broken(
        query_text["5"],
        """
        set constraints all deferred;
        update core.gl_entries set amount = amount + 1
         where gl_entry_id = (select min(gl_entry_id) from core.gl_entries where amount > 0)
        """,
    )
    assert offenders(rows) > 0


def test_6_catches_a_posted_movement_with_no_ledger_entry(query_text):
    rows = run_broken(
        query_text["6"],
        """
        update core.gl_transactions set source_entity_id = -source_entity_id
         where source_entity_code = 'transaction'
           and gl_transaction_id = (select min(gl_transaction_id) from core.gl_transactions
                                     where source_entity_code = 'transaction')
        """,
    )
    assert offenders(rows) > 0


def test_7_catches_an_overpaid_installment(query_text):
    rows = run_broken(
        query_text["7"],
        """
        update core.loan_installments set paid_amount = due_amount + 1
         where loan_installment_id = (select min(loan_installment_id)
                                        from core.loan_installments)
        """,
    )
    assert offenders(rows) > 0


def test_7_catches_a_schedule_that_does_not_match_the_term(query_text):
    rows = run_broken(
        query_text["7"],
        "update core.loans set term_months = term_months + 1 "
        "where loan_id = (select min(loan_id) from core.loans)",
    )
    assert offenders(rows) > 0


def test_8_catches_a_loan_above_its_approved_amount(query_text):
    rows = run_broken(
        query_text["8"],
        """
        update core.loans set principal_amount = principal_amount * 2
         where loan_id = (select min(loan_id) from core.loans)
        """,
    )
    assert offenders(rows) > 0


def test_9_catches_an_alert_on_a_transaction_with_no_card(query_text):
    rows = run_broken(
        query_text["9a"],
        """
        update core.transactions set card_id = null, is_card_present = null
         where transaction_id = (select transaction_id from core.fraud_alerts
                                  order by fraud_alert_id limit 1)
        """,
    )
    assert offenders(rows) > 0


def test_9_catches_an_alert_that_fired_before_its_transaction(query_text):
    rows = run_broken(
        query_text["9a"],
        """
        update core.fraud_alerts set alerted_at = alerted_at - interval '30 days'
         where fraud_alert_id = (select min(fraud_alert_id) from core.fraud_alerts)
        """,
    )
    assert offenders(rows) > 0


def test_10_catches_a_customer_under_eighteen_at_opening(query_text):
    rows = run_broken(
        query_text["10"],
        """
        update core.customers set date_of_birth = current_date - interval '17 years'
         where customer_id = (select min(customer_id) from core.customers)
        """,
    )
    assert offenders(rows) > 0


def test_11_catches_updated_at_before_created_at(query_text):
    rows = run_broken(
        query_text["11"],
        """
        update core.merchants set created_at = updated_at + interval '1 day'
         where merchant_id = (select min(merchant_id) from core.merchants)
        """,
    )
    assert offenders(rows) > 0


def test_11_catches_a_row_stamped_after_the_anchor(query_text):
    rows = run_broken(
        query_text["11"],
        f"""
        update core.merchants
           set created_at = timestamptz '{ANCHOR.isoformat()} 00:00:00+00' + interval '2 days',
               updated_at = timestamptz '{ANCHOR.isoformat()} 00:00:00+00' + interval '2 days'
         where merchant_id = (select min(merchant_id) from core.merchants)
        """,
    )
    assert offenders(rows) > 0


def test_12_catches_a_foreign_key_that_was_dropped(query_text):
    """The failure this invariant was specified to catch: a load that left the constraints off.

    The catalogue assertion notices it even though no orphan exists yet, which is the reason it
    is a catalogue assertion rather than a sampled anti-join.
    """
    rows = run_broken(
        query_text["12b"],
        "alter table core.transactions drop constraint transactions_account_id_fkey",
    )
    before = 59
    assert 0 < offenders(rows) < before


def test_12_catches_an_orphan_once_the_constraint_is_off(query_text):
    rows = run_broken(
        query_text["12c"],
        """
        alter table core.transactions drop constraint transactions_account_id_fkey;
        update core.transactions set account_id = 999999999
         where transaction_id = (select min(transaction_id) from core.transactions)
        """,
    )
    assert offenders(rows) > 0


def test_13_catches_login_coverage_falling_away(query_text):
    """Deleting the sessions drops the covered share to zero, which is below any floor."""
    rows = run_broken(query_text["13"], "delete from core.login_sessions")
    covered = int(rows[0][0]) if rows else 0
    total = int(rows[0][1]) if rows else 0
    assert total > 0
    assert covered == 0


def test_14_catches_a_first_digit_distribution_that_is_not_benford(query_text):
    """Force every card purchase to start with a nine and the deviation must blow past its limit."""
    from generator.realism.distributions import benford_mad

    rows = run_broken(
        query_text["14"],
        "update core.transactions set transaction_amount = -9000.00 "
        "where transaction_type_code = 'card_purchase'",
    )
    counts = [0] * 9
    for row in rows:
        digit = row[0].strip()
        if digit.isdigit() and digit != "0":
            counts[int(digit) - 1] = int(row[1])
    assert sum(counts) > 0
    assert benford_mad(counts) > 0.006


def test_the_database_is_unchanged_after_every_breakage(config):
    """Every test above rolls back, so the invariants still pass when they are done."""
    from generator.invariants import run

    report = run(config)
    assert report.passed, [result.detail for result in report.failures]
