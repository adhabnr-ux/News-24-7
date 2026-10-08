.PHONY: install dev test lint fmt run check demo
install:
	python3 -m venv .venv && .venv/bin/pip install -e .
dev:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
test:
	.venv/bin/pytest -q
lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .
fmt:
	.venv/bin/ruff format . && .venv/bin/ruff check --fix .
run:
	.venv/bin/news247 run
check:
	.venv/bin/news247 check
demo:
	.venv/bin/news247 demo
