import asyncio
import logging
import os
import sys
from datetime import datetime

# Ensure ~/bin is in PATH (ffmpeg/ffprobe installed there)
_home_bin = os.path.expanduser("~/bin")
if _home_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _home_bin + ":" + os.environ.get("PATH", "")

# --- Monkey-patch maxapi upload methods ---
# maxapi 0.9.13 creates bot.session with base_url, but get_upload_url() returns
# absolute URLs.  aiohttp asserts url.is_absolute()==False when base_url is set.
# Fix: always use a fresh ClientSession (no base_url) for file uploads.
def _patch_maxapi_uploads():
    from maxapi.connection.base import BaseConnection
    from aiohttp import ClientSession

    _orig_upload_file = BaseConnection.upload_file
    _orig_upload_file_buffer = BaseConnection.upload_file_buffer

    async def _patched_upload_file(self, url, path, type):
        import aiofiles, os, puremagic, mimetypes
        from aiohttp import FormData
        async with aiofiles.open(path, "rb") as f:
            file_data = await f.read()
        basename = os.path.basename(path)
        _, ext = os.path.splitext(basename)
        form = FormData(quote_fields=False)
        form.add_field(name="data", value=file_data, filename=basename,
                       content_type=f"{type.value}/{ext.lstrip('.')}")
        async with ClientSession() as temp_session:
            response = await temp_session.post(url=url, data=form)
            return await response.text()

    async def _patched_upload_file_buffer(self, filename, url, buffer, type):
        import mimetypes, puremagic
        from aiohttp import FormData
        try:
            matches = puremagic.magic_string(buffer[:4096])
            if matches:
                mime_type = matches[0][1]
                ext = mimetypes.guess_extension(mime_type) or ""
            else:
                mime_type = f"{type.value}/*"
                ext = ""
        except Exception:
            mime_type = f"{type.value}/*"
            ext = ""
        basename = f"{filename}{ext}"
        form = FormData(quote_fields=False)
        form.add_field(name="data", value=buffer, filename=basename,
                       content_type=mime_type)
        async with ClientSession() as temp_session:
            response = await temp_session.post(url=url, data=form)
            return await response.text()

    BaseConnection.upload_file = _patched_upload_file
    BaseConnection.upload_file_buffer = _patched_upload_file_buffer

_patch_maxapi_uploads()


# --- Monkey-patch MessageCallback.answer ---
# maxapi's answer() always copies self.message.body.attachments into the callback
# response.  When the original message has an inline keyboard whose buttons
# serialize as null, the Max API returns 400 ("getButtons() is null").
# Fix: build the callback message WITHOUT copying attachments; only send
# text/notification.
def _patch_callback_answer():
    from maxapi.types.updates.message_callback import MessageCallback

    _orig_answer = MessageCallback.answer

    async def _safe_answer(self, notification=None, new_text=None, link=None,
                           notify=True, format=None):
        from maxapi.types.updates.message_callback import MessageForCallback
        message = MessageForCallback()
        message.text = new_text
        # Do NOT copy attachments — that's the source of the 400 error
        message.link = link
        message.notify = notify
        message.format = format

        return await self._ensure_bot().send_callback(
            callback_id=self.callback.callback_id,
            message=message,
            notification=notification,
        )

    MessageCallback.answer = _safe_answer

_patch_callback_answer()
# --- End monkey-patches ---

import pytz
from apscheduler.triggers.cron import CronTrigger
from fluentogram import TranslatorHub

from max_handlers import (
    commands_handlers, user_handlers, settings_handlers,
    balance_handlers, admin_handlers, referral_handlers, test_handlers,
)
from max_middlewares.check_user import UserMiddleware
from models.orm import check_subscriptions, init_models, mark_sessions_interrupted_on_shutdown, \
    startup_handle_interrupted_sessions, init_background_logging
from services.init_max_bot import max_bot, config
from services.bot_provider import register_bot
from services.scheduler import scheduler
from services.telegram_alerts import init_telegram_logger, send_alert, get_telegram_logger
from utils.i18n import create_translator_hub

from maxapi import Dispatcher
from maxapi.types.command import BotCommand

logger = logging.getLogger(__name__)


async def main() -> None:
    dp = Dispatcher()

    translator_hub: TranslatorHub = create_translator_hub()

    # Include routers — order matters:
    # 1. commands (start, menu, cancel)
    # 2. settings, balance, admin, referral, test (specific callback/state handlers)
    # 3. user_handlers last (catch-all for audio/media/dialogue)
    dp.include_routers(
        commands_handlers.router,
        settings_handlers.router,
        balance_handlers.router,
        admin_handlers.router,
        referral_handlers.router,
        test_handlers.router,
        user_handlers.router,
    )

    # Register middleware — injects user + i18n into handler data
    dp.middleware(UserMiddleware())

    # on_started lifecycle hook — runs after bot connects, before processing updates
    @dp.on_started()
    async def on_started():
        logger.info('Starting Max bot initialization')

        # Register max bot in the provider so shared services can access it
        register_bot('max', max_bot)

        await init_models()
        await init_background_logging()
        logger.info('Database and background logging initialized')

        if config.tg_bot.log_chat_id:
            await init_telegram_logger(max_bot, config.tg_bot.log_chat_id)
            await send_alert("🟢 Max bot started successfully", "INFO", "SYSTEM")

        scheduler.start()
        scheduler.add_job(
            func=check_subscriptions,
            trigger='date',
            run_date=datetime.now(pytz.UTC),
            args=[scheduler]
        )
        scheduler.add_job(
            func=check_subscriptions,
            trigger=CronTrigger(hour=0, minute=0, timezone=pytz.UTC),
            args=[scheduler]
        )
        # Note: onboarding_reminders and payment_reminders are NOT scheduled here.
        # Those are Telegram-specific and run in the Telegram bot process (main.py).
        # Max-specific reminders can be added here when Max users are distinguishable in the DB.
        scheduler.print_jobs()

        await startup_handle_interrupted_sessions()

        await max_bot.set_my_commands(
            BotCommand(name='start', description='Главное меню | Home'),
            BotCommand(name='subscription', description='Купить подписку | Get subscription'),
            BotCommand(name='referral', description='Реферальная программа | Referral Program'),
            BotCommand(name='settings', description='Настройки | Settings'),
            BotCommand(name='support', description='Поддержка | Support'),
        )
        logger.info('Bot commands menu set')

        logger.info('Max bot initialization complete')

    # Store translator_hub in a way accessible to middleware
    # The middleware data dict receives context from the dispatcher;
    # we inject _translator_hub via a simple outer middleware
    class TranslatorHubMiddleware:
        async def __call__(self, handler, event_object, data):
            data['_translator_hub'] = translator_hub
            return await handler(event_object, data)

    dp.outer_middleware(TranslatorHubMiddleware())

    # Start polling
    logger.info('Starting Max bot in polling mode...')
    try:
        await dp.start_polling(max_bot, skip_updates=True)
    finally:
        # Cleanup on shutdown
        telegram_logger = get_telegram_logger()
        if telegram_logger:
            await send_alert("🔴 Max bot stopped", "INFO", "SYSTEM")
            await telegram_logger.stop()
        await mark_sessions_interrupted_on_shutdown()


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

        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.error('Max bot stopped!')
    except Exception as e:
        logger.error(f'Max bot crashed: {e}', exc_info=True)
