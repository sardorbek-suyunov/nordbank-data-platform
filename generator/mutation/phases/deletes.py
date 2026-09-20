"""What the source removes, and the one thing it removes for real.

**Soft deletes are the normal path.** `is_deleted` is set, `updated_at` moves, the row arrives
at bronze through the same incremental extraction as any other change, and silver applies the
delete. Three entity types, as acceptance criterion 8 requires: merged duplicate customers,
non-posted transactions, and applications that were withdrawn, expired or rejected.

**A posted transaction is never soft deleted.** Invariant 4 reconciles the balance against
posted transactions and payments and filters on `is_posted`, not on `is_deleted`, so a
soft-deleted posted transaction is a fork with no good branch: reverse the balance and the
invariant fails, leave it and the source says the money moved while silver, which drops deleted
rows, says it did not. A posting is reversed instead, by the movement phase, through
`reversal_of_transaction_id`.

**Exactly one kind of physical delete, and it exists so that a control at M7 is testable.**
`architecture.md` records that watermark extraction cannot detect a `DELETE` and schedules a
primary-key reconciliation at M7 to find the resulting orphans in silver. If nothing in the
source ever deleted a row, that reconciler could never be demonstrated against a known
positive — which is worse than not having it, because an untested control reads as a working
one.

So the source purges a duplicate customer record that no other row references. The candidate
list is built from `pg_constraint` rather than written down, so a table that gains a foreign key
to `core.customers` later cannot be missed. A dependent-free row cannot violate a foreign key,
so the delete needs nothing relaxed and nothing deferred. Measured on the loaded `ci` book,
**no customer has no dependent row at all**, so "dependent-free customer" is an exact,
data-derived description of a duplicate the dirt phase created and nothing has attached to.

Every purged key is written to `platform.tick_deleted_keys`, individually. M7's reconciler then
has an exact expected answer rather than a count, and can be validated in both directions: no
orphan in silver that is absent from that table, and no key in that table that silver still
holds.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ...realism.distributions import bernoulli
from ..tick import TickContext
from ..writer import apply_updates

# Statuses whose transactions never moved money and can therefore be removed outright.
VOIDABLE = ("pending", "authorised", "declined", "reversed")

# Applications a back office would clear away.
CLOSED_APPLICATIONS = ("withdrawn", "expired", "rejected")


def run(context: TickContext) -> None:
    rates = context.profile.params["mutation"]["deletes"]
    day_start, _ = context.window
    _resolve_duplicates(context, rates, day_start)
    _void_transactions(context, rates, day_start)
    _clear_applications(context, rates, day_start)


def _children_of_customers(context: TickContext) -> list[tuple[str, str]]:
    """Every table with a foreign key to `core.customers`, from the catalogue.

    Read rather than written down. A hand-maintained list would be correct until the next table
    referenced a customer, and the symptom would be a foreign key violation inside the tick's
    transaction — or, worse, a delete that succeeded because the list was checked and the
    constraint was not.
    """
    context.cursor.execute(
        """
        select child.relname, att.attname
          from pg_constraint con
          join pg_class child on child.oid = con.conrelid
          join pg_class parent on parent.oid = con.confrelid
          join pg_attribute att
            on att.attrelid = con.conrelid and att.attnum = con.conkey[1]
         where con.contype = 'f'
           and parent.relname = 'customers'
           and parent.relnamespace = 'core'::regnamespace
         order by child.relname
        """
    )
    return [(str(table), str(column)) for table, column in context.cursor.fetchall()]


def _dependent_free_customers(context: TickContext, day_start: dt.datetime) -> list[int]:
    children = _children_of_customers(context)
    anti_joins = "".join(
        f"\n           and not exists (select 1 from core.{table} x "  # noqa: S608
        f"where x.{column} = c.customer_id)"
        for table, column in children
    )
    context.cursor.execute(
        f"""
        select c.customer_id from core.customers c
         where not c.is_deleted and c.created_at < %s{anti_joins}
         order by c.customer_id
        """,  # noqa: S608 - table and column names come from pg_constraint
        (day_start,),
    )
    return [row[0] for row in context.cursor.fetchall()]


def _resolve_duplicates(context: TickContext, rates: dict, day_start: dt.datetime) -> None:
    """A duplicate is found and dealt with: tombstoned, or purged outright.

    Both are what real dedupe processes do. Most systems tombstone, so the purge share is small
    — and it is the whole reason the purge exists, since M7's reconciler needs one real instance
    of the condition it is written to detect.
    """
    candidates = _dependent_free_customers(context, day_start)
    if not candidates:
        return

    hazard = float(rates["duplicate_resolution_daily_hazard"])
    purge_share = float(rates["physical_purge_share"])

    tombstoned: list[int] = []
    purged: list[int] = []
    for customer_id in candidates:
        rng = context.stream("deletes.duplicate", customer_id)
        if not bernoulli(rng, hazard):
            continue
        (purged if bernoulli(rng, purge_share) else tombstoned).append(customer_id)

    context.report.record_update(
        "customers",
        apply_updates(
            context.cursor,
            "update core.customers set is_deleted = true "
            "where customer_id = any(%s::bigint[]) returning customer_id",
            tombstoned,
        ),
        soft_delete=True,
    )

    if not purged:
        return
    context.cursor.execute(
        "delete from core.customers where customer_id = any(%s::bigint[]) returning customer_id",
        (purged,),
    )
    for (customer_id,) in context.cursor.fetchall():
        context.report.record_delete("customers", customer_id)


def _void_transactions(context: TickContext, rates: dict, day_start: dt.datetime) -> None:
    """Authorisations and declines a back office voids. None of them ever moved money."""
    horizon = int(rates["void_max_age_days"])
    hazard = float(rates["void_transaction_daily_hazard"])

    context.cursor.execute(
        """
        select t.transaction_id from core.transactions t
         where t.transaction_status_code = any(%s)
           and not t.is_deleted
           and t.booked_at >= %s and t.booked_at < %s
         order by t.transaction_id
        """,
        (list(VOIDABLE), day_start - dt.timedelta(days=horizon), day_start),
    )
    ids = [
        transaction_id
        for (transaction_id,) in context.cursor.fetchall()
        if bernoulli(context.stream("deletes.transaction", transaction_id), hazard)
    ]
    _soft_delete(context, "transactions", "transaction_id", ids)


def _clear_applications(context: TickContext, rates: dict, day_start: dt.datetime) -> None:
    """Applications the funnel finished with, cleared away by a housekeeping job."""
    hazard = float(rates["clear_application_daily_hazard"])
    context.cursor.execute(
        """
        select a.loan_application_id from core.loan_applications a
         where a.loan_application_status_code = any(%s)
           and not a.is_deleted and a.created_at < %s
           and not exists (select 1 from core.loans l
                            where l.loan_application_id = a.loan_application_id)
         order by a.loan_application_id
        """,
        (list(CLOSED_APPLICATIONS), day_start),
    )
    ids = [
        application_id
        for (application_id,) in context.cursor.fetchall()
        if bernoulli(context.stream("deletes.application", application_id), hazard)
    ]
    _soft_delete(context, "loan_applications", "loan_application_id", ids)


def _soft_delete(context: TickContext, table: str, key: str, ids: list[int]) -> None:
    """Set `is_deleted`, spread across the phase's window."""
    if not ids:
        return
    for instant, group in context.jitter_groups("deletes", ids, key_of=_identity):
        context.set_clock(instant)
        context.report.record_update(
            table,
            apply_updates(
                context.cursor,
                f"update core.{table} set is_deleted = true "  # noqa: S608 - fixed identifiers
                f"where {key} = any(%s::bigint[]) returning {key}",
                group,
            ),
            soft_delete=True,
        )


def _identity(value: Any) -> Any:
    return value
