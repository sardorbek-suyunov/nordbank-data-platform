# Generator realism

What the synthetic bank's data looks like, why each number is what it is, and what is
deliberately not realistic.

Every parameter named here lives in [`generator/profiles.yml`](../generator/profiles.yml), which
is the single source of the values. This document carries the justification, not a second copy
of the numbers — with one unavoidable exception, the contract bands at the end, which spec 003's
invariants and acceptance criteria are written against *as this document states them*. A realism
document that stated no bands could not be reviewed for whether its bands are sensible, which is
its whole purpose. `scripts/check_docs.py` fails if the two disagree, so neither is maintained
by hand against the other. The rule is in [specs/README.md](specs/README.md): one source per
fact, and a mechanical check where two representations are unavoidable.

A number with no stated reason is a defect. Where the reason is "it produced data that looked
right", that is what it says.

## How the generator decides anything

### Three inputs, and nothing else

`NORDBANK_SEED`, `NORDBANK_ANCHOR_DATE` and `NORDBANK_ENV` determine the output completely.
Nothing reads the clock, no identifier is drawn from a random source outside the seed, and no
value depends on the order a mapping happens to iterate in. The anchor defaults to the real
current date so that local data always looks current; the `ci` profile pins it, because the
committed manifest is compared against a regeneration and an anchor that moved every midnight
would fail that comparison daily for a reason that is not a change.

### Sub-streams, and the failure mode they avoid

A stream's seed is a keyed hash of the run seed, the stream's name, and the coordinates of the
thing being generated. It is a pure function of those three, so it cannot observe the order
generators run in or how many draws any other stream has taken.

**The non-obvious part is why the obvious design is wrong.** Anything that derives stream *N* by
consuming from a shared parent — one generator threaded through the entity modules, or a
splitter that draws a child seed as each module starts — makes *N* depend on how many draws
streams 1 to *N−1* took before it. Add one decision to loan generation and every transaction
downstream of it changes. Nothing fails, nothing warns, and the manifest hash moves for a change
that touched no transaction code. Independence here is a property of the construction rather
than a discipline, and `generator/tests/test_rng.py` proves it by adding a draw and diffing.

**One limit on that claim, stated plainly.** Spec 003 phrases the requirement as "a change to
loan generation must not alter a single transaction". That held while loan cash flows were their
own kind of event. It does not hold now and cannot: invariant 4 requires `accounts.balance` to
be the signed sum of posted **transactions and payments**, and a loan disbursement moves the
balance, so it has to be one of those two. Changing loan generation therefore changes the loan
transactions and, through the interleaving, the ids of the transactions around them. What
survives — and what the requirement protects against — is that the random *streams* stay
independent, and that every entity not causally downstream of lending comes out byte-identical.
Both are tested.

### Addressable facts, and why the mutation engine forced some of them

A stream being independent is not the same as a value drawn from it being *addressable*. A value
drawn in sequence from a stream something else is also drawing from can only be reproduced by
replaying every draw that preceded it. That is free for a generator that holds the whole book in
memory, and impossible for the mutation engine, which starts from a database and knows an
account's key and the date and nothing else.

Three facts about an account are therefore drawn from streams of their own, keyed on the account
and — where the fact may change monthly — on the calendar month: its **regular credit**, the day
of month it arrives and its amount; the **familiar merchant set** it returns to; and its
**recurring mandates**. A fourth, the customer's **device count**, moved for the same reason
earlier in M3. `generator/realism/recurrence.py` holds all three, and both halves of the book
call it, so an account's income does not change when the simulation crosses the anchor.

The key is the calendar month, `"2026-09"`, rather than a position in the history. The historical
load knows a month by its index and a tick knows a date, and only the first of those is
computable from both. Using the index would not fail: it would give an account one set of
familiar merchants up to the anchor and a different set after it, and the only symptom would be
the fraud model's unfamiliar-merchant context stepping on a date nothing else steps on.

The rejected alternative was to read these facts back out of `core.transactions` — the income is
the regular credit, the mandates are the repeated amounts. It cannot work: a salary, a loan
disbursement and an opening deposit are all a `transfer_in` on the `api` channel, and telling
them apart would mean marking them in the source. A generator flag in the bank's schema is
exactly what the recurring-price rule in `generator/invariants.py` refuses to add.

### The random source is the standard library

