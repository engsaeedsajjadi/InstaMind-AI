# InstaMind AI — developer entry points.
#
#   make help          list targets
#   make setup         first-time setup
#   make dev           run the full stack
#   make test          run the backend test suite

SHELL := /bin/bash
BACKEND := apps/backend
PYTHON  ?= python3
PIP     ?= $(PYTHON) -m pip

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------- setup
.PHONY: setup
setup: ## Install backend dev dependencies
	$(PIP) install -e "$(BACKEND)[dev]"
	@echo "Done. Copy .env.example to .env and fill in the secrets."

.PHONY: env
env: ## Create .env from the template (never overwrites)
	@test -f .env && echo ".env already exists — leaving it alone." || cp .env.example .env

# ----------------------------------------------------------------------- run
.PHONY: dev
dev: ## Run the full stack (Postgres, Redis, MinIO, API, worker, frontend)
	docker compose up --build

.PHONY: dev-observability
dev-observability: ## Run the stack including Prometheus and Grafana
	docker compose --profile observability up --build

.PHONY: api
api: ## Run only the API locally (needs a reachable database)
	cd $(BACKEND) && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: worker
worker: ## Run the Celery worker locally
	cd $(BACKEND) && celery -A app.worker.celery_app worker -Q publishing,default -l info

.PHONY: beat
beat: ## Run the Celery beat scheduler locally
	cd $(BACKEND) && celery -A app.worker.celery_app beat -l info

.PHONY: down
down: ## Stop the stack
	docker compose down

.PHONY: nuke
nuke: ## Stop the stack and delete all volumes (destroys data)
	docker compose down -v

# ------------------------------------------------------------------ database
.PHONY: migrate
migrate: ## Apply pending migrations
	cd $(BACKEND) && alembic upgrade head

.PHONY: migration
migration: ## Create a new migration: make migration m="add foo table"
	cd $(BACKEND) && alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	cd $(BACKEND) && alembic downgrade -1

.PHONY: psql
psql: ## Open a psql shell in the dev database
	docker compose exec postgres psql -U instamind -d instamind

# --------------------------------------------------------------------- tests
.PHONY: test
test: ## Run the backend test suite
	cd $(BACKEND) && $(PYTHON) -m pytest -q

.PHONY: test-cov
test-cov: ## Run tests with coverage
	cd $(BACKEND) && $(PYTHON) -m pytest --cov=app --cov-report=term-missing

.PHONY: test-watch
test-watch: ## Re-run tests on change (needs pytest-watch)
	cd $(BACKEND) && $(PYTHON) -m pytest -q --lf

.PHONY: lint
lint: ## Ruff + mypy
	cd $(BACKEND) && $(PYTHON) -m ruff check app tests && $(PYTHON) -m mypy app || true

.PHONY: format
format: ## Auto-format with ruff
	cd $(BACKEND) && $(PYTHON) -m ruff format app tests && $(PYTHON) -m ruff check --fix app tests

# ------------------------------------------------------------------ utilities
.PHONY: openapi
openapi: ## Dump the OpenAPI spec to docs/openapi.json
	cd $(BACKEND) && $(PYTHON) -c "import json;from app.main import app;print(json.dumps(app.openapi(),indent=2))" > ../../docs/openapi.json

.PHONY: check
check: lint test ## Lint + test (what CI runs)

.PHONY: clean
clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
