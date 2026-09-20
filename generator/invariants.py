"""The coherence checks, runnable against a loaded database independently of a load.

Fourteen invariants from spec 003 section 5, as amended. Every one is a set-based query that
runs inside the database: nothing is pulled to the host and nothing is sampled. That is what
makes them exact at the full profile — the cost is I/O, not accuracy — and sampling would trade
away certainty to solve a problem that does not arise.

Three of them are not what the specification first said, and each change is recorded in its
amendment:

- **Invariant 9's statistical band is suspended at `ci`.** A few dozen fraudulent transactions
  cannot support a precision band, and a band wide enough to be honest at that n asserts
  nothing. At `ci` the check asserts structural properties and reports the n and the interval
  width that justify the suspension.
- **Invariant 12 asserts from the catalogue** that every foreign key exists and is validated,
  and anti-joins only the large tables. Its stated purpose is to catch a load that left the
  constraints off, and a catalogue check cannot miss that while a sampled anti-join can.
- **Invariant 14 asserts Benford on card purchases only**, excluding cash withdrawals and
  recurring prices, and measures conformity by mean absolute deviation rather than by a
  chi-square critical value. Chi-square tests significance rather than effect size and its
  power grows with the sample, so a fixed critical value is stricter at a larger profile.

Every check is run in one psql session. Sixteen round trips to a database inside a container
cost more than the ci load itself.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import RunConfig
from .realism.distributions import BENFORD, benford_chi_square, benford_mad
from .tables import LOAD_ORDER

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import source_db_exec as db  # noqa: E402

MARKER = "@@check@@"

PASS = "pass"
FAIL = "fail"
NOT_ASSERTED = "not asserted"


@dataclass
class CheckResult:
    number: int
    name: str
    status: str
    count: int = 0
    example: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != FAIL


@dataclass
class InvariantReport:
    results: list[CheckResult] = field(default_factory=list)
    measurements: dict[str, float] = field(default_factory=dict)
    benford_digits: list[int] = field(default_factory=list)
    composite_digits: list[int] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [result for result in self.results if result.status == FAIL]

    @property
    def passed(self) -> bool:
        return not self.failures


def _recurring_prices(config: RunConfig) -> str:
    """A SQL list of the recurring subscription prices, for the two checks that exclude them.

    Identified by price rather than by a column, because the source has no `is_recurring` flag
    and inventing one would put a generator detail into the bank's schema. The rule excludes a
    few genuinely one-off purchases that happen to cost 9.99, which is conservative in the
    direction that matters: it removes amounts from the Benford population rather than adding
    them.
    """
    prices = config.profile.params["amounts"]["recurring_amount_choices"]
    return ", ".join(f"{float(price):.4f}" for price in prices)


def _queries(config: RunConfig) -> list[tuple[str, str]]:
    anchor = config.anchor.isoformat()
    recurring = _recurring_prices(config)
    core_tables = LOAD_ORDER

    # One row per core table, counting rows whose audit timestamps are out of order or fall
    # after the simulated present. The bound is exclusive of the following day, so a row stamped
    # at 23:59:59 on that date is inside the history and one stamped a second later is not.
    #
    # **The bound is the simulated date, not the load anchor** (spec 003, amended 2026-09-20).
    # A tick advances the source past its anchor one simulated day at a time, so every row a
    # tick writes is after the anchor by construction and the anchor bound is false the moment
    # the first tick commits. The property is unchanged — no row carries a timestamp from the
    # future of the simulation, and none carries the real wall clock — and it is now stated
    # against the simulation's present rather than against its starting point. `platform` rows
    # are excluded: they carry real time deliberately, so asserting them here would fail on
    # every tick.
    audit_union = " union all ".join(
        f"""
        select '{table}' as tbl, count(*) as n
          from core.{table}
         where updated_at < created_at
            or created_at >= (select bound from sim)
            or updated_at >= (select bound from sim)
        """  # noqa: S608
        for table in core_tables
    )
    # Falls back to the run anchor when nothing has ticked, which is every run before M3's
    # first tick and every fresh load afterwards.
    simulated_present = f"""
        with sim as (
          select coalesce((select simulated_date from platform.simulation_state),
                          date '{anchor}') + interval '1 day' as bound
        )
    """

    return [
        # 1. No movement outside the account's open window.
        (
            "1",
            """
        select count(*), coalesce(min(id)::text, '') from (
          select t.transaction_id as id
            from core.transactions t join core.accounts a on a.account_id = t.account_id
           where t.booked_at::date < a.opened_date
              or (a.closed_date is not null and t.booked_at::date > a.closed_date)
          union all
          select p.payment_id
            from core.payments p join core.accounts a on a.account_id = p.account_id
           where p.initiated_at::date < a.opened_date
              or (a.closed_date is not null and p.initiated_at::date > a.closed_date)
          union all
          select c.card_id
            from core.cards c join core.accounts a on a.account_id = c.account_id
           where c.issued_date < a.opened_date
              or (a.closed_date is not null and c.issued_date > a.closed_date)
        ) x
        """,
        ),
        # 2. No card transaction outside the card's validity, or on a card that cannot authorise.
        (
            "2",
            """
        select count(*), coalesce(min(t.transaction_id)::text, '')
          from core.transactions t join core.cards c on c.card_id = t.card_id
         where t.booked_at::date < c.issued_date
            or t.booked_at::date >= c.expiry_date
            or c.card_status_code = 'issued'
        """,
        ),
        # 3. Ownership weights sum to one, and every account has a primary holder.
        (
            "3",
            """
        select count(*), coalesce(min(account_id)::text, '') from (
          select a.account_id,
                 coalesce(sum(ah.ownership_weight), 0) as weight_sum,
                 coalesce(bool_or(hr.is_primary), false) as has_primary
            from core.accounts a
            left join core.account_holders ah on ah.account_id = a.account_id
            left join ref.holder_roles hr on hr.code = ah.holder_role_code
           group by a.account_id
        ) x
        where weight_sum <> 1 or not has_primary
        """,
        ),
        # 4. The balance is the signed sum of posted transactions and payments.
        (
            "4",
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
        ) x
        where stored <> computed
        """,
        ),
        # 5. Debits equal credits per posting date and currency.
        (
            "5",
            """
        select count(*), coalesce(min(posting_date)::text, '') from (
          select posting_date, entry_currency_code
            from core.gl_entries
           group by posting_date, entry_currency_code
          having sum(amount) <> 0
        ) x
        """,
        ),
        # 6. Every posted monetary event reaches the ledger.
        (
            "6",
            """
        select count(*), coalesce(min(id)::text, '') from (
          select t.transaction_id as id
            from core.transactions t
            join ref.transaction_statuses ts on ts.code = t.transaction_status_code
           where ts.is_posted
             and not exists (select 1 from core.gl_transactions g
                              where g.source_entity_code = 'transaction'
                                and g.source_entity_id = t.transaction_id)
          union all
          select p.payment_id
            from core.payments p
            join ref.payment_statuses ps on ps.code = p.payment_status_code
           where ps.is_posted
             and not exists (select 1 from core.gl_transactions g
                              where g.source_entity_code = 'payment'
                                and g.source_entity_id = p.payment_id)
        ) x
        """,
        ),
        # 7. Installment schedules are consistent with the loan.
        (
            "7",
            """
        select count(*), coalesce(min(id)::text, '') from (
          select li.loan_installment_id as id
            from core.loan_installments li join core.loans l on l.loan_id = li.loan_id
           where li.paid_amount > li.due_amount
              or (li.paid_at is not null and li.paid_at::date < l.disbursed_date)
              or li.due_date < l.disbursed_date
          union all
          select l.loan_id
            from core.loans l
            join (select loan_id, count(*) as n, sum(due_amount) as total
                    from core.loan_installments group by loan_id) s on s.loan_id = l.loan_id
           where s.n <> l.term_months
              or s.total < l.principal_amount
              or s.total > l.principal_amount
                           * (1 + l.nominal_annual_rate * l.term_months / 12.0) + 1
        ) x
        """,
        ),
        # 8. Every loan traces to an approved application and is within its approved amount.
        (
            "8",
            """
        select count(*), coalesce(min(l.loan_id)::text, '')
          from core.loans l
          join core.loan_applications a on a.loan_application_id = l.loan_application_id
          join ref.loan_application_statuses s
            on s.code = a.loan_application_status_code
         where not s.is_approved
            or a.approved_amount is null
            or l.principal_amount > a.approved_amount
        """,
        ),
        # 9a. Structural: every alert is on a card transaction with a known disposition.
        (
            "9a",
            """
        select count(*), coalesce(min(f.fraud_alert_id)::text, '')
          from core.fraud_alerts f
          join core.transactions t on t.transaction_id = f.transaction_id
         where t.card_id is null
            or f.alerted_at < t.booked_at
        """,
        ),
        # 9b. The confirmed-fraud rate among dispositioned alerts, and the n behind it.
        (
            "9b",
            """
        select count(*) filter (where d.is_confirmed_fraud) as confirmed,
               count(*) as decided
          from core.fraud_alerts f
          join ref.fraud_dispositions d on d.code = f.fraud_disposition_code
         where d.is_final
        """,
        ),
        # 10. Every customer was at least eighteen at their first account opening.
        (
            "10",
            """
        select count(*), coalesce(min(x.customer_id)::text, '') from (
          select c.customer_id, c.date_of_birth, min(a.opened_date) as first_opened
            from core.customers c
            join core.account_holders ah on ah.customer_id = c.customer_id
            join core.accounts a on a.account_id = ah.account_id
           group by c.customer_id, c.date_of_birth
        ) x
        where x.first_opened < x.date_of_birth + interval '18 years'
        """,
        ),
        # 11. Audit timestamps are ordered and at or before the simulated present.
        (
            "11",
            f"{simulated_present} "
            f"select count(*), coalesce(min(tbl), '') from ({audit_union}) y where n > 0",
        ),
        # 11b. The bound the check used, reported so the result names the ceiling it asserted
        # against rather than leaving a reader to infer it from the anchor.
        (
            "11b",
            f"select coalesce((select simulated_date::text from platform.simulation_state), "
            f"'{anchor}'), "
            f"(select count(*) from platform.tick_log)",
        ),
        # 12a. Catalogue: every core foreign key exists and is validated.
        (
            "12a",
            """
        select count(*), coalesce(min(conname), '')
          from pg_constraint con
          join pg_class t on t.oid = con.conrelid
          join pg_namespace n on n.oid = t.relnamespace
         where n.nspname = 'core' and con.contype = 'f' and not con.convalidated
        """,
        ),
        (
            "12b",
            """
        select count(*), '' from pg_constraint con
          join pg_class t on t.oid = con.conrelid
          join pg_namespace n on n.oid = t.relnamespace
         where n.nspname = 'core' and con.contype = 'f'
        """,
        ),
        # 12c. Anti-joins on the large tables, where an orphan would matter most.
        (
            "12c",
            """
        select count(*), coalesce(min(id)::text, '') from (
          select t.transaction_id as id from core.transactions t
           where not exists (select 1 from core.accounts a where a.account_id = t.account_id)
              or (t.card_id is not null
                  and not exists (select 1 from core.cards c where c.card_id = t.card_id))
          union all
          select e.gl_entry_id from core.gl_entries e
           where not exists (select 1 from core.gl_transactions g
                              where g.gl_transaction_id = e.gl_transaction_id)
          union all
          select s.login_session_id from core.login_sessions s
           where not exists (select 1 from core.customers c
                              where c.customer_id = s.customer_id)
        ) x
        """,
        ),
        # 13. The share of digital customer-initiated transactions preceded by a login.
        (
            "13",
            f"""
        with population as (
          select t.transaction_id, t.booked_at, ah.customer_id
            from core.transactions t
            join ref.transaction_types tt on tt.code = t.transaction_type_code
            join core.account_holders ah on ah.account_id = t.account_id
            join ref.holder_roles hr on hr.code = ah.holder_role_code and hr.is_primary
           where tt.is_customer_initiated
             and t.channel_code in ('mobile_app', 'web', 'ecommerce')
             and (t.is_card_present is null or t.is_card_present = false)
             and abs(t.transaction_amount) not in ({recurring})
        )
        select count(*) filter (where exists (
                 select 1 from core.login_sessions s
                  where s.customer_id = population.customer_id
                    and s.started_at <= population.booked_at
                    and s.started_at > population.booked_at - interval '24 hours')) as covered,
               count(*) as total
          from population
        """,
        ),
        # 14. First-digit distribution of card purchase amounts, excluding recurring prices.
        (
            "14",
            f"""
        select substring(abs(transaction_amount)::text from '[1-9]') as digit, count(*)
          from core.transactions
         where transaction_type_code = 'card_purchase'
           and abs(transaction_amount) not in ({recurring})
           and transaction_amount <> 0
         group by 1 having substring(abs(transaction_amount)::text from '[1-9]') is not null
         order by 1
        """,
        ),
        # 14b. The composite distribution over every transaction amount, reported not asserted.
        (
            "14b",
            """
        select substring(abs(transaction_amount)::text from '[1-9]') as digit, count(*)
          from core.transactions where transaction_amount <> 0
         group by 1 having substring(abs(transaction_amount)::text from '[1-9]') is not null
         order by 1
        """,
        ),
    ]


