# generator

Synthetic core banking source system: the initial historical load and the daily mutation
engine that produces inserts, updates, soft deletes and late-arriving records.

Owns the contents of the `core` and `ref` schemas in the source Postgres database, and
nothing downstream of it. The generator writes only to the source database; it never writes
to the lake or to the warehouse.

Built at M2 (historical load) and M3 (mutation engine).
