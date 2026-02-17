from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from fluentogram import TranslatorHub, TranslatorRunner

from keyboards.user_keyboards import inline_change_language_menu, inline_change_model_menu, \
    inline_change_specify_language_menu, inline_change_transcription_format_menu, inline_user_settings
from models.orm import change_user_setting, get_user
from utils.i18n import create_translator_hub

router = Router()

async def process_settings(message: Message, state: FSMContext, i18n: TranslatorRunner, user: dict):
    await message.answer(text=i18n.settings_menu(),
                         reply_markup=inline_user_settings(user=await get_user(telegram_id=message.from_user.id), i18n=i18n))


@router.callback_query(F.data == 'settings')
async def process_callback_settings(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    await callback.message.delete()
    await process_settings(callback.message, state, i18n=i18n, user=user)

@router.callback_query(F.data == 'setting_menu')
async def process_setting_menu(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    await callback.message.edit_text(text=i18n.settings_menu(),
                                     reply_markup=inline_user_settings(user=await get_user(telegram_id=callback.from_user.id),
                                                                       i18n=i18n))

@router.callback_query(F.data.startswith('setting_menu|'))
async def process_setting_menu_callback(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    setting_name = callback.data.split('|')[-1]

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
        # Toggle the download_video setting
        setting_value = not user.get('download_video', False)
        await change_user_setting(telegram_id=user['telegram_id'], setting_name=setting_name, setting_value=setting_value)
        user['download_video'] = setting_value
        text = i18n.get('download_video_menu')
        keyboard = inline_user_settings(user=user, i18n=i18n)
    else:
        text = i18n.get(f'{setting_name}_menu')
        keyboard = inline_user_settings(user=user, i18n=i18n)
    await callback.message.edit_text(text=text,
                                     reply_markup=keyboard)

@router.callback_query(F.data.startswith('change_setting|'))
async def process_change_setting(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    setting_name = callback.data.split('|')[-1]
    setting_value = callback.data.split('|')[-2]
    
    # Для boolean настроек обрабатываем особым образом
    if setting_name == 'specify_audio_language':
        if setting_value == 'active':
            setting_value = True
        else:
            setting_value = False

        if user[setting_name] == setting_value:
            await callback.answer(text='Это значение уже установлено.')
            return
        setting_value = not user['specify_audio_language']
    else:
        # Для других настроек проверяем строковое равенство
        if user.get(setting_name) == setting_value:
            await callback.answer(text='Это значение уже установлено.')
            return
    
    await change_user_setting(telegram_id=user['telegram_id'], setting_name=setting_name, setting_value=setting_value)
    user[setting_name] = setting_value
    await process_setting_menu_callback(callback=callback, state=state, user=user, i18n=i18n)


@router.callback_query(F.data == 'change_language')
async def process_change_language(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    await callback.message.edit_text(text=i18n.change_language_menu(),
                                     reply_markup=inline_change_language_menu(user=user, i18n=i18n))

@router.callback_query(F.data.startswith('change_language|'))
async def process_change_language_callback(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    language = callback.data.split('|')[-1]
    if language == user['user_language']:
        await callback.answer(text=i18n.language_already_set())
        return
    else:
        await change_user_setting(telegram_id=user['telegram_id'], setting_name='user_language', setting_value=language)
        translator_hub: TranslatorHub = create_translator_hub()
        i18n = translator_hub.get_translator_by_locale(locale=language)
        user['user_language'] = language
        await callback.message.edit_text(text=i18n.change_language_menu(),
                                        reply_markup=inline_change_language_menu(user=user, i18n=i18n))


@router.callback_query(F.data == 'main_menu_for_settings')
async def process_main_menu_for_settings(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    await callback.message.delete()