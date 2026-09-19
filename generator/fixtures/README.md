# generator/fixtures

Committed inputs the generator draws from, as opposed to parameters it reads.

## screening_positives.csv

The counterparty names a small configurable share of payments is drawn from, so that sanctions
screening at M4 has something to match. M4 injects the same file into the local sanctions
snapshot, which is what makes a match possible at all: without a shared fixture the screening
job would run correctly against a list nothing in the book appears on, and Q12 would report zero
forever while looking like it worked.

**No real sanctioned individual's name appears in this repository, and none ever will.** Every
name here is prefixed `ZZ-TESTCASE` or `ZZ-TESTENTITY` and suffixed `SANCTIONS-FIXTURE`, which
is not a name any person or company has. The reason is stated rather than assumed: a simulated
bank does not need real people's names to demonstrate screening. The control being demonstrated
is that a tokenised counterparty name can be resolved through the vault, matched against a list
version, and recorded as a match — and a fabricated name exercises every step of that. Putting
real names from a sanctions list into a portfolio repository would publish accusations about
identifiable people to make a demonstration marginally more convincing, which is indefensible
whatever the demonstration is worth.

The share of payments that draw from this file is `payments.screening_positive_share` in
`generator/profiles.yml`, and it is justified in `docs/generator_realism.md`.
