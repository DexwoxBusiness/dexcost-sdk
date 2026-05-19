.PHONY: lint format typecheck test build clean

lint:
	ruff check src/ tests/

format:
	black src/ tests/

typecheck:
	mypy src/

test:
	python -m pytest

build:
	python -m build

clean:
	rm -rf dist/ build/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
