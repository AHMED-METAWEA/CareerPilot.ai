# CareerPilot — common operations. Run from the repository root.

BACKEND := backend
PY := $(BACKEND)/.venv/bin/python
PIP := $(BACKEND)/.venv/bin/pip

.PHONY: help setup db-up db-down migrate revision sync run-source worker api test test-unit lint format typecheck imports check stats validate-boards clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install dependencies
	python3 -m venv $(BACKEND)/.venv
	$(PIP) install --upgrade pip
	cd $(BACKEND) && .venv/bin/pip install -e ".[dev]"

db-up: ## Start PostgreSQL + pgvector
	docker compose -f infra/docker-compose.yml up -d db

db-down: ## Stop the database
	docker compose -f infra/docker-compose.yml down

migrate: ## Apply migrations
	cd $(BACKEND) && .venv/bin/alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	cd $(BACKEND) && .venv/bin/alembic revision --autogenerate -m "$(m)"

sync: ## Load config/boards/*.yaml into job_sources
	cd $(BACKEND) && .venv/bin/python -m app.cli sources sync

run-source: ## Run one source in the foreground: make run-source name=greenhouse:vercel
	cd $(BACKEND) && .venv/bin/python -m app.cli sources run $(name)

worker: ## Run the worker (scheduler + queue consumers)
	cd $(BACKEND) && .venv/bin/python -m app.cli worker

api: ## Run the API with reload
	cd $(BACKEND) && .venv/bin/uvicorn app.main:app --reload --port 8000

stats: ## Corpus statistics against the Phase 0 exit criteria
	cd $(BACKEND) && .venv/bin/python -m app.cli stats

validate-boards: ## Probe every curated board and report what it returns
	cd $(BACKEND) && .venv/bin/python -m app.cli boards validate

web-setup: ## Install frontend dependencies
	cd frontend && npm install

web: ## Run the web client (expects the API on :8000)
	cd frontend && npm run dev

web-check: ## Type-check, lint and build the web client
	cd frontend && npm run typecheck && npm run lint && npm run build

test: ## Full test suite
	cd $(BACKEND) && .venv/bin/pytest -q

test-unit: ## Domain tests only (no database required)
	cd $(BACKEND) && .venv/bin/pytest tests/unit -q

lint: ## ruff
	cd $(BACKEND) && .venv/bin/ruff check app tests

format: ## ruff --fix + format
	cd $(BACKEND) && .venv/bin/ruff check --fix app tests && .venv/bin/ruff format app tests

typecheck: ## mypy strict
	cd $(BACKEND) && .venv/bin/mypy app

imports: ## Import-discipline contracts (§14.1)
	cd $(BACKEND) && .venv/bin/lint-imports

check: lint typecheck imports test ## Everything CI runs for the backend

check-all: check web-check ## Backend and web client

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + ; rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
