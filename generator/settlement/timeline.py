"""The clearing file's layout over time: scripted drift in a third party's format.

The relational source's scripted drift lives in `generator/drift/timeline.py` and changes a
table. These events change a file layout, which a relational source cannot express: the
processor adds a field it did not send before, and later stops sending one. Both fire at an
offset from the anchor, for the reason the relational timeline gives, and every file whose
settlement date is on or after an event's day carries it.

The removal is the breaking kind specification 005 could prove only by unit test, because the
relational timeline scripts no column removal. Here it happens end to end.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

COLUMN_ADDED = "column_added"
COLUMN_REMOVED = "column_removed"

# Layout 1, as contracts/cardnet/README.md specifies it.
BASE_COLUMNS: tuple[str, ...] = (
    "record_type",
    "transaction_reference",
    "transaction_date",
    "clearing_date",
    "network",
    "card_reference",
    "masked_pan",
    "merchant_category_code",
    "merchant_name",
    "presentment",
    "settlement_currency",
    "settlement_amount",
)


@dataclass(frozen=True)
class FileEvent:
    name: str
    kind: str
    offset_days: int
    column: str

    def fires_on(self, anchor: dt.date) -> dt.date:
        return anchor + dt.timedelta(days=self.offset_days)


EVENTS: tuple[FileEvent, ...] = (
    # The processor starts sending the interchange fee on each item. Additive: the platform
    # logs it and does not land it, and nothing fails.
    FileEvent(
        name="clearing_interchange_fee_added",
        kind=COLUMN_ADDED,
        offset_days=20,
        column="interchange_fee_amount",
    ),
    # The processor stops sending the merchant name. Breaking: the whole file is quarantined,
    # its batch fails and the backfill halts until a person publishes a contract version that
    # no longer expects the field.
    FileEvent(
        name="clearing_merchant_name_removed",
        kind=COLUMN_REMOVED,
        offset_days=45,
        column="merchant_name",
    ),
)


def active(settlement_date: dt.date, anchor: dt.date) -> tuple[FileEvent, ...]:
    return tuple(event for event in EVENTS if event.fires_on(anchor) <= settlement_date)


def columns(settlement_date: dt.date, anchor: dt.date) -> tuple[str, ...]:
    """The detail fields a file for this settlement date carries, in order."""
    out = list(BASE_COLUMNS)
    for event in active(settlement_date, anchor):
        if event.kind == COLUMN_ADDED:
            out.append(event.column)
        elif event.kind == COLUMN_REMOVED:
            out.remove(event.column)
    return tuple(out)
