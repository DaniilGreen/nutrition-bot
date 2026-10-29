# Nutrition Tracker: Telegram bot + Mini App

A calorie and macro tracker inside Telegram with two interfaces over one backend:

- a **chat bot** for fast logging: type or say "chicken 250 g, two bananas", or send a photo
  of the plate;
- a **Telegram Mini App** (a web app that opens inside Telegram) for what chat does badly:
  browsing and searching history, fixing the product catalog in bulk, charts, and an AI
  assistant over your own data.

Both call the same application services over one PostgreSQL database, so there is nothing to
sync. It runs in production for a handful of users:
[@Nutrition_calorie_bot](https://t.me/Nutrition_calorie_bot).

## The LLM fills in, the database decides

The LLM (OpenAI or Anthropic Claude, switched by `LLM_PROVIDER`) only extracts entities from a
message and gives a first estimate of a product's nutrients per 100 g. The application does all
the arithmetic
(`app/domain/nutrition.py`). The first estimate is pinned in the `products` table and never
changes on its own, so "chicken breast" gives the same numbers every time, and each meal stores
a snapshot, so history doesn't drift when the catalog is edited.

The invariant has a test: `tests/e2e/test_meal_flow.py::test_density_pinned_on_first_sight`.

## Features

**Bot**

- Text and voice logging (Whisper); portion clarification with S/M/L buttons.
- Photo logging: the model reads the composition, the plate weight comes from the user, and the
  app turns shares into grams. Nutrition labels and InBody printouts are read from a photo too.
- Daily and weekly summaries, history, undo, per-item deletion, evening summary.
- "Open in app" buttons jump from a chat message to the matching Mini App screen.

**Mini App**

- Product catalog editor: search, fix nutrients, merge duplicates, see which values are AI
  estimates.
- Frequent items: repeat a common meal in one tap.
- Where the calories go: each product's contribution over a period.
- Charts: calories per day against the target, weight trend, body-composition scans (InBody).
- Day accuracy: the share of calories from labels versus AI estimates.
- AI assistant over the whole history, on demand only, because every question costs money.
- Text and voice input right on the screen.

**Both**

- Calorie target from the user's profile (Mifflin-St Jeor).
- Monthly LLM spend cap: past the limit, photos and the assistant stop; text logging never does.

## Measured accuracy

| Eval | Data | Result |
|---|---|---|
| Text parsing (gpt-5.1, Aug 2026) | 150-row golden set | median calorie error per item 2.4%, p90 16.9%; daily totals within ±15% in 96% of cases |
| Photo (gpt-5.1, Sep 2026) | 49 dishes from Nutrition5k | median calorie error 22% from the photo alone, 17% with plate weight |

Raw numbers are in `tests/eval/baselines/`; the photo write-up is `docs/research/photo-weight.md`.
Evals call the real OpenAI API and cost money, so they run on demand (`make eval`), not in CI.

## Architecture

```
bot (aiogram)  ─┐
web (FastAPI)  ─┴─►  application (use cases: Assistant, *Service)
                          │ depends on Protocol ports (application/ports.py)
                          ▼
                     domain (pure: nutrition, portions, goals, timeframe)
                          ▲
   infrastructure: db (SQLAlchemy) · nlu (OpenAI / fake) implement the ports
```

Services know nothing about SQLAlchemy or OpenAI. Tests swap the ports for in-memory fakes
(`tests/fakes.py`), so the suite runs without a database or an LLM.

The Mini App is plain HTML/JS served by FastAPI behind Caddy. Every API call carries Telegram's
signed `initData`, and the server checks its HMAC-SHA256 against the bot token, so there are no
passwords or sessions to manage.

## Tests, CI/CD and operations

- 1,200+ pytest tests, about 40 s locally. Postgres-specific SQL tests run against a Postgres
  service container in CI.
- GitHub Actions gates every deploy on the tests plus a front-end check (syntax and undeclared
  identifiers in the Mini App). Push to `main` deploys.
- Deploy goes over SSH: `git reset --hard origin/main` and `docker compose up -d --build` on the
  VPS, then the job waits for the bot to reach polling and probes the Mini App from outside:
  TLS, `/health`, and a 401 on unsigned API calls.
- On the server: nightly database backups kept for 14 days, a healthcheck every 10 minutes
  with Telegram alerts, and build-cache limits, because two bots share a 10 GB disk.

## Run locally

```bash
cp .env.example .env   # BOT_TOKEN and OPENAI_API_KEY
make up                # db + bot + api, migrations apply automatically
make test              # also: make lint, make typecheck
```

Set `USE_FAKE_NLU=true` in `.env` to run on the built-in heuristic parser without OpenAI.

## Stack

Python 3.12 · aiogram 3 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 · Alembic ·
pydantic-settings · OpenAI (gpt-5.1, Whisper) or Anthropic Claude · Docker Compose ·
GitHub Actions.
The Mini App is plain HTML/JS with no build step.

## About this repo

Built by Daniil Grinzhola with Claude Code as a pair programmer. Product decisions, architecture
and infrastructure are mine; `CLAUDE.md` holds the working rules for the agent. Specs and design
notes in `docs/` are in Russian, starting with `docs/SPEC.md`.
