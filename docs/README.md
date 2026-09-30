# docs

Hand-written documentation for the platform: architecture, conventions, requirements,
runbook, decision records and milestone checkpoints.

A document may contain a section generated from another source, and only between markers that
name the script that writes it, with a mechanical check that fails CI when the section and its
source disagree. The rest of the document stays hand-written. One such section exists: the
bronze models in `model_inventory.md`, written from `contracts/` by `make dbt-generate` and
checked by `make dbt-generate CHECK=1` (specification 007).

Specifications under `specs/` are the contract that implementation work is measured
against. The other documents describe the system as it is built. Diagrams belong in
`diagrams/`, consumption-layer documentation in `bi/`, milestone reports in `checkpoints/`.