def _run_all(config: RunConfig) -> dict[str, list[tuple[str, ...]]]:
    parts = []
    for key, sql in _queries(config):
        parts.append(f"\\echo {MARKER}{key}")
        parts.append(sql.strip().rstrip(";") + ";")
    command = db._psql_command(True, ["-A", "-t", "-F", db.FIELD_SEPARATOR])
    completed = db._run(command, stdin="\n".join(parts) + "\n")
    if completed.returncode != 0:
        raise RuntimeError(
            "invariants could not run:\n" + (completed.stdout + completed.stderr).strip()[:2000]
        )
    out: dict[str, list[tuple[str, ...]]] = {}
    current: str | None = None
    for line in completed.stdout.splitlines():
        if line.startswith(MARKER):
            current = line[len(MARKER) :].strip()
            out[current] = []
        elif current is not None and line.strip():
            out[current].append(tuple(line.split(db.FIELD_SEPARATOR)))
    return out


NAMES = {
    1: "no movement before the account opened or after it closed",
    2: "no card transaction outside the card's validity",
    3: "ownership weights sum to one and every account has a primary holder",
    4: "the balance is the signed sum of posted transactions and payments",
    5: "debits equal credits per posting date and currency",
    6: "every posted monetary event reaches the ledger",
    7: "installment schedules are consistent with the loan",
    8: "every loan traces to an approved application, within its approved amount",
    9: "fraud alerts are well formed and the confirmed-fraud rate is in band",
    10: "every customer was eighteen at their first account opening",
    11: "audit timestamps are ordered and at or before the simulated present",
    12: "no orphan in any foreign key",
    13: "digital transactions are preceded by a login",
    14: "card purchase first digits follow Benford",
}