`random.Random`, not a third-party generator. The committed `ci` manifest makes determinism a
CI-enforced invariant, and NumPy explicitly reserves the right to change a `Generator`'s stream
between versions: a routine dependency bump would break a committed hash for a change that
touched no generator code. A determinism guarantee a dependency bump can break is not a
guarantee. Measured throughput is 26.7 million uniform draws per second and 2.8 million
log-normal, which is ample at every profile, so nothing is traded away for this.

### Money is Decimal from the first quantisation

A draw is a float; an amount is a `Decimal`. The conversion happens the moment a number stops
being a sample and becomes money, and every balance fold, ledger sum and installment schedule
downstream of it is Decimal arithmetic, per `conventions.md`.

## Volume and timing

### Acquisition and growth

A quarter of the book predates the history window (`initial_customer_share` 0.25), spread over
the two years before it. A bank that starts from zero customers on the first day of its history
has no early cohort to retain, so Q2's twelve-month retention curve would measure nothing for
its first year.

The rest arrive across the history at a rate compounding at `monthly_growth_rate` 1.2 percent a
month, which is about 15 percent a year — a plausible growth rate for an established neobank
rather than a launch. Cohorts therefore differ in size and Q1's month-over-month growth measure
has a trend to find instead of noise around zero. Acquisition is seasonal on the same calendar
as spending but damped to 35 percent of it (`seasonality_weight`), because opening a bank
account is less seasonal than buying things.

### The edges of the window, and what they were getting wrong

Three defects lived at the boundaries of the history window. None of them was visible while the
book was a static snapshot; all three surfaced the moment the mutation engine produced a day
after the anchor and it could be compared with the day before it.

**A monthly rate is now scaled to the days its window actually covers.** The generator draws a
month's activity per account and places it inside the account's window within that month. Three
windows are short: the month the history starts in, the month it ends in, and the month an
account opens in. Spreading a whole month's activity over a short window makes those days
*denser* rather than fewer, and the last one is the worst place for it: measured at `ci`, the
eighteen days before the anchor ran at 608.7 transactions a day against August's 209.2, a cliff
on exactly the date M4 begins extracting.

**An entity whose date falls past the anchor does not exist yet.** Second and third accounts
open 14 to 900 days after signup and cards 0 to 10 days after the account. Dates past the anchor
were clamped onto it, which put 221 of 760 accounts and 138 of 464 cards on one day, against one
to four accounts on every other day. A third of the book therefore had no history at all, and
the last day of the history looked like a migration. They are now omitted instead: a customer
who joined last month has not opened their second account yet, which is both true and what makes
account count grow with tenure.

**A card's expiry is read off its expiry date rather than drawn.** `status_mix` used to draw
`expired` directly, and at `ci` every one of the 33 cards it produced had an expiry date years in
the future. The status is now derived — a card is expired when its expiry date has passed — and
`expired` is out of the mix. A four-year validity over a six-month history means no card expires
at `ci` or `dev`, and reporting none is the honest answer rather than a status the dates do not
support.

### Hour of day

`hour_weights` is bimodal: a lunchtime peak around 12:00 and a larger evening peak around 18:00
to 19:00, over a low overnight floor. The overnight floor is what makes the fraud model's
unusual-hour context meaningful — it is unusual because few legitimate transactions happen then,
and the prevalence is read off this profile rather than assumed to be five twenty-fourths.

### Month of year

`month_weights` peaks in December at 1.31 and troughs in August at 0.83. December is Christmas
spending; August is the European holiday month, when card spending at home falls. These are the
two seasonal features a reviewer looks for first.

### The working week

Weekend volume is 82 percent of weekday volume (`weekend_volume_factor`), and the category mix
shifts (`weekend_band_factors`): groceries rise to 1.24, travel to 1.31, fuel to 1.12, while
government services collapse to 0.22 because public offices are closed. A drawn day that lands
at the weekend is sometimes dropped rather than moved to a weekday, so the weekday shape is not
distorted by the transactions that would have been weekend ones.

### Activity per account

`per_account_month` is 17.5 customer-initiated transactions, varied by product class
(`per_account_month_by_product_class`): 20.5 for a current account, 2.2 for savings, and less
for the internal classes. Without that split every savings account spends like a current
account, drains, and declines for want of funds, because no salary reaches it.

