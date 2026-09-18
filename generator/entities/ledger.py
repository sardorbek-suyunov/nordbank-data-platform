"""Double-entry postings for every monetary event.

Spec 003 invariant 6 requires every monetary event in `core` to have corresponding GL entries,
and invariant 5 requires debits to equal credits per posting date per currency. The second
follows from the first for free: if every batch balances, any sum of batches balances, so a day
of balanced batches balances. The database already refuses an unbalanced batch through the
deferred constraint trigger, so what this module has to get right is that a batch exists at all
and that both of its legs are in the same currency.

Currency is the subtlety. `sum(amount) = 0` across mixed currencies means nothing — a euro debit
and a yen credit can cancel numerically and be nonsense — so both legs of an event post in the
event's own currency. The bank's foreign exchange position is a separate posting rather than an
implicit one, which is why `ref.gl_accounts` carries a conversion account at all.

Batches are emitted inline as the movement pass produces events. Nothing needs the ledger in
date order: a batch balances on its own, so a second pass to sort tens of millions of entries
by posting date would buy nothing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from ..spool import Spool

# Debit positive, credit negative, bound to entry_side_code by a check constraint on the table
# (spec 002 design rule 10, and the signed-amount exception in conventions.md).
DEBIT = "D"
CREDIT = "C"


class LedgerWriter:
    """Assigns ledger ids and writes balanced batches."""

    __slots__ = ("_batch_id", "_batch_rows", "_entry_id", "_entry_rows", "accounts")

    def __init__(self, spool: Spool, accounts: dict[str, str]) -> None:
        self._batch_rows = spool.table("gl_transactions")
        self._entry_rows = spool.table("gl_entries")
        self._batch_id = 0
        self._entry_id = 0
        self.accounts = accounts

    @property
    def batches(self) -> int:
        return self._batch_id

    @property
    def entries(self) -> int:
        return self._entry_id

    def post(
        self,
        posting_date: dt.date,
        description: str,
        source_entity_code: str,
        source_entity_id: int,
        currency_code: str,
        legs: Sequence[tuple[str, Decimal, int | None]],
        stamp: dt.datetime,
    ) -> int:
        """Write one batch. `legs` is (gl_account_code, signed_amount, account_id or None).

        The caller supplies signed amounts and this asserts they sum to zero, rather than
        deriving the credit from the debit. Deriving it would make an unbalanced batch
        impossible to express and so impossible to test for, and the deferred trigger exists
        precisely because balance is a property worth checking rather than assuming.
        """
        total = sum((amount for _, amount, _ in legs), Decimal("0"))
        if total != 0:
            raise ValueError(
                f"{source_entity_code} {source_entity_id}: batch does not balance in "
                f"{currency_code}, residual {total}"
            )
        if not legs:
            raise ValueError("a posting batch needs at least one line")

        self._batch_id += 1
        batch_id = self._batch_id
        self._batch_rows.write((
            batch_id,
            f"GLT{batch_id:012d}",
            posting_date,
            description[:200],
            source_entity_code,
            source_entity_id,
            stamp,
            stamp,
            False,
        ))

        for gl_account_code, amount, account_id in legs:
            if amount == 0:
                # gl_entries_amount_nonzero_ck refuses these, and a zero line carries no
                # information anyway.
                continue
            self._entry_id += 1
            self._entry_rows.write((
                self._entry_id,
                batch_id,
                posting_date,
                gl_account_code,
                DEBIT if amount > 0 else CREDIT,
                amount,
                currency_code,
                account_id,
                stamp,
                stamp,
                False,
            ))
        return batch_id

    def post_customer_debit(
        self,
        posting_date: dt.date,
        description: str,
        source_entity_code: str,
        source_entity_id: int,
        currency_code: str,
        amount: Decimal,
        account_id: int,
        contra_account: str,
        stamp: dt.datetime,
    ) -> int:
        """Money leaves the customer's account.

        The bank's liability to the customer falls, so customer deposits is debited, and
        whatever received the money is credited.
        """
        return self.post(
            posting_date,
            description,
            source_entity_code,
            source_entity_id,
            currency_code,
            (
                (self.accounts["customer_deposits"], amount, account_id),
                (contra_account, -amount, None),
            ),
            stamp,
        )

    def post_customer_credit(
        self,
        posting_date: dt.date,
        description: str,
        source_entity_code: str,
        source_entity_id: int,
        currency_code: str,
        amount: Decimal,
        account_id: int,
        contra_account: str,
        stamp: dt.datetime,
    ) -> int:
        """Money reaches the customer's account: the bank's liability to them rises."""
        return self.post(
            posting_date,
            description,
            source_entity_code,
            source_entity_id,
            currency_code,
            (
                (contra_account, amount, None),
                (self.accounts["customer_deposits"], -amount, account_id),
            ),
            stamp,
        )
