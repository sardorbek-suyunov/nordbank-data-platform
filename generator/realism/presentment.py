"""Whether the card was physically presented.

`core.transactions.is_card_present` exists as a column rather than as a derivation because the
channel does not determine presentment. A mobile wallet tap at a terminal is card present; an
ecommerce purchase from the same handset, on the same channel, is not. Deriving presentment
from the channel would make Q10 and Q19 measure the channel twice under two names.

Two things therefore decide it. The channel sets a base probability, because a point-of-sale
terminal is nearly always a presentment and an ecommerce checkout nearly never is. And the
card-not-present share rises across the history, because that is what happened to card
spending, so the base is scaled by how far through the history the transaction sits.
"""

from __future__ import annotations

import random
from typing import Any

from .distributions import bernoulli


def card_not_present_target(transactions: dict[str, Any], progress: float) -> float:
    """The intended card-not-present share at this point in the history, interpolated linearly.

    Linear rather than a curve: the trend is an assumption either way, and a straight line
    between two stated endpoints is one a reader can check against the parameters.
    """
    start = float(transactions["card_not_present_share_start"])
    end = float(transactions["card_not_present_share_end"])
    return start + (end - start) * progress


def baseline_present_share(transactions: dict[str, Any]) -> float:
    """The card-present share the channel mix alone would produce, over card-bearing types.

    Used as the denominator of the trend adjustment, so that the adjustment moves the aggregate
    to the target rather than moving each channel by an unrelated amount.
    """
    channel_mix_by_type = transactions["channel_mix_by_type"]
    present_by_channel = transactions["card_present_share_by_channel"]
    weighted, total = 0.0, 0.0
    for type_code, weight in transactions["type_mix"].items():
        mix = channel_mix_by_type.get(type_code)
        if mix is None:
            continue
        for channel, channel_weight in sorted(mix.items()):
            share = weight * channel_weight
            weighted += share * float(present_by_channel[channel])
            total += share
    return weighted / total if total else 1.0


def decide(
    rng: random.Random,
    transactions: dict[str, Any],
    channel_code: str,
    progress: float,
    baseline_present: float,
) -> bool:
    """Whether this card authorisation was presented.

    The channel's own share is scaled by the ratio between the card-present share the history
    should show at this point and the share the channel mix would give on its own. Because
    card-not-present rises, the scale factor is at most one and the clamp below never binds on
    the channels that sit near certainty.
    """
    base = float(transactions["card_present_share_by_channel"][channel_code])
    target_present = 1.0 - card_not_present_target(transactions, progress)
    scale = target_present / baseline_present if baseline_present > 0 else 1.0
    return bernoulli(rng, min(1.0, max(0.0, base * scale)))