The count is an over-dispersed Poisson: the rate itself varies log-normally per account-month
with `per_account_month_dispersion` 0.45. A plain Poisson would make the busiest account in a
book of 250,000 about twice as active as the median, and real books are far more skewed. The
skew is what dormancy and activity measures are read against.

### Income

`regular_credit_share_by_product_class` gives 88 percent of current accounts a salary and 74
percent of savings accounts a standing transfer, on a fixed day between the 24th and the 28th,
at a log-normal amount with a median near 2,000 for a current account and 300 for savings.

This exists because the first smoke run had **31 percent of card authorisations declining for
want of funds**. That is not a bank with tight customers; it is a generator with no income side.
With income the measured rate is about 9 percent, which is still higher than a real card book's
one to five percent, and the reason is stated under deliberate unrealism below.

## Amounts

Four populations, deliberately different in shape.

**Card purchases** are log-normal per MCC band, with the median and spread chosen per band:
groceries around 28 with a moderate spread, fuel around 56 with a narrow one, travel around 100
with a wide one. The spread is the parameter that matters and it is expressed as sigma of the
underlying normal, so the spread in orders of magnitude is sigma over ln(10).

**The sigmas were widened once, and the reason matters.** The originals put 68 percent of
general retail purchases between 9 and 74 euro. That is not what a card book looks like: the
same card buys a 2 euro coffee and an 800 euro laptop in the same week. The change was made on
that ground. It also moved the first-digit distribution closer to Benford, because a wider
spread in log space is what produces Benford — but it was not made for that reason, and the
before-and-after figures are reported here so a reader can judge which way round it was:

| | general retail sigma | Benford deviation, card purchases |
|---|---|---|
| before | 1.04 (0.45 orders of magnitude) | 0.00589 |
| after | 1.45 (0.63 orders of magnitude) | 0.00546 |

Fuel stays narrow at 0.52 deliberately. A tank of fuel costs what a tank of fuel costs, and
widening it to help a statistic would be exactly the tuning this note exists to rule out.

**Cash withdrawals** snap to note multiples — 20 most often, then 10 and 50 — because a machine
dispenses notes. The magnitude still comes from the cash band's log-normal, so the size
distribution is right; only the granularity is imposed.

**Recurring payments** repeat one price from a list of familiar subscription prices, on the same
day of month, for the life of the mandate. A subscription billed on the 31st is billed on the
30th in November, which is what a real mandate does.

**Fees** are a short list of product prices rather than a distribution, because that is what a
fee is.

### Benford's law, and the coupling the specification did not anticipate

Spec 003 requires log-normal amounts, cash clustered on note multiples, recurring payments
repeating an identical amount, *and* a first-digit distribution approximating Benford. The last
two are anti-Benford by construction: round-number clustering piles mass on leading digits 1, 2
and 5, and a repeated price contributes its own leading digit once a month forever.

**The amount model wins and the Benford assertion narrows.** Tuning sigmas and mix weights to
pass a statistical test would trade a property a reviewer recognises as real for a
demonstration. So:

- Invariant 14 asserts Benford on the population where the law is theoretically expected: card
  purchase amounts, excluding cash withdrawals and recurring prices.
- The composite distribution over every transaction amount is measured and reported beside it as
  an observation rather than a gate, with the deviation attributed to the excluded populations.

Recurring payments are excluded in SQL by price membership rather than by a column, because the
source has no `is_recurring` flag and inventing one would put a generator detail into the bank's
schema. The rule excludes a few genuinely one-off purchases that happen to cost 9.99, which is
conservative in the direction that matters.

### Gate on effect size, report significance beside it

This is a principle rather than a local choice about Benford, and it will apply to every
threshold the data quality framework sets at M7.

**A significance test is the wrong instrument for a quality gate.** Its power grows with the
sample, so a fixed critical value gets stricter as the data grows and converges on always-fail
at scale. The question a gate asks is "is this far enough from expected to act on", which is a
question about effect size. The question a significance test answers is "is this difference
distinguishable from noise", and at ten million rows every difference is.

So: **gate on a measure of effect size, and report the significance statistic alongside it.**
The effect size says whether to act; the statistic says how confident the measurement is, which
is worth recording and worth nothing as a threshold.

