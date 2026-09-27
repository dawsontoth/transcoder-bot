.PHONY: install lint format typecheck test test-unit check

install:  ## Create .venv and install the app + dev tools
	uv sync

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format:  ## Auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

test:  ## All tests (ffmpeg integration tests are skipped if ffmpeg isn't installed)
	uv run pytest

test-unit:  ## Fast tests only
	uv run pytest -m "not integration"

check: lint typecheck test  ## Everything CI runs
