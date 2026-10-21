import sys; sys.path.insert(0, ".")
import asyncio
from telegram.error import RetryAfter
from telegram.ext import ExtBot
from bot import ratelimit as R


async def test_retries_after_flood_control(monkeypatch):
    calls = []

    async def fake_send(self, chat_id, text, **kw):
        calls.append(text)
        if len(calls) == 1:
            raise RetryAfter(1)
        return "ok"

    monkeypatch.setattr(ExtBot, "send_message", fake_send)
    slept = []
    real_sleep = asyncio.sleep
    async def fast_sleep(t): slept.append(t); await real_sleep(0)
    monkeypatch.setattr(R.asyncio, "sleep", fast_sleep)
    bot = R.RetryBot("123:ABC")
    assert await bot.send_message(-100, "привет") == "ok"
    assert calls == ["привет", "привет"] and any(t >= 2 for t in slept)


async def test_gives_up_after_max_retries(monkeypatch):
    async def always(self, chat_id, text, **kw):
        raise RetryAfter(1)
    monkeypatch.setattr(ExtBot, "send_message", always)
    async def nosleep(t): pass
    monkeypatch.setattr(R.asyncio, "sleep", nosleep)
    import pytest
    with pytest.raises(RetryAfter):
        await R.RetryBot("123:ABC").send_message(5, "x")
