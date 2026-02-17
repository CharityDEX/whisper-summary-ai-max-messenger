"""
Health Monitor Bot - Standalone service for monitoring main bot availability.

This script:
- Runs independently from the main bot
- Periodically checks bot health using HealthMonitorService
- Sends alerts via Telethon (no dependency on main bot!)
- Logs all checks to database
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient

from config_data.config import load_config
from models.model import Base
from user_bot.health_monitor import HealthMonitorService
from user_bot.telethon_alert_sender import TelethonAlertSender

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

logger = logging.getLogger(__name__)

# Глобальные переменные для graceful shutdown
scheduler: AsyncIOScheduler = None
is_shutting_down = False


async def perform_scheduled_check(health_service: HealthMonitorService):
    """Выполняет запланированную проверку здоровья"""
    try:
        logger.info("Starting scheduled health check...")
        result = await health_service.perform_health_check()

        if result['success']:
            logger.info(f"✓ Health check passed: {result['response_time_ms']}ms")
        else:
            logger.warning(f"✗ Health check failed: {result.get('error_message', 'Unknown error')}")

    except Exception as e:
        logger.error(f"Error during scheduled health check: {e}", exc_info=True)


async def init_database(config):
    """Инициализирует подключение к БД и создает таблицы"""
    db_name = config.db.database
    user_name = config.db.user
    password = config.db.password

    engine = create_async_engine(
        f'postgresql+asyncpg://{user_name}:{password}@localhost:5432/{db_name}',
        echo=False,
        pool_size=10,
        max_overflow=10,
        pool_timeout=10,
        pool_recycle=3600,
        pool_pre_ping=True
    )

    # Создаем таблицы если их нет
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Создаем фабрику сессий
    async_session = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )

    return async_session


async def on_startup(config, alert_sender: TelethonAlertSender):
    """Startup hook"""
    logger.info("=" * 60)
    logger.info("Health Monitor Bot Starting...")
    logger.info("=" * 60)

    # Отправляем алерт о старте через Telethon
    if config.health_monitor.health_chat_id:
        config_info = {
            'interval': config.health_monitor.check_interval_minutes,
            'command': config.health_monitor.check_command,
            'warning': config.health_monitor.response_warning_seconds,
            'critical': config.health_monitor.response_critical_seconds
        }
        await alert_sender.send_startup_alert(config_info)

    logger.info("=" * 60)
    logger.info(f"Monitor is running. Next check in {config.health_monitor.check_interval_minutes} minute(s)")
    logger.info("=" * 60)


async def on_shutdown(config, alert_sender: TelethonAlertSender):
    """Shutdown hook"""
    global is_shutting_down
    is_shutting_down = True

    logger.info("=" * 60)
    logger.info("Health Monitor Bot Shutting Down...")
    logger.info("=" * 60)

    # Отправляем уведомление о выключении через Telethon
    if config.health_monitor.health_chat_id:
        await alert_sender.send_shutdown_alert()

    logger.info("Shutdown complete")


def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    global is_shutting_down
    logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
    is_shutting_down = True
    if scheduler:
        scheduler.shutdown(wait=False)


async def main():
    """Главная функция"""
    global scheduler, is_shutting_down

    shared_client = None  # Для cleanup в finally блоке

    try:
        # Загружаем конфигурацию
        logger.info("Loading configuration...")
        config = load_config('.env')

        # Проверяем, включен ли монитор
        if not config.health_monitor.enabled:
            logger.warning("Health monitor is DISABLED in configuration. Exiting...")
            return

        # Проверяем наличие обязательных настроек
        if not config.health_monitor.health_chat_id:
            logger.error("HEALTH_CHAT_ID is not configured. Please set it in .env file.")
            return

        # Инициализируем БД
        logger.info("Initializing database connection...")
        async_session = await init_database(config)

        # КРИТИЧНО: Создаем ОДИН shared Telethon клиент для всего
        logger.info("Creating shared Telethon client...")
        shared_client = TelegramClient(
            session=config.user_bot.session_name,
            api_id=config.user_bot.api_id,
            api_hash=config.user_bot.api_hash,
            device_model='Desktop',
            system_version='Linux ubuntu X11 glibc 2.35',
            app_version='4.8.3 Snap',
            system_lang_code='ru-RU',
            lang_code='ru'
        )

        logger.info("Starting shared Telethon connection...")
        await shared_client.start()
        logger.info("✓ Shared Telethon client connected")

        # Создаем alert sender (будет использовать shared client)
        logger.info("Creating alert sender...")
        alert_sender = TelethonAlertSender(
            health_chat_id=config.health_monitor.health_chat_id,
            client=shared_client
        )

        # Получаем username бота из конфига
        bot_username = config.tg_bot.bot_name

        # Создаем сервис мониторинга (будет использовать shared client)
        logger.info("Creating health monitor service...")
        health_service = HealthMonitorService(
            config=config,
            async_session=async_session,
            bot_username=bot_username,
            alert_sender=alert_sender,
            client=shared_client
        )

        # Регистрируем event handlers в сервисе
        health_service.set_client(shared_client)

        # Запускаем startup hook
        await on_startup(config, alert_sender)

        # Выполняем первую проверку сразу
        logger.info("Performing initial health check...")
        await perform_scheduled_check(health_service)

        # Настраиваем scheduler
        scheduler = AsyncIOScheduler()

        # Добавляем задачу проверки здоровья
        scheduler.add_job(
            func=perform_scheduled_check,
            trigger=IntervalTrigger(minutes=config.health_monitor.check_interval_minutes),
            args=[health_service],
            id='health_check',
            name='Bot Health Check',
            replace_existing=True
        )

        # Настраиваем обработчики сигналов
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Запускаем scheduler
        scheduler.start()

        # Держим приложение запущенным
        logger.info("Press Ctrl+C to stop")
        try:
            while not is_shutting_down:
                await asyncio.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Received KeyboardInterrupt, shutting down...")
            is_shutting_down = True

        # Graceful shutdown
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)

        await on_shutdown(config, alert_sender)

    except Exception as e:
        logger.error(f"Fatal error in monitor bot: {e}", exc_info=True)
        sys.exit(1)

    finally:
        # КРИТИЧНО: Всегда останавливаем shared Telethon клиент при выходе
        if shared_client and shared_client.is_connected():
            try:
                logger.info("Disconnecting shared Telethon client...")
                await shared_client.disconnect()
                logger.info("✓ Shared Telethon client disconnected")
            except Exception as e:
                logger.error(f"Error stopping shared client: {e}")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitor bot stopped by user")
    except Exception as e:
        logger.error(f"Monitor bot crashed: {e}", exc_info=True)
        sys.exit(1)
