import asyncio
import sys
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

import config
from data.storage import storage
from core.economy import MARKET
from bot.handlers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rpg")


async def on_startup():
    log.info("Инициализация базы данных...")
    await storage.connect()
    await MARKET.load_from_storage(storage)
    log.info(f"БД готова: {storage.db_path}, загружено объявлений: {len(MARKET.listings)}")

    log.info("Запуск Telegram-бота...")
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def main():
    log.info("=" * 40)
    log.info("RPG Bot — запуск")
    log.info("=" * 40)

    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN не задан! Укажите в config.py или переменной окружения.")
        log.warning("Работа без Telegram-бота невозможна.")
        log.warning("Завершение.")
        return

    try:
        await on_startup()
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")
    finally:
        await storage.close()
        log.info("БД закрыта. До свидания!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
