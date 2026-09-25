"""The card processor's clearing file: layout, defects and determinism (spec 006 section 2).

The builder is pure, so these run on fabricated items with no database and no object store.
"""

from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import random

import pytest

from generator.settlement import build, timeline
from generator.settlement.__main__ import due_on

ANCHOR = dt.date(2026, 7, 20)
DAY = dt.date(2026, 8, 3)
D = decimal.Decimal


def _items(count: int = 40) -> list[build.Item]:
    out = []
    for n in range(count):
        network = "visa" if n % 3 else "mastercard"
        currency = "EUR" if n % 5 else "DKK"
        out.append(
            build.Item(
                transaction_reference=f"TXN{n:013d}",
                transaction_date=DAY - dt.timedelta(days=1),
                clearing_date=DAY - dt.timedelta(days=1),
                network=network,
                card_reference=f"NB{n:010d}",
                card_bin="400001",
                card_last_four=f"{n:04d}",
                merchant_category_code=None if n % 7 == 0 else "5411",
                merchant_name=None if n % 7 == 0 else f"Shop, number {n}",
                is_card_present=bool(n % 2),
                settlement_currency=currency,
                settlement_amount=D(f"{10 + n}.25") if n % 11 else D("-4.5000"),
            )
        )
    return out


def _parameters(**overrides) -> build.Parameters:
    base = dict(
        processor_id="NBKPROC",
        malformed_record_share=0.0,
        malformed_kind_mix={
            build.UNPARSEABLE_AMOUNT: 0.25,
            build.INVALID_DATE: 0.25,
            build.MISSING_REQUIRED_FIELD: 0.25,
            build.WRONG_FIELD_COUNT: 0.25,
        },
        break_warn_share=0.0,
        break_error_share=0.0,
        break_warn_relative=(0.0001, 0.0008),
        break_error_relative=(0.005, 0.05),
    )
    base.update(overrides)
    return build.Parameters(**base)


def _ledger(items):
    totals: dict = {}
    for item in items:
        key = (item.network, item.settlement_currency)
        totals[key] = totals.get(key, D(0)) + item.settlement_amount
    return totals


def _build(items, parameters, seed=1, columns=timeline.BASE_COLUMNS):
    return build.build(
        settlement_date=DAY,
        items=items,
        columns=columns,
        parameters=parameters,
        rng=random.Random(seed),
        ledger_totals=_ledger(items),
    )


def _lines(built) -> list[list[str]]:
    return list(csv.reader(io.StringIO(built.body.decode("utf-8"))))


def test_a_clean_file_has_the_specified_records_and_agrees_with_the_ledger() -> None:
    items = _items()
    lines = _lines(_build(items, _parameters()))
    assert lines[0][:4] == ["H", "NBKPROC", DAY.isoformat(), "01"]
    assert tuple(lines[1]) == timeline.BASE_COLUMNS
    details = [line for line in lines if line[0] == "D"]
    trailers = [line for line in lines if line[0] == "T"]
    assert len(details) == len(items) == 40
    assert lines[-1] == ["Z", "40"]
    assert len(trailers) >= 2
    ledger = _ledger(items)
    for _t, network, currency, _count, total in trailers:
        assert D(total) == ledger[(network, currency)]
    assert sum(int(t[3]) for t in trailers) == 40


def test_the_same_inputs_produce_the_same_bytes() -> None:
    parameters = _parameters(malformed_record_share=0.2, break_error_share=0.5)
    assert _build(_items(), parameters).body == _build(_items(), parameters).body


def test_a_malformed_record_still_counts_in_the_trailer_at_its_true_amount() -> None:
    items = _items()
    built = _build(items, _parameters(malformed_record_share=0.3), seed=4)
    assert len(built.manifest["malformed"]) >= 5
    trailers = [line for line in _lines(built) if line[0] == "T"]
    ledger = _ledger(items)
    for _t, network, currency, _count, total in trailers:
        assert D(total) == ledger[(network, currency)], "malformation must not be a break"
    assert built.manifest["breaks"] == []


