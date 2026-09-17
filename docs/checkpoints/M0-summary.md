# M0 — Repository foundation and conventions

Specification: [000-repository-foundation.md](../specs/000-repository-foundation.md)
Status: complete
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
  `docs/business_questions.md` (18 questions with persona, target mart and grain),
  `docs/data_dictionary.md` (18 entities), `docs/runbook.md` skeleton, four ADRs.
- CI: `.github/workflows/ci.yml` with a `lint` job and a `docs` job, both passing on a
  repository that contains no code yet.

## Deliberately deferred

| Deferred | To |
|---|---|
| docker-compose, Postgres, MinIO, Airflow services, service endpoints in the runbook | M1 |
| Source DDL, `core` and `ref` schemas, initial historical load, first tests | M2 |
| Daily mutation engine: updates, soft deletes, late arrivals, schema drift | M3 |
| dbt project initialisation, bronze models, data contracts, quarantine, sqlfluff with the dbt templater | M4 |
| Silver conformance, SCD2, PII tokenisation | M5 |
| Gold dimensional model and the marts named in the business questions | M6 |
| Data quality gates, reconciliation, ops observability, escalation procedure | M7 |
| Lineage, GDPR deletion, access control | M8 |
| Power BI semantic model, Streamlit application, `docs/bi/` content | M9 |
| BigQuery target, Terraform | M10 |

Column-level data dictionary is deferred to spec 002, as stated in spec 000 section 3.

## Known gaps

- `make test` tolerates pytest exit code 5, which means no tests were collected. The
  tolerance is removed at M2, when the first tests exist. Until then the target cannot fail
  for the right reason.
- `make lint` skips SQL because there is no SQL. The sqlfluff configuration has been
  validated against a sample dbt model with `ref`, `source` and `config` calls, but no model
  in the repository exercises it yet.
- The architecture diagram is a link to a directory, not a diagram. Authored at M1.
- Four deviations from the approved specification are recorded as amendments in spec 000.
- Milestone identifiers M1 to M10 are referenced by the Makefile stubs and the README table.
  They are a plan, not a commitment to scope, and a later spec may resequence them.

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
