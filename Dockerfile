FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# шрифт с кириллицей для рендера дашборда картинкой (Pillow)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY web ./web
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir -e ".[dev]"

CMD ["python", "-m", "app.main_bot"]