Invariant 14 is the first application. The approved wording asked for a chi-square critical
value at 8 degrees of freedom; it was implemented, measured, and replaced. The same distribution
measures a chi-square of **68.6 at `ci` and 4,251.1 at `dev`** — sixty-two times larger — while
its mean absolute deviation moves from 0.00546 to 0.00549. The deviation is the thing that has
not changed, because the distribution has not changed; only the sample has. Invariant 14
therefore asserts the mean absolute deviation, which is the conformity measure Benford analysis
actually uses, with Nigrini's 0.006 threshold for *close conformity*, and reports the chi-square
statistic beside it.

The same shape of reasoning applies to a freshness threshold, a row count drift check, a null
rate check and every other gate M7 will define: choose the threshold on the size of the
departure that matters, not on the confidence that a departure exists.

## Card presentment

`core.transactions.is_card_present` is a decision, not a derivation. The channel sets a base
probability — `card_present_share_by_channel` puts point of sale at 0.985, ATM at 1.0, ecommerce
at 0.004 and mobile app at 0.640 — and the card-not-present share rises linearly across the
history from 0.31 to 0.52.

**The mobile app figure is the whole point.** A wallet tap at a terminal is card present; an
ecommerce checkout from the same handset, on the same channel, is not. Deriving presentment from
the channel would make Q10 and Q19 measure the channel twice under two names. The base is scaled
so the aggregate lands on the trend's target; because card-not-present rises, the scale factor
is at most one and the clamp never binds on the channels that sit near certainty.

## Currency and geography

Accounts are 89 percent EUR. The remaining 11 percent are spread over the currencies the bank
lists, weighted towards GBP and USD, with JPY given a small share deliberately: it has zero
minor units and exercises the presentation-rounding path that every other currency hides.

Customers are distributed over seventeen EEA countries, Germany-weighted at 32 percent, because
the bank is EU-licensed and passports into these markets. Merchant countries follow the customer
mix with a small non-EEA tail.

**The non-EEA merchant share is held to 4 percent, and that is not an estimate of a real card
book's geography.** It is the bounded null exposure of Q4. Interchange can only be priced for
intra-EEA consumer volume: the outbound inter-regional rate is null and stays null, because the
2019 Commission commitments cap the *inbound* corridor — cards issued outside the EEA and used
at EEA merchants — and Nordbank is an EEA issuer, so it never earns that interchange. The full
reasoning is in [metric_definitions.md](metric_definitions.md). Every non-EEA card transaction
is a transaction Q4 cannot price, so the share is kept small enough that the gap is visible
rather than dominant. This is recorded under deliberate unrealism.

Cross-border payments are 21 percent, with a directional corridor mix. Corridors are never
netted: inbound and outbound are different rows, per the cross-border rule.

## Fraud

### How much, and where

Between 0.05 and 0.15 percent of card transactions are fraudulent; the target sits mid-band at
0.10 percent so that sampling noise at `dev` does not push the measured rate outside it.

Fraud concentrates rather than spreading evenly. Card-not-present multiplies propensity by 7.5,
an unfamiliar merchant by 3.2, an unusual hour by 2.8, a velocity burst by 4.4, and a
transaction in a month the customer added a device by 3.9.

**The multipliers are calibrated against the contexts' own prevalence**, so changing where fraud
falls does not change how much of it there is. Without that separation, raising the
card-not-present multiplier would silently raise the overall fraud rate, and the two would have
to be tuned against each other every time either moved. The calibration is analytic, treating
the contexts as independent; they are not quite — card-not-present and new-device co-occur more
than chance — so the realised rate lands slightly above the target, which the band accommodates
and the report states.

An account draws a small stable set of familiar merchants at creation; anything outside it is an
unfamiliar merchant. A velocity burst is real rather than a flag: several card transactions
inside a half-hour window, which is the context the `velocity_card` detection rule exists for.

### Detection, imperfect in both directions

Recall is 0.62 and the false alert rate is 0.00042 per legitimate card transaction. A detector
that fired on exactly the fraudulent transactions would make Q10 report a precision of 100
percent and a false positive rate of zero, and the mart would be measuring the generator rather
than a fraud operation.

Alert scores come from overlapping Beta distributions — confirmed fraud scores higher on average
but the distributions overlap — because a score that separates the classes perfectly is the same
fake one step further in.

Dispositions arrive one to fourteen days after the alert, and about 10 percent of alerts are
still open at the anchor. That lag is exactly why `metric_definitions.md` attributes precision to
the disposition month: a generator that dispositioned everything before the anchor would leave
that rule with nothing to be right about.

### Why the band is not asserted at `ci`

