# burnfat

Telegram-бот учёта питания с AI-разбором сообщений. Пишешь/наговариваешь, что съел —
бот извлекает продукты, считает КБЖУ и показывает остаток на день.

## Главный архитектурный принцип

**LLM наполняет, БД фиксирует.** LLM (OpenAI) только извлекает сущности и даёт
*первичную* оценку КБЖУ на 100 г. Финальные калории всегда считает приложение
(`app/domain/nutrition.py`) из граммов и плотности продукта. При первом появлении
продукта его плотность фиксируется в таблице `products` и больше не меняется
автоматически — поэтому одна и та же «куриная грудка» всегда даёт одинаковые цифры.
База продуктов наполняется по факту использования; на старте подгружается курируемый
сид частых блюд (RU/Asian) как опора (`app/infrastructure/products/seed.py`).

Инвариант проверяется тестом `tests/e2e/test_meal_flow.py::test_density_pinned_on_first_sight`.

> **Спецификация (источник правды): [docs/SPEC.md](docs/SPEC.md).** Поведение и решения
> фиксируются там; код отталкивается от спеки.

## Слои (clean architecture)

```
bot (aiogram)  ─┐
web (FastAPI)  ─┴─►  application (use-cases: Assistant, *Service)
                          │ зависит от Protocol-портов (application/ports.py)
                          ▼
                     domain (чистый: nutrition, portions, goals, timeframe)
                          ▲
   infrastructure: db (SQLAlchemy) · nlu (OpenAI/Fake) — реализуют порты
```

Сервисы не знают про SQLAlchemy/OpenAI. В тестах порты подменяются in-memory
фейками (`tests/fakes.py`) — тесты гоняются без БД и без вызовов LLM.

## Запуск

```bash
cp .env.example .env          # вписать BOT_TOKEN и OPENAI_API_KEY
make up                       # db + bot + api, миграции применяются автоматически
make logs                     # логи бота
```

Без ключа OpenAI можно поднять на встроенном эвристическом парсере:
`USE_FAKE_NLU=true` в `.env`.

## Тесты

```bash
make test        # domain unit + e2e (на фейках, без БД/LLM)
make lint        # ruff
make typecheck   # mypy
```

## Что внутри (M0–M3)

- Текст: «курица 250 грамм», «два банана», «съел рис».
- **Голос** (M3): надиктовка → Whisper → транскрипт → тот же пайплайн.
- Уточнение порции (M2): «фо бо» → inline-кнопки S/M/L → дозапись (FSM).
- Учёт веса: «вес 86.8». Дневная статистика: «сколько осталось».
- `/goal 1800`, `/week`, `/history`, `/undo`.
- Multi-user с изоляцией по `user_id`, таймзоны пользователя.

## Дальше

- Админ-команды каталога продуктов, коррекция отдельных позиций записи.
- Этап 2: web-дашборд (Next.js) на этом же REST API.

## Стек

Python 3.12 · aiogram 3 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · Alembic ·
pydantic-settings · OpenAI · Docker Compose.
