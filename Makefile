.PHONY: install test lint run build clean

install:
	uv sync

test:
	PYTHONPATH=. uv run pytest tests/

lint:
	uv run ruff check .

run:
	uv run python server.py

build:
	docker build -t your-registry/sentinel-mcp:latest .

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
