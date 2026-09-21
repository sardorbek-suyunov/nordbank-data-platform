"""Identifier tokenisation (spec 005 section 6, ADR 0005).

A token is a keyed hash of the raw value, so the same value produces the same token in every
entity, every column and every batch, and the joins that used to run on the cleartext still
run. The reversible mapping lives in `meta.pii_vault` and nowhere else.

Three properties the rest of the platform depends on, stated here because they are easy to
break by accident:

- **The token is a function of the value alone.** Not of the column, not of the entity, not of
  the batch. Salting per column would make `customers.full_name` and `payments.counterparty_name`
  hash the same person to two tokens, and the 949 payments in the `ci` book that pay a Nordbank
  customer would stop joining.
- **A null stays null.** A null identifier has nothing to protect and produces no token and no
  vault row. `core.transactions.counterparty_reference` is classified `identifier` and is null
  in every row this source produces, so it contributes nothing to the vault at all.
- **The salt cannot be rotated.** Rotation changes every token, so it changes every join key
  derived from one, and the migration is: re-read every source entity under the new salt,
  re-tokenise the vault, and rebuild silver and gold from bronze written under the old salt,
  which is not possible without the old salt. In practice rotation means re-ingesting from the
  source. ADR 0005 records it as a known limitation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

SALT_VARIABLE = "PII_TOKEN_SALT"

# 128 bits of a SHA-256 HMAC, rendered as 32 hexadecimal characters. Full width would be 64,
# which doubles the size of every identifier column in bronze for collision resistance nothing
# here needs: at the `full` profile's order of a million distinct identifiers, the probability
# of any collision across 128 bits is around 10^-27.
TOKEN_WIDTH = 32


class TokeniserError(RuntimeError):
    """The salt is absent or empty. Never defaulted: a default salt is no salt."""


@dataclass(frozen=True)
class Tokeniser:
    salt: bytes

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> Tokeniser:
        source = os.environ if environ is None else environ
        raw = source.get(SALT_VARIABLE, "")
        if not raw.strip():
            raise TokeniserError(
                f"{SALT_VARIABLE} is not set. It is the key material for identifier "
                "tokenisation and has no default, because a default would be a published key."
            )
        return cls(salt=raw.encode("utf-8"))

    def token(self, value: object) -> str | None:
        """The token for one raw value, or None for a null.

        Non-string values are rendered with `str` before hashing. Every column classified
        `identifier` in this source is a character type, so the branch is a guard rather than a
        behaviour, and it is deterministic either way.
        """
        if value is None:
            return None
        material = value if isinstance(value, str) else str(value)
        digest = hmac.new(self.salt, material.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:TOKEN_WIDTH]

    def tokenise_row(self, row: dict, columns: tuple[str, ...]) -> tuple[dict, dict[str, str]]:
        """Replace each named column's value with its token.

        Returns the rewritten row and the raw-to-token pairs it produced, so a caller that has
        a legitimate place to put them can. The extract task has none — it must not carry
        cleartext to another task — and discards them; the register step re-reads the raw
        values from the source instead, under the window the registry recorded.
        """
        rewritten = dict(row)
        seen: dict[str, str] = {}
        for column in columns:
            if column not in rewritten:
                continue
            raw = rewritten[column]
            token = self.token(raw)
            rewritten[column] = token
            if token is not None:
                seen[str(raw)] = token
        return rewritten, seen
