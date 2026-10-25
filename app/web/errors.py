"""Единый конверт ошибок API (§7.4).

Тексты для пользователя одинаковые — причину отказа наружу не объясняем, иначе
подпись подбирается по подсказкам. Различается только `code`: он нужен фронту,
чтобы отличить «переоткрой аппку» от «сервер лежит», и нам — в логах.
"""
from __future__ import annotations

from fastapi import HTTPException


class ApiError(HTTPException):
    """Ошибка API. Наружу уходит через обработчик в `main_api`, который кладёт
    её в единый конверт §7.4 — здесь только код и текст."""

    def __init__(
        self, status_code: int, code: str, message: str, extra: dict | None = None
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        # Поля рядом с `error` в конверте (FR-47: `session` при `not_a_session`) — данные для
        # перерисовки, а не объяснение отказа; тексты по-прежнему одинаковые.
        self.extra = extra or {}


def unauthorized(code: str) -> ApiError:
    return ApiError(401, code, "Не удалось подтвердить вход. Переоткрой аппку из бота.")


def forbidden(code: str, message: str) -> ApiError:
    return ApiError(403, code, message)


def bad_request(code: str, message: str) -> ApiError:
    return ApiError(400, code, message)


def not_found(code: str, message: str) -> ApiError:
    return ApiError(404, code, message)


def unavailable(code: str, message: str) -> ApiError:
    return ApiError(503, code, message)
