"""Учёт расходов на LLM и лимит трат.

Без этого лимит $5 из OQ-M3 неисполним: тратить можно сколько угодно, узнаешь
из счёта в конце месяца. Считаем сами по токенам — провайдер не даёт остаток
в реальном времени, а если бы и давал, ходить за ним на каждый запрос дорого.

Цены — в долларах за миллион токенов, на 02.09.2026. Они меняются; когда
изменятся, счётчик станет врать в известную сторону. Это осознанный потолок:
точная сверка со счётом провайдера стоит дороже, чем ошибка в пару процентов
на бюджете в пять долларов.
"""
from __future__ import annotations

from dataclasses import dataclass

# $/1M токенов: (вход, выход)
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.1": (1.25, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "whisper-1": (0.006, 0.0),  # за минуту, не за токен — считается отдельно
}

DEFAULT_PRICE = (2.00, 8.00)  # неизвестная модель: считаем дороже, а не дешевле

# $/1M токенов входа, прочитанных из кэша промпта. У OpenAI кэш включается сам
# на префиксе от 1024 токенов, и у разбора он срабатывает почти всегда: промпт
# ~12.5k токенов, из них ~12.4k одинаковые. До 16.09.2026 скидку не учитывали,
# и счётчик завышал разбор в разы. Модели нет в словаре — кэш по полной цене.
CACHED_PRICES: dict[str, float] = {
    "gpt-5.1": 0.125,
    "gpt-4.1": 0.50,
    "gpt-4.1-mini": 0.10,
}


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0  # часть input_tokens, прочитанная из кэша промпта

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.model, DEFAULT_PRICE)
        cached = min(max(self.cached_tokens, 0), self.input_tokens)
        pcached = CACHED_PRICES.get(self.model, pin)
        return (
            (self.input_tokens - cached) * pin + cached * pcached + self.output_tokens * pout
        ) / 1_000_000


def month_limit_reached(spent_usd: float, limit_usd: float) -> bool:
    """Лимит проверяется ДО запроса, а не после.

    Проверка после означала бы «превысил — узнал», то есть лимит,
    который не ограничивает.
    """
    return spent_usd >= limit_usd


def demo() -> None:
    """Runnable-чек: стоимость и срабатывание лимита."""
    u = Usage("gpt-5.1", input_tokens=1_000_000, output_tokens=0)
    assert abs(u.cost_usd - 1.25) < 1e-9, u.cost_usd
    u2 = Usage("gpt-5.1", input_tokens=0, output_tokens=100_000)
    assert abs(u2.cost_usd - 1.0) < 1e-9, u2.cost_usd
    # Неизвестная модель считается по дорогому тарифу — ошибаться надо в свою сторону.
    assert Usage("неизвестная", 1_000_000, 0).cost_usd > u.cost_usd
    # Кэш: миллион входных, из них 900k из кэша → 0.1*1.25 + 0.9*0.125.
    c = Usage("gpt-5.1", 1_000_000, 0, cached_tokens=900_000)
    assert abs(c.cost_usd - (0.125 + 0.1125)) < 1e-9, c.cost_usd
    # Кэш больше входа — мусор провайдера; не уходим в минус.
    assert Usage("gpt-5.1", 10, 0, cached_tokens=99).cost_usd >= 0
    assert not month_limit_reached(4.99, 5.0)
    assert month_limit_reached(5.0, 5.0)      # ровно лимит — уже стоп
    assert month_limit_reached(7.3, 5.0)
    print("учёт расходов: ок")


if __name__ == "__main__":
    demo()
