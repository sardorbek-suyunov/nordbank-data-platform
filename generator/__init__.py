"""The Nordbank synthetic core banking generator.

Spec 003. Three inputs determine the output completely: the run seed, the anchor date, and the
profile. See `docs/generator_realism.md` for what the numbers mean and
`docs/adr/0011-copy-loader-with-explicit-identity-keys.md` for how the rows reach the database.
"""

from __future__ import annotations

# Recorded in every run manifest. Bump it when a change alters generated values, because the
# manifest is how a reader tells "the seed changed" from "the generator changed".
GENERATOR_VERSION = "1.0.0"

__all__ = ["GENERATOR_VERSION"]
