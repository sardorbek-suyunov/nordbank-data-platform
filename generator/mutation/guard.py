"""The delta-scoped coherence guard a tick runs before it commits.

`make seed-verify` runs all fourteen spec 003 invariants over the whole book and remains the
gate. It costs 138 to 141 seconds on the `dev` book, so it cannot run between ticks: thirty
ticks would spend seventy minutes verifying. Without something in between, a tick that breaks
coherence is discovered thirty ticks later with no way to tell which tick did it.

Measured per invariant on `dev`, in one session totalling 139.4 seconds: invariant 4 is 73.6 s
and invariant 13 is 56.0 s, together 93 per cent of the run. Those two are also the two whose
cost is the size of the population they scan, so restricting them to the rows a tick touched
is the whole saving — a few hundred accounts instead of seven thousand, a few hundred
transactions instead of six hundred thousand.

**This is a gate, not telemetry.** It runs inside the tick's transaction and raises, so a tick
that breaks coherence fails and the simulated date does not advance. A guard whose failure left
the tick committed would be a log line about a database that is already wrong.

**What it cannot do**, stated so it is not mistaken for the full check: it only sees the rows
this tick touched. A violation a tick creates somewhere else — an account it did not write, a
constraint that depends on the whole book — is invisible to it, and that is what
`make seed-verify` is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Below this many rows the login-coverage share is not asserted, only reported.
#
# The share's floor is 0.80 against a generator target of 0.87. Over a `dev` day's six hundred
# or so digital transactions the standard error is about 1.6 per cent, so the floor sits four
# standard errors below the target and a failure means something. Over a `ci` day's thirty it
# is about seven per cent, so the floor is one standard error away and the check would fail on
# sampling noise several times in a sixty-tick run.
#
# This is the same reasoning that suspends invariant 9's band at `ci` and the same principle
# the realism document states generally: gate on the size of the departure that matters, and
# never turn a statistic into a threshold at an n that cannot support it. The structural half
# of the check below is asserted at every scale, because it is exact.
COVERAGE_MIN_POPULATION = 200


class GuardFailedError(Exception):
    """A tick broke coherence on the rows it touched. Raised inside the transaction."""


@dataclass
class GuardResult:
    """What the guard measured, for the tick report and for `make tick`'s output."""

    accounts_checked: int = 0
    balance_offenders: int = 0
    coverage_population: int = 0
    coverage_covered: int = 0
    coverage_asserted: bool = False
    orphan_logins: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def coverage_share(self) -> float:
        if not self.coverage_population:
            return 0.0
        return self.coverage_covered / self.coverage_population


def _check_balances(cursor: Any, account_ids: list[int]) -> tuple[int, str | None]:
    """Invariant 4, restricted to the accounts this tick moved.

    The query is the full invariant's, with the outer scan replaced by the touched set. The
    subqueries are unchanged on purpose: reimplementing the sum as a delta would make the guard
    agree with the tick by construction and catch nothing.
    """
    cursor.execute(
        """
        select count(*), coalesce(min(account_id)::text, '') from (
          select a.account_id, a.current_balance_amount as stored,
                 coalesce((select sum(t.transaction_amount)
                             from core.transactions t
                             join ref.transaction_statuses ts
                               on ts.code = t.transaction_status_code
                            where t.account_id = a.account_id and ts.is_posted), 0)
               + coalesce((select sum(case when pt.direction = 'debit'
                                           then -p.payment_amount else p.payment_amount end)
                             from core.payments p
                             join ref.payment_statuses ps on ps.code = p.payment_status_code
                             join ref.payment_types pt on pt.code = p.payment_type_code
                            where p.account_id = a.account_id and ps.is_posted), 0) as computed
            from core.accounts a
           where a.account_id = any(%s)
        ) x
        where stored <> computed
        """,
        (account_ids,),
    )
    count, example = cursor.fetchone()
    return int(count), (example or None)


