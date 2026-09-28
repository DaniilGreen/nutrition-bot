# Лимит на ИИ для владельца и экономия на разборе — план реализации

> **Для исполнителя:** REQUIRED SUB-SKILL: superpowers:executing-plans (инлайн) — задачи
> завязаны на общий контекст разбора; ревью в конце — отдельным сабагентом.

**Цель:** владелец сам меняет месячный лимит в настройках; разбор еды дешевеет примерно вдвое
без потери точности; решение по модели и числу сэмплов — на цифрах eval, а не на прайсе.

**Архитектура:** лимит переезжает из переменной окружения в таблицу `app_settings`
(переменная — значение по умолчанию), читается через `SpendRepo`. Права — по существующей
колонке `users.role`. Экономия — правки адаптера разбора и учёта; поведение сверяется со
спекой (INV-12 уже требует сэмплировать только новый продукт).

**Стек:** FastAPI, SQLAlchemy 2 + alembic, OpenAI SDK (structured outputs), Mini App на
строковых шаблонах (`web/app.js`).

Сперва спека, потом код (CLAUDE.md §0). Тесты гоняются на сервере в одноразовом контейнере
с пустым `DATABASE_URL` (память: тесты на сервере видят прод-базу).

---

## Карта файлов

| Файл | Что меняется |
|---|---|
| `docs/SPEC.md`, `docs/MINIAPP-SPEC.md`, `docs/AI-PRINCIPLES.md` | правила: лимит у владельца, INV-12 уточнён, кэш и повторы |
| `migrations/versions/0013_app_settings.py` | новая таблица `app_settings` |
| `app/infrastructure/db/models.py` | `AppSettingORM` |
| `app/application/ports.py`, `app/infrastructure/db/repositories.py`, `tests/fakes.py` | `SpendRepo.month_limit / set_month_limit` |
| `app/application/assistant.py`, `app/application/coach.py` | лимит читается из репозитория |
| `app/web/routes.py` | `PATCH /api/settings/ai-limit`, лимит в `GET /api/settings` |
| `web/app.js` | группа «Лимит на ИИ» у владельца |
| `app/infrastructure/nlu/openai_nlu.py` | сэмплы только для новых, `return_exceptions`, кэш 24 ч |
| `app/infrastructure/nlu/prompts.py`, `repositories.py` | ключи каталога без обрезки, в порядке добавления |
| `app/container.py`, `app/config.py` | без вложенных повторов SDK, таймауты, `vision_model` |
| `app/infrastructure/openai_compat.py` | закреплённый `reasoning_effort`, кэш 24 ч |
| `app/domain/spend.py` | цена по самому длинному префиксу имени, gpt-5.4-mini |
| `app/infrastructure/nlu/openai_coach.py`, `app/application/coach.py` | расход ассистента — по раундам, своей сессией |

---

### Задача 1. Спека

- [ ] SPEC.md: INV-12 — явное «известный продукт (ключ есть в каталоге) не сэмплируется: его
  плотность берётся из каталога (INV-2), медиана выбрасывалась бы». NFR — лимит хранится в
  `app_settings`, переменная окружения — значение по умолчанию; меняет только владелец.
- [ ] MINIAPP-SPEC.md: §6.5 — группа «Лимит на ИИ» только у владельца; §7 —
  `PATCH /api/settings/ai-limit` (403 `owner_only`, 400 `bad_limit` вне 1–100); инвариант
  «лимит меняет только владелец, проверка на сервере».
- [ ] AI-PRINCIPLES.md §1–§2: свои повторы без вложенных повторов SDK, явные таймауты, кэш 24 ч,
  цена по префиксу.

### Задача 2. Сэмплы только для новых продуктов (INV-12)

**Файлы:** `app/infrastructure/nlu/openai_nlu.py:103-123`, тест в `tests/unit/`.

- [ ] Тест: приём из знакомых продуктов — ОДИН вызов; с незнакомым ключом — k; позиция с
  `clarify_options` — один; упавший сэмпл не роняет разбор.
