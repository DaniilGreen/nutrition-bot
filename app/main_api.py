"""Entrypoint FastAPI: REST мини-аппа плюс отдача её статики.

Фронт живёт здесь же, а не отдельным контейнером (§5.7): один reverse_proxy у
соседского Caddy вместо третьего сервиса и bind-mount в чужой репозиторий.
Сборщика нет — вкладки в одной странице, графики inline-SVG, telegram-web-app.js
обязан грузиться с telegram.org.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.container import Container
from app.logging import setup_logging
from app.web.drafts import DraftStore
from app.web.routes import router as api_router

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    settings = Settings()
    app.state.container = Container.create(settings)
    # Черновики уточнений живут в процессе api и переживают запросы, но не
    # перезапуск — их время жизни 15 минут (§7.1, app/web/drafts.py).
    app.state.drafts = DraftStore()
    yield
    await app.state.container.db.dispose()


app = FastAPI(
    title="burnfat API",
    version="0.2.0",
    lifespan=lifespan,
    # Схему наружу не публикуем: MA-INV-6 знает единственное исключение — /health,
    # а /docs выкладывал бы карту всех эндпоинтов любому желающему.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(api_router, prefix="/api")


@app.exception_handler(RequestValidationError)
async def malformed(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Кривое тело запроса отдаётся тем же конвертом, что и остальные ошибки (§7.4).

    По умолчанию FastAPI шлёт 422 со своей схемой — фронту пришлось бы разбирать
    два разных формата ошибок, и он разобрал бы неправильно.
    """
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "malformed_body", "message": "Запрос не разобран."}},
    )


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Любой необработанный сбой отдаётся конвертом §7.4, а не голым 500.

    Практический повод: связь с OpenAI рвётся (таймаут, лимит, авария) и без
    этого пользователь видел бы пустую ошибку, не понимая, повторять или нет.
    Причину наружу не пишем — она уходит в лог.
    """
    log.exception("необработанный сбой на %s %s", request.method, request.url.path)
    upstream = type(exc).__module__.split(".")[0] in {"openai", "httpx", "httpcore"}
    if upstream:
        return JSONResponse(
            status_code=503,
            content={"error": {
                "code": "upstream_unavailable",
                "message": "Разбор временно недоступен. Повтори через минуту.",
            }},
        )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal", "message": "Что-то сломалось. Попробуй ещё раз."}},
    )


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
