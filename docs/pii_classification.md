# PII classification

Four classes. Every column in every contract under `contracts/` carries exactly one of them
from M2 onward, and the classification is what the tokenisation, generalisation and erasure
rules dispatch on. A column with no classification fails the contract check; there is no
default, because the default would silently be the least protective one.

| Class | What it is | Handling |
|---|---|---|
| `identifier` | Singles out a person directly: full name, national identifier, email address, phone number, IBAN, card PAN, device fingerprint | Tokenised in the extraction task with a keyed hash, before anything is written to the lake. The token-to-value mapping goes to the vault in `meta`. Quarantine stores the token, never the cleartext. Erasable: deleting the vault entry is the erasure |
| `quasi-identifier` | Does not identify alone, but does in combination: date of birth, postcode, address, signup date, employer, IP address | Retained in the clear in bronze and silver, because generalising needs the underlying value. Generalised before gold: age band, country and region, tenure band. Never exposed raw in gold or in any export |
| `sensitive` | Special category or otherwise restricted, and meaningful only at full precision: KYC risk rating, sanctions match detail, fraud disposition notes, income | Retained at full precision, access-restricted at the schema level, and not generalised, because banding it would destroy the analysis it exists for. Exposed in gold only in aggregate |
| `non-personal` | Everything else: amounts, currencies, product codes, MCC, timestamps of system events, merchant identity | No restriction |

## Why date of birth is the case that proves the distinction

Date of birth is the obvious candidate for tokenisation, and tokenising it would be a mistake.

A keyed hash destroys order and distance. From a hashed date of birth you cannot compute an
age, so you cannot band it, so cohort analysis by age band and every credit risk cut that
depends on it become impossible. The same hash is also a poor protection: date of birth has
roughly thirty thousand plausible values, so an attacker with the key-less hashes and a
calendar can rebuild the mapping by brute force in seconds.

So date of birth is a quasi-identifier, not an identifier. It stays in the clear where the
platform controls access, it is generalised to an age band before it reaches gold, and the
protection comes from generalisation and access control rather than from a hash that would
have been both useless analytically and weak in practice.

The general rule follows from that example. Tokenise what needs to be joinable and never
needs to be interpreted. Generalise what needs to be interpreted but not identified.

## Interaction with erasure

Only `identifier` columns are erasable by crypto-shredding, because only they go through the
vault (ADR 0005).

A quasi-identifier is not reachable that way: an age band and a country survive erasure, and
that is intended, since what remains after the identifiers are unresolvable is not personal
data. The obligation this places on the model is that generalisation must be coarse enough
that a combination of surviving quasi-identifiers does not re-identify a subject. Bands are
chosen with that in mind, and the check belongs with the governance work at M8.

`sensitive` columns are erased along with their subject when the subject is erased, because
they are only meaningful when attached to one.
