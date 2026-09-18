"""Merchants and the partner agent network.

Both are sized per thousand customers rather than by a fixed count, so the ratio holds across
profiles. A fixed catalogue would give the full profile's quarter of a million customers the
same few hundred merchants the ci profile has, which would make every customer's spending look
like every other's and would collapse the unfamiliar-merchant signal the fraud model reads.

Merchant names are dirty on purpose. Spec 002 records that the acquirer's string is stored as
sent — casing, punctuation and trailing store or location noise included — so that conformance
in silver has real work rather than a cosmetic pass over already-clean values.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..config import RunConfig
from ..realism.calendar import at_time
from ..realism.distributions import bernoulli, weighted_choice
from ..refdata import RefData
from ..rng import SubStreams
from ..spool import Spool
from . import vocabulary as vocab


@dataclass
class MerchantBook:
    mcc_code: list[str] = field(default_factory=list)
    band_code: list[str] = field(default_factory=list)
    country_code: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.mcc_code)


@dataclass
class AgentBook:
    country_code: list[str] = field(default_factory=list)
    active_from: list[dt.date] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.country_code)


def _dirty(rng, name: str, city: str) -> str:
    template = vocab.DIRTY_SUFFIXES[rng.randrange(len(vocab.DIRTY_SUFFIXES))]
    noisy = name + template.format(store=rng.randrange(1, 999), city=city.upper())
    style = rng.randrange(3)
    if style == 0:
        return noisy.upper()
    if style == 1:
        return noisy.lower()
    return noisy


def generate_merchants(
    config: RunConfig, ref: RefData, streams: SubStreams, spool: Spool
) -> MerchantBook:
    params = config.profile.params
    merchants_params = params["merchants"]
    customer_country_mix = params["customers"]["country_mix"]

    count = max(
        1,
        int(config.profile.customers * int(merchants_params["per_thousand_customers"]) / 1000),
    )
    book = MerchantBook()
    rows = spool.table("merchants")
    created_at = at_time(config.history_start, 6, 0, 0)

    for merchant_id in range(1, count + 1):
        rng = streams.stream("merchants", merchant_id)
        mcc = weighted_choice(rng, merchants_params["mcc_mix"])
        mcc_row = ref.row("mcc_codes", mcc)
        band = str(mcc_row["band_code"])
        category = str(mcc_row["category"])

        # The merchant country distribution follows the customer country mix, with a small
        # non-EEA tail. Q4 can only price intra-EEA consumer volume, so that tail is the
        # bounded null exposure of the interchange mart.
        if bernoulli(rng, float(merchants_params["non_eea_share"])):
            country = weighted_choice(rng, merchants_params["non_eea_mix"])
        else:
            country = weighted_choice(rng, customer_country_mix)

        stem = vocab.MERCHANT_STEMS[rng.randrange(len(vocab.MERCHANT_STEMS))]
        suffixes = vocab.MERCHANT_SUFFIX_BY_CATEGORY.get(category, ("Trading",))
        clean = f"{stem} {suffixes[rng.randrange(len(suffixes))]}"
        cities = vocab.cities_for(country)
        city = cities[rng.randrange(len(cities))]
        name = (
            _dirty(rng, clean, city)
            if bernoulli(rng, float(merchants_params["dirty_name_share"]))
            else clean
        )

        book.mcc_code.append(mcc)
        book.band_code.append(band)
        book.country_code.append(country)

        rows.write((
            merchant_id,
            f"MER{merchant_id:010d}",
            name[:200],
            mcc,
            country,
            created_at,
            created_at,
            False,
        ))

    return book


def generate_agents(
    config: RunConfig, ref: RefData, streams: SubStreams, spool: Spool
) -> AgentBook:
    params = config.profile.params
    count = max(
        1,
        int(
            config.profile.customers
            * int(params["agent_locations"]["per_thousand_customers"])
            / 1000
        ),
    )
    book = AgentBook()
    rows = spool.table("agent_locations")
    created_at = at_time(config.history_start, 6, 0, 0)

    for agent_id in range(1, count + 1):
        rng = streams.stream("agent_locations", agent_id)
        country = weighted_choice(rng, params["customers"]["country_mix"])
        cities = vocab.cities_for(country)
        city = cities[rng.randrange(len(cities))]
        digits = vocab.postcode_digits(country)
        partner = vocab.AGENT_PARTNERS[rng.randrange(len(vocab.AGENT_PARTNERS))]
        active_from = config.history_start - dt.timedelta(days=rng.randrange(30, 900))

        book.country_code.append(country)
        book.active_from.append(active_from)

        rows.write((
            agent_id,
            f"AGT{agent_id:010d}",
            f"{partner} {city}",
            vocab.street_address(rng.randrange(64), rng.randrange(8), rng.randrange(1, 240)),
            city,
            str(rng.randrange(10 ** (digits - 1), 10**digits)),
            country,
            active_from,
            None,
            created_at,
            created_at,
            False,
        ))

    return book
