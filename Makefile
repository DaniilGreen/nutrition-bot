.PHONY: up down logs migrate revision test lint fmt typecheck shell

up:            ## Поднять стек (db + bot + api)
	docker compose up -d --build

down:          ## Остановить стек
	docker compose down

logs:          ## Логи бота
	docker compose logs -f bot

migrate:       ## Применить миграции
	docker compose run --rm bot alembic upgrade head

revision:      ## Создать миграцию: make revision m="add table"
	docker compose run --rm bot alembic revision --autogenerate -m "$(m)"

test:          ## Тесты (одноразовый контейнер, без БД — domain + e2e на фейках)
	docker compose run --rm --no-deps api pytest -q

lint:          ## Ruff
	docker compose run --rm --no-deps api ruff check app tests

fmt:           ## Ruff format
	docker compose run --rm --no-deps api ruff format app tests

typecheck:     ## Mypy
	docker compose run --rm --no-deps api mypy app

shell:         ## Shell в контейнере бота
	docker compose run --rm bot bash
