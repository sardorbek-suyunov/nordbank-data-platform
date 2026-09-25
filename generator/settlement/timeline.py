"""The clearing file's layout over time: scripted drift in a third party's format.

The relational source's scripted drift lives in `generator/drift/timeline.py` and changes a
table. These events change a file layout, which a relational source cannot express: the
processor adds a field it did not send before, and later stops sending one. Both fire at an
offset from the anchor, for the reason the relational timeline gives, and every file the
processor **sends** on or after an event's day carries it, whatever settlement date the file
covers: a sender changes its format at a point in its own time, which is when it sends. A late
file for a date before a change, sent after it, is in the new layout (ADR 0016).

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
    # The processor starts sending the acquirer's reference number on each item. Additive: the
    # platform logs it and does not land it, and nothing fails. A field no other source the
    # platform reads carries, so the drift event cannot create a second, contradicting source
    # of anything (the spec 006 review replaced an interchange amount here for that reason).
    FileEvent(
        name="clearing_acquirer_reference_added",
        kind=COLUMN_ADDED,
        offset_days=20,
        column="acquirer_reference_number",
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


# One late file is scripted rather than drawn, so that every acceptance run has a file that
# straddles the breaking change: its settlement date is two days before the removal fires, and
# it arrives three days late, one day after it, in the layout without `merchant_name`. Under
# ADR 0014's selection by settlement date it failed on every run with no way out; under ADR
# 0016's selection by delivery date it lands.
STRADDLING_LATE_OFFSET = 43


def held_late(settlement_date: dt.date, anchor: dt.date) -> bool:
    """Whether the timeline holds this settlement date's file back, whatever the draw says."""
    return (settlement_date - anchor).days == STRADDLING_LATE_OFFSET


def active(delivered_on: dt.date, anchor: dt.date) -> tuple[FileEvent, ...]:
    return tuple(event for event in EVENTS if event.fires_on(anchor) <= delivered_on)


def columns(delivered_on: dt.date, anchor: dt.date) -> tuple[str, ...]:
    """The detail fields a file sent on this day carries, in order."""
    out = list(BASE_COLUMNS)
    for event in active(delivered_on, anchor):
        if event.kind == COLUMN_ADDED:
            out.append(event.column)
        elif event.kind == COLUMN_REMOVED:
            out.remove(event.column)
    return tuple(out)
