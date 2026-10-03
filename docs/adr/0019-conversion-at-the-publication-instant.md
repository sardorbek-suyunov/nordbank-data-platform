# 0019 — A fact converts at the latest rate published before its business instant

Status: Accepted
Date: 2026-10-03

## Context

Every monetary fact carries an EUR equivalent beside its original amount and currency. The rule
that chooses the rate decides whether a reported total can be reproduced, and specification
008's planning measured the rule the documents stated at M0, an as-of join on the rate's date,
against the `ci` book on the throwaway stack:

- **The ECB publishes in the afternoon.** The reference rates appear around 16:00 Europe/Berlin
  on each TARGET business day. A transaction booked at 09:00 has no same-day rate yet; under the
  date rule it converts at that day's rate all the same, once the rate exists.
- **So the date rule restates on rebuild.** Silver built before the day's rate lands converts a
  morning fact at the day before's rate, carried; a rebuild after it lands converts the same fact
  at the day's rate. Specification 008 requires that a final conversion is never restated, and
  silver is tables rebuilt in full, so a rule whose answer depends on when the build ran breaks
  that requirement on every day.
- **The difference is not marginal.** With the full history landed, the instant rule chooses
  another rate date than the date rule for 2,464 of 6,163 non-EUR transactions and 339 of 1,097
  non-EUR payments. Transactions are booked through the whole day, with peaks at 02:00 and
  05:00 UTC from batch posting.
- **Decimal division in DuckDB 1.5.5 is floating point.** DECIMAL / DECIMAL, DECIMAL / INTEGER and
  HUGEINT / HUGEINT all return DOUBLE. A sweep of 200,000 conversions found the DOUBLE path wrong
  in 3,171, every one an exact half at the fourth decimal place, and scaling the integer result
  back by dividing by 10,000 was wrong in the last digit of 12,345,678,901,234.5678 at 1.6.

## Decision

A fact converts at **the latest rate whose publication instant is at or before the fact's
business instant**:

- the business instant is `booked_at` for a transaction and `initiated_at` for a payment, which
  is never null, where `booked_at` is null until a payment books (1,074 at planning);
- a rate's publication instant is its date at 16:00 Europe/Berlin, the nominal time, computed by
  one macro and verified across both daylight-saving changes;
- EUR converts at 1; a currency with no rate published before the instant has a null EUR
  amount and `fx_is_missing`, never zero and never the original amount.

**A conversion is provisional** when the rate chosen is the latest landed for its currency and
the business instant is after the publication instant of the next weekday after the rate's date:
a newer rate may still land, and a rebuild after it does would choose it. Every other conversion
is **final**: no rebuild from the same or from more bronze changes it, because every rate that
could precede its instant has either landed or, on a TARGET holiday, does not exist. Only a
provisional conversion may be restated. There is no holiday calendar: a holiday is a weekday on
which the expected publication never comes, and the rows it leaves provisional become final when
the next rate lands.

**The arithmetic is exact.** The amount in units of 0.0001 and the rate in units of 10^-8 are
HUGEINTs, each widened to DECIMAL(38, s) before it is scaled; the quotient is rounded half away
from zero to four decimal places in integer division, `(2n + r) // 2r` with the sign restored;
and it returns to DECIMAL(18,4) by multiplying by 0.0001, never by dividing. No intermediate
expression is floating point, and a test checks the type of each one. Half away from zero is the
rounding Council Regulation (EC) No 1103/97 prescribes for amounts converted to and from the euro
(Article 5), and the one DuckDB's own `round` applies to a decimal, so the platform has one
rounding rule.

`sl_fx_rates` holds, for every currency and calendar date, the same rule evaluated at 23:59:59
UTC, with the same carried and provisional flags, built from the same macros, and a test
restating the rule independently checks every row.

## Consequences

- **A reported total is reproducible from the data it was computed on, and stays so** except for
  its provisional rows, which are flagged on every converted fact. A report that must not move
  can exclude or footnote them.
- **The nominal 16:00 instant is not the real publication.** The ECB publishes at about that
  time, and a fact a few minutes either side of the real moment converts at the rate the nominal
  instant implies, which can differ from what a desk with a live feed saw. The platform has no
  record of the real moment; Frankfurter serves the rates, not their publication times.
- **Provisional conversions can restate.** A rebuild after a rate lands changes the EUR amount of
  every provisional row for that currency, and no other row. On the sixty-one-day history no row
  is provisional; on a stack running daily, the facts after the last rate's next weekday
  publication are, until the next rate lands.
- **A morning fact is carried.** `fx_is_carried` says the rate's date is not the fact's UTC
  business date, so a fact before the day's publication converts at the day before's rate and is
  carried: 3,968 of 6,163 non-EUR transactions on the history. It is a statement about the rate's
  date, not a defect.
- **A currency the ECB stops quoting stays provisional.** The rule cannot tell a currency that
  will never be quoted again from one whose rate is late: BGN, whose last rate is 2025-12-31 since
  Bulgaria adopted the euro, is the latest landed rate for its currency on every later date, so
  its rows in `sl_fx_rates` are provisional for good. No fact is in BGN.
- **Exact integer arithmetic is more SQL than `round(amount / rate, 4)`**, and every model that
  converts must use the macro. The conversion lives in one macro so nothing else writes it.

## Alternatives considered

**Convert as of the rate's date, the M0 rule.** Simple, and what `architecture.md` stated.
Rejected because it restates on rebuild: a fact converts at a carried rate before its day's rate
lands and at the day's rate after, so the same bronze yields different silver depending on when
the build ran, on 2,464 of 6,163 non-EUR transactions.

**Freeze converted rows: convert once and never recompute.** The as-of rule with its results
kept, which is what "a rate is never restated" meant at M0. Rejected because silver could no
longer be rebuilt from bronze: a row's EUR amount would depend on the build that first saw it,
so two warehouses built from the same lake would disagree and neither could be reproduced. Silver
is a pure function of bronze (specification 008), and freezing is state.

**Convert at the end-of-day rate of the fact's date.** Every fact of a day at one rate is easy to
explain. Rejected for the same reason as the date rule, a fact converted before the day's rate
lands restates, and because it converts a 09:00 transaction at a rate published seven hours
later.

**Round half to even.** The default of many numeric libraries and unbiased over many roundings.
Rejected because Regulation 1103/97 rounds half up, away from zero, for euro conversions, and two
rounding rules in one platform would make totals disagree with their parts in the last digit.