def test_every_malformation_kind_damages_what_it_says() -> None:
    columns = timeline.BASE_COLUMNS
    row = build._row(_items()[1], D("12.2500"), columns)
    rng = random.Random(0)
    amount, where = build._malform(row, columns, build.UNPARSEABLE_AMOUNT, rng)
    assert where == "settlement_amount" and amount[columns.index("settlement_amount")].endswith(
        "EUR"
    )
    dated, _ = build._malform(row, columns, build.INVALID_DATE, rng)
    assert "-13-" in dated[columns.index("transaction_date")]
    blank, name = build._malform(row, columns, build.MISSING_REQUIRED_FIELD, rng)
    assert blank[columns.index(name)] == ""
    counts = set()
    for seed in range(20):
        damaged, _ = build._malform(row, columns, build.WRONG_FIELD_COUNT, random.Random(seed))
        counts.add(len(damaged))
    assert counts == {len(columns) - 1, len(columns) + 1}


@pytest.mark.parametrize("kind", ["warn", "error"])
def test_a_break_moves_the_trailer_off_the_ledger_at_its_intended_severity(kind) -> None:
    items = _items(120)
    share = {"break_warn_share": 1.0} if kind == "warn" else {"break_error_share": 1.0}
    built = _build(items, _parameters(**share), seed=9)
    assert len(built.manifest["breaks"]) >= 2
    for entry in built.manifest["breaks"]:
        assert entry["expected_severity"] == kind
        assert D(entry["file_total"]) - D(entry["ledger_total"]) == D(entry["delta"])


@pytest.mark.parametrize(
    ("file_total", "ledger_total", "expected"),
    [
        ("1000.0000", "1000.0000", None),
        ("1000.0000", "999.5000", "warn"),
        ("1000.0000", "999.0000", "error"),
        ("200000.0000", "199900.0000", "error"),
        ("-50.0000", "-50.0100", "warn"),
        ("0.0000", "0.0001", "error"),
    ],
)
def test_severity_follows_the_metric_definition(file_total, ledger_total, expected) -> None:
    assert build.severity(D(file_total), D(ledger_total)) == expected


def test_the_layout_follows_the_scripted_timeline() -> None:
    added, removed = timeline.EVENTS
    before = timeline.columns(added.fires_on(ANCHOR) - dt.timedelta(days=1), ANCHOR)
    after_add = timeline.columns(added.fires_on(ANCHOR), ANCHOR)
    after_remove = timeline.columns(removed.fires_on(ANCHOR), ANCHOR)
    assert before == timeline.BASE_COLUMNS
    assert after_add == (*timeline.BASE_COLUMNS, "acquirer_reference_number")
    assert "merchant_name" not in after_remove and "acquirer_reference_number" in after_remove


def test_a_late_file_is_delivered_three_days_after_its_settlement_date() -> None:
    late = [
        day
        for day in (ANCHOR + dt.timedelta(days=n) for n in range(200))
        if due_on(day, ANCHOR, 42, 0.05, 3)
        and not any(s == day for s, _ in due_on(day, ANCHOR, 42, 0.05, 3))
    ]
    held = [
        s
        for day in (ANCHOR + dt.timedelta(days=n) for n in range(200))
        for s, is_late in due_on(day, ANCHOR, 42, 0.05, 3)
        if is_late
    ]
    assert held, "no late file in two hundred days at a five per cent share"
    for settlement_date in held:
        on_time = [s for s, _ in due_on(settlement_date, ANCHOR, 42, 0.05, 3)]
        assert settlement_date not in on_time
    assert all(day >= ANCHOR for day in late)
    assert due_on(ANCHOR - dt.timedelta(days=1), ANCHOR, 42, 0.05, 3) == []


def test_the_acquirer_reference_number_is_23_digits_with_a_valid_check_digit() -> None:
    items = _items()
    numbers = [build.acquirer_reference_number(item) for item in items]
    assert len(numbers) >= 40
    assert all(len(n) == 23 and n.isdigit() for n in numbers)
    assert all(build._luhn(n[:-1]) == n[-1] for n in numbers)
    assert len(set(numbers)) == len(numbers)
    assert build._luhn("7992739871") == "3"


