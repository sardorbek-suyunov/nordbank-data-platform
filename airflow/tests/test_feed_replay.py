"""CI's FX feed answers from recordings, through the feed's own path, and never guesses."""

import datetime as dt
import functools
from pathlib import Path

import pytest
from data_contract import load_history
from nordbank_ops.feeds import fx
from nordbank_ops.feeds.fetch import RetryPolicy, fetch
from nordbank_ops.feeds.replay import VARIABLE, NoRecordingError, Replay, from_environment

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "feeds"
CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
# The CI window, whose every date must have a recording (spec 007 section 8).
WINDOW = [dt.date(2026, 7, 20) + dt.timedelta(days=n) for n in range(7)]
UNROUTABLE = "http://192.0.2.1/v1"


def _fetcher():
    return functools.partial(
        fetch, policy=RetryPolicy(max_attempts=1), get=Replay(FIXTURES), sleep=lambda _s: None
    )


def test_every_date_of_the_ci_window_is_served_through_the_feeds_own_path():
    contract = load_history(CONTRACTS / "ecb")["fx_rates"][-1]
    fetched = fx.fetch_dates(WINDOW, contract, base_url=UNROUTABLE, fetch=_fetcher())
    assert fetched.failure is None
    outcomes = {o.key: o.outcome for o in fetched.outcomes}
    assert len(outcomes) == 7
    landed = [day for day, outcome in outcomes.items() if outcome == fx.LANDED]
    assert len(landed) == 5
    # The weekend answers with Friday's date and lands nothing, exactly as the live API does.
    assert outcomes["2026-07-25"] != fx.LANDED and outcomes["2026-07-26"] != fx.LANDED
    assert len(fetched.parsed.records) >= 5 * 20


def test_a_request_with_no_recording_raises_rather_than_being_answered():
    replay = Replay(FIXTURES)
    with pytest.raises(NoRecordingError):
        replay(f"{UNROUTABLE}/2026-08-03", params={"base": "EUR"})


def test_unset_is_the_live_path_and_an_empty_directory_is_refused(tmp_path):
    assert from_environment({}) is None
    assert from_environment({VARIABLE: "  "}) is None
    with pytest.raises(NoRecordingError):
        from_environment({VARIABLE: str(tmp_path)})
