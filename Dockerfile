FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# шрифт с кириллицей для рендера дашборда картинкой (Pillow)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Зависимости — до кода: правка `app/` не должна переустанавливать пакеты. Раньше `COPY app`
# стоял перед `pip install`, и каждый деплой собирал образы заново (сотни МБ на образ, их два):
# 10.11.2026 это забило диск сервера в 100% посреди деплоя. Пакет ставится editable на пустую
# заглушку `app/`, настоящий код ложится поверх следующим слоем.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install --no-cache-dir -e ".[dev]"
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
# Фронт — последним: правка HTML не должна инвалидировать слой с пакетами (§5.7).
COPY web ./web

CMD ["python", "-m", "app.main_bot"]
