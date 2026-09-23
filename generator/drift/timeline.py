"""The drift timeline: which events fire, when, and what each one does in both directions.

M3 implements the mechanism plus two events, an additive column and a type widening. The rest
are deferred to M4, where the contracts that have to survive them exist.

**Events fire at an offset from the anchor rather than on an absolute date.** Spec 004 section 3
says each event declares a simulated date, and the anchor is one of the three inputs that
determine a run — `NORDBANK_ANCHOR_DATE` defaults to today. An absolute date would fire for one
anchor and never for any other, so acceptance criterion 10's "at least one event fires within
sixty ticks" would hold or not depending on when the run happened. An offset is a simulated date
in every sense that matters and it is reproducible, which an absolute date is not.

**Both directions are idempotent.** `make schema-apply` re-applies the events the log says have
fired, because it reloads `platform.column_classifications` from the dictionary and would
otherwise drop the classification a drift event added. `make seed` reverts them, because a schema
that is post-drift while the log says nothing has fired is an incoherent state that
`schema-check` would fail against far from its cause.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from schema_contract import Column  # noqa: E402

COLUMN_ADDED = "column_added"
TYPE_WIDENED = "type_widened"

# The anchor the acceptance history is seeded at, and therefore the anchor every committed
# contract's `in_force_from` is authored against. The two cannot both be relative: an event
# fires at an offset from the anchor so that any anchor reaches it, while a contract states the
# business day it takes over from, which in a real deployment is a fixed date. So the contracts
# are written for one anchor, it is named here once, and a unit test asserts that every
# contract version accepting a scripted event takes over on the day that event fires at this
# anchor. 2026-07-20 plus sixty days is 2026-09-18, which is behind the real clock, as a
# backfill requires; it is also the anchor specification 005's acceptance run used.
ACCEPTANCE_ANCHOR = dt.date(2026, 7, 20)


@dataclass(frozen=True)
class DriftEvent:
    """One scripted change to the source's shape.

    `apply_sql` and `revert_sql` move the database. `added` or `widened_to` moves the documented
    schema by the same amount, so the two cannot disagree: they are declared here as one object
    rather than maintained in two files against each other.
    """

    name: str
    drift_type: str
    offset_days: int
    target_schema: str
    target_table: str
    target_column: str
    apply_sql: str
    revert_sql: str
    # For an additive event: the column as the dictionary would have documented it.
    added: Column | None = None
    # For a widening: the type the column becomes, in the dictionary's spelling.
    widened_to: str = ""
    expectation: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.target_schema, self.target_table, self.target_column)

    def fires_on(self, anchor: dt.date) -> dt.date:
        return anchor + dt.timedelta(days=self.offset_days)


# The two events M3 implements.
#
# The additive one is `core.merchants.merchant_risk_score`. It is not a demonstration column:
# spec 004 section 1 asks for merchant risk score revisions among the in-place updates, and spec
# 004's amendment of 2026-09-20 makes this the additive event precisely so that the column
# appears mid-history — which is the condition M4's additive-drift handling is written for — and
# the revisions begin once it exists. A risk score is an attribute of an operational row rather
# than a vocabulary, so it belongs on `core.merchants` and not in a `ref` lookup.
#
# The widening is `core.payments.remittance_reference`, from 140 characters to 280. A scheme
# extending a free-text field is the most ordinary widening a bank's source system produces, and
# it is the one that matters downstream: rows extracted before the event and after it carry
# different types for the same column, which is the type divergence spec 004 section 2 says a
# constrained source cannot emit at a single moment and can emit over time.
EVENTS: tuple[DriftEvent, ...] = (
    DriftEvent(
        name="merchants_risk_score_added",
        drift_type=COLUMN_ADDED,
        offset_days=12,
        target_schema="core",
        target_table="merchants",
        target_column="merchant_risk_score",
        apply_sql=(
            "alter table core.merchants add column if not exists merchant_risk_score numeric(18,8)"
        ),
        revert_sql="alter table core.merchants drop column if exists merchant_risk_score",
        added=Column(
            schema="core",
            table="merchants",
            name="merchant_risk_score",
            data_type="numeric(18,8)",
            is_nullable=True,
            classification="non-personal",
            description=(
                "Acquirer risk score for the merchant, added mid-history by a scripted drift "
                "event. Null until the acquirer first scores the merchant. A decimal fraction, "
                "not a percentage."
            ),
            consumed_by="-",
        ),
        expectation=(
            "bronze accepts the new column and records its appearance in meta; the contract "
            "version that predates it does not know about it and must not fail on it"
        ),
    ),
    DriftEvent(
        name="payments_remittance_widened",
        drift_type=TYPE_WIDENED,
        offset_days=37,
        target_schema="core",
        target_table="payments",
        target_column="remittance_reference",
        apply_sql=(
            "alter table core.payments "
            "alter column remittance_reference type character varying(280)"
        ),
        revert_sql=(
            "alter table core.payments "
            "alter column remittance_reference type character varying(140)"
        ),
        widened_to="character varying(280)",
        expectation=(
            "the whole batch is quarantined and the ingestion gate fails; resolving it means a "
            "new contract version and an explicit rerun"
        ),
    ),
)


@dataclass
class Fired:
    """A drift event that has fired, as the log records it."""

    name: str
    simulated_date: dt.date
    tick_sequence: int = 0
    applied_at: Any = None
    event: DriftEvent = field(default=None)  # type: ignore[assignment]


def events() -> tuple[DriftEvent, ...]:
    return EVENTS


def event_by_name(name: str) -> DriftEvent | None:
    for event in EVENTS:
        if event.name == name:
            return event
    return None


def due_events(anchor: dt.date, simulated_date: dt.date, already: set[str]) -> list[DriftEvent]:
    """Events whose date has arrived and which have not fired yet.

    `<=` rather than `==`, so an event whose date fell inside a window nothing ticked — a seed
    with an anchor past the offset, say — still fires on the next tick rather than being lost.
    """
    return [
        event
        for event in EVENTS
        if event.name not in already and event.fires_on(anchor) <= simulated_date
    ]


def fired_names(execute: Any) -> list[str]:
    """The events `platform.drift_log` says have fired, oldest first."""
    rows = execute("select event_name from platform.drift_log order by drift_log_id")
    return [str(row[0]).strip() for row in rows]


def apply_deltas(documented: list[Column], fired: list[str]) -> list[Column]:
    """The expected schema: the committed dictionary plus the deltas of what has fired.

    Order matters and is the log's. Two events against one column would otherwise resolve by
    whichever the timeline happened to list first, and the answer would depend on a tuple
    literal rather than on what the database did.
    """
    columns = list(documented)
    for name in fired:
        event = event_by_name(name)
        if event is None:
            continue
        if event.drift_type == COLUMN_ADDED and event.added is not None:
            columns.append(event.added)
        elif event.drift_type == TYPE_WIDENED:
            columns = [
                (
                    Column(
                        schema=column.schema,
                        table=column.table,
                        name=column.name,
                        data_type=event.widened_to,
                        is_nullable=column.is_nullable,
                        classification=column.classification,
                        description=column.description,
                        consumed_by=column.consumed_by,
                    )
                    if (column.schema, column.table, column.name) == event.key
                    else column
                )
                for column in columns
            ]
    return columns


def expected_schema(documented: list[Column], execute: Any) -> tuple[list[Column], list[str]]:
    """The schema the database should have: the committed dictionary plus what has fired.

    One function, because this is one fact and it had two implementations for about an hour —
    `make schema-check` read the drift log and the same comparison running inside the stack did
    not, so the second failed on the column the first expected. Both call this now.
    """
    fired = fired_names(execute)
    return apply_deltas(documented, fired), fired


def classification_rows(fired: list[str]) -> list[Column]:
    """The columns a fired event added, which `platform.column_classifications` has to hold."""
    out: list[Column] = []
    for name in fired:
        event = event_by_name(name)
        if event is not None and event.drift_type == COLUMN_ADDED and event.added is not None:
            out.append(event.added)
    return out


_CLASSIFY = """
insert into platform.column_classifications
    (schema_name, table_name, column_name, classification, rationale)
