# dbt/tests

Singular dbt tests: assertions that cannot be expressed as a generic test in a schema file.
Generic tests are declared in the model schema files and defined in `macros/`.

- `macros/`: tests of the macros alone, on inline fixtures, referencing no model, so
  `make dbt-prove` runs them with no stack and plants a defect in a copy of the macros for each.
  The exact conversion's types and values, the publication instant, the version rule, an inactive
  reference row, latest state, a backdated address joined by business validity, and name
  normalisation on diacritics, case and runs of spaces.
- `silver/`: tests over the built silver models and the bronze they read, such as the version
  rule on accounts re-derived from bronze and `sl_fx_rates` against the conversion rule restated
  without the macros.

A test returns the rows that violate it, naming keys and instants and never a value an
identifier, quasi-identifier or sensitive column holds, and never stores its failures.