At `ci` scale, 0.1 percent of tens of thousands of card transactions is a few dozen fraudulent
events, and a handful of alerts reach a final disposition. A precision band over that many
events has a binomial confidence interval wider than any band worth stating. **A band wide
enough to be honest at that n asserts nothing, which is a fake pass wearing a confidence
interval.**

So at `ci` invariant 9 asserts structural properties only — alerts exist, every alert is on a
card transaction, no alert fires before its transaction, dispositions come from the reference
vocabulary — and reports the band as not asserted, printing the n and the interval half-width
that justify the suspension. The band is asserted at `dev` and `full`.

## Lending

Applications arrive at 0.19 per customer per year over the customer's tenure. Approval varies by
risk band from 0.93 in band A to 0.17 in band E, which is what Q8 measures; a single approval
rate would make that mart one number repeated five times.

Not every decided application is approved or rejected: about 4 percent are withdrawn and 3
percent expire, and `metric_definitions.md` excludes both from the approval rate and reports them
beside it, so they have to exist. About 2 percent are still undecided at the anchor.

The approved amount is at most the requested amount and often less, drawn from a haircut list.
Invariant 8 asserts every disbursed loan is within its approved amount, so the haircut is
applied once and the disbursement reads it rather than drawing again. Disbursement lags the
decision by one to twenty-one days, and 9 percent of approved applications are never drawn down.

The schedule is a level-payment annuity computed in Decimal, with the last installment absorbing
the rounding so the principal components sum to exactly the principal.

### Default, and why it has to season

Lifetime default probability by risk band is 0.4 percent in A rising to 16.4 percent in E. Each
sits inside the probability-of-default interval its band is written for in `ref.risk_bands`,
which is what lets Q8 compare realised default against the band's own expectation. A band whose
modelled default rate fell outside its own interval would make that comparison meaningless
before any data existed, and a test asserts it.

**Delinquency emerges over the months after origination rather than at it.** The month a
defaulting loan stops paying is drawn by inverting a cumulative seasoning curve: almost nothing
in the first two months, a hazard peaking through the first two years, tailing off after. A loan
book where defaults were decided at origination and appeared immediately would give Q7 vintage
curves that are flat lines at their final value.

About 38 percent of borrowers in arrears cure. A cure shows as a fall in the delinquency rate at
the next reporting date, never as a retrospective edit of an earlier month. A loan six months in
default is written off with probability 0.58.

**Observed default is not lifetime default, and the band measures the first.** A loan book at
any instant contains vintages that have not seasoned, and a loan whose default month falls
after the anchor has not defaulted yet. The `default_rate_overall` band is therefore lower
than the lifetime rates by band: measured 0.0094 at the dev profile against lifetime rates of
0.004 to 0.164. Stating the band as if it were the lifetime rate would make it wrong about
the thing it is actually measured against.

**An account with a loan still collecting against it does not close.** A bank does not let you
close the account its direct debit collects a loan from. Without that rule the lending pass,
which runs before closures are decided, leaves repayments landing on closed accounts — 1,155
of them at the dev profile, which invariant 1 refused.

## Attrition and dormancy

Accounts close at a monthly hazard of 1.18 percent in the first year and 0.44 percent after,
which is the shape real attrition has. A per-month hazard rather than a lifetime probability
drawn at opening, so the survival curve comes out of the process instead of being imposed on it,
and Q2's retention curve decays rather than sitting flat.

**Dormancy is not closure and is modelled separately**: 0.75 percent a month becoming dormant,
2.1 percent a month reactivating, which settles near a quarter of the book dormant at any time. A
dormant account is open, carries a balance, and has stopped moving. Conflating the two would lose
the distinction `ref.account_statuses.is_open` exists to carry and would make every inactive
customer look like a lost one.

**An account does not close inside its first thirty days.** Cards are issued up to ten days after
opening, so a closure days after opening leaves a card issued onto a closed account — which
invariant 1 rightly refuses. A cooling-off floor is also what real accounts have, so the
realistic fix and the coherent one are the same fix.

## Digital behaviour

Background sessions run at 6.5 per customer per month, over-dispersed like transaction activity.
Sessions that precede a digital transaction are generated in addition to these, so the total per
customer is higher and is reported measured rather than configured.

