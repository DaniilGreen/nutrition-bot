"""Middleware: сессия БД на апдейт, резолв пользователя, Assistant; приём `widget_query`."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update
from aiogram.types import User as TgUser

from app.application.assistant import Assistant
from app.application.entities import User
from app.application.ports import UserRepo
from app.bot.widgets import live
from app.container import Container

logger = logging.getLogger(__name__)

#: Кнопка экрана, нарисованная в виджете (D-82): «k:» + callback_data прежней кнопки под ним.
TAP = "k:"
_ANSWERED: set[str] = set()


class _Tap(CallbackQuery):
    """Нажатие кнопки в виджете. Для обработчиков — обычный CallbackQuery (те же обработчики, что у
    кнопки под виджетом, логика не дублируется); отличие одно: текст `answer()` уходит виджету
    ответом на запрос (`{"ok":1,"t":…}`), и тот показывает его строкой у себя. Один раз."""

    async def answer(  # type: ignore[override]
        self, text: str | None = None, *args: Any, **kwargs: Any
    ) -> bool:
        if self.id in _ANSWERED or self.bot is None:
            return False
        _ANSWERED.add(self.id)
        await live.reply(self.bot, self.id, {"ok": 1, "t": text or ""})
        return True


async def user_of(
    container: Container, users: UserRepo, telegram_id: int, name: str | None,
    language_code: str | None,
) -> User:
    """Человек апдейта — один путь для сообщений, тапов и запросов виджетов."""
    return await users.get_or_create(
        telegram_id=telegram_id, name=name, locale=language_code or "ru",
        timezone=container.settings.default_timezone,
    )


def reader(container: Container) -> Callable[[], AbstractAsyncContextManager[Assistant]]:
    """Короткая сессия только на чтение — для пуша виджета дня после того, как хендлер
    закоммитил: соединение отдаётся в пул до похода в Telegram."""

    @asynccontextmanager
    async def open_() -> AsyncIterator[Assistant]:
        async with container.db.session_factory() as session:
            live.EXTRA.set({"scans": container.body_scans_for(session)})  # плашка недели (D-80)
            yield container.assistant_for(session)[0]

    return open_


class ServicesMiddleware(BaseMiddleware):
    def __init__(self, container: Container) -> None:
        self._container = container

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        shown = live.begin_update()
        async with self._container.db.session_factory() as session:
            assistant, users = self._container.assistant_for(session)
            user = await user_of(
                self._container, users, tg_user.id, tg_user.full_name, tg_user.language_code
            )
            data["assistant"] = assistant
            data["scans"] = self._container.body_scans_for(session)
            live.EXTRA.set({"users": users, "scans": data["scans"]})  # сборке «Сегодня» (D-80)
            data["users"] = users
            data["app_user"] = user
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        # FR-49.1, D-46: после апдейта — виджет дня показывает то, что теперь в базе. Хендлер сам
        # показал свежий «Сегодня» — пересобирать то же самое незачем.
        if not shown[0] and live.watching(user) and live.enabled(user):
            await self._push(data, user)
        return result

    async def _push(self, data: dict[str, Any], user: User) -> None:
        """Своя сессия: хендлер уже закоммитил. Сбой здесь не должен ронять ответ человеку."""
        bot = data.get("bot")
        if bot is None:
            return
        try:
            await live.push(bot, user, reader(self._container))
        except Exception:  # noqa: BLE001
            logger.exception("widget: пуш виджета дня упал")


class WidgetQueryMiddleware(BaseMiddleware):
    """`widget_query` — апдейт 10.4, которого aiogram 3.31 не знает (D-45): он лежит в
    `Update.model_extra`, и без перехвата до разбора типа aiogram выкинул бы его с
    «unknown update type». Ловим здесь, на outer-middleware `dp.update`.

    Порядок любого запроса: сессия БД → фиксация → сессия закрыта → ответ → правка виджета.
    Ответ уходит всегда — и при сбое БД (`{"err":"fail"}`), иначе виджет ждёт 15 с TIMEOUT."""

    def __init__(self, container: Container) -> None:
        self._container = container

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        q = (event.model_extra or {}).get("widget_query") if isinstance(event, Update) else None
        if not isinstance(q, dict):
            return await handler(event, data)
        bot: Bot = data["bot"]
        origin = live.parse_origin(q)
        # OQ-W1: по этой строке в логе видно, что запросы виджета доходят до бота при поллинге.
        logger.info("widget: запрос %r, источник принят: %s", q.get("data"), origin is not None)
        qid = q.get("id")
        if not isinstance(qid, str) or not qid:
            return None
        if origin is not None and origin[2].startswith(TAP) and not origin[2][2:].isdigit():
            await self._tap(bot, q, qid, origin, data)  # `k:<число>` — листание старой «Недели»
            return None
        route = live.ROUTES.get(origin[2].split(":", 1)[0]) if origin else None
        if origin is not None and route is not None:
            await self._route(bot, q, qid, origin, route)
            return None
        parsed = live.parse_query(q)
        if parsed is None:
            await live.reply(bot, qid, {"err": "bad"})  # AC-270: данные не читаем
            return None
        await self._today(bot, q, qid, parsed)
        return None

    async def _tap(
        self, bot: Bot, q: dict[str, Any], qid: str, origin: tuple[int, int, str],
        data: dict[str, Any],
    ) -> None:
        """Кнопка из виджета → тот же CallbackQuery через диспетчер (как finance_bot): внешние и
        внутренние middleware проходят заново — своя сессия БД, свой человек, пуш «Сегодня».
        Сообщение — этот виджет. Обработчик промолчал — ответ пустой: виджет не ждёт 15 с."""
        uid, mid, code = origin[0], origin[1], origin[2][len(TAP):]
        dispatcher = data.get("dispatcher")
        if dispatcher is None or not code:
            await live.reply(bot, qid, {"err": "bad"})
            return
        message = Message(
            message_id=mid, date=datetime.now(UTC), chat=Chat(id=uid, type="private"), widget={},
        ).as_(bot)
        tap = _Tap(
            id=qid, from_user=TgUser.model_validate(q["from"]), chat_instance="", data=code,
            message=message,
        ).as_(bot)
        try:
            # `.as_(bot)` обязателен: апдейт без бота aiogram пересобирает через JSON, и вместо
            # `_Tap` хендлер получает голый CallbackQuery — ответ виджету уходил бы только после
            # хендлера, а запись фото идёт дольше 15 с (TIMEOUT → «Не вышло»).
            await dispatcher.feed_update(bot, Update(update_id=0, callback_query=tap).as_(bot))
        except Exception:  # noqa: BLE001 — кнопка не должна ронять поллинг
            logger.exception("widget: кнопка %r не выполнена", code[:64])
            await tap.answer("Не вышло — попробуй ещё раз.")
        finally:
            await tap.answer()
            _ANSWERED.discard(qid)

    async def _user(self, users: UserRepo, q: dict[str, Any], uid: int) -> User:
        frm = q["from"]
        return await user_of(
            self._container, users, uid, frm.get("first_name"), frm.get("language_code")
        )

    async def _today(
        self, bot: Bot, q: dict[str, Any], qid: str, parsed: tuple[int, int, str]
    ) -> None:
        """`t:<день>` (FR-49.1, D-46): данные дня в ответ, затем правка виджета под замком."""
        obj: dict[str, Any] = {"err": "fail"}
        built: live.Built | None = None
        user: User | None = None
        try:
            async with self._container.db.session_factory() as session:
                assistant, users = self._container.assistant_for(session)
                user = await self._user(users, q, parsed[0])
                obj, built = await live.respond(parsed, user, assistant)
                await session.commit()  # новый человек из get_or_create
        except Exception:  # noqa: BLE001 — апдейт виджета не должен ронять поллинг
            logger.exception("widget: запрос виджета дня не обработан")
            obj, built = {"err": "fail"}, None
        await live.reply(bot, qid, obj)
        if built is not None and user is not None:
            try:
                await live.push(bot, user, reader(self._container))
            except Exception:  # noqa: BLE001
                logger.exception("widget: правка виджета дня после запроса упала")

    async def _route(
        self, bot: Bot, q: dict[str, Any], qid: str, origin: tuple[int, int, str],
        route: live.Route,
    ) -> None:
        """Пишущий запрос: обработчик → фиксация → ответ → пуш виджета дня. Сбой — `err`."""
        uid, mid, data = origin
        reply: dict[str, Any] = {"err": "fail"}
        user: User | None = None
        out: live.Outcome | None = None
        try:
            async with self._container.db.session_factory() as session:
                assistant, users = self._container.assistant_for(session)
                user = await self._user(users, q, uid)
                if not live.enabled(user):
                    reply = {"err": "bad"}
                else:
                    live.EXTRA.set({"users": users,
                                    "scans": self._container.body_scans_for(session)})
                    out = await route(uid, mid, data, user, assistant)
                    try:
                        await session.commit()  # «записал» в ответе не должен врать
                    except BaseException:
                        if out.abort is not None:
                            out.abort()
                        raise
                    if out.commit is not None:
                        out.commit()
                    reply = out.reply
            if not reply.get("ok"):
                out = None
        except Exception:  # noqa: BLE001
            logger.exception("widget: пишущий запрос не обработан")
            reply = {"err": "fail"}
        await live.reply(bot, qid, reply)
        if reply.get("ok") and out is not None and out.after is not None:
            try:
                await out.after(bot)
            except Exception:  # noqa: BLE001
                logger.exception("widget: правка сообщения виджета после запроса упала")
        if reply.get("ok") and user is not None:
            try:
                await live.push(bot, user, reader(self._container))
            except Exception:  # noqa: BLE001
                logger.exception("widget: пуш виджета дня после запроса упал")
