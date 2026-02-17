from asyncio import sleep

from maxapi import Router, F
from maxapi.context import MemoryContext
from maxapi.types import (
    MessageCreated, MessageCallback, CommandStart, BotStarted,
    CallbackButton, LinkButton,
)
from fluentogram import TranslatorRunner

from max_keyboards.user_keyboards import faq_keyboard, inline_main_menu
from models.orm import log_user_action_async
from services.max_audio_queue_service import max_audio_queue_manager
from services.max_static_files_cache import send_intro_video, send_faq_document

router = Router()


@router.message_callback(F.callback.payload == 'cancel')
async def process_cancel_callback(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    await event.answer()
    await context.clear()

    queue_was_cleared = await max_audio_queue_manager.clear_queue(event.callback.user.user_id)
    if queue_was_cleared and i18n:
        await event.message.answer(text=i18n.queue_cleared())

    if user and user.get('new_user'):
        text = i18n.new_user_start()
        await event.message.answer(
            text=text,
            attachments=[inline_main_menu(i18n=i18n)]
        )
        await sleep(3)
        await event.message.answer(text=i18n.new_user_free_requests())
    else:
        await _send_main_menu(event.message, i18n=i18n)


@router.bot_started()
async def process_bot_started(event: BotStarted, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    """Handle /start equivalent — BotStarted fires when user first interacts or presses Start."""
    await context.clear()

    queue_was_cleared = await max_audio_queue_manager.clear_queue(event.user.user_id)

    if user and user.get('new_user'):
        text = i18n.new_user_start() if i18n else "Welcome!"
        lang = user.get('user_language', 'ru') if user else 'ru'
        from services.max_static_files_cache import send_intro_video_to_chat
        await send_intro_video_to_chat(
            bot=event.bot,
            chat_id=event.chat_id,
            lang=lang,
            caption=text,
        )
        await sleep(3)
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=i18n.new_user_free_requests() if i18n else "You have free requests!",
        )
    else:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text=i18n.main_menu_message() if i18n else "Main menu",
            attachments=[inline_main_menu(i18n=i18n, notetaker_link=True)],
        )


@router.message_created(CommandStart())
async def process_start_command(event: MessageCreated, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    """Handle /start typed as text (BotStarted only fires on button press)."""
    await context.clear()

    queue_was_cleared = await max_audio_queue_manager.clear_queue(event.message.sender.user_id)
    if queue_was_cleared and i18n:
        await event.message.answer(text=i18n.queue_cleared())

    if user and user.get('new_user'):
        text = i18n.new_user_start() if i18n else "Welcome!"
        await event.message.answer(
            text=text,
            attachments=[inline_main_menu(i18n=i18n)],
        )
        await sleep(3)
        await event.message.answer(
            text=i18n.new_user_free_requests() if i18n else "You have free requests!",
        )
    else:
        await _send_main_menu(event.message, i18n=i18n)


@router.message_callback(F.callback.payload == 'main_menu')
async def process_main_menu_callback(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    await event.answer(
        new_text=i18n.main_menu_message() if i18n else "Main menu",
    )
    # Re-send menu with keyboard since callback answer may not support attachments
    await event.message.answer(
        text=i18n.main_menu_message() if i18n else "Main menu",
        attachments=[inline_main_menu(i18n=i18n, notetaker_link=True)],
    )

    if user:
        await log_user_action_async(
            user_id=user['id'],
            action_type='new_menu_opened',
            action_category='feature',
        )


@router.message_callback(F.callback.payload == 'faq')
async def process_faq_callback(event: MessageCallback, user: dict):
    await event.answer()
    lang = user.get('user_language', 'ru') if user else 'ru'
    await send_faq_document(event.message, lang=lang)


@router.message_callback(F.callback.payload == 'support')
async def process_support_callback(event: MessageCallback, i18n: TranslatorRunner):
    await event.answer()
    await event.message.answer(
        text=i18n.support_menu() if i18n else "Support",
        attachments=[faq_keyboard(i18n=i18n)],
    )


@router.message_callback(F.callback.payload == 'subscription')
async def process_subscription_callback(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    """Legacy fallback — 'subscription_menu' payload is handled by balance_handlers."""
    await event.answer()
    await event.message.answer(text=i18n.subscription_menu() if i18n else "Subscription menu")


@router.message_callback(F.callback.payload == 'new_audio')
async def process_new_audio_callback(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await event.answer()
    await event.message.answer(text=i18n.waiting_audio() if i18n else "Send me an audio or voice message")


async def _send_main_menu(message, i18n: TranslatorRunner = None):
    """Helper to send the main menu as an inline keyboard message."""
    await message.answer(
        text=i18n.main_menu_message() if i18n else "Main menu",
        attachments=[inline_main_menu(i18n=i18n, notetaker_link=True)],
    )
