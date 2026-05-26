import asyncio
import logging

from telegram.ext import Application, ApplicationBuilder

import config
from data.storage import storage
from core.economy import MARKET
from bot.handlers import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rpg")


async def main():
    log.info("=" * 40)
    log.info("RPG Bot — запуск")
    log.info("=" * 40)

    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN не задан! Укажите в config.py или переменной окружения.")
        log.warning("Работа без Telegram-бота невозможна.")
        return

    log.info("Инициализация базы данных...")
    await storage.connect()
    await MARKET.load_from_storage(storage)
    log.info(f"БД готова: {storage.db_path}, загружено объявлений: {len(MARKET.listings)}")

    log.info("Запуск Telegram-бота (python-telegram-bot)...")
    app = ApplicationBuilder().token(config.BOT_TOKEN).build()
    register_handlers(app)

    try:
        await app.run_polling(allowed_updates=["message", "callback_query"])
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
