# analytics/streamlit_app

Streamlit application: the public deployment of the platform and the operational views over
pipeline state, data quality results and source freshness.

Connects to DuckDB with a read-only connection, per the single-writer constraint in
ADR 0002.

Populated from M9.
