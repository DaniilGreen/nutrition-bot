"""NFR-4: о пустом балансе OpenAI владелец узнаёт один раз, а не на каждое сообщение."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.bot import handlers


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append(chat_id)


def _container():
    users = SimpleNamespace(list_active=None)

    async def list_active():
        return [SimpleNamespace(role="owner", telegram_id=1),
                SimpleNamespace(role="user", telegram_id=2)]

    users.list_active = list_active

    @asynccontextmanager
    async def session():
        yield None

    return SimpleNamespace(db=SimpleNamespace(session_factory=session),
                           assistant_for=lambda s: (None, users))


async def test_владельцу_одно_сообщение_и_только_ему(monkeypatch):
    monkeypatch.setattr(handlers, "_no_credits_alerted_at", None)
    bot, c = _Bot(), _container()
    await handlers._alert_owner_no_credits(bot, c)
    await handlers._alert_owner_no_credits(bot, c)
    assert bot.sent == [1]
