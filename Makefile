PROJECT_NAME = kagura-memory-cloud
COMPOSE_FILE = docker-compose.yml
BACKEND_DIR = backend

.PHONY: all
all: help

.PHONY: help
help:
	@echo "Kagura Memory Cloud - Makefile Commands"
	@echo ""
	@echo "Docker Operations:"
	@echo "  make up          - Start all services"
	@echo "  make down        - Stop all services"
	@echo "  make restart     - Restart API container"
	@echo "  make logs        - View API logs (follow mode)"
	@echo "  make build       - Build API image"
	@echo "  make rebuild     - Rebuild API (no cache) and restart"
	@echo "  make ps          - Show running containers"
	@echo ""
	@echo "Backend Testing:"
	@echo "  make test            - Run all tests (Docker, default)"
	@echo "  make test-local      - Run unit tests (local, fast; excludes e2e/integration)"
	@echo "  make test-cov        - Run tests with coverage (Docker)"
	@echo "  make test-cov-local  - Run unit tests with coverage (local; excludes e2e/integration)"
	@echo "  make test-neural     - Run Neural Memory tests only"
	@echo "  make test-neural-cov - Run Neural Memory tests with coverage"
	@echo "  make test-smoke      - Run smoke tests (health, auth, well-known)"
	@echo "  make test-e2e        - Run E2E tests (memory lifecycle, rate limit)"
	@echo "  make test-integration - Run integration tests (DB, migrations, attachments)"
	@echo "  make test-urls       - Run URL validation (all routes != 500)"
	@echo "  make test-watch      - Run tests in watch mode (local)"
	@echo "  make lint            - Run linter (ruff)"
	@echo "  make format          - Format code (ruff)"
	@echo "  make type-check      - Type checking (pyright)"
	@echo ""
	@echo "Database:"
	@echo "  make migrate         - Run all pending migrations"
	@echo "  make migrate-create  - Create new migration (use: make migrate-create name='description')"
	@echo "  make migrate-down    - Rollback last migration"
	@echo "  make migrate-status  - Show current migration status and history"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean       - Remove Python cache files"
	@echo "  make clean-docker - Stop containers and remove volumes"
	@echo ""
	@echo "Health Check:"
	@echo "  make health      - Check API health"
	@echo "  make ping        - Ping all endpoints"
	@echo ""
	@echo "MCP:"
	@echo "  make mcp-test    - Test MCP server connection"

# ============================================================================
# Docker Operations
# ============================================================================

.PHONY: up
up:
	@echo "Starting all services..."
	docker-compose -f $(COMPOSE_FILE) up -d
	@echo "Services started. API: http://localhost:8080"

.PHONY: down
down:
	@echo "Stopping all services..."
	docker-compose -f $(COMPOSE_FILE) down
	@echo "Services stopped."

.PHONY: restart
restart:
	@echo "Restarting API container..."
	docker-compose -f $(COMPOSE_FILE) restart api
	@echo "API restarted."

.PHONY: logs
logs:
	@echo "Following API logs (Ctrl+C to exit)..."
	docker logs -f kagura-api

.PHONY: build
build:
	@echo "Building API image..."
	docker-compose -f $(COMPOSE_FILE) build api
	@echo "Build complete."

.PHONY: rebuild
rebuild:
	@echo "Rebuilding API (no cache)..."
	docker-compose -f $(COMPOSE_FILE) build --no-cache api
	@echo "Restarting services..."
	docker-compose -f $(COMPOSE_FILE) up -d
	@echo "Rebuild complete."

.PHONY: ps
ps:
	@docker ps --filter "name=kagura" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# ============================================================================
# Backend Testing
# ============================================================================

.PHONY: test
test:
	@echo "Running all tests in Docker..."
	docker exec kagura-api python -m pytest -v --maxfail=5
	@echo "Tests complete."

.PHONY: test-local
test-local:
	@echo "Running unit tests locally (excluding tests/e2e and tests/integration)..."
	cd $(BACKEND_DIR) && pytest -v --maxfail=5 --ignore=tests/e2e --ignore=tests/integration
	@echo "Backend tests complete."

