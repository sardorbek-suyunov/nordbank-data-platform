"""What things cost.

Four populations, deliberately different in shape, and the difference is the point:

- **Card purchases** are log-normal per MCC band. This is the population Benford's law is
  expected to describe, and the one spec 003 invariant 14 asserts against it.
- **Cash withdrawals** cluster on note multiples. A machine dispenses notes, so the amounts
  are 20, 50, 100, 200 and almost nothing else.
- **Recurring payments** repeat one amount from a price list, on the same day each month.
- **Fees** are a short list of product prices.

The last three are anti-Benford by construction: round-number clustering piles mass on leading
digits 1, 2 and 5 and starves the rest, and a repeated price contributes its own leading digit
once per month forever. Both are properties a reviewer recognises as real, so the amount model
keeps them and the Benford assertion narrows to the population where the law is expected.
`docs/generator_realism.md` records the coupling and reports the composite distribution as an
observation.

A draw is a float; an amount is a Decimal. The conversion happens here, at the moment a number
stops being a sample and becomes money, and everything downstream — balance folds, ledger sums,
installment schedules — is Decimal arithmetic, per conventions.md.
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

from .distributions import bounded_lognormal, cents, weighted_pick


def purchase_amount(rng: random.Random, band_params: dict[str, Any]) -> Decimal:
    """A card purchase or merchant-facing amount, log-normal within the band's bounds."""
    return cents(
        bounded_lognormal(
            rng,
            band_params["mu"],
            band_params["sigma"],
            float(band_params["min"]),
            float(band_params["max"]),
        )
    )


def cash_amount(
    rng: random.Random, amounts: dict[str, Any], band_params: dict[str, Any]
) -> Decimal:
    """A cash withdrawal, snapped to a note multiple.

    The magnitude comes from the cash band's log-normal so that the size distribution is still
    right; the multiple is what the machine can actually dispense. A withdrawal that would
    round to zero is lifted to one multiple, because an ATM does not hand out nothing.
    """
    raw = bounded_lognormal(
        rng,
        band_params["mu"],
        band_params["sigma"],
        float(band_params["min"]),
        float(band_params["max"]),
    )
    multiple = int(weighted_pick(rng, amounts["cash_multiples"], amounts["cash_multiple_weights"]))
    units = max(1, int(round(raw / multiple)))
    return cents(units * multiple)


def transfer_amount(rng: random.Random, amounts: dict[str, Any]) -> Decimal:
    """A transfer or direct debit, which is not a merchant purchase and has its own shape."""
    return cents(
        bounded_lognormal(rng, amounts["transfer_mu"], amounts["transfer_sigma"], 1.0, 250_000.0)
    )


def payment_amount(rng: random.Random, payments: dict[str, Any]) -> Decimal:
    return cents(
        bounded_lognormal(rng, payments["amount_mu"], payments["amount_sigma"], 1.0, 500_000.0)
    )


def recurring_amount(rng: random.Random, amounts: dict[str, Any]) -> Decimal:
    """A subscription price, drawn once per mandate and then repeated unchanged."""
    return cents(rng.choice(amounts["recurring_amount_choices"]))


def fee_amount(rng: random.Random, amounts: dict[str, Any]) -> Decimal:
    return cents(rng.choice(amounts["fee_amount_choices"]))


def band_for_mcc(ref: Any, mcc_code: str) -> str:
    return str(ref.row("mcc_codes", mcc_code)["band_code"])


def is_benford_population(transaction_type_code: str, is_recurring: bool) -> bool:
    """Whether an amount belongs to the population invariant 14 asserts Benford against.

    Card purchases that are not recurring. Cash withdrawals are excluded because they are
    snapped to note multiples, and recurring payments because they repeat one price. Both
    exclusions are stated in the realism document rather than being a quiet filter that makes
    a check pass.
    """
    return transaction_type_code == "card_purchase" and not is_recurring
