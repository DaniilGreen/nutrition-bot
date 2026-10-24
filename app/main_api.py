"""Entrypoint FastAPI: REST мини-аппа плюс отдача её статики.

Фронт живёт здесь же, а не отдельным контейнером (§5.7): один reverse_proxy у
соседского Caddy вместо третьего сервиса и bind-mount в чужой репозиторий.
Сборщика нет — вкладки в одной странице, графики inline-SVG, telegram-web-app.js
обязан грузиться с telegram.org.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import QueryParams
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.assets import WEB_DIR, asset_version
from app.config import Settings
from app.container import Container
from app.logging import setup_logging
from app.web.drafts import DraftStore
from app.web.limits import IdempotencyStore, RateLimiter
from app.web.routes import router as api_router
from app.web.warmup import warm_up

log = logging.getLogger(__name__)

# Коды для ошибок, которые бросает сам фреймворк: без них фронт получил бы
# «error» и не смог бы отличить «не туда постучался» от «нет такого».
_CODES = {404: "not_found", 405: "method_not_allowed", 413: "too_large"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    settings = Settings()
    app.state.container = Container.create(settings)
    # Черновики уточнений живут в процессе api и переживают запросы, но не
    # перезапуск — их время жизни 15 минут (§7.1, app/web/drafts.py).
    app.state.drafts = DraftStore()
    app.state.idempotency = IdempotencyStore()
    app.state.limiter = RateLimiter()
    # MA-INV-77: прогрев фоном — старт его не ждёт, первый запрос не платит за
    # соединение и компиляцию. Ссылку держим: задачу без ссылки соберёт GC.
    app.state.warmup = asyncio.create_task(warm_up(app.state.container))
    yield
    # Дождаться отмены ДО dispose: иначе прогрев держит соединение закрываемого пула.
    app.state.warmup.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app.state.warmup
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


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Единый конверт §7.4 для ВСЕХ ошибок с кодом.

    Раньше их было три формы: `{"detail": {"error": …}}` от ApiError,
    `{"detail": "Method Not Allowed"}` от самого FastAPI и `{"error": …}` от
    обработчиков ниже. Фронт читал одну из трёх, поэтому тексты 500 и 503
    до пользователя не доходили вовсе.
    """
    code = getattr(exc, "code", None) or _CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else "Что-то пошло не так."
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": message}},
        headers=getattr(exc, "headers", None),
    )


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


def index_html(version: str, source: str) -> str:
    """Подставить версию в ссылки на статику. Отдельной функцией — чтобы
    проверялось тестом: молча не подставившаяся версия выглядит как рабочая."""
    return (source.replace('href="/app.css"', f'href="/app.css?v={version}"')
                  .replace('src="/app.js"', f'src="/app.js?v={version}"'))


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    """Точка входа аппки. Сама страница НЕ кэшируется никогда — иначе версия
    внутри неё замёрзнет вместе с ней и весь смысл отпечатка пропадёт."""
    source = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        index_html(asset_version(), source),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    """Единственный публичный endpoint без initData (MA-INV-6): его дёргает
    деплой, чтобы отличить «поднялось» от «отвечает 502»."""
    return {"status": "ok"}


# MA-INV-76: файл по адресу с ТЕКУЩЕЙ версией не меняется никогда — браузер держит
# его год и не переспрашивает (раньше каждое открытие стоило 304 на app.js и app.css,
# по обороту ~270 мс). Без версии или со старой — «переспроси»: такой адрес
# приходит только со старой страницы, и вечный кэш на нём закрепил бы не тот файл.
IMMUTABLE = "public, max-age=31536000, immutable"
VERSIONED = frozenset({"app.js", "app.css"})
# Наружу — только то, что грузит страница: check.mjs и responsive.md живут в web/
# ради разработки, публиковать их незачем.
PUBLIC = VERSIONED | {"index.html"}


class WebStatic(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        if path not in PUBLIC:
            raise StarletteHTTPException(404)
        response = await super().get_response(path, scope)
        if path in VERSIONED:
            pinned = QueryParams(scope.get("query_string", b"")).get("v") == asset_version()
            response.headers["Cache-Control"] = IMMUTABLE if pinned else "no-cache"
        elif path.endswith(".html"):
            # /index.html в обход корня: та же точка входа, тот же запрет (MA-INV-35).
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


# Монтируется последним: иначе StaticFiles на "/" перехватил бы /api и /health.
# html=True отдаёт index.html на корень; на пропавший файл — честный 404, а не
# HTML с кодом 200 (тот баг у соседа ломал запуск после каждой выкатки, §5.7).
if WEB_DIR.is_dir():
    app.mount("/", WebStatic(directory=WEB_DIR, html=True), name="web")