def _offender(rows: list[tuple[str, ...]]) -> tuple[int, str | None]:
    if not rows:
        return 0, None
    count = int(rows[0][0])
    example = rows[0][1] if len(rows[0]) > 1 and rows[0][1] else None
    return count, example


def _digits(rows: list[tuple[str, ...]]) -> list[int]:
    counts = [0] * 9
    for row in rows:
        digit = row[0].strip()
        if digit.isdigit() and digit != "0":
            counts[int(digit) - 1] = int(row[1])
    return counts


def run(config: RunConfig) -> InvariantReport:
    raw = _run_all(config)
    report = InvariantReport()
    bands = config.profile
    assert_bands = config.profile.assert_statistical_bands

    for number in (1, 2, 3, 4, 5, 6, 7, 8, 10):
        count, example = _offender(raw.get(str(number), []))
        report.results.append(
            CheckResult(
                number=number,
                name=NAMES[number],
                status=PASS if count == 0 else FAIL,
                count=count,
                example=example,
                detail="no offending rows" if count == 0 else f"{count:,} offending row(s)",
            )
        )

    # 9: structural always, the statistical band only where n supports it.
    structural_count, structural_example = _offender(raw.get("9a", []))
    rate_rows = raw.get("9b", [])
    confirmed = int(rate_rows[0][0]) if rate_rows else 0
    decided = int(rate_rows[0][1]) if rate_rows else 0
    rate = confirmed / decided if decided else 0.0
    report.measurements["confirmed_fraud_rate_among_alerts"] = rate
    report.measurements["dispositioned_alerts"] = float(decided)

    if structural_count > 0:
        report.results.append(
            CheckResult(
                9,
                NAMES[9],
                FAIL,
                structural_count,
                structural_example,
                f"{structural_count:,} malformed alert(s)",
            )
        )
    elif decided == 0:
        report.results.append(
            CheckResult(
                9,
                NAMES[9],
                FAIL,
                0,
                None,
                "no alert reached a final disposition",
            )
        )
    elif not assert_bands:
        # A binomial interval at this n is wider than any band worth stating, so the band is
        # reported rather than asserted. The half-width is printed so the suspension is
        # justified by a number rather than by a claim.
        half_width = 1.96 * (rate * (1 - rate) / decided) ** 0.5 if decided else 1.0
        report.results.append(
            CheckResult(
                9,
                NAMES[9],
                NOT_ASSERTED,
                decided,
                None,
                f"structural checks pass; band not asserted at this scale: "
                f"n={decided}, measured {rate:.3f}, 95% interval half-width "
                f"{half_width:.3f}",
            )
        )
    else:
        low, high = bands.band("confirmed_fraud_rate_among_alerts")
        inside = low <= rate <= high
        report.results.append(
            CheckResult(
                9,
                NAMES[9],
                PASS if inside else FAIL,
                decided,
                None,
                f"confirmed-fraud rate {rate:.4f} among {decided:,} dispositioned alerts, "
                f"band [{low}, {high}]",
            )
        )

    # 11
    count, example = _offender(raw.get("11", []))
    bound_rows = raw.get("11b", [])
    bound = bound_rows[0][0] if bound_rows else config.anchor.isoformat()
    ticks = int(bound_rows[0][1]) if bound_rows and len(bound_rows[0]) > 1 else 0
    report.measurements["simulated_present"] = float(ticks)
    ceiling = (
        f"simulated present {bound} after {ticks} tick(s)"
        if ticks
        else f"anchor {bound}, no tick has run"
    )
    report.results.append(
        CheckResult(
            11,
            NAMES[11],
            PASS if count == 0 else FAIL,
            count,
            example,
            f"no offending rows, bound at the {ceiling}"
            if count == 0
            else f"{count:,} table(s) with offending rows, bound at the {ceiling}",
        )
    )

    # 12: catalogue assertion plus anti-joins on the large tables.
    unvalidated, unvalidated_example = _offender(raw.get("12a", []))
    total_fks, _ = _offender(raw.get("12b", []))
    orphans, orphan_example = _offender(raw.get("12c", []))
    if unvalidated or orphans or total_fks == 0:
        report.results.append(
            CheckResult(
                12,
                NAMES[12],
                FAIL,
                unvalidated + orphans,
                unvalidated_example or orphan_example,
                f"{unvalidated} unvalidated constraint(s), {orphans} orphan(s), "
                f"{total_fks} foreign key(s) present",
            )
        )
    else:
        report.results.append(
            CheckResult(
                12,
                NAMES[12],
                PASS,
                0,
                None,
                f"{total_fks} foreign keys present and validated; "
                f"anti-joins on the large tables found no orphan",
            )
        )

    # 13
    rows = raw.get("13", [])
    covered = int(rows[0][0]) if rows else 0
    total = int(rows[0][1]) if rows else 0
    share = covered / total if total else 0.0
    report.measurements["login_precedes_transaction_share"] = share
    low, high = bands.band("login_precedes_transaction_share")
    if total == 0:
        report.results.append(
            CheckResult(
                13,
                NAMES[13],
                FAIL,
                0,
                None,
                "no digital customer-initiated transactions",
            )
        )
    else:
        report.results.append(
            CheckResult(
                13,
                NAMES[13],
                PASS if share >= low else FAIL,
                total,
                None,
                f"{share:.3f} of {total:,} digital transactions preceded by a login "
                f"within 24 hours, floor {low}",
            )
        )

    # 14
    report.benford_digits = _digits(raw.get("14", []))
    report.composite_digits = _digits(raw.get("14b", []))
    n = sum(report.benford_digits)
    mad = benford_mad(report.benford_digits)
    chi = benford_chi_square(report.benford_digits)
    report.measurements["benford_mad"] = mad
    report.measurements["benford_chi_square"] = chi
    limit = bands.limit("benford_mad_max")
    if n == 0:
        report.results.append(CheckResult(14, NAMES[14], FAIL, 0, None, "no card purchases"))
    else:
        report.results.append(
            CheckResult(
                14,
                NAMES[14],
                PASS if mad <= limit else FAIL,
                n,
                None,
                f"mean absolute deviation {mad:.5f} over {n:,} amounts, limit {limit}; "
                f"chi-square {chi:.1f} reported, not asserted",
            )
        )

    report.results.sort(key=lambda result: result.number)
    return report


