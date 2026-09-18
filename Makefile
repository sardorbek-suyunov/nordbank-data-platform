SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

.PHONY: help install lint format test test-dags test-integration up down nuke health logs \
	verify-dag seed tick dbt-build dbt-docs dq clean

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

test-integration: ## Run the smoke tests inside the running stack
	@echo "test-integration: running inside airflow-scheduler, where the volumes and network are"
	docker compose exec -T airflow-scheduler bash -c "cd /opt/airflow && pytest -q -m integration tests"

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

seed: ## Load the initial historical dataset into the source database
	@echo "not implemented until M2"

tick: ## Generate one business day of source mutations
	@echo "not implemented until M3"

dbt-build: ## Run dbt build against the active target
	@echo "not implemented until M4"

dbt-docs: ## Generate and serve the dbt documentation site
	@echo "not implemented until M4"

dq: ## Run the data quality gates and write results to the dq schema
	@echo "not implemented until M7"

clean: ## Remove caches, dbt output, service logs and the local warehouse file
	uv run python scripts/clean.py
