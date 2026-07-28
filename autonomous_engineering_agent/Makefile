.PHONY: install test lint check doctor seed release-check production-check dashboard

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest

lint:
	python3 -m ruff check .

check: lint test

doctor:
	agent doctor

seed:
	agent db-seed --profile local-dev

release-check:
	scripts/release-check.sh --env-file .env.ci.example --skip-docker-image

production-check:
	scripts/release-check.sh --env-file .env --production

dashboard:
	agent dashboard --host 127.0.0.1 --port 8080
