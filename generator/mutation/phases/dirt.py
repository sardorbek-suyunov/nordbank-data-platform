"""The dirt a constrained OLTP system genuinely emits.

`core` has foreign keys, check constraints and not-null constraints. An orphan in it is not
something this generator declines to produce, it is something the source **cannot express** —
the same shape of argument as ADR 0008, where bronze immutability is a property of key
construction rather than of restraint. Refusing to do something and being unable to say it are
different claims, and only the second one holds here. Genuine schema and type violations belong
to the file and API feeds at M4, where they can actually occur.

So what a tick produces is what a well-constrained system still emits:

- **Duplicate customer records.** The most valuable item, because it gives silver a real
  entity-resolution problem: the same person entered twice, both rows individually valid,
  detectable only by fuzzy matching. The duplicate carries the same date of birth and the same
  city, a name one edit away, and usually a different contact detail — which is what a second
  onboarding under a slightly misheard name produces.
- **Inconsistent casing and stray whitespace** in free-text fields. Nothing in a `varchar`
  rejects `" jan  KOWALSKI"`, and nothing should.
- **Unicode confusables and diacritic inconsistency** in names. `Müller` and `Muller` are the
  same customer and different strings, and a Cyrillic `е` in a Latin name is the same glyph and
  a different code point.
- **A KYC status the account activity contradicts**: `pending` or `expired` while the accounts
  are open and transacting normally. Both values are legal, every foreign key is satisfied, no
  constraint notices, and the combination is wrong in a way only a business rule can see. This
  replaces the income band inconsistent with occupation that spec 004 section 2 named, because
  neither of those columns exists.
- **A nullable column left null where business logic expects a value**: an email or a phone
  cleared on an account that is plainly active.

**A value can be malformed by the domain's rules while conforming to the column's.**
`core.payments.counterparty_iban` is checked against a shape, and nothing validates the mod-97
checksum — the generated IBANs carry none. That is a real validation failure for a contract to
catch at M4, produced by a source that violates no constraint, and it is the example to name
because a `varchar` cannot express the rule that would catch it.

**This phase only edits rows that existed before the tick began.** A row inserted and then
updated in one tick would be counted twice by the tick log and once by the window acceptance
criterion 9 reconciles against.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ...realism.distributions import bernoulli, weighted_choice
from ..tick import TickContext
from ..writer import apply_updates

# Latin letters and the Cyrillic code points that render identically in most fonts. A name
# carrying one of these is the same name to a reader and a different string to an equality join,
# which is exactly the case fuzzy matching at M5 has to survive.
CONFUSABLES = {"a": "а", "c": "с", "e": "е", "o": "о", "p": "р"}

# Latin letters and their diacritic forms, in both directions: a book holds both spellings of
# the same person and neither is wrong.
DIACRITICS = {"a": "ä", "o": "ö", "u": "ü", "e": "é", "s": "š", "z": "ž", "c": "ç", "n": "ñ"}

# KYC statuses that contradict an account that is open and transacting.
LAPSED_KYC = ("pending", "expired")


def run(context: TickContext) -> None:
    rates = context.profile.params["mutation"]["dirt"]
    day_start, _ = context.window
    _duplicate_customers(context, rates, day_start)
    _text_dirt(context, rates, day_start)
    _lapsed_kyc(context, rates, day_start)
    _cleared_contact(context, rates, day_start)


def _existing_customers(context: TickContext, day_start: dt.datetime, limit: int = 0) -> list:
    """Customers that existed before this tick, with what a duplicate would copy."""
    context.cursor.execute(
        """
        select c.customer_id, c.full_name, c.email, c.phone, c.date_of_birth,
               c.residence_country_code, c.risk_band_code
          from core.customers c
         where not c.is_deleted and c.created_at < %s
         order by c.customer_id
        """,
        (day_start,),
    )
    return context.cursor.fetchall()


def _duplicate_customers(context: TickContext, rates: dict, day_start: dt.datetime) -> None:
    """The same person, entered twice.

    The duplicate is an insert with no dependents: no account, no address, no session. That is
    what a second onboarding that never completed looks like, and it is also what makes the
    duplicate the only row the source may ever physically delete — a row nothing references
    cannot violate a foreign key, so the delete needs nothing relaxed and nothing deferred.
    """
    candidates = _existing_customers(context, day_start)
    if not candidates:
        return

    share = float(rates["duplicate_customer_daily_share"])
    day = context.simulated_date

    for customer_id, full_name, email, phone, date_of_birth, country, risk_band in candidates:
        rng = context.stream("dirt.duplicate", customer_id)
        if not bernoulli(rng, share):
            continue

        duplicate_id = context.snapshot.take_id("customers")
        stamp = context.phase_instant("dirt", context.bucket_of("dirt", duplicate_id))
        name = _near_miss(rng, full_name)
        context.writer.add(
            "customers",
            (
                duplicate_id,
                f"CUS{duplicate_id:010d}",
                name,
                # A second record usually carries one contact detail, not both, and often a
                # different one. That is what makes the match fuzzy rather than exact.
                email if bernoulli(rng, 0.35) else None,
                phone if bernoulli(rng, 0.45) else None,
                None,
                date_of_birth,
                weighted_choice(rng, context.profile.params["customers"]["kyc_status_mix"]),
                risk_band,
                day,
                country,
                stamp,
                stamp,
                False,
            ),
        )


def _near_miss(rng: Any, full_name: str) -> str:
    """A name one edit away: a confusable, a diacritic, a transposition or a dropped letter."""
    given, _, family = full_name.partition(" ")
    kind = rng.randrange(4)
    if kind == 0:
        return f"{given} {_swap(rng, family, CONFUSABLES)}"
    if kind == 1:
        return f"{given} {_swap(rng, family, DIACRITICS)}"
    if kind == 2 and len(family) > 3:
        cut = rng.randrange(1, len(family) - 1)
        return f"{given} {family[:cut]}{family[cut + 1]}{family[cut]}{family[cut + 2 :]}"
    if len(family) > 3:
        cut = rng.randrange(1, len(family) - 1)
        return f"{given} {family[:cut]}{family[cut + 1 :]}"
    return f"{given} {family}"


def _swap(rng: Any, word: str, table: dict[str, str]) -> str:
    positions = [index for index, letter in enumerate(word.lower()) if letter in table]
    if not positions:
        return word
    at = positions[rng.randrange(len(positions))]
    return word[:at] + table[word[at].lower()] + word[at + 1 :]


def _text_dirt(context: TickContext, rates: dict, day_start: dt.datetime) -> None:
    """Casing and whitespace, in the two free-text fields a human types."""
    share = float(rates["text_dirt_daily_share"])

    context.cursor.execute(
        """
        select c.customer_id, c.full_name from core.customers c
         where not c.is_deleted and c.created_at < %s
           and c.full_name = btrim(c.full_name)
         order by c.customer_id
        """,
        (day_start,),
    )
    ids: list[int] = []
    values: list[str] = []
    for customer_id, full_name in context.cursor.fetchall():
        rng = context.stream("dirt.text", customer_id)
        if not bernoulli(rng, share):
            continue
        ids.append(customer_id)
        values.append(_rough(rng, full_name))

    for instant, group in context.jitter_groups("dirt", range(len(ids)), key_of=lambda i: ids[i]):
        context.set_clock(instant)
        context.report.record_update(
            "customers",
            apply_updates(
                context.cursor,
                """
                update core.customers c
                   set full_name = d.value
                  from unnest(%s::bigint[], %s::text[]) as d(customer_id, value)
                 where c.customer_id = d.customer_id
                returning c.customer_id
                """,
                [ids[index] for index in group],
                [values[index] for index in group],
            ),
        )


def _rough(rng: Any, value: str) -> str:
    """Casing and whitespace as a keyboard produces them."""
    kind = rng.randrange(4)
    if kind == 0:
        return value.upper()
    if kind == 1:
        return value.lower()
    if kind == 2:
        return f"  {value} "
    given, _, family = value.partition(" ")
    return f"{given}  {family}"


def _lapsed_kyc(context: TickContext, rates: dict, day_start: dt.datetime) -> None:
    """A KYC status the account behind it contradicts.

    Verified, with open accounts transacting normally, and a status that says the review lapsed.
    Every value is legal and every foreign key holds; the combination is wrong in a way only a
    business rule can see, which is the property this case was chosen for. It is realistic too:
    review cycles lapse without the bank freezing the account the same day.
    """
    share = float(rates["lapsed_kyc_daily_share"])
    context.cursor.execute(
        """
        select c.customer_id from core.customers c
          join core.account_holders ah on ah.customer_id = c.customer_id
          join core.accounts a on a.account_id = ah.account_id
          join ref.account_statuses s on s.code = a.account_status_code
         where c.kyc_status_code = 'verified' and s.is_open and a.account_status_code = 'active'
           and not c.is_deleted and c.created_at < %s
         group by c.customer_id
         order by c.customer_id
        """,
        (day_start,),
    )
    ids: list[int] = []
    values: list[str] = []
    for (customer_id,) in context.cursor.fetchall():
        rng = context.stream("dirt.kyc", customer_id)
        if not bernoulli(rng, share):
            continue
        ids.append(customer_id)
        values.append(LAPSED_KYC[rng.randrange(len(LAPSED_KYC))])

    for instant, group in context.jitter_groups("dirt", range(len(ids)), key_of=lambda i: ids[i]):
        context.set_clock(instant)
        context.report.record_update(
            "customers",
            apply_updates(
                context.cursor,
                """
                update core.customers c
                   set kyc_status_code = d.value
                  from unnest(%s::bigint[], %s::text[]) as d(customer_id, value)
                 where c.customer_id = d.customer_id
                returning c.customer_id
                """,
                [ids[index] for index in group],
                [values[index] for index in group],
            ),
        )


def _cleared_contact(context: TickContext, rates: dict, day_start: dt.datetime) -> None:
    """A nullable column left null where the business expects a value.

    A back-office correction that removed a bounced email and never replaced it. The column is
    nullable, so nothing refuses it; what notices is a mart that counts contactable customers.
    """
    share = float(rates["cleared_contact_daily_share"])
    context.cursor.execute(
        """
        select c.customer_id from core.customers c
         where c.email is not null and not c.is_deleted and c.created_at < %s
         order by c.customer_id
        """,
        (day_start,),
    )
    ids = [
        customer_id
        for (customer_id,) in context.cursor.fetchall()
        if bernoulli(context.stream("dirt.contact", customer_id), share)
    ]
    for instant, group in context.jitter_groups("dirt", ids, key_of=lambda i: i):
        context.set_clock(instant)
        context.report.record_update(
            "customers",
            apply_updates(
                context.cursor,
                "update core.customers set email = null "
                "where customer_id = any(%s::bigint[]) returning customer_id",
                group,
            ),
        )
