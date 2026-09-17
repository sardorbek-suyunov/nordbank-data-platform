# dbt/models/bronze

Bronze models, named `br_<source>__<entity>`.

One model per source entity: typed, deduplicated on the source key and batch, and carrying
the audit columns `_ingested_at`, `_source_file`, `_batch_id`, `_source_system`.

No business logic, no joins, no renaming beyond casing. A value that is wrong in the source
is still wrong here, by design.

Populated from M4.
