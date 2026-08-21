.PHONY: install lint test migrate downgrade seed dev worker docker-up docker-down

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .

test:
	python -m pytest -q

migrate:
	alembic upgrade head

downgrade:
	alembic downgrade -1

seed:
	python scripts/seed_demo.py

dev:
	uvicorn app.main:app --reload --no-access-log

worker:
	python -m app.workers.worker

docker-up:
	docker compose up --build

docker-down:
	docker compose down