A customer has one to four devices, weighted towards one, with a new device appearing at a
monthly hazard of 2.8 percent. Q19 measures the share of activity from a device unseen in the
trailing 90 days, so a customer with a new fingerprint every session would make the measure
meaningless in one direction and a customer with one fingerprint forever would make it
meaningless in the other. The same monthly decision feeds the fraud model's new-device context,
so the two are correlated by construction rather than by coincidence.

Device fingerprints and IP addresses are derived by hash rather than drawn, so they are a
function of who the customer is and consume no draws. IP addresses come from the RFC 5737
documentation ranges, so no address here can belong to anybody. `ip_country_code` matches
residence 93.8 percent of the time; the rest is travel and VPNs, which is what gives Q19 a
geography signal instead of a constant.

### Invariant 13, as replaced

The original invariant required every active customer to have a login in the trailing 90 days. A
real bank fails that daily — dormancy is precisely the state of being active and not logging in,
and this generator models dormancy on purpose. Satisfying it would have meant constructing an
unrealism to pass a check.

It is replaced by a coverage share: a stated minimum of customer-initiated transactions on
digital channels are preceded by a login for the same customer within 24 hours. Card-present and
recurring transactions are excluded, since neither implies a login, and so is the `api` channel,
which is machine to machine — standing orders, direct debits, salary credits and loan movements
— and implies no login at all. The generator targets 0.87 and the invariant asserts a floor of
0.80.

## Sanctions screening positives

A small share of payment counterparty names — 0.14 percent — is drawn from
[`generator/fixtures/screening_positives.csv`](../generator/fixtures/screening_positives.csv).
M4 injects the same fixture into the local sanctions snapshot, which is what makes a match
possible at all: without a shared fixture the screening job would run correctly against a list
nothing in the book appears on, and Q12 would report zero forever while looking like it worked.

**No real sanctioned individual's name appears anywhere in this repository.** Every name in the
fixture is prefixed `ZZ-TESTCASE` or `ZZ-TESTENTITY` and suffixed `SANCTIONS-FIXTURE`, which is
not a name any person or company has. The reason is stated rather than assumed: a simulated bank
does not need real people's names to demonstrate screening. The control being demonstrated is
that a tokenised counterparty name can be resolved through the vault, matched against a list
version, and recorded — and a fabricated name exercises every step of that. Putting real names
from a sanctions list into a portfolio repository would publish accusations about identifiable
people to make a demonstration marginally more convincing, which is indefensible whatever the
demonstration is worth.

## What a tick changes

The mutation engine advances the source one simulated day at a time. Its volumes are not
parameters of their own: a tick draws the day's movements from the same per-account rates as the
historical load, divided by the length of the simulated month and carrying the same seasonality,
weekend factor and growth trend. The day after the anchor is meant to look like the day before
it, and it is measured rather than assumed — per-day means over the thirty days either side of
the anchor at `ci`: transactions 248.0 against 244.4, payments 50.8 against 53.5, login sessions
159.1 against 159.7, ledger batches 272.0 against 276.6.

Only the things the history has no analogue of carry parameters, and they are in the `mutation`
section of `generator/profiles.yml`.

### Late arrivals, and the only way an account goes past its limit

**Offline card authorisations** are the interesting mechanism. A transit gate, an aircraft or an
unattended pump takes a transaction without reaching the network and presents it days later, so
the bank learns of it after the fact. `offline_card_share` is 0.9 percent of a day's card
purchases and the lag is two to five days, weighted towards the shorter end because most offline
files present at the next clearing cycle.

Such a row **cannot be declined for insufficient funds**, because no online authorisation ever
existed to decline. It posts, the balance moves, and if that takes the account past its overdraft
limit then the account is past its overdraft limit. That is realistic and it is the point: the
loaded `ci` book contains 72 overdrawn accounts and not one beyond its limit, so every
unauthorised overdraft silver and gold ever see is created here and is attributable to it.

They are card present. An offline authorisation happens at a terminal with the card in the
customer's hand, which also keeps them out of invariant 13's population — there is no login
before a tap at a barrier.

**A deferred posting is not a late arrival**, and conflating them would be the easy mistake. An
authorisation taken on an earlier day that clears today is an *update*: nothing is inserted,
`booked_at` does not move, and only `updated_at` does. It is counted under `updated` rather than
under `late_arriving`, which is a subset of `inserted`. It is not declined either — an
authorisation already given is not taken back at clearing — so it produces the same unauthorised
overdraft by the other route.

