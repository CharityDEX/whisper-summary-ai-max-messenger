import logging

from maxapi import Router, F
from maxapi.context import MemoryContext
from maxapi.types import MessageCallback
from maxapi.types.message import Message
from fluentogram import TranslatorHub, TranslatorRunner

from max_keyboards.user_keyboards import (
    inline_change_language_menu, inline_change_model_menu,
    inline_change_specify_language_menu, inline_change_transcription_format_menu,
    inline_user_settings,
)
from models.orm import change_user_setting, get_user
from utils.i18n import create_translator_hub

router = Router()
logger = logging.getLogger(__name__)


async def process_settings(message: Message, i18n: TranslatorRunner, user: dict):
    """Show settings menu. Called from callback handlers."""
    user = await get_user(telegram_id=message.sender.user_id)
    await message.answer(
        text=i18n.settings_menu(),
        attachments=[inline_user_settings(user=user, i18n=i18n)],
    )


@router.message_callback(F.callback.payload == 'settings')
async def process_callback_settings(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    await event.message.delete()
    await process_settings(event.message, i18n=i18n, user=user)


@router.message_callback(F.callback.payload == 'setting_menu')
async def process_setting_menu(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    user = await get_user(telegram_id=event.callback.user.user_id)
    await event.message.edit(
        text=i18n.settings_menu(),
        attachments=[inline_user_settings(user=user, i18n=i18n)],
    )


@router.message_callback(F.callback.payload.startswith('setting_menu|'))
async def process_setting_menu_callback(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    setting_name = event.callback.payload.split('|')[-1]

    if setting_name == 'llm_model':
        text = i18n.get(f'{setting_name}_menu')
        keyboard = inline_change_model_menu(user=user, i18n=i18n)
    elif setting_name == 'specify_audio_language':
        text = i18n.get('specify_audio_language_menu')
        keyboard = inline_change_specify_language_menu(user=user, i18n=i18n)
    elif setting_name == 'transcription_format':
        text = i18n.get('transcription_format_menu')
        keyboard = inline_change_transcription_format_menu(user=user, i18n=i18n)
    elif setting_name == 'download_video':
        setting_value = not user.get('download_video', False)
        await change_user_setting(telegram_id=user['telegram_id'], setting_name=setting_name, setting_value=setting_value)
        user['download_video'] = setting_value
        text = i18n.get('download_video_menu')
        keyboard = inline_user_settings(user=user, i18n=i18n)
    else:
        text = i18n.get(f'{setting_name}_menu')
        keyboard = inline_user_settings(user=user, i18n=i18n)

    await event.message.edit(text=text, attachments=[keyboard])


@router.message_callback(F.callback.payload.startswith('change_setting|'))
async def process_change_setting(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    setting_name = event.callback.payload.split('|')[-1]
    setting_value = event.callback.payload.split('|')[-2]

    if setting_name == 'specify_audio_language':
        setting_value = setting_value == 'active'
        if user[setting_name] == setting_value:
            await event.answer()
            return
        setting_value = not user['specify_audio_language']
    else:
        if user.get(setting_name) == setting_value:
            await event.answer()
            return

    await change_user_setting(telegram_id=user['telegram_id'], setting_name=setting_name, setting_value=setting_value)
    user[setting_name] = setting_value
    await process_setting_menu_callback(event=event, context=context, user=user, i18n=i18n)


@router.message_callback(F.callback.payload == 'change_language')
async def process_change_language(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    await event.message.edit(
        text=i18n.change_language_menu(),
        attachments=[inline_change_language_menu(user=user, i18n=i18n)],
    )


@router.message_callback(F.callback.payload.startswith('change_language|'))
async def process_change_language_callback(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    language = event.callback.payload.split('|')[-1]
    if language == user['user_language']:
        await event.answer()
        return

    await change_user_setting(telegram_id=user['telegram_id'], setting_name='user_language', setting_value=language)
    translator_hub: TranslatorHub = create_translator_hub()
    i18n = translator_hub.get_translator_by_locale(locale=language)
    user['user_language'] = language
    await event.message.edit(
        text=i18n.change_language_menu(),
        attachments=[inline_change_language_menu(user=user, i18n=i18n)],
    )


@router.message_callback(F.callback.payload == 'main_menu_for_settings')
async def process_main_menu_for_settings(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    await event.message.delete()
