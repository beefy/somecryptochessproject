# Developer entry points (docs/IMPLEMENTATION_PLAN.md sections 4 and 7.2).
#
# Everything runs inside .venv so the same commands work locally and in CI.
PYTHON ?= .venv/bin/python
PYTEST_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help venv install test cov cov-gate lint format typecheck check lock clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*## "}; {printf "%-12s %s\n", $$1, $$2}'

venv:  ## Create .venv if it does not exist
	@test -x $(PYTHON) || python3 -m venv .venv

install: venv  ## Install the package with dev extras
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

test: venv  ## Run the unit/property test suite
	$(PYTHON) -m pytest $(PYTEST_ARGS)

cov: venv  ## Run tests with a whole-package coverage report
	$(PYTHON) -m pytest --cov=sccp --cov-report=term-missing $(PYTEST_ARGS)

cov-gate: venv  ## M0 gate: crypto/ and serialization.py at >= 95%
	$(PYTHON) -m pytest --cov=sccp.crypto --cov=sccp.serialization \
		--cov-report=term-missing --cov-fail-under=95 $(PYTEST_ARGS)

lint: venv  ## Check ruff lint and formatting
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

format: venv  ## Apply ruff formatting and safe lint fixes
	$(PYTHON) -m ruff check --fix src tests
	$(PYTHON) -m ruff format src tests

typecheck: venv  ## Run mypy in strict mode
	$(PYTHON) -m mypy

check: lint typecheck test  ## Everything CI runs

lock: venv  ## Regenerate requirements.lock from the current venv
	@printf '%s\n' \
		'# Locked versions for the validated environment (M0).' \
		'# Runtime and test dependencies are declared in pyproject.toml.' \
		'# Regenerate with: make lock' \
		> requirements.lock
	$(PYTHON) -m pip freeze --exclude-editable >> requirements.lock

clean:  ## Remove caches and build artefacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache .hypothesis htmlcov build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
