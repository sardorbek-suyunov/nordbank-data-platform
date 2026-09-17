# dbt/models/bronze

Bronze models, named `br_<source>__<entity>`.

One model per source entity: typed, deduplicated on the source key and batch, carrying the
raw payload alongside the parsed columns and the audit columns `_ingested_at`,
`_source_file`, `_batch_id`, `_source_system`. Direct identifiers arrive already tokenised;
the vault that resolves them lives in `meta` (ADR 0005).

No business logic, no joins, no renaming beyond casing. A value that is wrong in the source
is still wrong here, by design.

Populated from M4.