A third transition moves no money at all: a payment going from `booked` to `settled`, both of
which are `is_posted`. It is the largest single population of rows whose business time is days
behind their `updated_at`, and it is exactly what silver's late-arrival ordering is for.

`max_age_days` of 7 bounds what may still clear. Without it a tick would drain the historical
book's whole standing backlog over its first few days, which is a spike rather than a steady
state: 298 of the 378 non-posted transactions in the loaded `ci` book are more than thirty days
old, and a real system would not clear those either.

### The posting date is the tick's date

A late arrival's ledger entry posts to the current open period, never to the business date.
Posting into a closed period would restate totals for a day that has already been reported,
which is the same reproducibility argument that makes FX rates non-restating in
`architecture.md`. Business date, posting date and value date therefore diverge on a late
arrival and all three mean something: `booked_at` is when the customer transacted,
`posting_date` is when the bank recognised it, `value_date` is when the money moved.

Invariant 5 holds on the posting date because every batch balances within itself. Q15 and Q16
meet the divergence and `metric_definitions.md` records what it means for each.

### The clearing cycle runs before the day

Late arrivals, deferred postings, reversals and loan collections are applied in the small hours,
which is when a real core system clears them, and the day's decline decisions therefore see the
result. An account an offline transaction pushed past its limit overnight declines the debits it
attempts that afternoon, which is the behaviour a customer would recognise.

### A posting is reversed, never deleted

A soft-deleted posted transaction is a fork with no good branch: invariant 4 filters on
`is_posted` and not on `is_deleted`, so reversing the balance fails the invariant and leaving it
makes the source say money moved while silver says it did not. A tick therefore inserts a
reversing movement carrying `reversal_of_transaction_id`, with its own ledger batch, and leaves
the original alone. Both rows stay posted, both are in the ledger, and the net balance effect is
zero — which is what a real ledger does. Nothing in the source populated that column before this
milestone.

### Dirt, and what a constrained source can and cannot produce

`core` has foreign keys, check constraints and not-null constraints. An orphan in it is not
something the generator declines to produce, it is something the source **cannot express** —
the same shape of argument as ADR 0008, where bronze immutability is a property of key
construction rather than of restraint. Genuine schema and type violations belong to the file and
API feeds at M4, where they can actually occur.

So a tick produces what a well-constrained system still emits. **Duplicate customer records**
are the valuable one: the same person entered twice, under a name one edit away, sharing a date
of birth and usually one contact detail. Four kinds of edit, and two of them differ in a way
that matters downstream — a diacritic normalises back to its Latin letter and a Cyrillic
confusable does not, so a pipeline that casefolds and strips accents resolves one and not the
other. Beside them: casing and stray whitespace, a KYC status the account activity contradicts,
and a nullable column left null where the business expects a value.

**A value can be malformed by the domain's rules while conforming to the column's.**
`core.payments.counterparty_iban` is checked against a shape and nothing validates the mod-97
checksum — the generated IBANs carry none, which this document already recorded as an
unrealism. That is a real validation failure for a contract to catch at M4, produced by a source
that violates no constraint, and a `varchar` cannot express the rule that would catch it.

### The verdict follows the score, not the truth

A tick reads a fraud alert out of the database and cannot know whether the transaction behind it
was fraudulent. Nothing in `core` records that, and nothing should: a bank does not store which
of its transactions were really fraud, it stores which alerts its analysts confirmed.

So the disposition is the posterior of the two score distributions the detector draws from,
against a prior that is itself derived from the detector — recall times the fraud rate, over
that plus the false alert rate times everything else, which is 0.596 at the stated parameters
and sits inside the `confirmed_fraud_rate_among_alerts` band. It reproduces the historical
precision by construction rather than by a second parameter, because the scores it reads were
drawn from those same two distributions, and it gives a high-scoring alert a higher chance of
being confirmed: 0.016 at a score of 0.2, 0.698 at 0.6, 0.955 at 0.8.

### One physical delete, and why it exists at all

`architecture.md` records that watermark extraction cannot detect a `DELETE` and schedules a
primary-key reconciliation at M7 to find the resulting orphans in silver. If nothing in the
source ever deleted a row, that reconciler could never be demonstrated against a known
positive — which is worse than not having it, because an untested control reads as a working
one.

