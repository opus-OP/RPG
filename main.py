"""Точка входа в приложение.

Запускает Telegram-бота с python-telegram-bot.
При старте: подключение к БД, загрузка рынка.
При остановке: закрытие БД.
"""

import logging

from telegram.ext import ApplicationBuilder

import config
from data.storage import storage
from core.economy import MARKET
from bot.handlers import register_handlers, _load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rpg")


async def post_init(app):
    """Инициализация после создания приложения: БД + рынок + настройки."""
    await storage.connect()
    await _load_settings()
    await MARKET.load_from_storage(storage)
    if config.CAMPAIGN_ENABLED:
        from bot.campaign_handlers import recover_campaigns
        closed = await recover_campaigns(app)
        if closed:
            log.info("Закрыто прерванных кампаний: %d", closed)
    log.info(f"БД готова: {storage.db_path}, загружено объявлений: {len(MARKET.listings)}")
    log.info("Бот запущен. Ожидание команд...")


async def post_shutdown(app):
    """Завершение работы: закрытие БД."""
    await storage.close()
    log.info("БД закрыта. До свидания!")


def main():
    log.info("=" * 40)
    log.info("RPG Bot — запуск")
    log.info("=" * 40)

    if not config.BOT_TOKEN:
        log.warning("BOT_TOKEN не задан! Укажите его в переменной окружения BOT_TOKEN.")
        log.warning("Работа без Telegram-бота невозможна.")
        return

    if not config.NN_API_KEY:
        log.warning("NN_API_KEY не задан: нарратив и статус-эффекты от нейросети работать не будут "
                    "(бой пойдёт без них).")
    if not config.ADMIN_PASSWORD:
        log.info("ADMIN_PASSWORD не задан: админ-команды отключены.")

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
