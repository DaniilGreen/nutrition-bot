"""Entrypoint FastAPI: REST мини-аппа плюс отдача её статики.

Фронт живёт здесь же, а не отдельным контейнером (§5.7): один reverse_proxy у
соседского Caddy вместо третьего сервиса и bind-mount в чужой репозиторий.
Сборщика нет — вкладки в одной странице, графики inline-SVG, telegram-web-app.js
обязан грузиться с telegram.org.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.container import Container
from app.logging import setup_logging
from app.web.routes import router as api_router

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = Settings()
    app.state.container = Container.create(settings)
    yield
    await app.state.container.db.dispose()


app = FastAPI(title="burnfat API", version="0.2.0", lifespan=lifespan)
app.include_router(api_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    """Единственный публичный endpoint без initData (MA-INV-6): его дёргает
    деплой, чтобы отличить «поднялось» от «отвечает 502»."""
    return {"status": "ok"}


# Монтируется последним: иначе StaticFiles на "/" перехватил бы /api и /health.
# html=True отдаёт index.html на корень; на пропавший файл — честный 404, а не
# HTML с кодом 200 (тот баг у соседа ломал запуск после каждой выкатки, §5.7).
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
