# analytics

Consumption-side code that reads the gold layer.

Read-only by contract: nothing here writes to the warehouse or to the lake, and nothing here
triggers a pipeline run.

Contains `streamlit_app/`. Power BI artifacts are documented in `docs/bi/` instead, because
the model is not reviewable as text in this repository.
