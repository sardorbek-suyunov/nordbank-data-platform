"""Acquisition, attrition, dormancy, and digital behaviour.

These decide how long a customer stays and how much they do while they are there, which is
what Q2's retention curve and Q19's device signal are read off.

**Attrition has to be real.** A book where nothing closes gives Q2 a retention curve that is a
flat line at 100 percent, which is not a measurement. The hazard is higher in the first year
than afterwards, which is the shape real attrition has.

**Dormancy is not closure and is modelled separately.** A dormant account is open, carries a
balance, and has stopped moving. Conflating the two would lose the distinction
`ref.account_statuses.is_open` exists to carry, and would make every inactive customer look
like a lost one.

**Devices are stable with occasional additions.** Q19 measures the share of activity from a
device the customer has not used in the trailing 90 days, so a customer with a new fingerprint
every session would make the measure meaningless in one direction and a customer with one
fingerprint forever would make it meaningless in the other.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
from typing import Any

from .calendar import growth_multiplier, month_weight
from .distributions import bernoulli, weighted_choice


def acquisition_weights(
    acquisition: dict[str, Any], month_weights: list[float], months: list[dt.date]
) -> list[float]:
    """Relative acquisition volume per month of the history.

    Two effects compose. A compound growth trend, so later cohorts are larger and Q1's growth
    measure has a trend to find. And seasonality, damped by `seasonality_weight` because
    signing up for a bank account is less seasonal than spending is.
    """
    damping = float(acquisition["seasonality_weight"])
    rate = float(acquisition["monthly_growth_rate"])
    out = []
    for index, month in enumerate(months):
        seasonal = 1.0 + (month_weight(month_weights, month) - 1.0) * damping
        out.append(growth_multiplier(rate, index) * seasonal)
    return out


def closes_this_month(
    rng: random.Random, attrition: dict[str, Any], months_since_opening: int
) -> bool:
    """Whether an open account closes in this month.

    A per-month hazard rather than a lifetime probability drawn at opening, so that the
    survival curve comes out of the process instead of being imposed on it.
    """
    hazard = (
        attrition["monthly_close_hazard_first_year"]
        if months_since_opening < 12
        else attrition["monthly_close_hazard_thereafter"]
    )
    return bernoulli(rng, float(hazard))


def becomes_dormant(rng: random.Random, attrition: dict[str, Any]) -> bool:
    return bernoulli(rng, float(attrition["monthly_dormancy_hazard"]))


def reactivates(rng: random.Random, attrition: dict[str, Any]) -> bool:
    return bernoulli(rng, float(attrition["dormancy_reactivation_hazard"]))


def device_count(rng: random.Random, digital: dict[str, Any]) -> int:
    weights = {str(k): float(v) for k, v in digital["devices_per_customer_weights"].items()}
    return int(weighted_choice(rng, weights))


def device_fingerprint(seed: int, customer_id: int, index: int) -> str:
    """A stable, opaque device fingerprint.

    Derived by hash rather than drawn, so that a customer's devices are a function of who they
    are and do not consume from any stream. The column is classified `identifier` and is
    tokenised at ingest, so its content only has to be stable and unique, not meaningful.
    """
    payload = f"{seed}:{customer_id}:{index}".encode()
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def session_channel(rng: random.Random, digital: dict[str, Any]) -> str:
    return weighted_choice(rng, digital["channel_mix"])


def session_outcome(rng: random.Random, digital: dict[str, Any]) -> str:
    return weighted_choice(rng, digital["outcome_mix"])


def ip_address(seed: int, customer_id: int, session_ordinal: int) -> str:
    """A stable synthetic IPv4 address.

    Drawn from the documentation ranges reserved by RFC 5737, so no address here can belong to
    anybody. Like the device fingerprint it is derived rather than drawn, and it is tokenised
    at ingest in any case.
    """
    digest = hashlib.blake2b(
        f"{seed}:ip:{customer_id}:{session_ordinal}".encode(), digest_size=4
    ).digest()
    # 203.0.113.0/24, 198.51.100.0/24 and 192.0.2.0/24 are the three TEST-NET blocks.
    blocks = (("203", "0", "113"), ("198", "51", "100"), ("192", "0", "2"))
    block = blocks[digest[0] % len(blocks)]
    return f"{block[0]}.{block[1]}.{block[2]}.{digest[3] % 254 + 1}"
