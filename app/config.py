"""Конфигурация через pydantic-settings (читает .env / переменные окружения)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.goals import Goal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://burnfat:burnfat@localhost:5432/burnfat"
    # Адрес мини-аппа. Не секрет (публичный домен), поэтому с дефолтом: без него
    # кнопка меню осталась бы с зафиксированным вручную URL, и отпечаток статики
    # в неё не попадал бы — то есть защита от кэша работала бы вполсилы.
    miniapp_url: str = "https://nutrition-calorie.greenjola.win"

    llm_provider: str = "openai"
    openai_api_key: str = ""
    # gpt-4.1 даёт 30k TPM (Tier 1) — на этом лимите ~70% запросов падали с 429, бот был
    # нерабочим. У gpt-5.1 те же деньги, но 500k TPM. По eval на честной метрике 4.1 точнее
    # (0.920 против 0.812), НО это качество на ОТВЕЧЕННЫЙ запрос: с учётом доступности
    # 0.920×0.3 против 0.812×1.0 — выигрывает 5.1. Порог уточнений в промпте — следующим шагом.
    openai_model: str = "gpt-5.1"
    openai_stt_model: str = "whisper-1"
    # ИИ-ассистент вкладки AI (OQ-M3). Модель та же, что в разборе еды: держать
    # две — значит держать два набора цен и два поведения.
    coach_model: str = "gpt-5.1"
    # Жёсткий месячный потолок в долларах на всю инсталляцию, не на пользователя:
    # платит владелец, и его интересует счёт целиком. Проверяется ДО запроса.
    # Считает ВСЕ вызовы модели (разбор, голос, фото, ассистент) — решение владельца
    # 16.09.2026. Отказывают при превышении только ассистент и фото блюда: запись
    # едой текстом и голосом не блокируется никогда, полнота учёта важнее счёта.
    ai_month_limit_usd: float = 15.0
    use_fake_nlu: bool = False
    # Сэмплов self-consistency (D-15), только для новых продуктов (INV-12). 28.09.2026 парный
    # замер на одних и тех же ответах gpt-5.1 (28 строк golden с сэмплами): медиана трёх и семи
    # не поменяла ни одного вердикта «в допуске»; медианная ошибка 2.9% против 1.5%. Решение
    # владельца — 3: четыре вызова из шести на каждом новом продукте не окупают полтора пункта.
    sample_k: int = 3
    # Повторы SDK — только там, где нет своего цикла (фото, голос, ассистент). У разбора цикл
    # свой (AI-PRINCIPLES §1), и SDK-повторы вкладывались в него: до 15 запросов на вызов.
    openai_max_retries: int = 2
    openai_timeout_s: float = 60.0
    # Фото, этикетки и бланки — своей моделью: смена модели разбора ради экономии не должна
    # молча уводить за собой чтение цифр с бланка, где цена ошибки выше.
    vision_model: str = "gpt-5.1"

    default_timezone: str = "Europe/Moscow"
    default_goal_kcal: float = 2000
    default_goal_protein: float = 150
    default_goal_fat: float = 65
    default_goal_carb: float = 250

    reminder_enabled: bool = True
    reminder_hour: int = 23  # локальное время вечернего итога — 23:59, у самого конца дня
    reminder_minute: int = 59

    log_level: str = "INFO"
    db_echo: bool = False

    def default_goal(self) -> Goal:
        return Goal(
            daily_kcal=self.default_goal_kcal,
            protein_g=self.default_goal_protein,
            fat_g=self.default_goal_fat,
            carb_g=self.default_goal_carb,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
