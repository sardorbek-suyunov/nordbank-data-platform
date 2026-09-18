"""The seeded random source and the sub-streams derived from it.

Spec 003 makes sub-stream independence a hard requirement: adding a new entity generator, or
adding a draw to an existing one, must not shift a single value produced by any other. That
rules out the obvious design, and the reason is worth stating because the obvious design looks
correct.

**The failure mode.** Anything that derives stream N by consuming from a shared parent — one
`Random` threaded through the generators, or a splitter that draws a child seed from a parent
as each generator starts — makes stream N depend on how many draws streams 1 to N-1 took
before it. Add one draw to loan generation and every transaction downstream of it changes.
Nothing fails, nothing warns, and the manifest hash moves for a change that touched no
transaction code.

**What is done instead.** A stream's seed is a keyed hash of the run seed, the stream's name,
and the coordinates of the thing being generated. It is a pure function of those inputs, so it
cannot observe the order generators run in, how many draws any other stream took, or the
iteration order of any container. Independence is a property of the construction rather than
a discipline, and `generator/tests/test_rng.py` proves it by adding a draw and diffing.

The generator is `random.Random`, the standard library Mersenne Twister, and not a third-party
one. The committed `ci` manifest makes determinism a CI-enforced invariant, and a determinism
guarantee that a routine dependency bump can break is not a guarantee. See ADR 0011.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable

__all__ = ["SEED_BYTES", "SubStreams", "derive_seed"]

# blake2b accepts a key of up to 64 bytes; 8 is the width of the run seed.
SEED_BYTES = 8


def _seed_key(seed: int) -> bytes:
    """The run seed as blake2b key material, for any integer a CLI can supply."""
    return (seed & 0xFFFF_FFFF_FFFF_FFFF).to_bytes(SEED_BYTES, "big")


def derive_seed(seed: int, name: str, key: Iterable[object] = ()) -> int:
    """Seed for the sub-stream `name` at coordinates `key`, under run seed `seed`.

    The coordinates are rendered positionally and separated by a byte that cannot appear in
    their decimal or ASCII rendering, so ("ab", 1) and ("a", "b1") cannot collide.
    """
    payload = b"\x00".join([name.encode("utf-8"), *(str(part).encode("utf-8") for part in key)])
    digest = hashlib.blake2b(payload, digest_size=SEED_BYTES, key=_seed_key(seed)).digest()
    return int.from_bytes(digest, "big")


class SubStreams:
    """A factory for independent, reproducible sub-streams of one run seed.

    Deriving a stream is a hash, so it is not free. Callers take a stream at the coarsest grain
    that still preserves independence — per entity instance, or per account and month for the
    movement generators — rather than one per row.
    """

    __slots__ = ("_seed",)

    def __init__(self, seed: int) -> None:
        self._seed = int(seed)

    @property
    def seed(self) -> int:
        return self._seed

    def stream(self, name: str, *key: object) -> random.Random:
        """A `random.Random` for `name` at `key`, identical on every run of this seed."""
        return random.Random(derive_seed(self._seed, name, key))

    def __repr__(self) -> str:
        return f"SubStreams(seed={self._seed})"
