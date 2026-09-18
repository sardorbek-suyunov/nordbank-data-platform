# PII classification

Five classes. Every column in every contract under `contracts/` carries exactly one of them
from M2 onward, and the classification is what the tokenisation, generalisation and erasure
rules dispatch on. A column with no classification fails the contract check; there is no
default, because the default would silently be the least protective one.

| Class | What it is | Handling |
|---|---|---|
| `identifier` | Singles out a person directly: full name, national identifier, email address, phone number, IBAN, card PAN, device fingerprint, IP address, address lines, a payment counterparty name | Tokenised in the extraction task with a keyed hash, before anything is written to the lake. The token-to-value mapping goes to the vault in `meta`. Quarantine stores the token, never the cleartext. Erasable: deleting the vault entry is the erasure |
| `quasi-identifier` | Does not identify alone, but does in combination: date of birth, city, postal code, country, signup date, employer | Retained in the clear in bronze and silver, because generalising needs the underlying value. Generalised before gold: age band, country and region, tenure band. Never exposed raw in gold or in any export |
| `sensitive` | Special category or otherwise restricted, and meaningful only at full precision: KYC risk rating, sanctions match detail, fraud disposition notes, income | Retained at full precision, access-restricted at the schema level, and not generalised, because banding it would destroy the analysis it exists for. Exposed in gold only in aggregate |
| `pseudonymous_key` | A key that identifies a person only by reference within the platform: `customers.customer_id` and every foreign key that points at a person-bearing entity | Retained unchanged in every layer, because it is the join path. Never tokenised, because it is the pseudonym rather than the identifier. Its personal character is neutralised not by transforming it but by destroying the vault mappings of the identifiers it links to, which is what makes crypto-shredding sufficient rather than merely convenient |
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

## Where the line falls on an address and on an IP address

Spec 002 applies the taxonomy column by column, and two of its rulings moved columns this page
had previously grouped together.

**An address splits.** The address lines are an `identifier`: a street and number identify a
household directly, and nothing downstream interprets them. The city, the postal code and the
country are `quasi-identifiers`: they are what geography reporting and cross-border checks are
built from, so they stay in the clear and are generalised before gold.

The postal code is stored in full and the district used before gold is `left(postal_code, 2)`.
That is a deliberate simplification, recorded rather than hidden: EU postcode formats differ,
and a production system would apply a country-specific rule instead of a fixed prefix.

**An IP address is an `identifier`, not a quasi-identifier.** It singles out a connection and,
with a subscriber record, a person; and unlike a date of birth, nothing downstream needs to
interpret its value. It is therefore tokenised. The consequence is that the geography Q19
groups by cannot be recovered from the token, so `core.login_sessions.ip_country_code` is
resolved at the source and classified as a quasi-identifier alongside the address geography.
Resolving it at the source rather than after tokenisation is the whole reason that column
exists.

## Why a pseudonymous key is not non-personal

The first pass at the column-level classification put every surrogate and foreign key in
`core` into `non-personal`, and that was wrong.

An internal customer number is pseudonymised personal data. `customer_id = 4711` does not name
anyone by itself, but inside a platform that holds the mapping it picks out exactly one person,
and every row carrying it is data about that person. Calling it non-personal says the opposite
of what is true, and it is the label the extraction layer and the access model dispatch on.

It cannot be tokenised either, and that is why a fourth class was not enough. The key **is**
the pseudonym. Replacing it with a keyed hash would produce a different pseudonym of the same
personal character, at the cost of breaking every join in the platform: a bigint foreign key
cannot hold a hash, and silver would lose the relationships it exists to conform.

So the class says what is true and asks for nothing to be done to the column. What protects the
subject is not a transformation of the key but the fact that the identifiers the key leads to —
the name, the email, the national identifier — are tokenised and resolvable only through the
vault. Delete the vault entries and the key still joins perfectly while resolving to nobody.
That is the property that makes crypto-shredding sufficient rather than merely convenient, and
naming the class is how the design records that it depends on it.

The line is drawn at keys to person-bearing entities: `customers`, `customer_addresses`,
`accounts`, `account_holders`, `cards`, `loans`, `loan_applications`, and every foreign key
pointing at one. Event keys such as `transaction_id` and `gl_entry_id` are not pseudonymous
keys: they identify an event, and the event row reaches a person through a column that is
classified. Business keys that leave the platform, such as `customer_reference`,
`account_number`, `iban` and `card_reference`, stay `identifier` and are tokenised, because
they appear on statements and in support calls, and the surrogate key carries the join in their
place.

## Interaction with erasure

Only `identifier` columns are erasable by crypto-shredding, because only they go through the
vault (ADR 0005). A `pseudonymous_key` is not erased and does not need to be: after the
identifiers on its subject are shredded it resolves to a row that names nobody.

A quasi-identifier is not reachable that way: an age band and a country survive erasure, and
that is intended, since what remains after the identifiers are unresolvable is not personal
data. The obligation this places on the model is that generalisation must be coarse enough
that a combination of surviving quasi-identifiers does not re-identify a subject. Bands are
chosen with that in mind, and the check belongs with the governance work at M8.

`sensitive` columns are erased along with their subject when the subject is erased, because
they are only meaningful when attached to one.
