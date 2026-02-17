import logging
from datetime import datetime
import pytz
import sys

from aiohttp import web
from aiogram import Dispatcher, Bot
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from fluentogram import TranslatorHub

from handlers import balance_hanlders, commands_handlers, settings_handlers, user_handlers, admin_handlers, \
    test_handlers, referral_handlers
from keyboards.set_menu import set_main_menu
from middlewares.check_user import UserMiddleware
from models.orm import check_subscriptions, init_models, mark_sessions_interrupted_on_shutdown, \
    startup_handle_interrupted_sessions, init_background_logging
from services.init_bot import config, bot
from services.scheduler import scheduler
from services.telegram_alerts import init_telegram_logger, send_alert, get_telegram_logger
from services.payment_reminders import send_first_payment_reminder, send_second_payment_reminder
from services.onboarding_reminders import send_onboarding_reminders
from services.internal_metrics import start_metrics_collector, stop_metrics_collector, metrics_handler
from apscheduler.triggers.cron import CronTrigger

from utils.i18n import create_translator_hub

logger = logging.getLogger(__name__)

# Webserver settings
WEB_SERVER_HOST = "127.0.0.1"
WEB_SERVER_PORT = 3000

# Path to webhook route
WEBHOOK_PATH = "/webhook"
# Base URL для Local Bot API
BASE_WEBHOOK_URL = "http://localhost:3000"


async def on_startup() -> None:
    """Startup hook для инициализации всех сервисов"""
    logger.info('Starting bot initialization')

    # Запускаем сборщик внутренних метрик (event loop lag, GC, threads, API latency)
    await start_metrics_collector(sample_interval_ms=100, bot=bot, api_check_interval_sec=30)
    logger.info('Internal metrics collector started (with Telegram API latency monitoring)')

    # Инициализируем базу данных
    await init_models()

    # Инициализируем систему фонового логирования
    await init_background_logging()
    logger.info('Background logging system initialized')

    # Инициализируем Telegram Logger
    if config.tg_bot.log_chat_id:
        await init_telegram_logger(bot, config.tg_bot.log_chat_id)
        await send_alert("🟢 Bot started successfully", "INFO", "SYSTEM")

    # Устанавливаем главное меню
    await set_main_menu(bot, language_code=config.tg_bot.default_lang)

    # Запускаем scheduler
    scheduler.start()
    # Не блокируем стартап: запускаем проверку подписок в фоне сразу после старта
    scheduler.add_job(
        func=check_subscriptions,
        trigger='date',
        run_date=datetime.now(pytz.UTC),
        args=[scheduler]
    )

    # Запускаем проверку подписок каждый день в полночь по UTC
    scheduler.add_job(
        func=check_subscriptions,
        trigger=CronTrigger(hour=0, minute=0, timezone=pytz.UTC),
        args=[scheduler]
    )

    # Запускаем отправку онбординг-напоминаний каждый день в 12:00 MSK (09:00 UTC)
    scheduler.add_job(
        func=send_onboarding_reminders,
        trigger=CronTrigger(hour=9, minute=0, timezone=pytz.UTC),
        id='onboarding_reminders',
        replace_existing=True
    )

    # # Запускаем проверку напоминаний о незавершенных платежах каждые 15 минут
    # # Первое напоминание (по умолчанию через 2 часа после первого действия конверсии)
    scheduler.add_job(
        func=send_first_payment_reminder,
        trigger='interval',
        minutes=15,
        id='payment_reminder_first',
        replace_existing=True
    )
    #
    # # Второе напоминание (по умолчанию через 24 часа после последнего действия)
    # scheduler.add_job(
    #     func=send_second_payment_reminder,
    #     trigger='interval',
    #     minutes=15,
    #     id='payment_reminder_second',
    #     replace_existing=True
    # )

    scheduler.print_jobs()

    # Обрабатываем прерванные сессии
    await startup_handle_interrupted_sessions()

    # Устанавливаем webhook
    await bot.set_webhook(f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}", drop_pending_updates=True)

    logger.info(f'Webhook set to {BASE_WEBHOOK_URL}{WEBHOOK_PATH}')


async def on_shutdown() -> None:
    """Shutdown hook для корректного завершения работы"""
    # Останавливаем сборщик внутренних метрик
    await stop_metrics_collector()

    await mark_sessions_interrupted_on_shutdown()

    # Graceful shutdown telegram logger
    telegram_logger = get_telegram_logger()
    if telegram_logger:
        await send_alert("🔴 Bot stopped", "INFO", "SYSTEM")
        await telegram_logger.stop()

    # Удаление webhook при завершении
    await bot.delete_webhook()


def main() -> None:
    # Инициализируем диспетчер
    dp: Dispatcher = Dispatcher()

    # Создаем translator hub
    translator_hub: TranslatorHub = create_translator_hub()

    # Подключаем роутеры
    dp.include_router(test_handlers.router)
    dp.include_router(commands_handlers.router)
    dp.include_router(referral_handlers.router)
    dp.include_router(balance_hanlders.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(admin_handlers.router)
    dp.include_router(user_handlers.router)

    # Подключаем middleware
    dp.update.middleware(UserMiddleware())

    # Регистрируем startup и shutdown hooks
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Создаем web приложение
    app = web.Application()

    # Добавляем endpoint для внутренних метрик бота (event loop lag, GC, threads)
    app.router.add_get('/metrics', metrics_handler)

    # Создание request handler с translator_hub
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    # Передаем translator_hub в data
    webhook_requests_handler.data.update({"_translator_hub": translator_hub})

    # Регистрируем webhook handler
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    # Настройка приложения с translator_hub
    setup_application(app, dp, bot=bot, _translator_hub=translator_hub)

    # Запуск веб-сервера
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format=u'%(filename)s:%(lineno)d #%(levelname)-8s '
               u'[%(asctime)s] - %(name)s - %(message)s',
        stream=sys.stdout
    )

    try:
        import faulthandler
        faulthandler.enable(all_threads=True)

        main()
    except (KeyboardInterrupt, SystemExit):
        logger.error('Bot stopped!')
    except Exception as e:
        logger.error(f'Bot crashed: {e}', exc_info=True)
