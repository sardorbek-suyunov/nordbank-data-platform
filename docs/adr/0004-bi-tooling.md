# 0004 — BI tooling

Status: Accepted
Date: 2026-09-17

## Context

The gold layer has two audiences with different needs. One is the reporting audience that
expects a semantic model with defined measures and relationships. The other is anyone
following a link to a running deployment, plus the operator who needs to see pipeline state,
freshness and quality results without opening Airflow.

## Decision

Power BI holds the semantic model and the executive reporting: relationships, DAX measures,
and the reports built on them. Its model description, measure catalogue and refresh
configuration are documented in `docs/bi/`; the `.pbix` binary is not the source of truth for
anything, because a binary is not reviewable.

Streamlit is the public deployment and the operational tooling: pipeline state, source
freshness, data quality results and a small number of analytical screens. It reads DuckDB
with a read-only connection, per ADR 0002.

Metabase is rejected. It would sit between the two, adding a third service to run and a
second semantic definition to maintain, while being weaker than Power BI at the semantic
model and heavier than Streamlit for a public deployment.

Gold stays BI-tool agnostic. No column, grain or naming choice in gold exists to suit Power
BI or Streamlit, and any measure that both tools need is defined in gold rather than twice in
DAX and Python.

## Consequences

The semantic model lives in a proprietary tool on a desktop, which limits what CI can check:
Power BI changes are reviewed by their documentation, not by a diff of the model. Two
consumption tools mean two client implementations to keep working against the same gold
models. In exchange, the project shows both the enterprise reporting path and a deployment a
reader can open, and a change of BI tool stays a change of client rather than a rebuild of
gold.

## Alternatives considered

Streamlit alone: nothing to review as a semantic model, and no demonstration of the tool most
of the target roles actually use.

Power BI alone: no public deployment, and the operational views would have to be rebuilt as
reports over data the platform would have to expose for that purpose.
