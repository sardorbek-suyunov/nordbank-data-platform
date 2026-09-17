# dbt/macros

Project macros: surrogate key hashing, money and timestamp casting, as-of currency
conversion, and audit column injection. Identifier tokenisation happens in extraction, before
dbt sees the data, so it is not a macro.

SQL lives here when two or more models need it. Single-use SQL stays in the model that uses
it.

Populated from M4.
