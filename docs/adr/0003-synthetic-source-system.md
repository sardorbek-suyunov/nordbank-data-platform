# 0003 — Synthetic core banking source system

Status: Accepted
Date: 2026-09-17

## Context

The engineering problems this platform exists to solve are all problems of change over time:
incremental extraction against a watermark, updates to rows already loaded, soft deletes,
records that arrive after the day they belong to, and a source schema that changes without
warning. A static public dataset is a finished export. It has no `updated_at` that moves, no
deletes, no late arrivals and no drift, so an incremental pipeline over it is indistinguishable
from a full reload and cannot be tested.

## Decision

Generate the core banking system: a Postgres database with the `core` and `ref` schemas, an
initial historical load, and a mutation engine that advances it one business day at a time.
The engine produces inserts, updates to existing rows with a moving `updated_at`, soft deletes
via `is_deleted`, a controlled share of late-arriving records, and scheduled schema drift such
as an added column or a widened type, so the contract and quarantine machinery has something
real to catch.

Generation is seeded, so a dataset is reproducible from `GENERATOR_SEED` and the profile
parameters rather than being distributed as a dump.

Real external data is used where the value is in the data itself rather than in its
behaviour: ECB reference rates from the Frankfurter API, because FX conversion has to be
correct and dated; the OpenSanctions consolidated list, because screening against a plausible
sanctions list is what makes the AML questions meaningful; and FRED macro series, because a
slow-moving external series is a genuine third cadence.

## Consequences

Every ingestion pattern in the platform can be demonstrated and tested on demand, including
failure modes that are otherwise impossible to reproduce. The realism of the analytics now
depends on the quality of the generator: distributions, seasonality and fraud patterns are
modelling decisions, and a naive generator produces marts that are technically correct and
analytically meaningless. The generator becomes a component with its own tests and its own
maintenance cost, and it is built before any pipeline exists to consume it.

## Alternatives considered

A public banking dataset such as the PaySim or IBM card transaction sets: realistic
distributions, no mutation, no history, no drift.

Replaying a public dataset through a fake CDC feed: adds movement, but the movement is
invented at the edge rather than in the source, so soft deletes and updates never touch an
actual source row.
