"""Кэш промпта считается по своей цене (AI-PRINCIPLES §2)."""
from types import SimpleNamespace

from app.domain.spend import Usage
from app.infrastructure.openai_compat import token_counts


def test_кэш_берётся_из_вложенных_деталей():
    u = SimpleNamespace(
        prompt_tokens=12574, completion_tokens=218,
        prompt_tokens_details=SimpleNamespace(cached_tokens=12416),
    )
    assert token_counts(u) == (12574, 12416, 218)


def test_без_деталей_кэш_ноль():
    assert token_counts(SimpleNamespace(prompt_tokens=5, completion_tokens=1)) == (5, 0, 1)
    assert token_counts(None) == (0, 0, 0)


def test_кэшированный_вызов_разбора_дешевле_в_разы():
    """Живые цифры с прода 16.09.2026: второй одинаковый вызов разбора."""
    cold = Usage("gpt-5.1", 12574, 218)
    warm = Usage("gpt-5.1", 12574, 218, cached_tokens=12416)
    assert warm.cost_usd < cold.cost_usd / 4