- [ ] Реализация:
  ```python
  known = set(known_keys)
  todo = [i for i in first.items
          if _needs_estimate(i) and i.canonical_key not in known and not i.clarify_options]
  if self._k <= 1 or first.intent is not Intent.MEAL or not todo:
      return [first]
  extra = await asyncio.gather(
      *[self._parse_at(text, known_keys, SAMPLE_TEMPERATURE) for _ in range(self._k - 1)],
      return_exceptions=True,
  )
  return [first, *(s for s in extra if isinstance(s, ParsedMessage))]
  ```

### Задача 3. Кэш промпта на 24 часа и закреплённый `reasoning_effort`

**Файлы:** `openai_compat.py`, `openai_nlu.py:_parse_once`, тест.

- [ ] Живой вызов с коротким промптом: принимает ли gpt-5.1 `prompt_cache_retention="24h"`,
  `prompt_cache_key` и `reasoning_effort="none"`. Ответ 400 — параметр не берём.
- [ ] `openai_compat.request_extras(model) -> dict`: для `gpt-5*` — подтверждённые параметры,
  для остальных — `{}` (у gpt-4.1 нет рассуждений). Тест на обе ветки.
- [ ] `_parse_once`: `**request_extras(self._model)`.

### Задача 4. Ключи каталога — все и в порядке добавления

- [ ] `all_keys`: `order_by(ProductORM.id)`.
- [ ] `build_user_prompt`: без `sorted` и без `[:400]` — новый ключ дописывается в конец и не
  сбивает кэш предыдущих. `# ponytail:` — потолок ~1500 ключей, дальше отбор по частоте.
- [ ] Тест: порядок сохраняется, 450 ключей — все в промпте.

### Задача 5. Повторы, таймауты, отдельная модель для фото

- [ ] Клиент разбора — `max_retries=0` (свой цикл уже есть). Vision, голос, ассистент —
  `max_retries=2`, `timeout=60`.
- [ ] `vision_model` (по умолчанию `gpt-5.1`): смена модели разбора не уводит фото и бланки.

### Задача 6. Цена по префиксу и новая модель

- [ ] Цена — по самому длинному ключу `PRICES`, с которого начинается имя модели; добавить
  `gpt-5.4-mini` (0.75 / 0.075 / 4.50); выход в `DEFAULT_PRICE` поднять до 15.
- [ ] Ассерты в `demo()`.

### Задача 7. Расход ассистента — по раундам, своей сессией

- [ ] `OpenAiCoach(client, model, meter=None)`; после каждого раунда `meter.note(...)`.
- [ ] `Coach` больше не пишет `purpose="coach"` сам — иначе двойной учёт.

### Задача 8. Лимит в базе и права владельца

- [ ] Миграция 0013: `app_settings(key varchar(64) PK, value jsonb NOT NULL, updated_at timestamptz)`.
- [ ] `SpendRepo.month_limit(default)`, `set_month_limit(usd)`; фейк.
- [ ] `ensure_budget` в обоих сервисах читает лимит из репозитория.
- [ ] `GET /api/settings`: лимит из базы; блок `ai_limit` — только владельцу.
- [ ] `PATCH /api/settings/ai-limit`: 403 `owner_only`, 400 `bad_limit`.
- [ ] e2e-тесты. Прод: `role = 'owner'` для владельца — после деплоя.

### Задача 9. Группа «Лимит на ИИ» в настройках

- [ ] Только при наличии `ai_limit`; кнопки 10/15/20/25/30/50 без перерисовки экрана;
  строка «Потрачено $X из $Y в <месяц>». Проверка на стенде на 320 и 1100.

### Задача 10. Eval (пакет Б)

- [ ] Срез 46 строк, k=1: gpt-5.1, gpt-5.4-mini, gpt-4.1-mini — в один день, без базы.
- [ ] Число сэмплов: один прогон с k=7, медиана первых трёх против медианы всех семи и против
  эталона — парное сравнение на одних и тех же ответах модели.
- [ ] Отчёт владельцу; решение о смене — за ним.

### Задача 11. Проверка и выкатка

- [ ] Полный набор тестов, `node web/check.mjs`, ревью отдельным сабагентом.
- [ ] Коммит, пуш, зелёный прогон по хэшу, содержимое прода.
- [ ] Через сутки — цена вызова и вызовов на приём в `llm_spend`.
