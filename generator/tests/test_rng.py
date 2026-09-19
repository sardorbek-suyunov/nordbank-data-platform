"""Sub-stream independence, which spec 003 makes a hard requirement.

The requirement is that adding a draw to one entity generator does not shift the values another
one produces. These tests prove it by actually adding a draw and diffing the output, rather than
by asserting that the construction looks right.

One thing the tests draw a line under, because the specification's phrasing predates a decision
that changed it. Spec 003 section 2 states the requirement as "a change to loan generation must
not alter a single transaction". That held while loan cash flows were their own kind of event.
It does not hold now, and cannot: invariant 4 requires `accounts.balance` to be the signed sum
of posted **transactions and payments**, and a loan disbursement moves the balance, so it has to
be one of those two. Changing loan generation therefore changes the loan transactions, and
through the interleaving it changes the ids of the transactions around them.

What holds — and what the requirement is actually protecting against — is that the *random
streams* stay independent. A change to loan generation does not shift the values any other
stream produces, and every entity that is not causally downstream of lending comes out
byte-identical. Both are tested below. The distinction is recorded in
`docs/generator_realism.md`.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pytest

from generator.config import RunConfig, load_profile
from generator.rng import SubStreams, derive_seed

SEED = 4242


def test_derive_seed_is_a_pure_function_of_its_inputs():
    first = derive_seed(SEED, "transactions", (17, "2026-03"))
    second = derive_seed(SEED, "transactions", (17, "2026-03"))
    assert first == second


def test_different_names_and_keys_give_different_streams():
    base = derive_seed(SEED, "transactions", (1,))
    assert derive_seed(SEED, "loans", (1,)) != base
    assert derive_seed(SEED, "transactions", (2,)) != base
    assert derive_seed(SEED + 1, "transactions", (1,)) != base


def test_coordinate_rendering_cannot_collide():
    """("ab", 1) and ("a", "b1") must not hash to the same stream.

    They would under naive concatenation, which is the reason the separator exists.
    """
    assert derive_seed(SEED, "x", ("ab", 1)) != derive_seed(SEED, "x", ("a", "b1"))


def test_a_stream_does_not_observe_draws_taken_from_another():
    """The property the whole design exists for.

    Take a sample from stream B. Then consume a large and arbitrary number of draws from stream
    A, which is what adding a parameter or a new decision to A's generator amounts to. B must be
    unchanged.
    """
    streams = SubStreams(SEED)
    expected = [streams.stream("transactions", 7).random() for _ in range(5)]

    noisy = streams.stream("loans", 7)
    for _ in range(1000):
        noisy.random()

    actual = [streams.stream("transactions", 7).random() for _ in range(5)]
    assert actual == expected


def test_stream_order_does_not_matter():
    """Taking streams in a different order gives the same values.

    A parent-consuming splitter would fail this: stream N's seed would depend on how many
    children were taken before it.
    """
    streams = SubStreams(SEED)
    forward = {
        name: streams.stream(name, 3).random()
        for name in ("customers", "accounts", "transactions", "loans")
    }
    backward = {
        name: streams.stream(name, 3).random()
        for name in ("loans", "transactions", "accounts", "customers")
    }
    assert forward == backward


def test_a_sub_stream_is_a_plain_seeded_random():
    stream = SubStreams(SEED).stream("x", 1)
    assert isinstance(stream, random.Random)
    assert stream.random() == random.Random(derive_seed(SEED, "x", (1,))).random()


@pytest.mark.integration
def test_changing_lending_leaves_unrelated_entities_byte_identical(tmp_path: Path, monkeypatch):
    """Add a draw to the lending generator and diff every entity that lending cannot cause.

    Marked as an integration test because it reads the seeded reference vocabularies from the
    database, which is where the generator gets them.
    """
    from generator import pipeline, refdata
    from generator.realism import lending as lending_model
    from generator.spool import Spool

    ref = refdata.load()
    config = RunConfig(profile=load_profile("ci"), seed=SEED, anchor=dt.date(2026, 9, 18))

    def generate_into(directory: Path) -> dict[str, str]:
        spool = Spool(directory)
        pipeline.generate(config, ref, spool)
        spool.close()
        return {
            path.stem: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.csv"))
        }

    before = generate_into(tmp_path / "before")

    original = lending_model.draw_product

    def draw_product_with_an_extra_draw(rng, lending):
        rng.random()  # the change being simulated: one more decision in loan generation
        return original(rng, lending)

    monkeypatch.setattr(lending_model, "draw_product", draw_product_with_an_extra_draw)
    after = generate_into(tmp_path / "after")

    # Entities lending cannot cause. These must be byte-identical.
    independent = (
        "customers",
        "customer_addresses",
        "merchants",
        "agent_locations",
        "account_holders",
        "cards",
    )
    for table in independent:
        assert before[table] == after[table], (
            f"{table} changed when only loan generation changed, which means its stream is not "
            f"independent of lending"
        )

    # And lending itself did change, or the test proved nothing.
    assert before["loans"] != after["loans"]
