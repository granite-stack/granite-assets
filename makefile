# Load variables from .env if it exists
ifneq (,$(wildcard .env))
include .env
export
endif

# Project variables
PART ?= patch  # can be overwritten with: make bump-version PART=minor

SONAR_HOST_URL ?= http://localhost:9000
SONAR_TOKEN ?=
PY_SRC ?= src


# Delete the virtual environment and force a sync
venv:
	rm -rf .venv && \
	echo "Deleted virtual environment" && \
	uv sync && \
	echo "Created virtual environment" && \
	uvx --from=toml-cli toml get --toml-path=pyproject.toml project.version

# Bump patch/minor/major version
bump-version:
	@v=$$(uvx --from=toml-cli toml get --toml-path=pyproject.toml project.version) && \
	echo "Current version: $$v" && \
	uvx --from bump2version bumpversion --allow-dirty --current-version "$$v" $(PART) pyproject.toml && \
	echo "Version bumped to new $(PART)"

# Build python package
build: bump-version
	uv build

# Clean build artifacts
clean-artifacts:
	rm -rf dist *.egg-info build && \
	echo "Cleaned build artifacts"

clean: clean-artifacts ## Clean temporary files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true

# Publish package on PyPI (use UV_PYPI_TOKEN or .pypirc for authentication)
publish: build
	uv publish

# Publish on TestPyPI
publish-test: build
	uv publish --repository testpypi

# Run linting with ruff
lint:
	uv run ruff check src/ tests/

format: ## Format code
	uv run ruff format .

# Run type checking with mypy
type-check:
	uv run mypy src/

# Run security checks with bandit
security-check:
	uv run bandit -r src/

# Run all quality checks
check: lint format type-check security-check
	echo "All checks completed"

test: ## Run tests
	uv run pytest -v

test-cov: ## Run tests with coverage report
	uv run pytest --cov --cov-report=xml --cov-report=term --cov-report=html

# Install project in development mode
install:
	uv sync

# Install project with all optional dependencies
install-all:
	uv sync --all-extras

# Run the application
run:
	uv run python -m granite_assets

# Open Python REPL with project environment
shell:
	uv run python

# Show project info
info:
	@echo "Project: granite-assets"
	@echo "Version: $$(uvx --from=toml-cli toml get --toml-path=pyproject.toml project.version)"
	@echo "Python: $$(uv run python --version)"
	@echo "Virtual env: $$(if [ -d .venv ]; then echo ".venv exists"; else echo ".venv not found"; fi)"

# Build documentation
docs:
	uv run sphinx-build docs_source docs

# Serve documentation locally
docs-serve: docs
	uv run python -m http.server 8000 --directory docs

# Help target
help:
	@echo "Available targets:"
	@echo "  venv          - Delete and recreate virtual environment"
	@echo "  install       - Install project dependencies"
	@echo "  install-all   - Install with all optional dependencies"
	@echo "  bump-version  - Bump version (PART=patch|minor|major)"
	@echo "  build         - Build package"
	@echo "  publish       - Publish to PyPI"
	@echo "  publish-test  - Publish to TestPyPI"
	@echo "  test          - Run tests with coverage"
	@echo "  lint          - Run linting"
	@echo "  format        - Format code"
	@echo "  type-check    - Run type checking"
	@echo "  security-check- Run security checks"
	@echo "  check         - Run all quality checks"
	@echo "  docs          - Build documentation"
	@echo "  docs-serve    - Serve documentation locally"
	@echo "  run           - Run the application"
	@echo "  shell         - Open Python REPL"
	@echo "  clean         - Clean build artifacts"
	@echo "  info          - Show project information"
	@echo "  help          - Show this help"

sonar: ## Run SonarQube analysis with pysonar
	@test -n "$(SONAR_TOKEN)" || (echo "ERROR: define SONAR_TOKEN"; exit 1)
	SONAR_HOST_URL=$(SONAR_HOST_URL) SONAR_TOKEN=$(SONAR_TOKEN) uv run pysonar

sonar-check: test-cov sonar ## Run coverage and SonarQube analysis

.PHONY: venv bump-version build clean-artifacts clean publish publish-test lint format type-check security-check check test test-cov install install-all run shell info docs docs-serve help sonar sonar-check
