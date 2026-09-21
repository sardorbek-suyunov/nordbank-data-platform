"""Accounts, the holder bridge, and cards.

Account rows are built here but written at the end of the run, because
`accounts.current_balance_amount` is a fold over the account's movements and is not known until
the movement pass has finished. Spec 003 invariant 4 reconciles the two, and the only way to
pass it reliably is to derive one from the other rather than draw both and hope. The cost is
that the account book is held in memory for the whole run; at the full profile that is a few
hundred thousand small arrays, which is affordable, while the movement stream is not.

Ownership weights sum to exactly one per account by construction: the primary holder takes
whatever the others leave, in Decimal. Drawing all of them and normalising afterwards would
leave a remainder at the eighth decimal place, and invariant 3 asks for exactly one.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal

from ..config import RunConfig
from ..realism import recurrence
from ..realism.calendar import at_time
from ..realism.distributions import bernoulli, cents, weighted_choice
from ..refdata import RefData
from ..rng import SubStreams
from ..spool import Spool


@dataclass
class AccountBook:
    """The deposit book, as parallel arrays indexed by account id minus one."""

    customer_id: list[int] = field(default_factory=list)
    account_type_code: list[str] = field(default_factory=list)
    product_class: list[str] = field(default_factory=list)
    currency_code: list[str] = field(default_factory=list)
    country_code: list[str] = field(default_factory=list)
    opened_date: list[dt.date] = field(default_factory=list)
    closed_date: list[dt.date | None] = field(default_factory=list)
    status_code: list[str] = field(default_factory=list)
    overdraft_limit: list[Decimal] = field(default_factory=list)
    balance: list[Decimal] = field(default_factory=list)
    opening_balance: list[Decimal] = field(default_factory=list)
    created_at: list[dt.datetime] = field(default_factory=list)
    updated_at: list[dt.datetime] = field(default_factory=list)
    account_number: list[str] = field(default_factory=list)
    iban: list[str | None] = field(default_factory=list)
    card_ids: list[list[int]] = field(default_factory=list)
    holders: list[list[int]] = field(default_factory=list)
    # The regular incoming credit, decided once per account rather than drawn each
    # month, so a customer's salary is the same amount on the same day as a real one is.
    # Zero day means no regular credit reaches this account.
    salary_day: list[int] = field(default_factory=list)
    salary_amount: list[Decimal] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.customer_id)


@dataclass
class CardBook:
    """Issued cards, as parallel arrays indexed by card id minus one."""

    account_id: list[int] = field(default_factory=list)
    product_code: list[str] = field(default_factory=list)
    issued_date: list[dt.date] = field(default_factory=list)
    expiry_date: list[dt.date] = field(default_factory=list)
    status_code: list[str] = field(default_factory=list)
    # The day the card stops being able to authorise: its expiry for a card that is still
    # active at the anchor, and the day it was blocked or cancelled for one that is not.
    #
    # A terminal status is a state the card reached *during* the history, and the movement pass
    # has to know when. Without it a card blocked in March goes on spending until September and
    # then stops dead at the anchor, because that is the only date the status is attached to —
    # which is a step in card volume on a date with no business meaning, and it is what the
    # mutation engine's first continuity comparison found. Generator-internal: the source has no
    # column for when a status changed, and inventing one would put a simulation detail in the
    # bank's schema.
    usable_until: list[dt.date] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.account_id)


def _iban(country_code: str, account_id: int, rng_digits: int) -> str:
    """A syntactically valid IBAN shape: two letters then eighteen digits.

    The check digits are not a real mod-97 checksum. That is deliberate and recorded as an
    unrealism: a valid checksum would make these look like usable bank identifiers, and nothing
    in the platform validates one.
    """
    return f"{country_code}{rng_digits:02d}{account_id:016d}"


def generate(
    config: RunConfig,
    ref: RefData,
    streams: SubStreams,
    spool: Spool,
    customer_country: list[str],
    customer_signup: list[dt.date],
) -> tuple[AccountBook, CardBook]:
    params = config.profile.params
    accounts_params = params["accounts"]
    cards_params = params["cards"]
    txn_params = params["transactions"]

    accounts = AccountBook()
    cards = CardBook()
    holder_rows = spool.table("account_holders")
    card_rows = spool.table("cards")
    holder_id = 0

    customer_count = len(customer_country)

    for customer_id in range(1, customer_count + 1):
        rng = streams.stream("accounts", customer_id)
        count = int(
            weighted_choice(
                rng, {str(k): float(v) for k, v in accounts_params["count_weights"].items()}
            )
        )
        country = customer_country[customer_id - 1]
        signup = customer_signup[customer_id - 1]

        for ordinal in range(count):
            account_id = len(accounts) + 1
            type_code = weighted_choice(rng, accounts_params["type_mix"])
            product_class = str(ref.row("account_types", type_code)["product_class_code"])

            # The first account opens with the customer; later ones open later. An account
            # whose opening date falls past the anchor has not been opened yet, so it does not
            # exist — it is not clamped onto the anchor. Clamping put 221 of 760 accounts on one
            # day at `ci`, against one to four on every other day, which gave a third of the
            # book no history at all and handed M4's first extraction a day that looks like a
            # migration.
            opened = signup + dt.timedelta(days=0 if ordinal == 0 else rng.randrange(14, 900))
            if opened > config.anchor:
                continue

            currency = "EUR"
            if bernoulli(rng, float(accounts_params["non_eur_share"])):
                currency = weighted_choice(rng, accounts_params["non_eur_mix"])

            overdraft = Decimal("0.0000")
            if product_class == "current" and bernoulli(
                rng, float(accounts_params["overdraft_share"])
            ):
                overdraft = cents(rng.choice(accounts_params["overdraft_limit_choices"]))

            opening_balance = cents(
                min(
                    250_000.0,
                    max(
                        0.0,
                        math.exp(
                            rng.gauss(
                                float(accounts_params["opening_balance_mu"]),
                                float(accounts_params["opening_balance_sigma"]),
                            )
                        ),
                    ),
                )
            )

            created_at = at_time(opened, rng.randrange(8, 20), rng.randrange(60), rng.randrange(60))

            accounts.customer_id.append(customer_id)
            accounts.account_type_code.append(type_code)
            accounts.product_class.append(product_class)
            accounts.currency_code.append(currency)
            accounts.country_code.append(country)
            accounts.opened_date.append(opened)
            accounts.closed_date.append(None)
            accounts.status_code.append("active")
            accounts.overdraft_limit.append(overdraft)
            # The balance starts at zero and the opening deposit arrives as a
            # transaction, because invariant 4 asserts that the balance IS the signed
            # sum of posted movements. A starting value that no movement explains would
            # make the invariant unsatisfiable by construction, and hiding it by adding
            # the opening balance to both sides would make the check assert nothing.
            accounts.balance.append(Decimal("0.0000"))
            accounts.opening_balance.append(opening_balance)
            accounts.created_at.append(created_at)
            accounts.updated_at.append(created_at)
            accounts.account_number.append(f"ACC{account_id:012d}")
            accounts.iban.append(_iban(country, account_id, rng.randrange(10, 99)))
            accounts.card_ids.append([])
            accounts.holders.append([customer_id])

            # On its own stream, keyed on the account, so that a tick can re-derive the same
            # income for the same account without replaying this loop. See
            # generator/realism/recurrence.py.
            salary_day, salary_amount = recurrence.regular_credit(
                streams.stream("recurrence.credit", account_id), txn_params, product_class
            )
            accounts.salary_day.append(salary_day)
            accounts.salary_amount.append(salary_amount)

            # The holder bridge. The primary holder takes the remainder so the weights sum to
            # exactly one, which is what invariant 3 asserts.
            joint = bernoulli(rng, float(accounts_params["joint_share"])) and customer_count > 1
            secondary_weights: list[tuple[int, Decimal | None]] = []
            if joint:
                other = rng.randrange(1, customer_count + 1)
                if other != customer_id:
                    if bernoulli(rng, float(accounts_params["signatory_share"])):
                        secondary_weights.append((other, None))
                    else:
                        share = Decimal(rng.randrange(20, 51)) / Decimal(100)
                        secondary_weights.append((other, share.quantize(Decimal("0.00000001"))))
                    accounts.holders[-1].append(other)

            allocated = sum(
                (weight for _, weight in secondary_weights if weight is not None),
                Decimal("0"),
            )
            primary_weight = (Decimal(1) - allocated).quantize(Decimal("0.00000001"))

            holder_id += 1
            holder_rows.write(
                (
                    holder_id,
                    account_id,
                    customer_id,
                    "primary",
                    primary_weight,
                    created_at,
                    created_at,
                    False,
                )
            )
            for other_id, weight in secondary_weights:
                holder_id += 1
                holder_rows.write(
                    (
                        holder_id,
                        account_id,
                        other_id,
                        "authorised_signatory" if weight is None else "joint",
                        weight,
                        created_at,
                        created_at,
                        False,
                    )
                )

            # Cards, on current accounts almost always and on savings almost never.
            card_share = float(cards_params["share_by_product_class"].get(product_class, 0.0))
            if bernoulli(rng, card_share):
                card_id = len(cards) + 1
                product_code = weighted_choice(rng, cards_params["product_mix"])
                issued = opened + dt.timedelta(days=rng.randrange(0, 10))
                if issued > config.anchor:
                    # Not yet issued, for the same reason an account past the anchor is not yet
                    # opened. The clamp put 138 of 464 cards on the anchor date.
                    continue
                expiry = dt.date(
                    issued.year + int(cards_params["validity_years"]),
                    issued.month,
                    min(issued.day, 28),
                )
                status = weighted_choice(rng, cards_params["status_mix"])
                if expiry <= config.anchor and status == "active":
                    status = "expired"

                # A card stops authorising at the earlier of two dates: its expiry, and the day
                # it reached a terminal status. Blocked and cancelled are reached on some day
                # between issue and the anchor, drawn uniformly because nothing in the model
                # says a block is more likely early or late in a card's life. Expired is not
                # drawn at all — it *is* the expiry.
                #
                # The `min` is load-bearing and only the `dev` profile shows it: a four-year
                # validity over six months of `ci` history means no card ever expires, so a
                # drawn date cannot exceed an expiry there. Over three years it can, and a card
                # transacting past its expiry is what invariant 2 refuses — 1,766 rows of it.
                usable_until = expiry
                if status not in ("active", "expired"):
                    span = (config.anchor - issued).days
                    drawn = (
                        issued + dt.timedelta(days=rng.randrange(1, span + 1))
                        if span > 0
                        else issued
                    )
                    usable_until = min(drawn, expiry)

                card_created = at_time(issued, rng.randrange(8, 20), rng.randrange(60), 0)

                cards.account_id.append(account_id)
                cards.product_code.append(product_code)
                cards.issued_date.append(issued)
                cards.expiry_date.append(expiry)
                cards.status_code.append(status)
                cards.usable_until.append(usable_until)
                accounts.card_ids[-1].append(card_id)

                card_rows.write(
                    (
                        card_id,
                        f"NB{card_id:010d}",
                        account_id,
                        product_code,
                        f"{400000 + (card_id % 99999):06d}",
                        f"{card_id % 10000:04d}",
                        status,
                        issued,
                        expiry,
                        card_created,
                        card_created,
                        False,
                    )
                )

    return accounts, cards


def write_accounts(accounts: AccountBook, spool: Spool) -> None:
    """Write the account rows, after the movement pass has folded the balances."""
    rows = spool.table("accounts")
    for index in range(len(accounts)):
        rows.write(
            (
                index + 1,
                accounts.account_number[index],
                accounts.iban[index],
                accounts.account_type_code[index],
                accounts.currency_code[index],
                accounts.country_code[index],
                accounts.status_code[index],
                accounts.opened_date[index],
                accounts.closed_date[index],
                accounts.overdraft_limit[index],
                accounts.balance[index],
                accounts.created_at[index],
                accounts.updated_at[index],
                False,
            )
        )
