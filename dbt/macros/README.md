# dbt/macros

Project macros. SQL lives here when two or more models need it; single-use SQL stays in the
model that uses it. Identifier tokenisation happens in extraction, before dbt sees the data, so
it is not a macro.

- `bronze_read.sql`, `bronze_tests.sql`, `bronze_guard.sql`: how bronze reads the lake, its
  generic tests, and the structural rules checked before a build (specification 007).
- `historisation.sql`: `scd2`, `latest_state` and business validity (specification 008, ADR 0020).
- `fx.sql`: the exact conversion, the publication instant, the landed rates and the provenance
  every converted fact carries (ADR 0019).
- `generalisation.sql`: the age and tenure band intervals.
- `silver_tests.sql`: the generic tests silver models carry.
- `gold_guard.sql`: no gold model selects a quasi-identifier.
- `generate_schema_name.sql`: custom schemas used as written.
