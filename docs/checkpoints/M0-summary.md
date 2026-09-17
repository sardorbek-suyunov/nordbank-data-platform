# M0 — Repository foundation and conventions

Specification: [000-repository-foundation.md](../specs/000-repository-foundation.md)
Status: complete, including the remediation pass of 2026-09-17
Date: 2026-09-17

## What was built

Repository skeleton, engineering conventions and the requirements baseline. No service runs
at this milestone and no pipeline code exists.

- Directory structure with a purpose README in every directory, stating what the directory
  owns and where its boundary is.
- Tooling: `pyproject.toml` (Python 3.11, uv, dev group), committed `uv.lock`, ruff
  configuration in `pyproject.toml` (line length 100, `E,F,I,N,UP,B,SIM,PTH`), `.sqlfluff`
  (duckdb dialect, jinja templater with dbt macro stubs), `.pre-commit-config.yaml`,
  `.gitignore`, `.env.example` with a comment per variable, MIT `LICENSE`.
- A self-documenting `Makefile` with fifteen targets. The ones whose dependency does not
  exist yet report the milestone that implements them.
- Three scripts behind the Makefile and CI: `scripts/lint_sql.py`, `scripts/clean.py`,
  `scripts/check_docs.py`.
- Documentation: `README.md`, `docs/architecture.md`, `docs/conventions.md`,
  `docs/business_questions.md` (19 questions with persona, target mart and grain),
  `docs/metric_definitions.md` (the exact rule behind every contested term),
  `docs/data_dictionary.md` (18 entities), `docs/runbook.md` skeleton, five ADRs.
- CI: `.github/workflows/ci.yml` with a `lint` job and a `docs` job, both passing on a
  repository that contains no code yet.

## What the remediation pass changed

The first delivery had design contradictions and a repository hygiene failure. Each change is
recorded as a dated amendment in spec 000; the summary is:

- Agent and editor configuration is excluded from the repository; the patterns are in
  `.gitignore`. One such file had been committed, so history was rewritten with
  `git filter-repo` and `main` was force-pushed. Commit hashes before the rewrite no longer
  exist.
- PII handling is crypto-shredding: tokenise at ingest, keep the reversible mapping in a vault
  in `meta`, erase by deleting the vault entry. Bronze stays immutable. ADR 0005 records it.
- Ingestion correctness: watermark extraction reads with an explicit `EXTRACT_LAG` overlap,
  physical deletes are documented as undetectable with a reconciliation mitigation at M7, and
  bronze keeps the raw payload beside the typed columns with failures quarantined.
- Currency conversion is an as-of join that is never restated, with mandatory rate provenance
  columns.
- Schema drift: additive columns are accepted and logged, anything else quarantines the batch.
- Three scale profiles (`ci`, `dev`, `full`) replace two.
- The consumption path is concrete: Power BI over exported Parquet until M10, then BigQuery;
  Streamlit read-only over DuckDB.
- Conventions gained deterministic silver naming, a numeric precision rule, reserved dimension
  members, an SCD2 join policy, a primary key testing rule and the rate provenance rule.
- `login_sessions` gained a consuming question (19) and the coverage rule that would have
  caught it. `branches` became `agent_locations`, which is what a branchless bank actually has.

## Deliberately deferred

| Deferred | To |
|---|---|
| docker-compose, Postgres, MinIO, Airflow services, service endpoints in the runbook | M1 |
| Source DDL, `core` and `ref` schemas, initial historical load, first tests | M2 |
| Daily mutation engine: updates, soft deletes, late arrivals, schema drift | M3 |
| dbt project initialisation, bronze models, data contracts, quarantine, identifier tokenisation, sqlfluff with the dbt templater | M4 |
| Silver conformance, SCD2, token resolution | M5 |
| Gold dimensional model and the marts named in the business questions | M6 |
| Data quality gates, settlement and primary-key reconciliation, ops observability, escalation procedure | M7 |
| Lineage, erasure DAG and the PII vault, access control | M8 |
| Power BI semantic model, Streamlit application, `docs/bi/` content | M9 |
| BigQuery target, Terraform | M10 |

Column-level data dictionary is deferred to spec 002, as stated in spec 000 section 3.

## Known gaps

- Two metric definitions are open and marked in `docs/metric_definitions.md`: the
  inter-regional and commercial interchange rates, and the funding cost assumption behind the
  net interest income proxy. Both must be resolved before M6, and both block only the marts
  that consume them.
- `make test` tolerates pytest exit code 5, which means no tests were collected. The
  tolerance is removed at M2, when the first tests exist. Until then the target cannot fail
  for the right reason.
- `make lint` skips SQL because there is no SQL. The sqlfluff configuration has been
  validated against a sample dbt model with `ref`, `source` and `config` calls, but no model
  in the repository exercises it yet.
- `ruff format --check .` reports 46 files: 3 Python files and 43 Markdown files. Ruff opens
  Markdown to format embedded Python code blocks, which was verified against a test file. None
  of the 43 Markdown files currently contains a Python block, so no documentation prose is
  formatted or checked by that number. It measures files opened, not files with formatting
  enforced.
- The architecture diagram is a link to a directory, not a diagram. Authored at M1.
- The milestone table in `README.md` labels M5 as PII tokenisation. Tokenisation now happens
  at ingest in M4; the M5 label is kept as agreed and refers to token resolution in silver.
- Every deviation from the approved specification is recorded as an amendment in spec 000.
- Milestone identifiers M1 to M10 are a plan, not a commitment to scope, and a later spec may
  resequence them.

## Verifying the repository

From the repository root. On Windows, run these from Git Bash with GNU make on the PATH.

```bash
uv sync                  # create .venv from the committed uv.lock
make                     # print the target list
make lint                # ruff check, ruff format --check, sqlfluff when SQL exists
make test                # pytest; exit code 5 (no tests collected) is tolerated until M2
make clean               # remove caches, dbt output, logs and the local warehouse file
uv run python scripts/check_docs.py   # the checks the CI docs job runs
git log --oneline        # commit history for this milestone
```

CI runs the same `lint` and `docs` checks on every push and pull request.