def test_adding_the_reference_number_moves_no_other_draw() -> None:
    """The additive field is derived, not drawn, so the file's defects do not move with it."""
    items = _items()
    parameters = _parameters(
        malformed_record_share=0.2, break_warn_share=0.3, break_error_share=0.3
    )
    before = _build(items, parameters, columns=timeline.BASE_COLUMNS).manifest
    added = (*timeline.BASE_COLUMNS, "acquirer_reference_number")
    after = _build(items, parameters, columns=added).manifest
    assert before["malformed"] and before["breaks"]
    assert (before["malformed"], before["breaks"]) == (after["malformed"], after["breaks"])


def test_the_straddling_late_file_is_sent_after_the_removal_in_the_new_layout() -> None:
    """R13: settlement date anchor + 43, sent anchor + 46, after the removal at anchor + 45."""
    _added, removed = timeline.EVENTS
    settlement = ANCHOR + dt.timedelta(days=timeline.STRADDLING_LATE_OFFSET)
    sent = settlement + dt.timedelta(days=3)
    assert settlement < removed.fires_on(ANCHOR) <= sent
    assert all(s != settlement for s, _ in due_on(settlement, ANCHOR, 42, 0.0, 3))
    assert (settlement, True) in due_on(sent, ANCHOR, 42, 0.0, 3)
    assert "merchant_name" in timeline.columns(settlement, ANCHOR)
    assert "merchant_name" not in timeline.columns(sent, ANCHOR)


def test_the_header_says_when_the_file_was_produced() -> None:
    produced = dt.datetime(2026, 8, 6, 5, 0, tzinfo=dt.UTC)
    built = build.build(
        settlement_date=DAY,
        items=_items(),
        columns=timeline.BASE_COLUMNS,
        parameters=_parameters(),
        rng=random.Random(1),
        ledger_totals={},
        created_at=produced,
    )
    header = next(csv.reader(io.StringIO(built.body.decode())))
    assert header == ["H", "NBKPROC", DAY.isoformat(), "01", "1", "2026-08-06T05:00:00Z", "1"]


def test_a_correction_is_revision_2_of_the_same_sequence_under_its_own_key() -> None:
    from generator.settlement.__main__ import file_key

    assert file_key(DAY) == "cardnet/NBK_CLR_20260803_01.csv"
    assert file_key(DAY, 2) == "cardnet/NBK_CLR_20260803_01_R2.csv"
    built = build.build(
        settlement_date=DAY,
        items=_items(),
        columns=timeline.BASE_COLUMNS,
        parameters=_parameters(),
        rng=random.Random(1),
        ledger_totals={},
        revision=2,
    )
    header = next(csv.reader(io.StringIO(built.body.decode())))
    assert header[3:5] == ["01", "2"] and built.manifest["revision"] == 2


def test_the_plan_corrects_a_broken_file_the_next_day_and_the_scripted_one_on_arrival(
    monkeypatch,
) -> None:
    from generator.settlement import __main__ as settlement

    section = {
        "late_file_share": 0.05,
        "late_by_days": 3,
        "correction_share": 1.0,
        "correction_lag_days": 1,
    }
    broken = {ANCHOR + dt.timedelta(days=4)}

    def fake_build(_cursor, settlement_date, **_kwargs):
        return build.Built(body=b"", manifest={"breaks": [1] if settlement_date in broken else []})

    monkeypatch.setattr(settlement, "build_file", fake_build)
    day_after = ANCHOR + dt.timedelta(days=5)
    planned = settlement.plan(None, day_after, anchor=ANCHOR, seed=42, section=section)
    assert settlement.Delivery(ANCHOR + dt.timedelta(days=4), False, 2) in planned

    scripted = ANCHOR + dt.timedelta(days=timeline.CORRECTED_ON_ARRIVAL_OFFSET)
    arrival = scripted + dt.timedelta(days=3)
    planned = settlement.plan(None, arrival, anchor=ANCHOR, seed=42, section=section)
    revisions = [d.revision for d in planned if d.settlement_date == scripted]
    assert revisions == [1, 2], "the scripted late file and its correction arrive together"
