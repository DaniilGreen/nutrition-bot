"""Отказы провайдера модели — одним словарём на оба (NFR-4, NFR-6, AI-PRINCIPLES §9).

Бот и аппка не должны знать, у кого кончились деньги: им важно «повтор не поможет»
против «подожди». Голос на Whisper остаётся и при `LLM_PROVIDER=anthropic`, поэтому
распознаём оба провайдера всегда, а не по настройке.
"""
from __future__ import annotations

import openai

from app.infrastructure import claude_compat, openai_compat

# Куда идти чинить — владелец получает ссылку провайдера, у которого сломалось.
BILLING = {
    "openai": ("OpenAI", "platform.openai.com → Settings → Billing"),
    "anthropic": ("Anthropic", "platform.claude.com → Settings → Billing"),
}
KEYS = {
    "openai": "platform.openai.com → API keys",
    "anthropic": "platform.claude.com → Settings → API keys",
}


def credits_provider(exc: BaseException) -> str | None:
    """У какого провайдера пустой баланс; None — отказ не про деньги."""
    if openai_compat.out_of_credits(exc):
        return "openai"
    if claude_compat.out_of_credits(exc):
        return "anthropic"
    return None


def out_of_credits(exc: BaseException) -> bool:
    return credits_provider(exc) is not None


def misconfigured(exc: BaseException) -> bool:
    """Неверная настройка: ключ не принят или отозван, модели нет у аккаунта (401/403/404).
    Повтор не поможет — нужен человек с ключом (NFR-6, AC-263)."""
    return isinstance(
        exc, (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)
    ) or claude_compat.misconfigured(exc)


def provider_down(exc: BaseException) -> tuple[str, str] | None:
    """(провайдер, причина) отказа, при котором повтор не поможет: причина `credits` или
    `config`. None — обычный сбой: его ретраят или показывают «что-то пошло не так»."""
    provider = credits_provider(exc)
    if provider is not None:
        return provider, "credits"
    if misconfigured(exc):
        return ("anthropic" if claude_compat.misconfigured(exc) else "openai"), "config"
    return None


def overloaded(exc: BaseException) -> bool:
    """Временная перегрузка (429 у обоих, 529 у Anthropic). Пустой баланс сюда не входит:
    у OpenAI он тоже 429, поэтому вызывающий проверяет `provider_down` первым."""
    return isinstance(exc, openai.RateLimitError) or claude_compat.overloaded(exc)
