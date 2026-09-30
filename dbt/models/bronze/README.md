# dbt/models/bronze

Bronze models, named `br_<source>__<entity>`, one per contract entity: 50 at specification 007.

**Generated, not hand-written.** Every file here but this one is written from `contracts/` by
`make dbt-generate`, and `make dbt-generate CHECK=1` fails CI on any difference. Change a contract
and regenerate; never edit a model.

Each model is a view over its entity's lake prefix, read through one macro, `bronze_read`
(`dbt/macros/bronze_read.sql`), which keeps only batches registered in `ops.batch_registry` and
casts every column to the contract's type (ADR 0018). A model carries the union of every
contract version's columns, the raw payload for a file, snapshot or API source, the four audit
columns `_ingested_at`, `_source_file`, `_batch_id` and `_source_system`, and the object key's
`_object_batch_id` and `_object_ingest_date`.

Bronze is not deduplicated. Its grain is the primary key plus `_batch_id`: the same key recurs
across batches by design, because reference data re-lands its book every run and incremental
entities re-land changed rows. Silver deduplicates.

No business logic, no joins, no renaming. A value that is wrong in the source is still wrong
here, by design. Identifiers arrive already tokenised; the vault that resolves them lives in
`meta` (ADR 0005), and dbt never reads it.
