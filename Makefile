SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

.PHONY: help install init-env lint format test test-dags test-integration up down nuke health \
	logs verify-dag schema-apply schema-dump schema-check warehouse-apply contracts-bootstrap \
	contracts-diff extract backfill bronze-stats seed seed-verify seed-manifest tick tick-to \
	tick-status tick-acceptance dbt-build dbt-docs dq clean

help: ## List the available targets
	@echo "nordbank-data-platform targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create the virtual environment and install the git hooks
	uv sync
	uv run pre-commit install

init-env: ## Generate .env from the template, filling every generated secret
	uv run python scripts/init_env.py

lint: ## Check Python formatting and lint rules, and SQL when SQL exists
	uv run ruff check .
	uv run ruff format --check .
	uv run python scripts/lint_sql.py

format: ## Apply ruff formatting and the safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

test: ## Run the unit tests, which need neither Airflow nor a running stack
	uv run pytest -q -m "not dags and not integration"

test-dags: ## Run the DAG integrity tests, natively or in the project image
	uv run python scripts/run_dag_tests.py

test-integration: ## Run the smoke tests inside the running stack, then check for schema drift
	@echo "test-integration: running inside airflow-scheduler, where the volumes and network are"
	docker compose exec -T airflow-scheduler bash -c "cd /opt/airflow && pytest -q -m integration tests"
	$(MAKE) schema-check
	@echo "test-integration: generator integration tests run on the host, where the Docker socket is"
	uv run pytest -q -m integration generator/tests
	$(MAKE) seed-verify

up: ## Generate .env if absent, validate it, then start the stack and wait for health
	uv run python scripts/stack_up.py

down: ## Stop the local stack and keep the volumes
	docker compose down

nuke: ## Stop the stack and delete every volume (FORCE=1 skips the prompt)
	uv run python scripts/stack_nuke.py

health: ## Probe every component (STRICT=1 treats a busy warehouse as a failure)
	uv run python scripts/stack_health.py

logs: ## Follow the stack logs (SERVICE=<name> to filter)
	docker compose logs --follow $(SERVICE)

verify-dag: ## Trigger the health-check DAG from the CLI and verify it from the task records
	uv run python scripts/stack_verify_dag.py

schema-apply: ## Apply the source DDL, the reference seeds and the column classifications
	uv run python scripts/schema_apply.py

schema-dump: ## Print the normalised live schema (OUT=<path> writes it to a file instead)
ifdef OUT
	uv run python scripts/dump_schema.py > $(OUT)
	@echo "schema-dump: wrote $(OUT)"
else
	uv run python scripts/dump_schema.py
endif

schema-check: ## Fail if the live schema and the committed data dictionary disagree
	uv run python scripts/schema_check.py

warehouse-apply: ## Apply the warehouse operational schema (runs inside the stack)
	@echo "warehouse-apply: running inside airflow-scheduler, where the warehouse volume is"
	docker compose exec -T airflow-scheduler bash -c "python /opt/airflow/scripts/warehouse_apply.py"

contracts-bootstrap: ## Write a contract from the dictionary for any entity that has none
	uv run python scripts/contracts_bootstrap.py

contracts-diff: ## Report contract divergence from the dictionary (CHECK=1 fails on it)
ifdef CHECK
	uv run python scripts/contracts_diff.py --check
else
	uv run python scripts/contracts_diff.py
endif

seed: ## Generate and load the historical dataset (NORDBANK_ENV, _SEED, _ANCHOR_DATE)
	uv run python -m generator

seed-verify: ## Run the coherence invariants against the loaded database
	uv run python -m generator --verify

seed-manifest: ## Write the run manifest (CHECK=1 compares against the committed one)
ifdef CHECK
	uv run python -m generator --manifest --check
else
	uv run python -m generator --manifest
endif

tick: ## Advance the simulated source by one business day (DATE=YYYY-MM-DD to name it)
ifdef DATE
	uv run python -m generator.mutation --date $(DATE)
else
	uv run python -m generator.mutation
endif

tick-to: ## Advance the simulated source to DATE, one transaction per day
	uv run python -m generator.mutation --to $(DATE)

tick-status: ## Print the simulation state and the last ten ticks
	uv run python -m generator.mutation --status

tick-acceptance: ## Seed, tick and report specification 004's evidence (REPLAY=1, TICKS=n)
ifdef REPLAY
	uv run python scripts/tick_acceptance.py --replay --ticks $(or $(TICKS),60)
else
	uv run python scripts/tick_acceptance.py --ticks $(or $(TICKS),60)
endif

dbt-build: ## Run dbt build against the active target
	@echo "not implemented until M4"

dbt-docs: ## Generate and serve the dbt documentation site
	@echo "not implemented until M4"

dq: ## Run the data quality gates and write results to the dq schema
	@echo "not implemented until M7"

clean: ## Remove caches, dbt output, service logs and the local warehouse file
	uv run python scripts/clean.py
