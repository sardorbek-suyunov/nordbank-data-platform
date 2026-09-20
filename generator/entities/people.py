"""Customers and their address history.

Customers arrive in cohorts across the history, plus an opening book that predates it. A bank
whose history starts at zero customers has no early cohort to retain, so Q2's twelve-month
retention curve would have nothing to measure for its first year.

Everything about customer *i* derives from `substream("customers", i)`. Cohort sizing is the
one exception and takes a stream of its own, used once: if it drew from the same stream as the
customers it sizes, adding a customer attribute would change how many customers there are.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..config import RunConfig
from ..realism import lifecycle
from ..realism.calendar import UTC, at_time
from ..realism.distributions import bernoulli, triangular_int, weighted_choice
from ..refdata import RefData
from ..rng import SubStreams
from ..spool import Spool
from . import vocabulary as vocab


@dataclass
class CustomerBook:
    """What later generators need to know about the people, held as parallel arrays.

    Tuples and lists rather than one object per customer: the full profile holds a quarter of a
    million of these at once, alongside the accounts and cards that hang off them.
    """

    signup_date: list[dt.date] = field(default_factory=list)
    country_code: list[str] = field(default_factory=list)
    risk_band_code: list[str] = field(default_factory=list)
    date_of_birth: list[dt.date] = field(default_factory=list)
    kyc_status_code: list[str] = field(default_factory=list)
    device_count: list[int] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.signup_date)

    def ids(self) -> range:
        return range(1, len(self) + 1)


def _cohort_sizes(config: RunConfig, streams: SubStreams) -> list[tuple[dt.date, int]]:
    """How many customers sign up in each month, plus the opening book before the window.

    The remainder from the proportional split is given to the last month rather than dropped,
    so the profile's customer count is exact and acceptance criterion 1's row count band is
    about the generator rather than about rounding.
    """
    params = config.profile.params
    months = config.months
    weights = lifecycle.acquisition_weights(
        params["acquisition"], params["transactions"]["month_weights"], months
    )
    initial = int(config.profile.customers * float(params["acquisition"]["initial_customer_share"]))
    remaining = config.profile.customers - initial

    # The first and last months of the window are partial, so their weight is the share of the
    # month the window actually covers. Without it the last month acquires a full month of
    # customers into however many days remain before the anchor, which is an acquisition spike
    # on the last day of history rather than a trend.
    weights = [
        weight * _window_fraction(month, config)
        for month, weight in zip(months, weights, strict=True)
    ]

    total_weight = sum(weights)
    sizes = [int(remaining * weight / total_weight) for weight in weights]
    sizes[sizes.index(max(sizes))] += remaining - sum(sizes)

    # The opening book signs up before the window, spread over the two years before it, so the
    # oldest cohorts differ in age rather than all starting on the same day.
    rng = streams.stream("acquisition")
    opening: list[tuple[dt.date, int]] = []
    window_start = config.history_start
    for _ in range(initial):
        offset = rng.randrange(1, 730)
        opening.append((window_start - dt.timedelta(days=offset), 1))

    cohorts: list[tuple[dt.date, int]] = opening
    for month, size in zip(months, sizes, strict=True):
        if size > 0:
            cohorts.append((month, size))
    return cohorts


def _window_fraction(month: dt.date, config: RunConfig) -> float:
    """The share of `month` that falls inside the history window."""
    from ..realism.calendar import days_in_month  # noqa: PLC0415 - one call site

    span = days_in_month(month.year, month.month)
    first = max(month.replace(day=1), config.history_start)
    last = min(month.replace(day=span), config.anchor)
    if last < first:
        return 0.0
    return ((last - first).days + 1) / span


def _signup_dates(config: RunConfig, streams: SubStreams) -> list[dt.date]:
    """One signup date per customer, ordered oldest first so customer ids follow time."""
    rng = streams.stream("acquisition", "days")
    dates: list[dt.date] = []
    for month, size in _cohort_sizes(config, streams):
        if size == 1 and month < config.history_start:
            dates.append(month)
            continue
        from ..realism.calendar import days_in_month

        span = days_in_month(month.year, month.month)
        # Drawn inside the part of the month the history covers, rather than across the whole
        # month and then clamped. Clamping put every signup that landed past the anchor onto the
        # anchor itself: measured at `ci`, 25 of 500 customers signed up on one day.
        first = max(month.replace(day=1), config.history_start)
        last = min(month.replace(day=span), config.anchor)
        available = (last - first).days + 1
        if available <= 0:
            continue
        for _ in range(size):
            dates.append(first + dt.timedelta(days=rng.randrange(available)))
    dates.sort()
    return dates[: config.profile.customers]


def generate(config: RunConfig, ref: RefData, streams: SubStreams, spool: Spool) -> CustomerBook:
    params = config.profile.params
    customers = params["customers"]
    digital = params["digital"]
    book = CustomerBook()

    customer_rows = spool.table("customers")
    address_rows = spool.table("customer_addresses")
    address_id = 0

    for customer_id, signup in enumerate(_signup_dates(config, streams), start=1):
        rng = streams.stream("customers", customer_id)

        age = triangular_int(
            rng,
            int(customers["age_at_signup_min"]),
            int(customers["age_at_signup_mode"]),
            int(customers["age_at_signup_max"]),
        )
        # Born `age` years before signup, offset within the year so birthdays are spread.
        birth_year = signup.year - age
        date_of_birth = dt.date(birth_year, 1, 1) + dt.timedelta(days=rng.randrange(365))
        if date_of_birth >= signup:
            date_of_birth = signup - dt.timedelta(days=365 * 18 + 1)

        country = weighted_choice(rng, customers["country_mix"])
        risk_band = weighted_choice(rng, customers["risk_band_mix"])
        kyc_status = weighted_choice(rng, customers["kyc_status_mix"])

        given = vocab.GIVEN_NAMES[rng.randrange(len(vocab.GIVEN_NAMES))]
        family = vocab.FAMILY_NAMES[rng.randrange(len(vocab.FAMILY_NAMES))]
        full_name = f"{given} {family}"

        email = (
            f"{given.lower()}.{family.lower()}{customer_id}@example.invalid"
            if bernoulli(rng, float(customers["email_share"]))
            else None
        )
        phone = (
            f"+{rng.randrange(30, 49)}{rng.randrange(100000000, 999999999)}"
            if bernoulli(rng, float(customers["phone_share"]))
            else None
        )
        national_identifier = (
            f"{country}-{rng.randrange(10**8, 10**9 - 1)}"
            if bernoulli(rng, float(customers["national_identifier_share"]))
            else None
        )

        created_at = at_time(signup, rng.randrange(7, 22), rng.randrange(60), rng.randrange(60))
        book.signup_date.append(signup)
        book.country_code.append(country)
        book.risk_band_code.append(risk_band)
        book.date_of_birth.append(date_of_birth)
        book.kyc_status_code.append(kyc_status)
        # On its own addressable stream rather than from this customer's general stream.
        # The M3 mutation engine needs the same number to pick a device for a session, and
        # the alternative is reading it back: `count(distinct device_fingerprint)` over
        # core.login_sessions measures 3.0 s on the dev book, which is a third of a tick's
        # whole budget and grows with the history. Consuming it here would also make the
        # value depend on how many draws precede it in this loop, so adding a customer
        # attribute would silently move every device count.
        book.device_count.append(
            lifecycle.device_count(streams.stream("devices", customer_id), digital)
        )

        customer_rows.write(
            (
                customer_id,
                f"CUS{customer_id:010d}",
                full_name,
                email,
                phone,
                national_identifier,
                date_of_birth,
                kyc_status,
                risk_band,
                signup,
                country,
                created_at,
                created_at,
                False,
            )
        )

        cities = vocab.cities_for(country)
        digits = vocab.postcode_digits(country)
        city = cities[rng.randrange(len(cities))]
        postal_code = str(rng.randrange(10 ** (digits - 1), 10**digits))

        address_id += 1
        address_rows.write(
            (
                address_id,
                customer_id,
                "residential",
                vocab.street_address(rng.randrange(64), rng.randrange(8), rng.randrange(1, 240)),
                None,
                city,
                postal_code,
                country,
                signup,
                None,
                created_at,
                created_at,
                False,
            )
        )

        if bernoulli(rng, float(customers["correspondence_address_share"])):
            other_city = cities[rng.randrange(len(cities))]
            address_id += 1
            address_rows.write(
                (
                    address_id,
                    customer_id,
                    "correspondence",
                    vocab.street_address(
                        rng.randrange(64), rng.randrange(8), rng.randrange(1, 240)
                    ),
                    f"Flat {rng.randrange(1, 40)}" if bernoulli(rng, 0.4) else None,
                    other_city,
                    str(rng.randrange(10 ** (digits - 1), 10**digits)),
                    country,
                    signup,
                    None,
                    created_at,
                    created_at,
                    False,
                )
            )

    return book


def first_login_window(signup: dt.date) -> dt.datetime:
    return dt.datetime(signup.year, signup.month, signup.day, 9, 0, 0, tzinfo=UTC)
