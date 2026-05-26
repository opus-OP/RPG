import logging

from telegram.ext import ApplicationBuilder

import config
from data.storage import storage
from core.economy import MARKET
from bot.handlers import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rpg")


async def post_init(app):
    await storage.connect()
    await MARKET.load_from_storage(storage)
    log.info(f"БД готова: {storage.db_path}, загружено объявлений: {len(MARKET.listings)}")
    log.info("Бот запущен. Ожидание команд...")


async def post_shutdown(app):
    await storage.close()
    log.info("БД закрыта. До свидания!")


def main():
    log.info("=" * 40)
    log.info("RPG Bot — запуск")
    log.info("=" * 40)

    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN не задан! Укажите в config.py или переменной окружения.")
        log.warning("Работа без Telegram-бота невозможна.")
        return

    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    register_handlers(app)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