def format_report(report: InvariantReport, config: RunConfig) -> str:
    lines = [
        f"seed-verify: profile {config.profile.name}, anchor {config.anchor}",
        "",
        f"  {'#':>2}  {'status':<12} {'invariant':<62} detail",
        f"  {'-' * 2}  {'-' * 12} {'-' * 62} {'-' * 40}",
    ]
    for result in report.results:
        detail = result.detail
        if result.example:
            detail = f"{detail}; example {result.example}"
        lines.append(f"  {result.number:>2}  {result.status:<12} {result.name:<62} {detail}")

    lines.append("")
    if report.benford_digits:
        total = sum(report.benford_digits)
        composite_total = sum(report.composite_digits) or 1
        lines.append("  first digit distribution")
        lines.append("    digit   expected   card purchases   all amounts")
        for index in range(9):
            lines.append(
                f"      {index + 1}     {BENFORD[index] * 100:6.2f}%        "
                f"{report.benford_digits[index] / max(1, total) * 100:6.2f}%       "
                f"{report.composite_digits[index] / composite_total * 100:6.2f}%"
            )
        lines.append(
            f"    composite deviation {benford_mad(report.composite_digits):.5f} "
            f"over {composite_total:,} amounts, observed rather than asserted: cash "
            f"withdrawals snap to note multiples and recurring prices repeat."
        )
        lines.append("")

    failures = report.failures
    lines.append(
        f"seed-verify: {len(report.results) - len(failures)} of {len(report.results)} "
        f"invariants pass" + (f", {len(failures)} FAIL" if failures else "")
    )
    return "\n".join(lines)


def anchor_from_environment(default: dt.date) -> dt.date:
    import os

    raw = os.environ.get("NORDBANK_ANCHOR_DATE", "").strip()
    return dt.date.fromisoformat(raw) if raw else default
