"""Бот с защитой от флуд-контроля Telegram (RetryAfter) и паузой между сообщениями в группу."""

import asyncio
import logging
import time

from telegram.error import RetryAfter
from telegram.ext import ExtBot

log = logging.getLogger("rpg.ratelimit")

GROUP_MIN_INTERVAL = 1.0   # сек между сообщениями в один групповой чат
MAX_RETRIES = 4


class RetryBot(ExtBot):
    """ExtBot, который при `RetryAfter` ждёт указанное время и повторяет отправку/правку."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_sent: dict = {}
        self._pace_lock = asyncio.Lock()

    async def _pace(self, chat_id):
        if not isinstance(chat_id, int) or chat_id >= 0:
            return
        async with self._pace_lock:
            wait = self._last_sent.get(chat_id, 0) + GROUP_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_sent[chat_id] = time.monotonic()

    async def _retrying(self, call, chat_id, *args, **kwargs):
        for attempt in range(MAX_RETRIES):
            await self._pace(chat_id)
            try:
                return await call(*args, **kwargs)
            except RetryAfter as e:
                delay = float(getattr(e.retry_after, "total_seconds", lambda: e.retry_after)()) + 1
                log.warning("Флуд-контроль: жду %.0f с (попытка %d)", delay, attempt + 1)
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(delay)

    async def send_message(self, chat_id, *args, **kwargs):
        return await self._retrying(super().send_message, chat_id, chat_id, *args, **kwargs)

    async def edit_message_text(self, *args, **kwargs):
        chat_id = kwargs.get("chat_id")
        return await self._retrying(super().edit_message_text, chat_id, *args, **kwargs)
