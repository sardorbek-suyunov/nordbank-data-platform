"""Read one card processor clearing file against its contracts (spec 006 sections 2 and 5).

Three kinds of finding, at three grains, and keeping them apart is the point:

- **The file.** Its structure — a header record, a column line, detail records, trailers and an
  end record whose count matches — and its column line against the contract. A structural fault
  or a breaking change in the column line refuses the whole file: every record is quarantined
  with the reason and both of the file's batches fail. An additive column is noticed, logged and
  not landed.
- **A record.** A detail record with the wrong number of fields, a field that does not parse as
  its declared type, a blank required field or a duplicated key is quarantined on its own with
  its reason, and the rest of the file lands.
- **The totals.** The trailer records are the processor's control totals and land as their own
  entity; they are what the settlement reconciliation compares with the ledger.

**What a payload is.** A record's `_raw_payload` is its line as delivered, re-serialised with the
same delimiter and quoting, with the card reference replaced by its token. A record whose field
count is wrong cannot say which of its fields is the card reference — a missing field shifts
every later one — so every field of such a record is tokenised. That keeps its shape, which is
the evidence of what was wrong with it, and gives up the values, which is the price of not
knowing where the identifier is.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field

from nordbank_ops.feeds.cast import CastError, cast
from nordbank_ops.feeds.land import Parsed
from nordbank_ops.validation import QuarantineReason, Rejection

WRONG_FIELD_COUNT = "record has {got} fields and the column line names {expected}"


@dataclass
class ClearingFile:
    settlement_date: dt.date | None = None
    file_sequence: int | None = None
    processor_id: str | None = None
    column_line: list[str] = field(default_factory=list)
    details: Parsed = field(default_factory=Parsed)
    totals: Parsed = field(default_factory=Parsed)
    structural_fault: str | None = None
    card_references: list[str] = field(default_factory=list)


def _serialise(fields: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="", quoting=csv.QUOTE_MINIMAL).writerow(fields)
    return buffer.getvalue()


def classify_column_line(
    column_line: list[str], expected: tuple[str, ...]
) -> tuple[list, str | None]:
    """Drift observations, and the breaking reason if any.

    Additive: a name the contract does not have. Breaking: a name the contract has and the file
    does not, or the contract's names present in a different order.
    """
    observations = []
    for name in column_line:
        if name not in expected:
            observations.append(
                {
                    "column": name,
                    "kind": "additive",
                    "detail": "a field the contract does not describe",
                    "action": "logged, not landed",
                }
            )
    missing = [name for name in expected if name not in column_line]
    for name in missing:
        observations.append(
            {
                "column": name,
                "kind": "removed_column",
                "detail": "a field the contract requires is absent from the column line",
                "action": "file quarantined, batch failed",
            }
        )
    if missing:
        return observations, f"removed column(s) {', '.join(missing)}"
    present = [name for name in column_line if name in expected]
    if present != list(expected):
        observations.append(
            {
                "column": "*",
                "kind": "field_order",
                "detail": f"fields arrive as {present}, the contract names {list(expected)}",
                "action": "file quarantined, batch failed",
            }
        )
        return observations, "the contract's fields arrive in a different order"
    return observations, None


def _structure(rows: list[list[str]]) -> str | None:
    if not rows or not rows[0] or rows[0][0] != "H":
        return "the first line is not a header record"
    if len(rows) < 3 or not rows[1] or rows[1][0] != "record_type":
        return "the second line is not a column line"
    if not rows[-1] or rows[-1][0] != "Z" or len(rows[-1]) != 2:
        return "the last line is not an end record"
    kinds = {row[0] for row in rows[2:-1] if row}
    unknown = kinds - {"D", "T"}
    if unknown:
        return f"unknown record type(s) {sorted(unknown)}"
    details = sum(1 for row in rows[2:-1] if row and row[0] == "D")
    if str(details) != rows[-1][1]:
        return f"the end record declares {rows[-1][1]} detail records and the file has {details}"
    return None


def read(body: bytes, detail_contract, totals_contract, tokeniser) -> ClearingFile:
    out = ClearingFile()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        out.structural_fault = "the file is not UTF-8"
        return out
    rows = list(csv.reader(io.StringIO(text)))
    rows = [row for row in rows if row]
    out.structural_fault = _structure(rows)
    if out.structural_fault:
        return out

    header = dict(zip(detail_contract.format["header_record"]["fields"], rows[0], strict=False))
    try:
        out.settlement_date = cast(header.get("settlement_date"), "date")
        out.file_sequence = cast(header.get("file_sequence"), "integer")
    except CastError as exc:
        out.structural_fault = f"the header record does not parse: {exc}"
        return out
    out.processor_id = header.get("processor_id")
    header_values = {
        "settlement_date": out.settlement_date,
        "file_sequence": out.file_sequence,
        "processor_id": out.processor_id,
    }

    out.column_line = rows[1]
    expected = detail_contract.record_columns
    observations, breaking = classify_column_line(out.column_line, expected)
    out.details.drift = observations
    out.details.breaking = breaking
    card_index = (
        out.column_line.index("card_reference") if "card_reference" in out.column_line else None
    )

    for row in rows[2:-1]:
        if row[0] == "D":
            _detail(out, row, detail_contract, header_values, card_index, tokeniser)
        else:
            _total(out, row, totals_contract, header_values)
    out.details.identifiers_seen = list(out.card_references)
    out.totals.breaking = breaking
    return out


def _detail(out, row, contract, header_values, card_index, tokeniser) -> None:
    if len(row) != len(out.column_line):
        payload = _serialise([row[0], *(tokeniser.token(v) if v else v for v in row[1:])])
        rejection = Rejection(
            "*",
            WRONG_FIELD_COUNT.format(got=len(row), expected=len(out.column_line)),
            None,
            record_key=None,
        )
        out.details.refused.append(({}, rejection, payload))
        return

    raw = dict(zip(out.column_line, row, strict=True))
    card = raw.get("card_reference") or None
    if card:
        out.card_references.append(card)
    tokenised = list(row)
    if card_index is not None and tokenised[card_index]:
        tokenised[card_index] = tokeniser.token(tokenised[card_index])
    payload = _serialise(tokenised)

    record = dict(header_values)
    for column in contract.columns:
        if column.origin != "record":
            continue
        value = raw.get(column.name)
        try:
            record[column.name] = cast(value, column.data_type)
        except CastError:
            rejection = Rejection(
                column.name,
                QuarantineReason.TYPE_MISMATCH,
                value,
                record_key=raw.get("transaction_reference"),
            )
            out.details.refused.append((record, rejection, payload))
            return
    out.details.records.append(record)
    out.details.payloads.append(payload)


def _total(out, row, contract, header_values) -> None:
    names = contract.format["fields"]
    payload = _serialise(row)
    if len(row) != len(names):
        rejection = Rejection(
            "*", WRONG_FIELD_COUNT.format(got=len(row), expected=len(names)), None, None
        )
        out.totals.refused.append(({}, rejection, payload))
        return
    raw = dict(zip(names, row, strict=True))
    record = dict(header_values)
    for column in contract.columns:
        if column.origin != "record":
            continue
        try:
            record[column.name] = cast(raw.get(column.name), column.data_type)
        except CastError:
            rejection = Rejection(
                column.name, QuarantineReason.TYPE_MISMATCH, raw.get(column.name), None
            )
            out.totals.refused.append((record, rejection, payload))
            return
    out.totals.records.append(record)
    out.totals.payloads.append(payload)
