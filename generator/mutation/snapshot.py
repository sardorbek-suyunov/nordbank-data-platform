"""What a tick reads before it decides anything.

The historical load holds the whole book in memory as it builds it, so it always knows an
account's balance and which merchants it uses. A tick starts from a database and has to ask.

**What is read and what is re-derived.** Anything that is a pure function of the run seed is
re-derived rather than read, because the read is the expensive half: a customer's device
fingerprints and IP addresses are hashes of the seed and the customer id, and the device count
is a draw on an addressable stream (spec 003, amended 2026-09-20). Reading the device count
back instead — `count(distinct device_fingerprint)` grouped by customer — measures 3.0 seconds
on the `dev` book against 71 milliseconds for every identity high-water mark in the schema.

**Identities come from `max(id)`, not from the sequences.** `setval` is not transactional, so
a tick that advanced a sequence and then rolled back would leave it advanced, and acceptance
criterion 2 says a failed tick changes nothing. The tick assigns explicit keys from the
high-water mark exactly as the `COPY` loader does, and the sequences are resynchronised after
the commit, for hygiene only.

**Sizes, measured on the `dev` book.** 6,730 open accounts, 3,560 active cards, 7,357 primary
holders, 1,200 merchants, 260 alerts awaiting disposition. The whole snapshot is four figures
of rows and reads in tens of milliseconds through the driver, which is why a tick can afford
to read its world rather than carry it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, NamedTuple

from .. import refdata as refdata_module
from ..realism.calendar import add_months

# The identity column of every core table, for the high-water mark read.
IDENTITY = {
    "customers": "customer_id",
    "customer_addresses": "customer_address_id",
    "merchants": "merchant_id",
    "agent_locations": "agent_location_id",
    "accounts": "account_id",
    "account_holders": "account_holder_id",
    "cards": "card_id",
    "loan_applications": "loan_application_id",
    "loans": "loan_id",
    "loan_installments": "loan_installment_id",
    "transactions": "transaction_id",
    "payments": "payment_id",
    "gl_transactions": "gl_transaction_id",
    "gl_entries": "gl_entry_id",
    "fraud_alerts": "fraud_alert_id",
    "login_sessions": "login_session_id",
}


class Account(NamedTuple):
    account_id: int
    customer_id: int
    currency_code: str
    country_code: str
    product_class: str
    status_code: str
    opened_date: dt.date
    overdraft_limit: Decimal
    balance: Decimal


class Card(NamedTuple):
    card_id: int
    account_id: int
    status_code: str
    issued_date: dt.date
    expiry_date: dt.date


class Merchant(NamedTuple):
    merchant_id: int
    mcc_code: str
    band_code: str
    country_code: str


class Alert(NamedTuple):
    fraud_alert_id: int
    alerted_at: dt.datetime
    disposition_code: str


@dataclass
class Snapshot:
    """The state one tick decides against. Read once, at the top of the transaction."""

    # Where the history the tick continues began and ended. The seasonality, growth and
    # card-not-present trends are all expressed against those two dates, so a tick that did not
    # know them would produce a day that did not belong to the same curve as the day before it.
    anchor_date: dt.date = dt.date.min
    history_start: dt.date = dt.date.min
    # The `ref` vocabularies, read through the tick's own cursor rather than through psql, so
    # the tick and the historical load draw from the same codes with the same attributes.
    ref: Any = None
    accounts: dict[int, Account] = field(default_factory=dict)
    cards_by_account: dict[int, list[Card]] = field(default_factory=dict)
    customer_country: dict[int, str] = field(default_factory=dict)
    merchants: list[Merchant] = field(default_factory=list)
    agent_ids: list[int] = field(default_factory=list)
    open_alerts: list[Alert] = field(default_factory=list)
    next_id: dict[str, int] = field(default_factory=dict)

    def take_id(self, table: str) -> int:
        """The next identity for `table`, assigned by the tick rather than by a sequence."""
        value = self.next_id[table] + 1
        self.next_id[table] = value
        return value

    @property
    def open_account_ids(self) -> list[int]:
        return sorted(self.accounts)

    def cards_of(self, account_id: int) -> list[Card]:
        return self.cards_by_account.get(account_id, [])


# Accounts a movement may post to: open by the reference vocabulary, not soft deleted, and
# not dormant. Dormancy is the state of being open and not moving (generator_realism.md), so
# a dormant account is read — a tick may reactivate it — but the movement pass skips it, which
# is what the historical load does too.
_ACCOUNTS = """
select a.account_id, ah.customer_id, a.currency_code, a.country_code,
       at.product_class_code, a.account_status_code, a.opened_date,
       a.overdraft_limit_amount, a.current_balance_amount
  from core.accounts a
  join ref.account_types at on at.code = a.account_type_code
  join ref.account_statuses st on st.code = a.account_status_code
  join core.account_holders ah on ah.account_id = a.account_id
  join ref.holder_roles hr on hr.code = ah.holder_role_code and hr.is_primary
 where st.is_open and not a.is_deleted and not ah.is_deleted
