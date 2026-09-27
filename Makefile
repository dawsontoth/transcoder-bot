.PHONY: install lint format typecheck test test-unit check flowchart

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

# Pass extra mermaid-cli options with MMDC_ARGS, e.g. MMDC_ARGS="-p puppeteer.json" to pick the browser.
MMDC = npx --yes @mermaid-js/mermaid-cli@12.0.0 --quiet --no-font-embed $(MMDC_ARGS)

flowchart:  ## Re-render the README flowchart from docs/flowchart/flowchart.mmd (needs Node.js)
	cd docs/flowchart && $(MMDC) -c light.json -b '#ffffff' -i flowchart.mmd -o flowchart-light.svg
	cd docs/flowchart && $(MMDC) -c dark.json -b '#0d1117' -i flowchart.mmd -o flowchart-dark.svg
