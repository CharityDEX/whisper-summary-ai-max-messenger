from asyncio import sleep
from datetime import datetime

import io

from aiogram import Router, F, types
from aiogram.filters import StateFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, LinkPreviewOptions
from fluentogram import TranslatorRunner

from handlers.balance_hanlders import process_subscription
from handlers.settings_handlers import process_settings
from handlers.user_handlers import process_new_audio_start
from keyboards.user_keyboards import faq_keyboard, inline_main_menu, back_to_menu_keyboard
from models.orm import log_user_action_async
from services.audio_queue_service import audio_queue_manager
from services.static_files_cache import send_intro_video, send_faq_document

from lexicon.lexicon_ru import LEXICON_BUTTONS_EN, LEXICON_BUTTONS_RU
from aiogram.types import FSInputFile
router = Router()


@router.callback_query(F.data == 'cancel')
@router.message(F.text == LEXICON_BUTTONS_RU['restart_bot'])
@router.message(F.text == LEXICON_BUTTONS_EN['restart_bot'])
@router.message(CommandStart())
async def process_start_command(message: Message | CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    source = None
    if type(message) is CallbackQuery:
        await message.answer()
        message = message.message
    else:
        source = message.text.strip('/start ')

    # Очищаем очередь пользователя и проверяем, была ли она непустой
    queue_was_cleared = await audio_queue_manager.clear_queue(message.from_user.id)


    await state.clear()

    # Если очередь была очищена, отправляем уведомление
    if queue_was_cleared:
        await message.answer(text=i18n.queue_cleared())

    if user['new_user']:
        text = i18n.new_user_start()
        await send_intro_video(message, lang=user["user_language"], caption=text,
                               reply_markup=inline_main_menu(i18n=i18n))
        await sleep(3)
        await message.answer(text=i18n.new_user_free_requests(), reply_markup=back_to_menu_keyboard(i18n=i18n))
    else:
        await process_back_to_menu_reply_command(event=message, state=state, i18n=i18n, user=user)


@router.message(F.text == '🏠 Меню' or F.text == '🏠 Menu')
@router.message(F.text == 'Назад в меню' or F.text == 'Back to menu')
@router.callback_query(F.data == 'main_menu')
async def process_back_to_menu_reply_command(event: Message | CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    if isinstance(event, CallbackQuery):
        event: CallbackQuery
        await event.message.edit_text(text=i18n.main_menu_message(), reply_markup=inline_main_menu(i18n=i18n, notetaker_link=True))
    else:
        await event.answer(text=i18n.main_menu_message(), reply_markup=inline_main_menu(i18n=i18n, notetaker_link=True))

    await log_user_action_async(
        user_id=user['id'],
        action_type='new_menu_opened',
        action_category='feature',
    )

@router.message(F.text == LEXICON_BUTTONS_EN['faq'])
@router.message(F.text == LEXICON_BUTTONS_RU['faq'])
@router.callback_query(F.data == 'faq')
@router.message(F.text == '/faq')
async def process_faq_command(message: Message | CallbackQuery, user: dict):
    lang = user['user_language']
    await send_faq_document(message, lang=lang)

@router.message(F.text == LEXICON_BUTTONS_RU['settings'])
@router.message(F.text == LEXICON_BUTTONS_EN['settings'])
@router.message(F.text == '/settings')
async def process_settings_command(message: Message, state: FSMContext, i18n: TranslatorRunner, user: dict):
    await process_settings(message, state, i18n=i18n, user=user)


@router.message(F.text == LEXICON_BUTTONS_RU['support'])
@router.message(F.text == LEXICON_BUTTONS_EN['support'])
@router.callback_query(F.data == 'support')
@router.message(F.text == '/support')
async def process_support_command(message: Message, i18n: TranslatorRunner):
    if type(message) is CallbackQuery:
        message: CallbackQuery
        await message.answer()
        message: Message = message.message

    # video = FSInputFile('resources/whisper_video2.mp4', filename='whisper_bot_intro.mp4')
    await message.answer(text=i18n.support_menu(), reply_markup=faq_keyboard(i18n=i18n), link_preview_options=LinkPreviewOptions(is_disabled=True))
    # await message.answer_video(caption=LEXICON_RU['/support'], video=video)


@router.message(F.text == LEXICON_BUTTONS_RU['subscription'])
@router.message(F.text == LEXICON_BUTTONS_EN['subscription'])
@router.callback_query(F.data == 'subscription')
@router.message(F.text == '/subscription')
async def process_subscription_command(message: Message, state: FSMContext, user: dict, i18n: TranslatorRunner):
    await process_subscription(message, state, user, i18n)



@router.message(F.text == LEXICON_BUTTONS_RU['new_session'])
@router.message(F.text == LEXICON_BUTTONS_EN['new_session'])
@router.callback_query(F.data == 'new_audio')
@router.message(F.text == '/new_audio')
async def process_new_audio_command(message: Message, state: FSMContext, i18n: TranslatorRunner):
    await process_new_audio_start(message, state, i18n=i18n)


@router.message(F.text == '/bip')
async def process_bip_command(message: Message):
    await message.reply(text='bop')