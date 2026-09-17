SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

.PHONY: help install lint format test up down health logs seed tick dbt-build dbt-docs dq clean

help: ## List the available targets
	@echo "nordbank-data-platform targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  %-11s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create the virtual environment and install the git hooks
	uv sync
	uv run pre-commit install

lint: ## Check Python formatting and lint rules, and SQL when SQL exists
	uv run ruff check .
	uv run ruff format --check .
	uv run python scripts/lint_sql.py

format: ## Apply ruff formatting and the safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

# Exit code 5 is pytest reporting that it collected no tests. Drop the tolerance at M2,
# which adds the first tests.
test: ## Run the test suite
	uv run pytest -q || [ $$? -eq 5 ]

up: ## Start the local stack
	@echo "not implemented until M1"

down: ## Stop the local stack and keep the volumes
	@echo "not implemented until M1"

health: ## Report the health of every service in the local stack
	@echo "not implemented until M1"

logs: ## Follow the logs of the local stack
	@echo "not implemented until M1"

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