.PHONY: test-frontend
test-frontend:
	@echo "Running frontend tests..."
	cd frontend && npm test
	@echo "Frontend tests complete."
	@echo "Tests complete."

.PHONY: test-smoke
test-smoke:
	@echo "Running smoke tests..."
	cd $(BACKEND_DIR) && pytest tests/smoke/ -v --timeout=30
	@echo "Smoke tests complete."

.PHONY: test-e2e
test-e2e:
	@echo "Running E2E tests..."
	cd $(BACKEND_DIR) && pytest tests/e2e/ -v --timeout=60
	@echo "E2E tests complete."

.PHONY: test-integration
test-integration:
	@echo "Running integration tests..."
	cd $(BACKEND_DIR) && pytest tests/integration/ -v --timeout=120
	@echo "Integration tests complete."

.PHONY: test-urls
test-urls:
	@echo "Running URL validation tests..."
	cd $(BACKEND_DIR) && pytest tests/smoke/test_all_routes.py -v --timeout=60
	@echo "URL validation tests complete."

.PHONY: test-cov
test-cov:
	@echo "Running tests with coverage in Docker..."
	docker exec kagura-api python -m pytest --cov=src --cov-report=html --cov-report=term-missing
	@echo "Coverage report: backend/htmlcov/index.html"

.PHONY: test-cov-local
test-cov-local:
	@echo "Running unit tests with coverage locally (excluding tests/e2e and tests/integration)..."
	cd $(BACKEND_DIR) && pytest --cov=src --cov-report=html --cov-report=xml --cov-report=term-missing --ignore=tests/e2e --ignore=tests/integration
	@echo "Coverage report: backend/htmlcov/index.html"

.PHONY: test-unit
test-unit:
	@echo "Running unit tests (no DB required)..."
	cd $(BACKEND_DIR) && pytest tests/api/ tests/neural/ tests/utils/ tests/auth/ -v --ignore=tests/integration --ignore=tests/e2e

.PHONY: coverage-upload
coverage-upload:
	@echo "Running unit tests with coverage and uploading to Codecov..."
	cd $(BACKEND_DIR) && pytest tests/api/ tests/auth/ tests/smoke/ tests/neural/test_hebbian.py -v --cov=src --cov-report=xml --cov-report=term-missing || true
	@CODECOV_TOKEN=$${CODECOV_TOKEN:-$$(grep '^CODECOV_TOKEN=' .env.local 2>/dev/null | cut -d= -f2)}; \
	if [ -z "$$CODECOV_TOKEN" ]; then echo "Error: CODECOV_TOKEN not set (add to .env.local)"; exit 1; fi; \
	cd $(BACKEND_DIR) && codecovcli upload-process --token $$CODECOV_TOKEN -f coverage.xml \
		--sha $$(git rev-parse HEAD) \
		--slug kagura-ai/memory-cloud \
		--git-service github
	@echo "Coverage uploaded to Codecov."

.PHONY: test-neural
test-neural:
	@echo "Running Neural Memory tests..."
	docker exec kagura-api python -m pytest tests/neural/ -v
	@echo "Neural Memory tests complete."

.PHONY: test-neural-cov
test-neural-cov:
	@echo "Running Neural Memory tests with coverage..."
	docker exec kagura-api python -m pytest tests/neural/ -v --cov=src/neural --cov-report=html --cov-report=term-missing
	@echo "Coverage report: backend/htmlcov/index.html"

.PHONY: test-watch
test-watch:
	@echo "Running tests in watch mode (local)..."
	cd $(BACKEND_DIR) && pytest-watch

.PHONY: lint
lint: lint-models-no-column
	@echo "Running linter..."
	cd $(BACKEND_DIR) && ruff check src/ tests/
	@echo "Lint complete."

