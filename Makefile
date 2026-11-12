# Developer entry points.
#
# Everything runs inside .venv so the same commands work locally and in CI.
PYTHON ?= .venv/bin/python
PYTEST_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help venv install test test-all adversarial cov cov-gate lint format typecheck check lock dist dist-check release-check devnet stack clean

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

test-all: test  ## Add the integration/e2e tests that exist (engine tests stay deselected)
	@dirs=""; \
	for dir in tests/integration tests/e2e; do \
		[ -d "$$dir" ] && dirs="$$dirs $$dir"; \
	done; \
	if [ -z "$$dirs" ]; then echo "no integration/e2e tests yet; nothing extra to run"; exit 0; fi; \
	$(PYTHON) -m pytest $$dirs $(PYTEST_ARGS); \
	status=$$?; \
	if [ $$status -eq 5 ]; then \
		echo "only engine-marked tests exist; run: $(PYTHON) -m pytest -m engine $$dirs"; \
		exit 0; \
	fi; \
	exit $$status

adversarial: venv  ## Run the cheat/self-play/tamper suites (-m adversarial)
	$(PYTHON) -m pytest -m adversarial $(PYTEST_ARGS)

cov: venv  ## Whole-package report with the project-wide 85% gate
	$(PYTHON) -m pytest --cov=sccp --cov-report=term-missing \
		--cov-fail-under=85 $(PYTEST_ARGS)

cov-gate: venv  ## Crypto gate: crypto/ and serialization.py at >= 95%
	$(PYTHON) -m pytest --cov=sccp.crypto --cov=sccp.serialization \
		--cov-report=term-missing --cov-fail-under=95 $(PYTEST_ARGS)

lint: venv  ## Check ruff lint and formatting
	$(PYTHON) -m ruff check src tests scripts deploy
	$(PYTHON) -m ruff format --check src tests scripts deploy

format: venv  ## Apply ruff formatting and safe lint fixes
	$(PYTHON) -m ruff check --fix src tests scripts deploy
	$(PYTHON) -m ruff format src tests scripts deploy

typecheck: venv  ## Run mypy in strict mode
	$(PYTHON) -m mypy

check: lint typecheck test  ## Everything CI runs

lock: venv  ## Regenerate requirements.lock from the current venv
	@printf '%s\n' \
		'# Locked versions for the validated environment.' \
		'# Runtime and test dependencies are declared in pyproject.toml.' \
		'# Regenerate with: make lock' \
		> requirements.lock
	$(PYTHON) -m pip freeze --exclude-editable >> requirements.lock

release-check: venv  ## Verify pyproject/version.py/RELEASE_TAG agree
	$(PYTHON) scripts/check_release_version.py

dist: venv  ## Build sdist and wheel into dist/ (needs `build`)
	$(PYTHON) -m build

dist-check: dist  ## Validate dist/ with twine check --strict (needs `twine>=7`)
	$(PYTHON) -m twine check --strict dist/*

devnet: venv  ## Run the in-process devnet (node + relay + four mock miners)
	$(PYTHON) -m sccp.cli devnet up --miners 4 --blocks 10

stack: venv  ## Write the deterministic stack keys into .stack
	$(PYTHON) deploy/init_testnet_stack.py --out .stack
	@if command -v docker >/dev/null 2>&1; then \
		docker compose config --quiet && echo "compose config ok"; \
	else \
		echo "docker not installed; skipped compose validation"; \
	fi

clean:  ## Remove caches and build artefacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache .hypothesis htmlcov build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
