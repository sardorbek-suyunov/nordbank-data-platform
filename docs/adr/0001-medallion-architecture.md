# 0001 — Medallion architecture

Status: Accepted
Date: 2026-09-17

## Context

The platform ingests five sources with different shapes, cadences and reliability: a mutating
OLTP database, two REST APIs, a file drop and a weekly bulk list. The questions in
`docs/business_questions.md` need conformed, joinable entities and a dimensional model on top
of them. A single transformation step from source to reporting model would couple three
concerns that fail for different reasons and at different times: capturing what arrived,
deciding what it means, and shaping it for a report.

## Decision

Three layers with separate guarantees.

Bronze is the landed source data, typed against a contract and otherwise unchanged, append
only and immutable once a partition is registered. Corrections arrive as new batches. Rows
failing the contract are quarantined with a reason rather than dropped.

Silver is the conformed model at entity grain: deduplicated on the business key, late
arrivals ordered by `updated_at`, soft deletes applied, amounts in `DECIMAL(18,4)` with an EUR
equivalent, timestamps in UTC, PII tokenised, SCD2 history where the source mutates.

Gold is the dimensional model and the marts, each with a declared and tested grain, rebuilt
from silver and holding no state of its own.

## Consequences

Any reporting model can be rebuilt from bronze without touching a source system, which makes
a logic change cheap and a re-extraction rare. Storage is spent three times on the same
facts. Each layer needs its own tests, and a change to a silver contract forces a rebuild of
everything above it. A simple metric costs three models instead of one, which is the price
paid for the rebuild guarantee.

## Alternatives considered

Source to mart in one dbt layer: fewer models, but no immutable record of what arrived, so a
transformation bug destroys history and a late-arriving correction cannot be replayed.

Bronze and gold without silver: conformance logic would be duplicated in every mart, and the
first disagreement between two marts would be unresolvable.
