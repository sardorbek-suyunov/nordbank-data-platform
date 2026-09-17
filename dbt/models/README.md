# dbt/models

Model tree, one directory per medallion layer.

What each layer guarantees is defined in `docs/architecture.md`; naming rules are in
`docs/conventions.md`. References run in one direction only: gold reads silver, silver reads
bronze, bronze reads the landed source data.
