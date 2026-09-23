"""Build one clearing file from the items it covers. Pure: no database, no object store.

The order of the steps is the order a processor's own failures would occur in, and it decides
what each defect does to the reconciliation:

1. **Breaks** alter an item's amount before anything is totalled. The processor believes its own
   number, so the trailer agrees with the damaged detail and the file disagrees with the ledger.
   This is what criterion 10 detects, and nothing else in the file produces it.
2. **The trailer** is computed next, over every item the processor wrote, per network and
   currency. It is the sender's control total.
3. **Malformation** happens last, in transmission: a field is garbled or dropped after the
   trailer was computed. A malformed record therefore still counts in the trailer at its true
   amount, so it quarantines without creating a settlement break. Without that ordering the
   malformed rate and the break rate would be one parameter, and the reconciliation would
   measure quarantine noise (the ruling that put the trailer in).

A break's magnitude is drawn relative to the total of the network and currency it lands in, not
as an absolute amount. The severity rule in `metric_definitions.md` has a relative threshold,
0.1 per cent of the file total, and an absolute one, 100 units; at `ci` a minor-currency day
totals tens of units and a euro day thousands, so an absolute magnitude that is a `warn` in one
is an `error` in the other, and the same magnitude changes severity between profiles.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import random
from dataclasses import dataclass, field

QUANTUM = decimal.Decimal("0.0001")
RELATIVE_THRESHOLD = decimal.Decimal("0.001")
ABSOLUTE_THRESHOLD = decimal.Decimal("100")

UNPARSEABLE_AMOUNT = "unparseable_amount"
INVALID_DATE = "invalid_date"
MISSING_REQUIRED_FIELD = "missing_required_field"
WRONG_FIELD_COUNT = "wrong_field_count"

WARN = "warn"
ERROR = "error"


@dataclass(frozen=True)
class Item:
    """One cleared card item, as the processor knows it."""

    transaction_reference: str
    transaction_date: dt.date
    clearing_date: dt.date
    network: str
    card_reference: str
    card_bin: str
    card_last_four: str
    merchant_category_code: str | None
    merchant_name: str | None
    is_card_present: bool
    settlement_currency: str
    settlement_amount: decimal.Decimal


@dataclass
class Parameters:
    processor_id: str
    malformed_record_share: float
    malformed_kind_mix: dict[str, float]
    break_warn_share: float
    break_error_share: float
    break_warn_relative: tuple[float, float]
    break_error_relative: tuple[float, float]

    @classmethod
    def from_profile(cls, section: dict) -> Parameters:
        return cls(
            processor_id=section["processor_id"],
            malformed_record_share=float(section["malformed_record_share"]),
            malformed_kind_mix=dict(section["malformed_kind_mix"]),
            break_warn_share=float(section["break_warn_share"]),
            break_error_share=float(section["break_error_share"]),
            break_warn_relative=(
                float(section["break_warn_relative_min"]),
                float(section["break_warn_relative_max"]),
            ),
            break_error_relative=(
                float(section["break_error_relative_min"]),
                float(section["break_error_relative_max"]),
            ),
        )


@dataclass
class Built:
    body: bytes
    manifest: dict = field(default_factory=dict)


def severity(file_total: decimal.Decimal, ledger_total: decimal.Decimal) -> str | None:
    """The severity `metric_definitions.md` assigns to a difference, or None for no break.

    The relative threshold is taken against the absolute file total: a refund-dominated day has
    a negative total, and a threshold of a negative amount would make every difference an error.
    """
    difference = abs(file_total - ledger_total)
    if difference == 0:
        return None
    if difference < RELATIVE_THRESHOLD * abs(file_total) and difference < ABSOLUTE_THRESHOLD:
        return WARN
    return ERROR


def _amount(value: decimal.Decimal) -> str:
    return str(value.quantize(QUANTUM))


def _weighted(rng: random.Random, mix: dict[str, float]) -> str:
    names = sorted(mix)
    return rng.choices(names, weights=[mix[n] for n in names], k=1)[0]


def _break_delta(
    rng: random.Random, total: decimal.Decimal, band: tuple[float, float], kind: str
) -> decimal.Decimal | None:
    """A signed delta relative to the cell total, or None when the cell cannot carry one.

    A `warn` delta must be representable at four decimal places and stay under both thresholds;
    on a cell whose total is a fraction of a unit no such delta exists, and the cell is skipped
    rather than given a break of the wrong severity.
    """
    magnitude = abs(total)
    if magnitude == 0:
        return None
    low, high = band
    if kind == WARN:
        floor = float(QUANTUM / magnitude)
        low = max(low, floor)
        if low > high:
            return None
    relative = decimal.Decimal(str(rng.uniform(low, high)))
    delta = (magnitude * relative).quantize(QUANTUM)
    if delta == 0:
        return None
    if kind == WARN and not (delta < RELATIVE_THRESHOLD * magnitude and delta < ABSOLUTE_THRESHOLD):
        return None
    return delta if rng.random() < 0.5 else -delta


def _row(item: Item, amount: decimal.Decimal, columns: tuple[str, ...]) -> list[str]:
    values = {
        "record_type": "D",
        "transaction_reference": item.transaction_reference,
        "transaction_date": item.transaction_date.isoformat(),
        "clearing_date": item.clearing_date.isoformat(),
        "network": item.network,
        "card_reference": item.card_reference,
        "masked_pan": f"{item.card_bin}******{item.card_last_four}",
        "merchant_category_code": item.merchant_category_code or "",
        "merchant_name": item.merchant_name or "",
        "presentment": "CP" if item.is_card_present else "CNP",
        "settlement_currency": item.settlement_currency,
        "settlement_amount": _amount(amount),
        # The interchange the issuer earns on the item, sent once the processor starts sending
        # it. A flat 0.2 per cent: the field exists to be additive drift, not to be priced.
        "interchange_fee_amount": _amount(abs(amount) * decimal.Decimal("0.002")),
    }
    return [values[name] for name in columns]


def _malform(
    row: list[str], columns: tuple[str, ...], kind: str, rng: random.Random
) -> tuple[list[str], str]:
    """Damage one row in transit. Returns the row and the field the damage fell on."""
    out = list(row)
    if kind == UNPARSEABLE_AMOUNT:
        index = columns.index("settlement_amount")
        out[index] = f"{out[index]}{out[columns.index('settlement_currency')]}"
        return out, "settlement_amount"
    if kind == INVALID_DATE:
        index = columns.index("transaction_date")
        year, _month, day = out[index].split("-")
        out[index] = f"{year}-13-{day}"
        return out, "transaction_date"
    if kind == MISSING_REQUIRED_FIELD:
        name = rng.choice(["settlement_currency", "clearing_date", "network"])
        out[columns.index(name)] = ""
        return out, name
    if kind == WRONG_FIELD_COUNT:
        if rng.random() < 0.5:
            out.append("UNEXPECTED")
            return out, "extra field"
        out.pop()
        return out, "missing last field"
    raise ValueError(f"unknown malformation {kind!r}")


def build(
    *,
    settlement_date: dt.date,
    items: list[Item],
    columns: tuple[str, ...],
    parameters: Parameters,
    rng: random.Random,
    ledger_totals: dict[tuple[str, str], decimal.Decimal],
) -> Built:
    """The file's bytes and the manifest of what was injected into it."""
    items = sorted(items, key=lambda item: item.transaction_reference)
    amounts = {item.transaction_reference: item.settlement_amount for item in items}

    # 1. Breaks, per network and currency, before anything is totalled.
    cells: dict[tuple[str, str], list[Item]] = {}
    for item in items:
        cells.setdefault((item.network, item.settlement_currency), []).append(item)
    breaks = []
    broken_references: set[str] = set()
    skipped_cells = 0
    for cell in sorted(cells):
        draw = rng.random()
        if draw < parameters.break_warn_share:
            kind, band = WARN, parameters.break_warn_relative
        elif draw < parameters.break_warn_share + parameters.break_error_share:
            kind, band = ERROR, parameters.break_error_relative
        else:
            continue
        total = sum((amounts[i.transaction_reference] for i in cells[cell]), decimal.Decimal(0))
        delta = _break_delta(rng, total, band, kind)
        if delta is None:
            skipped_cells += 1
            continue
        target = rng.choice(cells[cell])
        amounts[target.transaction_reference] += delta
        broken_references.add(target.transaction_reference)
        breaks.append(
            {
                "network": cell[0],
                "settlement_currency": cell[1],
                "transaction_reference": target.transaction_reference,
                "delta": _amount(delta),
                "intended_severity": kind,
            }
        )

    # 2. The trailer, over every item written, at the amounts the processor believes.
    totals: dict[tuple[str, str], list] = {}
    for item in items:
        key = (item.network, item.settlement_currency)
        entry = totals.setdefault(key, [0, decimal.Decimal(0)])
        entry[0] += 1
        entry[1] += amounts[item.transaction_reference]
    for entry in breaks:
        key = (entry["network"], entry["settlement_currency"])
        file_total = totals[key][1].quantize(QUANTUM)
        ledger_total = ledger_totals.get(key, decimal.Decimal(0)).quantize(QUANTUM)
        entry["file_total"] = _amount(file_total)
        entry["ledger_total"] = _amount(ledger_total)
        entry["expected_severity"] = severity(file_total, ledger_total)

    # 3. Transmission damage, after the trailer.
    rows = []
    malformed = []
    for item in items:
        row = _row(item, amounts[item.transaction_reference], columns)
        if (
            item.transaction_reference not in broken_references
            and rng.random() < parameters.malformed_record_share
        ):
            kind = _weighted(rng, parameters.malformed_kind_mix)
            row, where = _malform(row, columns, kind, rng)
            malformed.append(
                {
                    "transaction_reference": item.transaction_reference,
                    "kind": kind,
                    "field": where,
                }
            )
        rows.append(row)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(
        [
            "H",
            parameters.processor_id,
            settlement_date.isoformat(),
            "01",
            f"{settlement_date.isoformat()}T05:00:00Z",
            "1",
        ]
    )
    writer.writerow(list(columns))
    writer.writerows(rows)
    for (network, currency), (count, total) in sorted(totals.items()):
        writer.writerow(["T", network, currency, str(count), _amount(total)])
    writer.writerow(["Z", str(len(rows))])

    manifest = {
        "settlement_date": settlement_date.isoformat(),
        "columns": list(columns),
        "detail_records": len(rows),
        "cells": len(cells),
        "breaks": breaks,
        "break_cells_skipped": skipped_cells,
        "malformed": malformed,
        "trailer": [
            {
                "network": network,
                "settlement_currency": currency,
                "record_count": count,
                "amount_total": _amount(total),
                "ledger_total": _amount(ledger_totals.get((network, currency), decimal.Decimal(0))),
            }
            for (network, currency), (count, total) in sorted(totals.items())
        ],
    }
    return Built(body=buffer.getvalue().encode("utf-8"), manifest=manifest)
