"""The generation run: which generator runs when, and why that is the order.

The order is not arbitrary and is not just the foreign keys.

1. **People**, then **accounts, holders and cards**, then the **merchant and agent catalogue**.
   Nothing here depends on anything later.
2. **Lending**, before the movements. A disbursement credits the customer's account and every
   installment debits it, so those cash flows have to exist before the balance fold runs or
   `accounts.balance` will not reconcile against them.
3. **Movements**, which fold the balances, post the ledger inline, and emit the fraud alerts and
   login sessions that hang off individual transactions.
4. **Account rows last**, because their closing balance, final status, closing date and
   `updated_at` are all outputs of the fold rather than inputs to it.

The file write order is not the load order: `generator/tables.py` holds that, and the loader
copies parents before children regardless of when their rows were spooled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import RunConfig
from .entities import catalogue, deposits, lending, movements, people
from .refdata import RefData
from .rng import SubStreams
from .spool import Spool


@dataclass
class RunResult:
    """Everything a manifest, a report or an invariant run needs from one generation."""

    counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    stats: Any = None
    loans: Any = None
    accounts: Any = None
    customers: Any = None


def generate(config: RunConfig, ref: RefData, spool: Spool) -> RunResult:
    started = time.perf_counter()
    streams = SubStreams(config.seed)

    customers = people.generate(config, ref, streams, spool)
    accounts, cards = deposits.generate(
        config, ref, streams, spool, customers.country_code, customers.signup_date
    )
    merchants = catalogue.generate_merchants(config, ref, streams, spool)
    agents = catalogue.generate_agents(config, ref, streams, spool)

    loans = lending.generate(
        config,
        ref,
        streams,
        spool,
        customers.signup_date,
        customers.risk_band_code,
        accounts.customer_id,
        accounts.product_class,
        accounts.currency_code,
        accounts.opened_date,
    )

    stats = movements.generate(
        config,
        ref,
        streams,
        spool,
        customers,
        accounts,
        cards,
        merchants,
        agents,
        loans.cash_events,
        loans.hold_until,
    )

    deposits.write_accounts(accounts, spool)
    spool.close()

    return RunResult(
        counts=spool.counts(),
        seconds=time.perf_counter() - started,
        stats=stats,
        loans=loans,
        accounts=accounts,
        customers=customers,
    )


def spool_root(profile_name: str, base: Path | None = None) -> Path:
    """Where a run's CSV files land.

    Under the repository's gitignored `data/` directory rather than the system temp directory,
    so that a failed run leaves its spool behind to look at instead of deleting the evidence.
    """
    root = base or Path(__file__).resolve().parent.parent / "data" / "generator"
    return root / profile_name
