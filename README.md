# burnfat

Telegram-бот учёта питания с AI-разбором сообщений. Пишешь/наговариваешь, что съел —
бот извлекает продукты, считает КБЖУ и показывает остаток на день.

## Главный архитектурный принцип

**LLM наполняет, БД фиксирует.** LLM (OpenAI) только извлекает сущности и даёт
*первичную* оценку КБЖУ на 100 г. Финальные калории всегда считает приложение
(`app/domain/nutrition.py`) из граммов и плотности продукта. При первом появлении
продукта его плотность фиксируется в таблице `products` и больше не меняется
автоматически — поэтому одна и та же «куриная грудка» всегда даёт одинаковые цифры.
База продуктов наполняется сама по факту использования: пред-сида нет.

Инвариант проверяется тестом `tests/e2e/test_meal_flow.py::test_density_pinned_on_first_sight`.

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

## Что внутри (M0 + M1)

- Текстовый happy-path: «курица 250 грамм», «два банана», «съел рис».
- Учёт веса тела: «вес 86.8».
- Дневная статистика и остаток: «сколько осталось».
- Цель по калориям: `/goal 1800`.
- Multi-user с изоляцией по `user_id`, таймзоны пользователя.

## Дальше (не входит в M0/M1)

- **M2:** гибридные уточняющие вопросы (порция S/M/L через inline-кнопки), недельная
  статистика, история, коррекция записей, админ-команды каталога.
- **M3:** голосовые (Whisper).
- Этап 2: web-дашборд (Next.js) на этом же REST API.

## Стек

Python 3.12 · aiogram 3 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · Alembic ·
pydantic-settings · OpenAI · Docker Compose.