The source therefore purges a duplicate customer record that no other row references, at a small
share of the duplicates it resolves; the rest are tombstoned with `is_deleted`, which is what
most systems do. The candidate list of referencing tables is read from `pg_constraint` rather
than written down, and measured on the loaded `ci` book no customer has no dependent row at all,
so "dependent-free customer" is an exact description of a duplicate nothing has attached to.
Every purged key is written to `platform.tick_deleted_keys` individually, so M7 has an expected
answer rather than a count.

A posted transaction is never soft deleted. Invariant 4 filters on `is_posted` and not on
`is_deleted`, so a soft-deleted posting is a fork with no good branch: reverse the balance and
the invariant fails, leave it and the source says money moved while silver says it did not.

## Deliberately unrealistic

Everything below is wrong on purpose, or wrong and accepted. It is listed so that nobody has to
discover it.

| What | Why |
|---|---|
| **The non-EEA merchant share is 4 percent** | Not a geography estimate. It is the bounded null exposure of Q4, kept small because the outbound inter-regional interchange rate is null and every non-EEA card transaction is one the mart cannot price |
| **Declines for want of funds run at about 9 percent** | Real card books run at one to five. The generator draws spending independently of the balance, so a customer with a thin balance keeps trying and keeps being declined. Fixing it properly means making spending respond to the balance, which is a behavioural model the milestone does not need |
| **IBANs carry no valid checksum** | They match the shape the schema's regular expression requires, and nothing in the platform validates mod-97. A valid checksum would make them look like usable bank identifiers |
| **Card BINs are sequential rather than issuer-assigned** | Nothing downstream reads a BIN as an issuer key. The column exists to prove no column on `core.cards` can hold a full card number |
| **Merchant names are dirty by design** | Casing, punctuation and trailing store or location noise are left as an acquirer would have sent them, so conformance in silver has real work rather than a cosmetic pass over clean values |
| **A customer belongs to one country forever** | No migration between countries. Address history exists but the country does not change, so the quasi-identifier generalisation at M5 has a stable answer |
| **Every account of a customer shares their country** | Real customers hold accounts in more than one country. The cross-border rule reads the account's country, so this makes account geography a proxy for customer geography, which it would not be in a real book |
| **A Poisson count above a mean of 12 uses a normal approximation** | Knuth's method costs one iteration per unit of the mean, and the full profile draws tens of millions of them. The error is under one percent in the body of the distribution, and no count is read individually |
| **A bounded log-normal is clamped, not resampled** | Resampling until a draw lands in range makes the number of draws depend on the values rejected, which would make one stream's consumption depend on its own parameters. Clamping keeps it at exactly one draw and puts a little extra mass on the bounds |
| **Transaction ids are chronological within a month, not within a day** | The month is the unit the generator sorts, so ids are ordered by instant across the whole run. Nothing depends on finer ordering, and finer would mean a global sort |
| **Login coverage is constructed, not emergent** | A session is generated before a digital transaction at the configured rate. Real coverage would emerge from customers who happen to have logged in; here it is placed, so Q19 has signal by construction |
| **The ledger has no accruals, revaluation or impairment** | Postings exist for customer movements, fees, interest paid and loan cash flows. A real ledger carries far more, and `ref.gl_source_entities` is an open vocabulary precisely so those can arrive when something writes them |
| **Fraud is labelled, not inferred** | The generator knows which transactions are fraudulent because it made them so. A real bank only ever has dispositions. The label never reaches the database — only the alert and its disposition do — so downstream models see what a bank sees |

## Contract bands

These are what spec 003's invariants and acceptance criteria assert. They are restated from
`generator/profiles.yml`, and `scripts/check_docs.py` fails if the two disagree.

| Band | Minimum | Maximum |
|---|---|---|
| `fraud_rate` | 0.0005 | 0.0015 |
| `alert_precision` | 0.42 | 0.78 |
| `confirmed_fraud_rate_among_alerts` | 0.42 | 0.78 |
| `approval_rate_overall` | 0.55 | 0.78 |
| `default_rate_overall` | 0.004 | 0.075 |
| `login_precedes_transaction_share` | 0.80 | 0.95 |
| `non_eea_merchant_share` | 0.02 | 0.07 |
| `card_not_present_share` | 0.28 | 0.56 |
| `benford_mad_max` | 0.006 | — |

`benford_mad_max` is a ceiling rather than an interval: it is Nigrini's threshold for close
conformity of a first-digit distribution to Benford's law, and there is no lower bound worth
stating because a deviation of zero would be a perfect fit.