values (%s, %s, %s, %s, %s)
on conflict (schema_name, table_name, column_name) do update
    set classification = excluded.classification,
        rationale      = excluded.rationale
  where (platform.column_classifications.classification,
         platform.column_classifications.rationale)
        is distinct from (excluded.classification, excluded.rationale)
"""

_LOG = """
insert into platform.drift_log
    (event_name, drift_type, target_schema, target_table, target_column,
     simulated_date, tick_sequence, applied_at)
values (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def record_fired(
    cursor: Any,
    names: list[str],
    *,
    simulated_date: dt.date,
    tick_sequence: int,
    applied_at: Any,
) -> None:
    """Write the log row and the classification row for every event that fired this tick.

    Called after the simulation clock is cleared, so both carry real time like every other
    `platform` row. The DDL itself carries no timestamp, so splitting them across the clock
    loses nothing: they are still one transaction, which is what matters.
    """
    for name in names:
        event = event_by_name(name)
        if event is None:
            raise ValueError(f"drift event {name!r} fired but is not in the timeline")
        cursor.execute(
            _LOG,
            (
                event.name,
                event.drift_type,
                event.target_schema,
                event.target_table,
                event.target_column,
                simulated_date,
                tick_sequence,
                applied_at,
            ),
        )
        if event.added is not None:
            cursor.execute(
                _CLASSIFY,
                (
                    event.added.schema,
                    event.added.table,
                    event.added.name,
                    event.added.classification,
                    event.added.description,
                ),
            )


def revert_all(execute_sql: Any, fired: list[str]) -> list[str]:
    """Undo every fired event, newest first, and return what was reverted.

    Newest first because a later event may depend on an earlier one's column. Both directions
    are idempotent, so reverting an event that was never applied is a no-op rather than an
    error.
    """
    reverted: list[str] = []
    for name in reversed(fired):
        event = event_by_name(name)
        if event is None:
            continue
        execute_sql(event.revert_sql)
        reverted.append(name)
    return reverted
