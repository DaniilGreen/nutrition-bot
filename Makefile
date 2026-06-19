.PHONY: up down logs migrate revision test eval backup restore lint fmt typecheck shell

# ./tests не копируется в образ (Dockerfile) → монтируем для прогона (CLAUDE.md §3).
TESTS_MOUNT = -v "$$PWD/tests:/app/tests"

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

test:          ## Тесты (без БД/LLM — domain + e2e на фейках)
	docker compose run --rm --no-deps $(TESTS_MOUNT) api pytest -q

eval:          ## Точность по golden-set (NFR-1; бьёт реальный OpenAI, нужен ключ в .env)
	docker compose run --rm --no-deps $(TESTS_MOUNT) api python -m tests.eval.run_eval --gate

backup:        ## Дамп БД в ./backups/ (запиннутые КБЖУ + история, INV-2/3)
	sh scripts/backup_db.sh

restore:       ## Восстановить БД из дампа: make restore f=backups/burnfat_XXXX.sql.gz
	sh scripts/restore_db.sh "$(f)"

lint:          ## Ruff
	docker compose run --rm --no-deps $(TESTS_MOUNT) api ruff check app tests

fmt:           ## Ruff format
	docker compose run --rm --no-deps $(TESTS_MOUNT) api ruff format app tests

typecheck:     ## Mypy
	docker compose run --rm --no-deps api mypy app

shell:         ## Shell в контейнере бота
	docker compose run --rm bot bash
