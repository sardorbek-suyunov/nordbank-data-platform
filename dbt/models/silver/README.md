# dbt/models/silver

Silver models, named `sl_<entity>`.

Conformed, business-keyed entities with SCD2 history where the source mutates, amounts
normalised to `DECIMAL(18,4)` with their rate provenance, and timestamps in UTC. Identifiers
arrive already tokenised from ingest. SCD2 models carry `_valid_from`, `_valid_to` and
`_is_current`.

Joins across sources are allowed here; aggregation to a reporting grain is not.

Populated from M5.