"""

_CARDS = """
select c.card_id, c.account_id, c.card_status_code, c.issued_date, c.expiry_date
  from core.cards c
 where not c.is_deleted and c.card_status_code in ('issued', 'active', 'blocked')
"""

_CUSTOMER_COUNTRY = """
select customer_id, residence_country_code from core.customers where not is_deleted
"""

_MERCHANTS = """
select m.merchant_id, m.mcc_code, mc.band_code, m.country_code
  from core.merchants m
  join ref.mcc_codes mc on mc.code = m.mcc_code
 where not m.is_deleted
"""

_AGENTS = """
select agent_location_id from core.agent_locations
 where not is_deleted and (active_to_date is null or active_to_date >= %s)
"""

# Alerts an analyst has not finished with. The disposition lag is one to fourteen days
# (generator_realism.md), so this set is small and bounded: 260 rows on the dev book.
_OPEN_ALERTS = """
select f.fraud_alert_id, f.alerted_at, f.fraud_disposition_code
  from core.fraud_alerts f
  join ref.fraud_dispositions d on d.code = f.fraud_disposition_code
 where not d.is_final and not f.is_deleted
"""


def _max_ids_sql() -> str:
    return " union all ".join(
        f"select '{table}' as t, coalesce(max({column}), 0) as n from core.{table}"  # noqa: S608
        for table, column in sorted(IDENTITY.items())
    )


def _ref_executor(cursor: Any) -> Any:
    """Run `generator/refdata.py`'s queries through the tick's cursor.

    The reference loader was written against psql's text output and takes an executor precisely
    so that a second transport can supply one. Its converters parse strings, so the values come
    back rendered the way psql renders them: `t` and `f` for a boolean, an empty field for null.
    """

    def run(sql: str) -> list[tuple[str, ...]]:
        cursor.execute(sql)
        return [
            tuple(
                ""
                if value is None
                else ("t" if value else "f")
                if isinstance(value, bool)
                else str(value)
                for value in row
            )
            for row in cursor.fetchall()
        ]

    return run


def read(
    cursor: Any, simulated_date: dt.date, *, anchor_date: dt.date, history_months: int
) -> Snapshot:
    """Read the whole of a tick's decision state. Seven queries, tens of milliseconds."""
    snapshot = Snapshot(
        anchor_date=anchor_date,
        history_start=add_months(anchor_date, -history_months),
        ref=refdata_module.load(_ref_executor(cursor)),
    )

    cursor.execute(_ACCOUNTS)
    for row in cursor.fetchall():
        snapshot.accounts[row[0]] = Account(*row)

    cursor.execute(_CARDS)
    for row in cursor.fetchall():
        card = Card(*row)
        snapshot.cards_by_account.setdefault(card.account_id, []).append(card)

    cursor.execute(_CUSTOMER_COUNTRY)
    snapshot.customer_country = dict(cursor.fetchall())

    cursor.execute(_MERCHANTS)
    snapshot.merchants = [Merchant(*row) for row in cursor.fetchall()]

    cursor.execute(_AGENTS, (simulated_date,))
    snapshot.agent_ids = [row[0] for row in cursor.fetchall()]

    cursor.execute(_OPEN_ALERTS)
    snapshot.open_alerts = [Alert(*row) for row in cursor.fetchall()]

    cursor.execute(_max_ids_sql())
    snapshot.next_id = {row[0]: int(row[1]) for row in cursor.fetchall()}

    return snapshot


def synchronise_sequences(cursor: Any) -> None:
    """Point every identity sequence past the largest key its table holds.

    **Called after the tick's transaction has committed, never inside it.** `setval` is not
    transactional: measured on PostgreSQL 16.15, a `setval` inside a transaction that then
    rolled back kept the value it had set. Inside the tick it would break acceptance
    criterion 2's promise that a failed tick changes nothing.

    Nothing reads these sequences — both the loader and the tick assign keys from `max(id)` —
    so a crash between the commit and this call leaves a sequence behind the data, which is
    harmless and is corrected by the next successful tick.
    """
    for table, column in sorted(IDENTITY.items()):
        cursor.execute(
            "select setval(pg_get_serial_sequence(%s, %s), "
            f"coalesce((select max({column}) from core.{table}), 1), "  # noqa: S608
            f"(select count(*) > 0 from core.{table}))",  # noqa: S608
            (f"core.{table}", column),
        )