# Guard against drift back to the legacy SQLAlchemy 1.x Column() pattern in
# model files (#596 / epic #370). The regex accepts both forms the half-
# migration trap produces:
#
#   - bare:        `name = Column(...)`
#   - annotated:   `id: int = Column(...)`  ← pyright accepts but SA 2.0 does
#                                              NOT recognize as a Mapped attr
#
# POSIX character classes ([[:space:]] / [[:alnum:]_]) are used instead of
# the GNU \s / \w shorthand so the guard is portable across GNU grep
# (Linux CI) and BSD grep (macOS dev). The shorthand would be treated as
# literal `s` / `w` on BSD and silently let regressions through.
#
# Negative-case smoke test lives at backend/tests/test_models_no_column_guard.py.
.PHONY: lint-models-no-column
lint-models-no-column:
	@! grep -rnE '^[[:space:]]+[[:alnum:]_]+(:[[:space:]]*[^=]+)?[[:space:]]*=[[:space:]]*Column\(' $(BACKEND_DIR)/src/models/ \
	  || (echo "ERROR: legacy 'Column(...)' usage detected in $(BACKEND_DIR)/src/models/. Use 'Mapped[T] = mapped_column(...)' instead. The annotated half-migration form 'id: int = Column(...)' is also rejected because SQLAlchemy 2.0 does not recognize it as a Mapped attribute (it silently fails type-resolution)."; exit 1)

.PHONY: format
format:
	@echo "Formatting code..."
	cd $(BACKEND_DIR) && ruff format src/ tests/
	@echo "Format complete."

.PHONY: type-check
type-check:
	@echo "Type checking..."
	cd $(BACKEND_DIR) && pyright src/
	@echo "Type check complete."

# ============================================================================
# Database Migrations
# ============================================================================

.PHONY: migrate
migrate:
	@echo "Running migrations..."
	cd $(BACKEND_DIR) && alembic upgrade head
	@echo "Migrations complete."

.PHONY: migrate-create
migrate-create:
	@echo "Creating migration: $(name)"
	cd $(BACKEND_DIR) && alembic revision --autogenerate -m "$(name)"
	@echo "Migration created."

.PHONY: migrate-down
migrate-down:
	@echo "Rolling back last migration..."
	cd $(BACKEND_DIR) && alembic downgrade -1
	@echo "Rollback complete."

.PHONY: migrate-status
migrate-status:
	@echo "Migration status:"
	cd $(BACKEND_DIR) && alembic current
	@echo ""
	@echo "Migration history:"
	cd $(BACKEND_DIR) && alembic history --verbose

# ============================================================================
# Cleanup
# ============================================================================

.PHONY: clean
clean:
	@echo "Cleaning Python cache files..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleanup complete."

.PHONY: clean-docker
clean-docker:
	@echo "Stopping containers and removing volumes..."
	docker-compose -f $(COMPOSE_FILE) down -v
	@echo "Pruning Docker system..."
	docker system prune -f
	@echo "Docker cleanup complete."

# ============================================================================
# Health Check
# ============================================================================

.PHONY: health
health:
	@echo "Checking API health..."
	@curl -s http://localhost:8080/health || echo "API not responding"

.PHONY: ping
ping:
	@echo "=== Root ==="
	@curl -s http://localhost:8080/ 2>/dev/null || echo "Not responding"
	@echo ""
	@echo "=== Health ==="
	@curl -s http://localhost:8080/health 2>/dev/null || echo "Not responding"
	@echo ""
	@echo "=== OAuth2 Resource Metadata ==="
	@curl -s http://localhost:8080/.well-known/oauth-protected-resource 2>/dev/null || echo "Not responding"
	@echo ""

# ============================================================================
# MCP Testing
# ============================================================================

.PHONY: mcp-test
mcp-test:
	@echo "Testing MCP server..."
	@echo "Note: Requires Claude Code or MCP client"
	@echo "Connection URL: http://localhost:8080/mcp/sse"
	@echo ""
	@echo "Available tools:"
	@echo "  - remember: Store memory"
	@echo "  - recall: Search with Neural Memory"
	@echo "  - forget: Delete memory"
	@echo "  - reference: Get full details"
	@echo "  - explore: Graph traversal"

# ============================================================================
# Development
# ============================================================================

.PHONY: dev
dev: up logs

.PHONY: stop
stop: down