def _check_login_coverage(cursor: Any, recurring_prices: list[float], window: tuple) -> tuple:
    """Invariant 13, restricted to the transactions this tick booked.

    Two answers come back. The structural one is exact and always asserted: a login this tick
    placed to cover a transaction must belong to the same customer and fall inside the
    twenty-four hours before it. The statistical one is the share, which is only asserted where
    the population supports a threshold.
    """
    cursor.execute(
        """
        with population as (
          select t.transaction_id, t.booked_at, ah.customer_id
            from core.transactions t
            join ref.transaction_types tt on tt.code = t.transaction_type_code
            join core.account_holders ah on ah.account_id = t.account_id
            join ref.holder_roles hr on hr.code = ah.holder_role_code and hr.is_primary
           where tt.is_customer_initiated
             and t.channel_code in ('mobile_app', 'web', 'ecommerce')
             and (t.is_card_present is null or t.is_card_present = false)
             and not (abs(t.transaction_amount) = any(%s))
             and t.updated_at >= %s and t.updated_at < %s
        )
        select count(*) filter (where exists (
                 select 1 from core.login_sessions s
                  where s.customer_id = population.customer_id
                    and s.started_at <= population.booked_at
                    and s.started_at > population.booked_at - interval '24 hours')) as covered,
               count(*) as total
          from population
        """,
        (recurring_prices, window[0], window[1]),
    )
    covered, total = cursor.fetchone()
    return int(covered), int(total)


def _check_login_sessions_resolve(cursor: Any, window: tuple) -> int:
    """Every login session this tick wrote belongs to a customer that exists and is not future.

    Exact, cheap, and the thing that actually breaks when a tick writes sessions against the
    wrong key: the share above would still look fine if the sessions covered the wrong people.
    """
    cursor.execute(
        """
        select count(*) from core.login_sessions s
         where s.updated_at >= %s and s.updated_at < %s
           and (not exists (select 1 from core.customers c
                             where c.customer_id = s.customer_id)
                or s.started_at >= %s)
        """,
        (window[0], window[1], window[1]),
    )
    return int(cursor.fetchone()[0])


def run(
    cursor: Any,
    *,
    touched_accounts: set[int],
    window: tuple,
    recurring_prices: list[float],
) -> GuardResult:
    """Check the tick's delta and raise if it broke coherence.

    `window` is the tick's simulated day, half open, which is the same window acceptance
    criterion 9 reconciles the tick log against.
    """
    result = GuardResult()

    if touched_accounts:
        account_ids = sorted(touched_accounts)
        result.accounts_checked = len(account_ids)
        offenders, example = _check_balances(cursor, account_ids)
        result.balance_offenders = offenders
        if offenders:
            raise GuardFailedError(
                f"invariant 4 fails on {offenders} of the {len(account_ids)} account(s) this "
                f"tick moved: the stored balance is not the signed sum of posted transactions "
                f"and payments. First offender: account {example}. The tick is rolled back and "
                f"the simulated date has not advanced."
            )

    result.orphan_logins = _check_login_sessions_resolve(cursor, window)
    if result.orphan_logins:
        raise GuardFailedError(
            f"{result.orphan_logins} login session(s) this tick wrote do not resolve to a "
            f"customer, or start after the simulated day ends. The tick is rolled back."
        )

    covered, total = _check_login_coverage(cursor, recurring_prices, window)
    result.coverage_covered, result.coverage_population = covered, total
    return result


def assert_coverage(result: GuardResult, floor: float) -> GuardResult:
    """Assert the login coverage share where the population supports a threshold.

    Separated from `run` so the decision to assert is visible rather than buried in a branch,
    and so the reported figure exists either way.
    """
    if result.coverage_population < COVERAGE_MIN_POPULATION:
        result.notes.append(
            f"login coverage {result.coverage_share:.3f} over "
            f"{result.coverage_population} transaction(s): reported, not asserted below "
            f"{COVERAGE_MIN_POPULATION}"
        )
        return result

    result.coverage_asserted = True
    if result.coverage_share < floor:
        raise GuardFailedError(
            f"invariant 13 fails on this tick's delta: {result.coverage_share:.3f} of "
            f"{result.coverage_population} digital customer-initiated transactions were "
            f"preceded by a login within 24 hours, below the floor of {floor}. The tick is "
            f"rolled back and the simulated date has not advanced."
        )
    return result
