import traceback
from datetime import datetime, timedelta
import io
import logging
import asyncio
import time
import uuid

from aiogram import Router, F, types
from aiogram.filters import StateFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, BufferedInputFile, LinkPreviewOptions, InlineKeyboardMarkup, \
    InlineKeyboardButton, FSInputFile, InputMediaAnimation
from fluentogram import TranslatorRunner
import pytz
from models.orm import get_transcript_by_chat_session
from handlers.balance_hanlders import process_subscription
from keyboards.user_keyboards import continue_without_language, inline_change_model_menu, \
    inline_change_specify_language_menu, inline_main_menu, inline_new_session, inline_cancel, \
    bill_keyboard, inline_user_settings, subscription_menu, subscription_forward, main_menu_keyboard, \
    inline_download_file, transcription_no_summary_keyboard, notetaker_menu_keyboard
from models.orm import (change_user_setting, get_transcription_data, get_user, create_new_user, add_gpt_use, add_voice_use,
                        renew_subscription_db, update_user_blocked_status, update_audio_log,
                        create_processing_session, update_processing_session, create_audio_log_with_session,
                        increment_download_attempts, log_anonymous_chat_message, count_user_chat_requests_by_session,
                        get_processing_session_by_id, find_cached_transcription, find_cached_summary,
                        find_cached_transcription_by_file_path, log_user_action_async)
from services.cache_normalization import generate_prompt_hash, generate_file_hash_async
from services.content_downloaders.file_handling import download_file, identify_url_source
from services.fedor_api import convert_file_fedor_api, download_file_fedor_api, process_audio_fedor_api
from services.general_functions import process_chat_request, process_audio, summarise_text, generate_title
from services.dynamic_progress_manager import DynamicProgressManager, create_progress_manager, ProgressPhase
from services.google_docs_service_lite import create_two_google_docs_lite
from services.init_bot import bot, config
from services.openai_functions import prepare_language_code
from services.services import create_input_file_from_text, extract_audio_from_video, convert_to_mp3, delete_file, get_file_size, \
    progress_bar, replace_markdown_bold_with_html, sanitize_html_for_telegram, split_title_and_summary, get_audio_duration
from services.youtube_funcs import get_content_from_url, is_valid_video_url, get_audio_from_url
from states.states import UserAudioSession
from services.video_title_extractor import get_video_title
from services.audio_queue_service import audio_queue_manager
from services.static_files_cache import send_intro_video, edit_message_with_intro_video

logger = logging.getLogger(__name__)
router = Router()


def extract_domain_from_url(url: str) -> str:
    """Извлекает домен из URL для безопасного логирования."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or 'unknown'
    except Exception:
        return 'unknown'


@router.my_chat_member()
async def process_user_blocked_bot(event: types.ChatMemberUpdated, state: FSMContext, user: dict):
    # Получаем данные о пользователе
    user_id = event.from_user.id

    # Проверяем новый статус бота в чате
    if event.new_chat_member.status == "kicked":
        # Пользователь заблокировал бота
        logging.info(f"User {user_id} blocked the bot")
        await update_user_blocked_status(user_id, True)

        # Логируем блокировку бота
        await log_user_action_async(
            user_id=user['id'],
            action_type='bot_blocked',
            action_category='bot_interaction',
            metadata={
                'telegram_id': user_id,
                'old_status': event.old_chat_member.status,
                'new_status': event.new_chat_member.status,
                'subscription_status': user['subscription']
            }
        )
    elif event.new_chat_member.status == "member":
        # Пользователь разблокировал бота
        logging.info(f"User {user_id} unblocked the bot")
        await update_user_blocked_status(user_id, False)

        # Логируем разблокировку бота
        await log_user_action_async(
            user_id=user['id'],
            action_type='bot_unblocked',
            action_category='bot_interaction',
            metadata={
                'telegram_id': user_id,
                'old_status': event.old_chat_member.status,
                'new_status': event.new_chat_member.status,
                'subscription_status': user['subscription']
            }
        )
    else:
        logger.error(f'Unknown user status: {event.new_chat_member.status}')

    return

# @router.callback_query(F.data == 'main_menu')
# async def process_main_menu_callback(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
#     text = i18n.new_user_start()
#     if callback.message.text:
#         await callback.message.delete()
#         await send_intro_video(
#             callback.message,
#             lang=user["user_language"],
#             caption=text,
#             reply_markup=inline_main_menu(i18n=i18n),
#             link_preview_options=LinkPreviewOptions(is_disabled=True)
#         )
#     else:
#         await edit_message_with_intro_video(
#             callback.message,
#             lang=user["user_language"],
#             caption=text,
#             reply_markup=inline_main_menu(i18n=i18n)
#         )

@router.callback_query(F.data == 'subscription_menu')
async def process_subscription_menu(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    await callback.answer()
    await process_subscription(event=callback.message, state=state, user=user, i18n=i18n)



async def process_new_audio_start(message: Message | CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if type(message) is CallbackQuery:
        await message.answer()
        is_callback = True
        user: dict = await get_user(telegram_id=message.from_user.id)
        message = message.message
    else:
        is_callback = False
        user: dict = await get_user(telegram_id=message.from_user.id)

    if user['subscription'] != 'True' and user['subscription'] != 'trial' and user['audio_uses'] >= 50:
        # Логируем событие исчерпания лимита
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_limit_exceeded',
            action_category='conversion',
            metadata={
                'audio_uses': user['audio_uses'],
                'limit': 50,
                'source': 'new_audio_start',
                'shown_subscription_menu': True
            }
        )
        if is_callback:
            await message.edit_reply_markup(reply_markup=None)
            await message.answer(text=i18n.free_audio_limit_exceeded(),
                                    reply_markup=subscription_menu(i18n))
        else:
            await message.answer(text=i18n.free_audio_limit_exceeded(),
                                 reply_markup=subscription_menu(i18n))
        return

    if is_callback:
        if message.video:
            # await message.edit_reply_markup(reply_markup=None)
            await message.answer(text=i18n.send_new_audio())
        else:
            await message.edit_reply_markup(reply_markup=None)
            await message.answer(text=i18n.send_new_audio())
    else:
        await message.answer(text=i18n.send_new_audio())
    await state.clear()
    await state.set_state(UserAudioSession.waiting_user_audio)


async def _process_audio_internal(message: Message, state: FSMContext, i18n: TranslatorRunner, language_code: str | None = None, queue_message: Message | None = None, media_data: dict | None = None):
    """Внутренняя функция обработки аудио без проверки очереди"""
    user: dict = await get_user(telegram_id=message.from_user.id)

    # Извлекаем медиа-данные из сообщения
    if media_data is None:
        media_data = await extract_media_from_message(message, state, i18n, user)
    if media_data is None:
        return

    # Распаковываем полученные данные
    audio = media_data['audio']
    is_link = media_data['is_link']
    is_document = media_data['is_document']
    video_url = media_data['video_url']
    file_name = media_data['file_name']
    url = media_data['url']
    audio_file_source_type = media_data['audio_file_source_type']

    data = await state.get_data()

    if data.get('ask_language_message', None):
        await data['ask_language_message'].delete()
        await state.update_data(ask_language_message=None)
    if queue_message:
        waiting_message = await queue_message.edit_text(text=i18n.wait_for_your_response(), reply_markup=None)
    else:
        waiting_message = await message.reply(text=i18n.wait_for_your_response())
    await state.set_state(UserAudioSession.user_wait)
    
    # === ТОЧКА 1: Создание ProcessingSession ===
    start_time = time.time()
    source_type = 'url' if is_link else 'telegram'
    original_identifier = url if is_link else audio.file_id

    if source_type == 'url':
        specific_source = identify_url_source(url)
    else:
        specific_source = None
    
    # Создаем новую сессию обработки
    session_id = await create_processing_session(
        user_id=user['id'],
        original_identifier=original_identifier,
        source_type=source_type,
        specific_source=specific_source,
        waiting_message_id=waiting_message.message_id,
        user_original_message_id=message.message_id
    )

    logger.info(f'Starting processing session {session_id} for user {user["id"]}')

    if not session_id:
        await waiting_message.edit_text(text=i18n.something_went_wrong())
        return

    await state.update_data(session_id=session_id)
    # Создаем запись аудио лога для сессии
    audio_log_id = await create_audio_log_with_session(
        session_id=session_id,
        user_id=user['id']
    )
    try:

        # === ПРОВЕРКА КЭША ТРАНСКРИПЦИИ ===
        cached_transcription: dict | None = await find_cached_transcription(
            source_type=source_type,
            original_identifier=original_identifier
        )

        raw_transcript = None
        timecoded_transcript = None
        voice_summary = None
        transcription_id = None
        generated_file_name = None
        audio_duration = None
        original_file_size = 0
        # Использовать ли приоритетную модель. В нашем случае это deepgram
        use_quality_model = False
        temp_files: list[str] = []
        # Условие "то же аудио повторно в течение часа от того же пользователя" -> используем deepgram
        if cached_transcription:
            try:
                prev_session = await get_processing_session_by_id(cached_transcription.get('created_by_session_id'))
            except Exception:
                prev_session = None
            same_user = bool(prev_session and prev_session.get('user_id') == user['id'])
            created_at = cached_transcription.get('created_at')
            if created_at is not None and getattr(created_at, 'tzinfo', None) is None:
                created_at = created_at.replace(tzinfo=pytz.UTC)
            now_utc = datetime.now(pytz.UTC)
            within_hour = bool(created_at and created_at > now_utc - timedelta(hours=1))
            if same_user and within_hour and cached_transcription['transcription_provider'] != 'deepgram':
                use_quality_model = True
                # Перезапустить транскрипцию без использования кэша, чтобы обновить запись deepgram'ом
                cached_transcription = None

        # Initialize dynamic progress manager
        progress_manager = await create_progress_manager(waiting_message, progress_bar, i18n, session_id=session_id)

        if cached_transcription:
            result: dict | None = await _process_cached_transcription(cached_transcription=cached_transcription, user=user, i18n=i18n, session_id=session_id, message=message, state=state, waiting_message=waiting_message,
                                                                      progress_manager=progress_manager, audio_file_source_type=audio_file_source_type)
        else:
            result: dict | None = await _process_uncached_transcription(user=user, i18n=i18n, session_id=session_id, message=message, state=state,
                file_name=file_name, url=url, audio=audio, is_document=is_document, transcript_id=transcription_id, progress_manager=progress_manager,
                waiting_message=waiting_message, language_code=language_code, original_identifier=original_identifier, is_link=is_link, use_quality_model=use_quality_model, audio_file_source_type=audio_file_source_type)

        if result:
            raw_transcript = result.get('raw_transcript', None)
            timecoded_transcript = result.get('timecoded_transcript', None)
            voice_summary = result.get('summary', None)
            generated_file_name = result.get('file_name', None)
            transcription_id = result.get('transcription_id', None)
            audio_duration = result.get('audio_duration', None)
        else:
            raise ValueError(f'Result is empty. Result of _process_cached_transcription or _process_uncached_transcription in _process_audio_internal is empty or None')

        try:
            # Start finalization phase
            await progress_manager.start_phase(ProgressPhase.FINALIZING, 95)
        except Exception as e:
            pass

        await state.update_data(transcript=raw_transcript)
        await state.update_data(context=[
            {'role': 'user', 'content': f"{i18n.audio_text_prefix()} {raw_transcript}"}])
        
        # Создаем анонимную сессию чата для маркетингового анализа
        anonymous_chat_session = str(uuid.uuid4())
        await state.update_data(anonymous_chat_session=anonymous_chat_session)
        
        # Логируем первое сообщение (аудио текст) в анонимный чат
        try:
            await log_anonymous_chat_message(
                chat_session=anonymous_chat_session,
                message_from='user',
                text=raw_transcript,
                message_order=1
            )
        except Exception as e:
            logger.warning(f"Failed to log anonymous chat message: {e}")

        # Отправляем транскрипцию
        if not voice_summary and raw_transcript:
            await send_transcription(message=message, i18n=i18n,
                                    transcription_raw=raw_transcript,
                                        transcription_timecoded=timecoded_transcript,
                                        file_name=file_name if file_name else generated_file_name,
                                        transcription_format=user['transcription_format'],
                                        chat_session=anonymous_chat_session,
                                        session_id=session_id,
                                     no_summary=True,
                                     audio_file_source_type=audio_file_source_type,
                                     is_link=is_link)
        else:
            await send_transcription(message=message, i18n=i18n,
                                     transcription_raw=raw_transcript,
                                     transcription_timecoded=timecoded_transcript,
                                     file_name=file_name if file_name else generated_file_name,
                                     transcription_format=user['transcription_format'],
                                     chat_session=anonymous_chat_session,
                                     session_id=session_id,
                                     no_summary=False,
                                     audio_file_source_type=audio_file_source_type,
                                     is_link=is_link)
            # Отправляем резюме
            await send_summary(message=message, state=state, i18n=i18n, summary=voice_summary, is_link=is_link,
                               chat_session=anonymous_chat_session, session_id=session_id)
        await add_voice_use(message.from_user.id)
        
        # === ТОЧКА 2A: Успешное завершение всей сессии ===
        try:
            # Обновляем аудио лог
            if audio_log_id:
                await update_audio_log(
                    audio_log_id=audio_log_id,
                    length=audio_duration,
                    file_size_bytes=original_file_size,
                    processing_duration=time.time() - start_time,
                    success=True
                )
        except Exception as log_error:
            logger.warning(f"Failed to update session logs {session_id}: {log_error}")



        # Обновляем сессию с успешным завершением
        await update_processing_session(
            session_id=session_id,
            completed_at=datetime.utcnow(),
            total_duration=time.time() - start_time,
            final_status='success',
        )
        logger.info(f"Processing session {session_id} for user {message.from_user.id} completed successfully")
        await state.set_state(UserAudioSession.dialogue)

        # Stop progress manager before deleting message
        if 'progress_manager' in locals():
            await progress_manager.stop()

        await waiting_message.delete()

        # Уведомляем менеджер очередей о завершении обработки
        await audio_queue_manager.finish_processing(message.from_user.id)
    except Exception as e:
        logger.error(i18n.process_audio_error(error=str(e)))
        # Stop progress manager in case of error
        if 'progress_manager' in locals():
            await progress_manager.stop()
        # Уведомляем менеджер очередей о завершении обработки (даже при ошибке)
        await audio_queue_manager.finish_processing(message.from_user.id)
        # === ТОЧКА 2B: Обработка ошибок сессии ===
        try:
            # Определяем стадию ошибки
            error_stage = 'download'  # по умолчанию
            if 'audio_buffer' in locals():
                error_stage = 'audio_extraction'
            if 'raw_transcript' in locals():
                error_stage = 'transcription'
            if 'voice_summary' in locals():
                error_stage = 'summary'
            
            # Обновляем аудио лог с ошибкой
            if audio_log_id:
                await update_audio_log(
                    audio_log_id=audio_log_id,
                    processing_duration=time.time() - start_time,
                    success=False,
                    error_message=str(e)
                )
            
            # Обновляем сессию с ошибкой
            await update_processing_session(
                session_id=session_id,
                completed_at=datetime.utcnow(),
                total_duration=time.time() - start_time,
                final_status='failed',
                error_stage=error_stage,
                error_message=str(e)
            )
            
        except Exception as log_error:
            logger.warning(f"Failed to update session logs {session_id} with error: {log_error}")
        
        try:
            await waiting_message.edit_text(text=i18n.something_went_wrong())
        except Exception as edit_error:
            # Игнорируем ошибку "message is not modified" и другие ошибки редактирования
            if "message is not modified" not in str(edit_error):
                logger.warning(f"Failed to edit waiting message: {edit_error}")


async def _extract_audio_message(
    message: Message | None,
    state: FSMContext,
    audio_key: str | None = None,
    user_id: int | None = None
) -> Message:
    """
    Универсальная функция для извлечения аудио-сообщения из разных источников.

    Args:
        message: Прямое сообщение (если есть)
        state: FSMContext
        audio_key: Ключ для извлечения из state (если есть)
        user_id: ID пользователя для поиска последнего ключа

    Returns:
        Message: Извлеченное аудио-сообщение
    """
    if message:
        return message

    if not audio_key and user_id:
        # Находим последний ключ аудио для этого пользователя
        data = await state.get_data()
        audio_keys: list = [key for key in data.keys() if key.startswith(f'audio_msg_{user_id}_')]

        if not audio_keys:
            logger.error(f"No audio keys found for user {user_id}, state keys: {list(data.keys())}")
            raise ValueError("No audio message found")

        # Берем самый последний ключ (с наибольшим timestamp)
        audio_key = max(audio_keys, key=lambda x: int(x.split('_')[-1]) if x.split('_')[-1].isdigit() else 0)

    if audio_key:
        data = await state.get_data()
        audio_message: Message = data.get(audio_key)

        if not audio_message:
            logger.error(f"No audio_message found for key {audio_key}")
            raise ValueError("No audio message found")

        return audio_message

    raise ValueError("No valid source for audio message")


async def _prepare_and_process_audio(
    message: Message | None,
    state: FSMContext,
    i18n: TranslatorRunner,
    language_code: str | None,
    audio_key: str | None = None,
    user_id: int | None = None,
    delete_message: Message | None = None
):
    """
    Универсальная функция для подготовки и обработки аудио.

    Args:
        message: Аудио-сообщение (если есть)
        state: FSMContext
        i18n: TranslatorRunner
        language_code: Код языка или None
        audio_key: Ключ для извлечения из state (если нужен)
        user_id: ID пользователя для поиска ключа
        delete_message: Сообщение для удаления (если callback)
    """
    # 1. Извлечение аудио-сообщения
    audio_message = await _extract_audio_message(message, state, audio_key, user_id)

    # 2. Очистка state данных
    cleanup_data = {'language_code': None}
    if audio_key:
        cleanup_data[audio_key] = None
    if delete_message:
        cleanup_data['ask_language_message'] = None
    await state.update_data(cleanup_data)

    # 3. Переключение состояния обратно
    await state.set_state(UserAudioSession.waiting_user_audio)

    try:
        media_data = await extract_media_from_message(audio_message, state, i18n)
    except ValueError:
        return

    if media_data is None:
        return

    # 4. Добавление в очередь или обработка
    is_queued, queue_message = await audio_queue_manager.add_to_queue(
        user_id=audio_message.from_user.id,
        message=audio_message,
        state=state,
        i18n=i18n,
        language_code=language_code
    )

    # 5. Удаление сообщения (если нужно)
    if delete_message:
        await delete_message.delete()

    # 6. Обработка если не в очереди
    if not is_queued:
        await _process_audio_internal(
            message=audio_message,
            state=state,
            i18n=i18n,
            language_code=language_code,
            queue_message=queue_message,
            media_data=media_data
        )


@router.message(StateFilter(UserAudioSession.waiting_user_audio))
async def process_new_audio(message: Message, state: FSMContext, i18n: TranslatorRunner):
    """Главная функция обработки аудио с поддержкой очереди"""
    user: dict = await get_user(telegram_id=message.from_user.id)
    if user:
        username = user.get('username', '') if user.get('username', '') else ''
        if 'fastsaver' in username:
            logger.warning(f'Запрос от fastsaver: telegram_id - {user["telegram_id"]}, username - {user["username"]}')
            return

    # Проверяем лимиты подписки
    if user['subscription'] != 'True' and user['audio_uses'] >= 50 and user['subscription'] != 'trial':
        # Логируем событие исчерпания лимита
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_limit_exceeded',
            action_category='conversion',
            metadata={
                'audio_uses': user['audio_uses'],
                'limit': 50,
                'source': 'audio_upload',
                'shown_subscription_menu': True
            }
        )
        await message.answer(text=i18n.free_audio_limit_exceeded(),
                             reply_markup=subscription_menu(i18n))
        return

    # Проверяем нужно ли уточнить язык ПЕРЕД добавлением в очередь
    data = await state.get_data()
    logger.debug(f"Processing audio from user {message.from_user.id}, current state data keys: {list(data.keys())}")

    if user['specify_audio_language'] and data.get('language_code', None) is None and data.get('language_code', None) != 'skip':
        # Создаем уникальный ключ для этого аудио
        audio_key = f"audio_msg_{message.from_user.id}_{message.message_id}_{int(time.time())}"

        # Сохраняем аудио с уникальным ключом
        await state.update_data({audio_key: message})

        ask_language_message = await message.reply(text=i18n.enter_language(),
                                                   reply_markup=continue_without_language(i18n=i18n,
                                                                                          audio_key=audio_key))
        await state.update_data({'ask_language_message': ask_language_message})
        await state.set_state(UserAudioSession.enter_language)
        return

    # Извлекаем language_code
    language_code = data.get('language_code')
    await state.update_data(language_code=None)

    # Универсальная обработка
    await _prepare_and_process_audio(
        message=message,
        state=state,
        i18n=i18n,
        language_code=language_code
    )


@router.message(StateFilter(UserAudioSession.enter_language))
async def process_enter_language(message: Message, state: FSMContext, i18n: TranslatorRunner):
    # Парсинг языка
    try:
        language_code: str | None = await prepare_language_code(message.text)
    except Exception as e:
        logger.error(i18n.language_error(error=str(traceback.format_exc())))
        language_code = None

    if language_code:
        await message.reply(text=i18n.language_specified(language=language_code))

    # Универсальная обработка (функция сама найдет последний аудио-ключ)
    try:
        await _prepare_and_process_audio(
            message=None,
            state=state,
            i18n=i18n,
            language_code=language_code,
            user_id=message.from_user.id
        )
    except ValueError as e:
        logger.error(f"Failed to process audio: {e}")
        await message.answer(text=i18n.something_went_wrong())
        await state.clear()


@router.callback_query(F.data.startswith('cont_lang_'))
async def process_continue_without_language(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    await callback.answer()
    language_code = 'skip'

    # Извлекаем audio_key из callback_data
    audio_key = callback.data.replace('cont_lang_', '')

    # Универсальная обработка
    try:
        await _prepare_and_process_audio(
            message=None,
            state=state,
            i18n=i18n,
            language_code=language_code,
            audio_key=audio_key,
            delete_message=callback.message
        )
    except ValueError as e:
        logger.error(f"Failed to process audio: {e}")
        await callback.message.answer(text=i18n.something_went_wrong())
        await state.clear()

@router.message(StateFilter(UserAudioSession.dialogue), ~F.text)
async def process_not_text_dialog(message: Message, state: FSMContext, i18n: TranslatorRunner):
    await process_new_audio(message=message, state=state, i18n=i18n)

@router.message(StateFilter(UserAudioSession.dialogue), F.text)
async def process_dialogue(message: Message, state: FSMContext, user: dict, i18n: TranslatorRunner):
    if is_valid_video_url(message.text):
        await process_new_audio(message=message, state=state, i18n=i18n)
        return

    data = await state.get_data()
    context = data['context']
    anonymous_chat_session = data.get('anonymous_chat_session')
    session_id = data.get('session_id')

    # Проверяем лимит на 20 сообщений чата для этой сессии
    if session_id:
        chat_requests_count = await count_user_chat_requests_by_session(user['id'], session_id)
        if chat_requests_count >= 20:
            await message.answer(
                text=i18n.warning_message_limit()
            )
            await state.set_state(None)
            return

    # Определяем номер сообщения (текущая длина контекста + 1)
    message_order = len(context) + 1
    
    # Логируем вопрос пользователя в анонимный чат
    if anonymous_chat_session:
        try:
            await log_anonymous_chat_message(
                chat_session=anonymous_chat_session,
                message_from='user',
                text=message.text,
                message_order=message_order
            )
        except Exception as e:
            logger.warning(f"Failed to log anonymous user message: {e}")
    
    context.append({'role': 'user', 'content': message.text})
    waiting_message = await message.reply(text=i18n.wait_for_chat_response(dots="."))
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    await state.set_state(UserAudioSession.user_wait)

    # Запускаем анимацию точек параллельно с запросом к LLM
    animation_running = True

    async def animate_dots():
        dots_count = 1
        max_dots = 3
        while animation_running:
            await asyncio.sleep(5)
            if not animation_running:
                break
            dots_count = (dots_count % max_dots) + 1
            dots = "." * dots_count
            try:
                await waiting_message.edit_text(text=i18n.wait_for_chat_response(dots=dots))
                await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            except Exception:
                pass  # Игнорируем ошибки редактирования (message not modified и т.д.)

    animation_task = asyncio.create_task(animate_dots())

    try:
        model_answer: str = await process_chat_request(context=context, user=user, i18n=i18n, session_id=data['session_id'])
    finally:
        animation_running = False
        animation_task.cancel()
        try:
            await animation_task
        except asyncio.CancelledError:
            pass
    if model_answer is False or model_answer == '':
        await message.answer(text=i18n.something_went_wrong(),
                             reply_markup=inline_new_session(i18n, download_button=False))
        return
    else:    
        try:
            context = await send_dialogue_result(message=waiting_message, i18n=i18n, model_answer=model_answer, context=context)
        except Exception as e:
            logger.error(f"Error sending dialogue result with HTML: {e}")
            # Пытаемся отправить как обычный текст без форматирования
            try:
                await waiting_message.delete()
                # Удаляем все HTML теги полностью
                import re
                plain_text = re.sub(r'<[^>]+>', '', model_answer)
                if len(plain_text) > 4096:
                    # Разбиваем на части
                    parts = [plain_text[i:i+4000] for i in range(0, len(plain_text), 4000)]
                    for part in parts[:-1]:
                        await message.answer(text=part)
                    await message.answer(text=parts[-1],
                                       reply_markup=inline_new_session(i18n, download_button=False))
                else:
                    await message.answer(text=plain_text,
                                       reply_markup=inline_new_session(i18n, download_button=False))
                # Обновляем контекст
                if context:
                    context.append({'role': 'assistant', 'content': model_answer})
            except Exception as e2:
                logger.error(f"Error sending dialogue result as plain text: {e2}")
                await message.answer(text=i18n.something_went_wrong(),
                                   reply_markup=inline_new_session(i18n, download_button=False))
                return
        
        # Логируем ответ ассистента в анонимный чат
        if anonymous_chat_session and model_answer:
            try:
                await log_anonymous_chat_message(
                    chat_session=anonymous_chat_session,
                    message_from='assistant',
                    text=model_answer,  # Используем оригинальный ответ без HTML форматирования
                    message_order=message_order + 1
                )
            except Exception as e:
                logger.warning(f"Failed to log anonymous assistant message: {e}")
    
    await add_gpt_use(message.from_user.id)
    await state.set_state(UserAudioSession.dialogue)
    await state.update_data(context=context)


@router.callback_query(F.data.startswith('ask_questions'))
async def process_you_can_ask(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if '|' in callback.data: #В этом случае у нас записан session_id, а не chat_session. Происходит это в тех случаях, когда мы не создавали саммари при выдачи транскрипции
        callback_parts = callback.data.split('|')
        session_id = callback_parts[1]

        transcript_data: dict = await get_transcription_data(session_id=session_id)
        
        chat_session = str(uuid.uuid4())
        await state.update_data(anonymous_chat_session=chat_session)
        
        # Логируем первое сообщение (аудио текст) в анонимный чат
        try:
            await log_anonymous_chat_message(
                chat_session=chat_session,
                message_from='user',
                text=transcript_data['raw_transcript'],
                message_order=1
            )
        except Exception as e:
            logger.warning(f"Failed to log anonymous chat message: {e}")
    elif ':' in callback.data:
        callback_parts = callback.data.split(':')
        chat_session = callback_parts[1]
    else:
        chat_session = None

    await callback.answer()
    # # Удаляем кнопку "ask questions" из клавиатуры
    # reply_markup = callback.message.reply_markup
    # reply_markup.inline_keyboard.pop(0)
    # await callback.message.edit_reply_markup(reply_markup=reply_markup)

    if chat_session:
        # Загружаем транскрипт из базы данных
        transcript = await get_transcript_by_chat_session(chat_session)

        if transcript:
            # Создаем новый контекст с транскриптом
            new_context = [{'role': 'user', 'content': f"{i18n.audio_text_prefix()} {transcript}"}]

            # Обновляем состояние с новым контекстом и chat_session
            await state.update_data(
                context=new_context,
                anonymous_chat_session=chat_session,
                session_id=None  # Сбрасываем session_id так как переключаемся на другую сессию
            )
            await state.set_state(UserAudioSession.dialogue)

            await callback.message.reply(text=i18n.you_can_ask_questions())
        else:
            await callback.message.answer(text=i18n.something_went_wrong())
    else:
        # Старое поведение для обратной совместимости
        await callback.message.answer(text=i18n.you_can_ask_questions())

@router.callback_query(F.data.startswith('get_video'))
async def process_get_video(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    # Extract session_id from callback data
    callback_parts = callback.data.split(':')
    if len(callback_parts) > 1:
        session_id = callback_parts[1]
        # Get session data from database
        session_data = await get_processing_session_by_id(session_id)
        if not session_data:
            await callback.message.answer(text=i18n.video_send_error_no_data())
            return

        # Verify user owns this session
        if session_data['user_id'] != user['id']:
            await callback.message.answer(text=i18n.video_send_error_no_data())
            return

        url = session_data['original_identifier']
        source_type = session_data['source_type']

        # Only allow download for URL sources
        if source_type != 'url':
            await callback.message.answer(text=i18n.video_send_error_no_data())
            return
    else:
        # Fallback to old behavior for backward compatibility
        data = await state.get_data()
        url: str | None = data.get('video_url', None)
        session_id = None
        source_type = 'url'
        logger.warning('No session id')

    # Remove download button
    reply_markup = callback.message.reply_markup
    if reply_markup and len(reply_markup.inline_keyboard) > 1:
        for row in reply_markup.inline_keyboard:
            for button in row:
                if button.callback_data.startswith("get_video:"):
                    reply_markup.inline_keyboard.remove(row)
        await callback.message.edit_reply_markup(reply_markup=reply_markup)

    if url:
        # Логируем начало скачивания видео
        start_time = time.time()
        await log_user_action_async(
            user_id=user['id'],
            action_type='video_download_started',
            action_category='content_processing',
            metadata={
                'trigger_source': 'get_video_callback',
                'url_domain': extract_domain_from_url(url),
                'source_type': source_type,
            },
            session_id=session_id if session_id else None
        )

        video_message = await callback.message.answer(text=i18n.downloading_video())
        retries = 0
        video_path: str | None = None
        download_method = 'fedor_api'
        try:
            video_data: dict = await download_file_fedor_api(url, user_data=user, session_id=session_id, result_content_type='video', destination_type='disk', add_file_size_to_session=True)
            video_path = video_data['file_path']
        except Exception as e:
            logger.error(f"Ошибка при загрузке видео {url} из Fedor API. Функция process_get_video: {e}")

        if not video_path:
            download_method = 'fallback'
            while retries < 5:
                try:
                    # video_data: bytes = await get_video_from_url(url, user_data=user)
                    video_path: str = await get_content_from_url(url, user_data=user, download_mode='video', destination_type='disk')
                    break
                except Exception as e:
                    logger.error(f"Ошибка при загрузке видео {url}. Попытка {retries + 1}: {e}")
                    retries += 1
                    continue

        if retries == 5:
            logger.error(f"Ошибка при загрузке полного видео: {url}")
            # Логируем неудачное скачивание
            await log_user_action_async(
                user_id=user['id'],
                action_type='video_download_failed',
                action_category='content_processing',
                metadata={
                    'trigger_source': 'get_video_callback',
                    'url_domain': extract_domain_from_url(url),
                    'download_method': download_method,
                    'retries': retries,
                    'error_message': 'Max retries exceeded',
                    'duration_ms': int((time.time() - start_time) * 1000),
                },
                session_id=session_id if session_id else None
            )
            try:
                await video_message.edit_text(text=i18n.video_download_error())
                return
            except Exception as edit_error:
                if "message is not modified" not in str(edit_error):
                    logger.warning(f"Failed to edit video message: {edit_error}")
            return

        try:
            # Get filename from session data or fallback to state
            if len(callback_parts) > 1:
                filename = "video"  # We could enhance this by storing filename in session if needed
            else:
                data = await state.get_data()
                filename = data.get("file_name", "video")

            video_to_send = FSInputFile(path=video_path, filename=f'{filename}.mp4')
            # Pass session_id to download button if available
            download_keyboard = inline_download_file(i18n=i18n, session_id=callback_parts[1] if len(callback_parts) > 1 else None)
            await callback.message.answer_video(video=video_to_send, caption=i18n.requested_video(), reply_markup=download_keyboard)
            
            # Логируем успешное скачивание видео
            await log_user_action_async(
                user_id=user['id'],
                action_type='video_download_completed',
                action_category='content_processing',
                metadata={
                    'trigger_source': 'get_video_callback',
                    'url_domain': extract_domain_from_url(url),
                    'download_method': download_method,
                    'retries': retries,
                    'duration_ms': int((time.time() - start_time) * 1000),
                },
                session_id=session_id if session_id else None
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке видео {url}: {e}")
            # Логируем ошибку отправки
            await log_user_action_async(
                user_id=user['id'],
                action_type='video_download_failed',
                action_category='content_processing',
                metadata={
                    'trigger_source': 'get_video_callback',
                    'url_domain': extract_domain_from_url(url),
                    'download_method': download_method,
                    'retries': retries,
                    'error_message': f'Send error: {str(e)[:150]}',
                    'duration_ms': int((time.time() - start_time) * 1000),
                },
                session_id=session_id if session_id else None
            )
            try:
                await video_message.edit_text(text=i18n.video_send_error())
            except Exception as edit_error:
                if "message is not modified" not in str(edit_error):
                    logger.warning(f"Failed to edit video message: {edit_error}")
        finally:
            await delete_file(video_path)

    else:

        await callback.message.answer(text=i18n.video_send_error_no_data())


@router.callback_query(F.data.startswith('download_file'))
async def process_download_video(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    # Extract session_id from callback data
    callback_parts = callback.data.split(':')
    if len(callback_parts) > 1:
        session_id = callback_parts[1]
        # Get session data from database
        session_data = await get_processing_session_by_id(session_id)
        if not session_data:
            await callback.message.answer(text=i18n.video_send_error_no_data())
            return

        # Verify user owns this session
        if session_data['user_id'] != user['id']:
            await callback.message.answer(text=i18n.video_send_error_no_data())
            return

        url = session_data['original_identifier']
        source_type = session_data['source_type']

        # Only allow download for URL sources
        if source_type != 'url':
            await callback.message.answer(text=i18n.video_send_error_no_data())
            return
    else:
        # Fallback to old behavior for backward compatibility
        data = await state.get_data()
        url: str | None = data.get('video_url', None)
        session_id = None
        source_type = 'url'
        logger.warning('No session id')

    await callback.message.edit_reply_markup(reply_markup=None)
    if url:
        # Логируем начало скачивания файла
        start_time = time.time()
        await log_user_action_async(
            user_id=user['id'],
            action_type='file_download_started',
            action_category='content_processing',
            metadata={
                'trigger_source': 'download_file_callback',
                'url_domain': extract_domain_from_url(url),
                'source_type': source_type,
            },
            session_id=session_id if session_id else None
        )

        video_message = await callback.message.answer(text=i18n.downloading_video())
        retries = 0
        video_path: str | None = None
        download_method = 'fedor_api'
        try:
            video_data: dict = await download_file_fedor_api(url, user_data=user, session_id=session_id, result_content_type='video', destination_type='disk', add_file_size_to_session=True)
            video_path = video_data['file_path']
        except Exception as e:
            logger.error(f"Ошибка при загрузке видео {url} из Fedor API. Функция process_download_video: {e}")
        
        if not video_path:
            download_method = 'fallback'
            while retries < 5:
                try:
                    # video_data: bytes = await get_video_from_url(url, user_data=user)
                    video_path: str = await get_content_from_url(url, user_data=user, download_mode='video', destination_type='disk')
                    break
                except Exception as e:
                    logger.error(f"Ошибка при загрузке видео {url}. Попытка {retries + 1}: {e}")
                    retries += 1
                    continue

        if retries == 5:
            logger.error(f"Ошибка при загрузке полного видео: {url}")
            # Логируем неудачное скачивание файла
            await log_user_action_async(
                user_id=user['id'],
                action_type='file_download_failed',
                action_category='content_processing',
                metadata={
                    'trigger_source': 'download_file_callback',
                    'url_domain': extract_domain_from_url(url),
                    'download_method': download_method,
                    'retries': retries,
                    'error_message': 'Max retries exceeded',
                    'duration_ms': int((time.time() - start_time) * 1000),
                },
                session_id=session_id if session_id else None
            )
            try:
                await video_message.edit_text(text=i18n.video_download_error())
            except Exception as edit_error:
                if "message is not modified" not in str(edit_error):
                    logger.warning(f"Failed to edit video message: {edit_error}")
            return

        try:
            # Get filename from session data or fallback to state
            if len(callback_parts) > 1:
                filename = "video"  # We could enhance this by storing filename in session if needed
            else:
                data = await state.get_data()
                filename = data.get("file_name", "video")

            video_to_send = FSInputFile(path=video_path, filename=f'{filename}.mp4')
            await callback.message.answer_document(document=video_to_send, caption=i18n.requested_video())
            
            # Логируем успешное скачивание файла
            await log_user_action_async(
                user_id=user['id'],
                action_type='file_download_completed',
                action_category='content_processing',
                metadata={
                    'trigger_source': 'download_file_callback',
                    'url_domain': extract_domain_from_url(url),
                    'download_method': download_method,
                    'retries': retries,
                    'duration_ms': int((time.time() - start_time) * 1000),
                },
                session_id=session_id if session_id else None
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке видео {url}: {e}")
            # Логируем ошибку отправки файла
            await log_user_action_async(
                user_id=user['id'],
                action_type='file_download_failed',
                action_category='content_processing',
                metadata={
                    'trigger_source': 'download_file_callback',
                    'url_domain': extract_domain_from_url(url),
                    'download_method': download_method,
                    'retries': retries,
                    'error_message': f'Send error: {str(e)[:150]}',
                    'duration_ms': int((time.time() - start_time) * 1000),
                },
                session_id=session_id if session_id else None
            )
            try:
                await video_message.edit_text(text=i18n.video_send_error())
            except Exception as edit_error:
                if "message is not modified" not in str(edit_error):
                    logger.warning(f"Failed to edit video message: {edit_error}")
        finally:
            await delete_file(video_path)
    else:
        await callback.message.answer(text=i18n.video_send_error_no_data())


@router.callback_query(F.data.startswith('get_full_transcription|'))
async def process_get_full_transcription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    callback_parts = callback.data.split('|')
    session_id = callback_parts[1]
    transcription_data: dict | None = await get_transcription_data(session_id)

    if not transcription_data:
        await callback.message.answer(text=i18n.transcription_not_found())
        return

    # Генерируем название вместо использования первых 25 символов
    try:
        file_name = await generate_title(text=transcription_data['raw_transcript'], user=user, i18n=i18n)
    except Exception as e:
        logger.error(f"Failed to generate title for transcription: {e}")
        file_name = transcription_data['raw_transcript'][:25]

    inline_keyboard = callback.message.reply_markup
    if inline_keyboard:
        for row in inline_keyboard.inline_keyboard:
            for button in row:
                if button.callback_data.startswith('get_full_transcription|'):
                    inline_keyboard.inline_keyboard.remove(row)
                    await callback.message.edit_reply_markup(reply_markup=inline_keyboard)
                    break

    await send_transcription(message=callback.message, i18n=i18n, transcription_raw=transcription_data['raw_transcript'], 
        transcription_timecoded=transcription_data['timecoded_transcript'], file_name=file_name, 
        transcription_format=user['transcription_format'], session_id=session_id, no_summary=False)

    await log_user_action_async(
        user_id=user['id'],
        action_type='full_transcription_requested',
        action_category='feature',
        session_id=session_id if session_id else None,
    )

@router.callback_query(F.data.startswith('get_summary|'))
async def process_get_summary(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    callback_parts = callback.data.split('|')
    session_id = callback_parts[1]

    try:
        transcription_data: dict | None = await get_transcription_data(session_id)

        if not transcription_data:
            await callback.message.answer(text=i18n.transcription_not_found())
            return

        keyboard = callback.message.reply_markup
        # file_name = transcription_data['raw_transcript'][:25]
        old_inline = keyboard.inline_keyboard
        new_inline = old_inline.copy()
        if old_inline:
            for row in old_inline:
                for button in row:
                    if button.callback_data.startswith('get_summary|') or button.callback_data.startswith('ask_questions|'):
                        new_inline.remove(row)
            keyboard.inline_keyboard = new_inline
            await callback.message.edit_reply_markup(reply_markup=keyboard)

        waiting_message = await callback.message.reply(text=i18n.waiting_message_summary_in_process())

        summary_data: dict = await summarise_text(text=transcription_data['raw_transcript'], user=user, i18n=i18n, session_id=session_id,
                                                          transcription_id=transcription_data['transcription_id'])

        summary = summary_data['summary_text']
        await waiting_message.delete()
        await send_summary(message=callback.message, state=state, i18n=i18n, summary=summary, is_link=False, session_id=session_id)
    except:
        await callback.message.answer(text=i18n.something_went_wrong())

@router.message()
async def process_text_last(message: Message, state: FSMContext, i18n: TranslatorRunner):
    await process_new_audio(message, state, i18n=i18n)


########################################################################################################################


async def send_dialogue_result(message: Message, i18n: TranslatorRunner, model_answer: str, context: list[dict]| None = None):
    """
    Отправляет результат диалога пользователю.
    Если результат слишком длинный, разделяет его на части и отправляет по частям.
    """ 
    await message.delete()
    if context:
        context.append({'role': 'assistant', 'content': model_answer})
        
    # Преобразуем markdown в HTML и очищаем неподдерживаемые теги
    model_answer_formatted = await replace_markdown_bold_with_html(model_answer)
    model_answer_formatted = await sanitize_html_for_telegram(model_answer_formatted)
    final_response = model_answer_formatted
    
    if len(final_response) > 4096:
        parts = await split_summary(final_response)
        for part in parts[:-1]:
            await message.answer(text=part, parse_mode='HTML')
        
        await message.answer(text=parts[-1],
                             reply_markup=inline_new_session(i18n, download_button=False),
                             parse_mode='HTML')
    else:
        await message.answer(text=final_response,
                             reply_markup=inline_new_session(i18n, download_button=False),
                             parse_mode='HTML')
    
    return context


async def send_summary(message: Message, state: FSMContext, i18n: TranslatorRunner, summary: str, is_link: bool, chat_session: str = None, session_id: str = None):
    """
    Отправляет резюме пользователю.
    Если резюме пустое, отправляет сообщение о том, что резюме нет.
    Если резюме слишком длинное, разделяет его на части и отправляет по частям.
    """
    if summary == '':
        await message.reply(text=i18n.no_voice_summary(),
                            reply_markup=inline_new_session(i18n, is_summary=True, download_button=is_link, chat_session=chat_session, session_id=session_id))
        return

    # Преобразуем markdown в HTML и очищаем неподдерживаемые теги
    full_message_formatted = await replace_markdown_bold_with_html(summary)
    full_message_formatted = await sanitize_html_for_telegram(full_message_formatted)

    made_by_text = f'<a href="{config.tg_bot.bot_url}?start=link">Whisper AI</a>'
    full_message = i18n.voice_summary(summary=full_message_formatted, bot_link=made_by_text)

    if len(full_message) > 4096:
        parts = await split_summary(summary)

        # Обрабатываем каждую часть
        first_part_formatted = await replace_markdown_bold_with_html(parts[0])
        first_part_formatted = await sanitize_html_for_telegram(first_part_formatted)
        await message.answer(text=i18n.voice_summary_first_part(summary=first_part_formatted, bot_link=made_by_text),
                            link_preview_options=LinkPreviewOptions(is_disabled=True))
        
        for part in parts[1:-1]:
            part_formatted = await replace_markdown_bold_with_html(part)
            part_formatted = await sanitize_html_for_telegram(part_formatted)
            await message.answer(text=i18n.voice_summary_next_part(summary=part_formatted))
        
        last_part_formatted = await replace_markdown_bold_with_html(parts[-1])
        last_part_formatted = await sanitize_html_for_telegram(last_part_formatted)
        await message.answer(text=i18n.voice_summary_last_part(summary=last_part_formatted),
                            reply_markup=inline_new_session(i18n, is_summary=True, download_button=is_link,
                                                            chat_session=chat_session, session_id=session_id),
                            link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:
        await message.answer(text=full_message,
                            reply_markup=inline_new_session(i18n, is_summary=True, download_button=is_link,
                                                            chat_session=chat_session, session_id=session_id),
                            link_preview_options=LinkPreviewOptions(is_disabled=True))


async def split_summary(text: str, max_length: int = 3500) -> list[str]:
    """
    Разделяет текст на части размером до max_length. Предпочтительно по предложениям. Если предложение не помещается, то оно делится на части.
    Принимает текст и максимальную длину части.
    Возвращает список частей, строками.
    """
    sentences: list[str] = await split_by_sentence(text, max_length)
    result: list[str] = []
    for sentence in sentences:
        if len(sentence) == 0:
            continue
        if len(sentence) > max_length:
            part_one = sentence[:max_length]
            part_two = sentence[max_length:]
            result.append(part_one)
            result.append(part_two)
        else:
            result.append(sentence)

    return result


async def split_by_sentence(text: str, max_length: int = 3500) -> list[str]:
    """
    Разделяет текст на части размером до max_length по предложениям.
    Принимает текст и максимальную длину части.
    Возвращает список частей, строками.
    """
    sentences: list[str] = text.split('.')
    result: list[str] = []
    current_part: str = ''
    for sentence in sentences:
        if len(current_part) + len(sentence) < max_length:
            current_part += sentence + '.'
        else:
            result.append(await replace_markdown_bold_with_html(current_part))
            current_part = sentence + '.'
    result.append(current_part)
    return result

async def send_transcription(message: Message,
                             i18n: TranslatorRunner, 
                             transcription_raw: str, 
                             transcription_timecoded: str,
                             file_name: str,
                             transcription_format: str = 'google_docs',
                             chat_session: str = None,
                             no_summary: bool = False,
                             session_id: str = None,
                             audio_file_source_type: str = None,
                             is_link: bool = False):
    """
    Send transcription to user in the specified format.
    
    Args:
        message: Telegram message object
        state: FSM context
        i18n: Translator instance
        transcription_raw: Clean transcript without timestamps
        transcription_timecoded: Full transcript with timestamps
        file_name: Name for the document
        transcription_format: Format to send ('google_docs' or 'file')
        chat_session: Chat session ID
        no_summary: True if we don't plan to send summary. Adds reply_markup to the message and change output format.
        is_link: True if the source was a URL link
    """
    if transcription_raw == '':
        await message.reply(text=i18n.no_voice_summary(),
                            reply_markup=inline_new_session(i18n, is_summary=True, download_button=False,
                                                            chat_session=chat_session))
        return

    if no_summary and len(transcription_raw) < 2000\
            and audio_file_source_type in ['voice', 'video_note']:

        made_with_hyperlink = f'<a href="{config.tg_bot.bot_url}?=link">{i18n.transcription_file_made_with_prefix()} {config.tg_bot.bot_name}</a>'

        await message.reply(text=i18n.short_transcription_template(transcription_text=transcription_raw,
                                                                   made_with_hyperlink=made_with_hyperlink),
                            reply_markup=transcription_no_summary_keyboard(i18n, session_id, show_video_button=is_link),
                            link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    

    if transcription_format == 'google_docs':
        # Пробуем создать Google Docs и выходим из функции, если не получилось, то отправляем файл
        try:
            # from services.google_docs_utils import create_transcript_google_doc
            
            clean_doc_url, full_doc_url = await create_two_google_docs_lite(
                title=file_name,
                clean_transcript=transcription_raw,
                full_transcript=transcription_timecoded,
                i18n=i18n
            )
            if clean_doc_url and full_doc_url:
                # Send Google Docs links

                google_docs_hyperlink_1 = f'<a href="{clean_doc_url}">— Google Docs</a>'
                google_docs_hyperlink_2 = f'<a href="{full_doc_url}">— Google Docs</a>'

                
                clean_link_text = i18n.google_docs_first_transcription(google_docs_hyperlink=google_docs_hyperlink_1)
                full_link_text = i18n.google_docs_second_transcription(google_docs_hyperlink=google_docs_hyperlink_2)

                
                await message.reply(text=clean_link_text)
                if no_summary:
                    await message.answer(text=full_link_text, reply_markup=transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link))
                else:
                    await message.answer(text=full_link_text)
                return
            else:
                logger.warning("Google Docs creation failed, falling back to file")
                
        except Exception as e:
            logger.error(f"Error creating Google Doc: {e}")
    elif transcription_format in ['pdf', 'docx', 'md', 'txt']:
        from services.services import create_two_input_files_from_text
        
        clean_file, full_file = await create_two_input_files_from_text(
            full_transcript=transcription_timecoded,
            clean_transcript=transcription_raw,
            filename=file_name,
            i18n=i18n,
            format_type=transcription_format
        )
        await message.reply_document(document=clean_file, caption=i18n.file_transcription_ready_1())
        if no_summary:
            await message.answer_document(document=full_file, caption=i18n.file_transcription_ready_2(), reply_markup=transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link))
        else:
            await message.answer_document(document=full_file, caption=i18n.file_transcription_ready_2())
        return
    
    # Fallback to file format
    from services.services import create_two_input_files_from_text
    try:
        try:
            clean_file, full_file = await create_two_input_files_from_text(
                full_transcript=transcription_timecoded,
                clean_transcript=transcription_raw,
                filename=file_name,
                i18n=i18n,
                format_type='docx'
            )
            await message.reply_document(document=clean_file, caption=i18n.file_transcription_ready_clean())
            if no_summary:
                await message.answer_document(document=full_file, caption=i18n.file_transcription_ready_full(), reply_markup=transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link))
            else:
                await message.answer_document(document=full_file, caption=i18n.file_transcription_ready_full())
        except Exception as e:
            from services.services import create_two_input_files_from_text

            clean_file, full_file = await create_two_input_files_from_text(
                full_transcript=transcription_timecoded,
                clean_transcript=transcription_raw,
                filename=file_name,
                i18n=i18n,
                format_type='txt'
            )
            await message.reply_document(document=clean_file, caption=i18n.file_transcription_ready_1())
            if no_summary:
                await message.answer_document(document=full_file, caption=i18n.file_transcription_ready_2(), reply_markup=transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link))
            else:
                await message.answer_document(document=full_file, caption=i18n.file_transcription_ready_2())
            return
    except Exception as e:

        logger.error(f"Failed to send transcript files: {e}")
        await message.reply(text=i18n.something_went_wrong())


@router.callback_query(F.data.startswith('cancel_queue|'))
async def process_cancel_queue(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    await callback.answer()
    message_id = int(callback.data.replace('cancel_queue|', ''))
    await audio_queue_manager.remove_from_queue(user_id=callback.from_user.id, message_id=message_id)
    await audio_queue_manager.update_queue_count_in_messages(user_id=callback.from_user.id, i18n=i18n)
    await callback.message.delete()
    await callback.answer(text=i18n.queue_cancelled())


async def extract_media_from_message(message: Message, state: FSMContext, i18n: TranslatorRunner, user: dict | None = None) -> dict | None:
    """
    Извлекает медиа-данные из сообщения Telegram.

    Args:
        message: Сообщение Telegram
        state: FSM контекст
        i18n: Интернационализация
        user: Словарь с данными пользователя (опционально, для логирования)

    Returns:
        dict: Словарь с извлеченными данными:
            - audio: объект аудио/видео
            - is_link: булево (True если ссылка)
            - is_document: булево (True если документ)
            - video_url: URL видео или None
            - file_name: имя файла или None
            - url: URL если ссылка или None
    """
    if type(message) is CallbackQuery:
        message = message.message
    
    # Получаем user если не передан (для логирования)
    if user is None:
        user = await get_user(telegram_id=message.from_user.id)

    result = {
        'audio': None,
        'is_link': False,
        'is_document': False,
        'video_url': None,
        'file_name': None,
        'url': None,
        'audio_file_source_type': None
    }

    #is_document используется для того, чтобы брать название файла, если что. Работает, соответственно, в комбинации с file_name. Если нам не нужно использовать стандартное название (или его нет), то не указываем is_document как True.

    if message.voice:
        result['audio'] = message.voice
        result['audio_file_source_type'] = 'voice'
    elif message.text and is_valid_video_url(message.text):
        if 'playlist' in message.text:
            await message.answer(text=i18n.no_playlists_please())
            raise ValueError(f'WARNING: Url to playlist: {message.text}')
        result['url'] = message.text
        result['video_url'] = message.text
        result['is_link'] = True
        await state.update_data(video_url=result['video_url'])
        result['audio_file_source_type'] = 'video_link'
    elif message.audio:
        result['audio'] = message.audio
        result['is_document'] = True
        result['file_name'] = message.audio.file_name
        result['audio_file_source_type'] = 'audio'
    elif message.video:
        result['audio'] = message.video
        # result['is_document'] = True  # Закомментировано в оригинале
        # result['file_name'] = message.video.file_name  # Закомментировано в оригинале
        result['audio_file_source_type'] = 'video'
    elif message.video_note:
        result['audio']: str = message.video_note
        # result['is_document'] = True
        # result['file_name'] = message.video_note.file
        result['audio_file_source_type'] = 'video_note'
    elif message.document:
        if message.document.file_name.split('.')[-1] in ('wav', 'mp3', 'mp4', 'webm', 'm4a', 'avi', 'mov', 'WAV', 'MOV', 'MP4', 'MP3'):
            result['audio'] = message.document
            result['is_document'] = True
            result['file_name'] = message.document.file_name
            result['audio_file_source_type'] = 'document'
        else:
            # Логируем неподдерживаемый файл
            file_extension = message.document.file_name.split('.')[-1] if '.' in message.document.file_name else 'unknown'
            await log_user_action_async(
                user_id=user['id'],
                action_type='unsupported_content_sent',
                action_category='bot_interaction',
                metadata={
                    'file_extension': file_extension,
                    'file_name': message.document.file_name,
                    'content_type': 'document',
                    'file_size': message.document.file_size
                }
            )
            await message.answer(text=i18n.please_send_audio_or_video(), link_preview_options=LinkPreviewOptions(is_disabled=True))
            return None
    else:
        # Логируем неподдерживаемый контент
        metadata = {'content_type': 'unknown'}
        
        # Определяем тип контента
        if message.text:
            metadata['content_type'] = 'text'
            metadata['text'] = message.text
            # Проверяем, может быть это URL
            if message.text.startswith('http://') or message.text.startswith('https://'):
                metadata['content_type'] = 'url'
                metadata['url'] = message.text
                metadata['domain'] = extract_domain_from_url(message.text)
        elif message.photo:
            metadata['content_type'] = 'photo'
        elif message.sticker:
            metadata['content_type'] = 'sticker'
        elif message.animation:
            metadata['content_type'] = 'animation'
        elif message.location:
            metadata['content_type'] = 'location'
        elif message.contact:
            metadata['content_type'] = 'contact'
        elif message.poll:
            metadata['content_type'] = 'poll'
        
        await log_user_action_async(
            user_id=user['id'],
            action_type='unsupported_content_sent',
            action_category='bot_interaction',
            metadata=metadata
        )
        await message.answer(text=i18n.please_send_audio_or_video(), link_preview_options=LinkPreviewOptions(is_disabled=True))
        return None

    return result

async def _process_cached_transcription(cached_transcription: dict, user: dict, i18n: TranslatorRunner, session_id: str,
                                        message: Message, state: FSMContext, waiting_message: Message, progress_manager: DynamicProgressManager,
                                        audio_file_source_type: str = None):
    """
    Обрабатывает кэшированную транскрипцию
    Возвращаем dict с результатами обработки: raw_transcript, timecoded_transcript, summary, file_name
    """
    logger.info(f"Cache HIT for transcription: session={session_id}, transcription_id={cached_transcription['id']}")
    raw_transcript = cached_transcription['transcript_raw']
    timecoded_transcript = cached_transcription['transcript_timecoded']
    transcription_id = cached_transcription['id']
    audio_duration = cached_transcription.get('audio_duration')
    original_file_size = cached_transcription.get('file_size_bytes', 0)



    # Обновляем сессию с информацией о кэше
    await update_processing_session(
        session_id=session_id,
        transcription_id=transcription_id
    )

    #Для маленького (по кол-ву символов) аудио (только от голосового или кружка) нам не нужно отправлять транскрипцию. Поэтому просто пропускаем.
    target_date = datetime(2025, 12, 9, 7, 0, 0)
    
    should_skip_summary = False
    
    if user['created_at'] > target_date:
        should_skip_summary = True
        
    if len(raw_transcript) < 2000 and audio_file_source_type in ['voice', 'video_note']:
        should_skip_summary = True

    if should_skip_summary:
        # Генерируем название для транскрипции без саммари
        try:
            file_name = await generate_title(text=raw_transcript, user=user, i18n=i18n)
        except Exception as e:
            logger.error(f"Failed to generate title for cached transcription: {e}")
            file_name = "transcript"
        
        return {
                'raw_transcript': raw_transcript,
                'timecoded_transcript': timecoded_transcript,
                'transcription_id': transcription_id,
                'audio_duration': audio_duration,
                'summary': None,
                'file_name': file_name
            }

    # Проверяем кэш саммари
    actual_system_prompt = i18n.summarise_text_base_system_prompt_openai() if user['llm_model'] == 'gpt-4o' else i18n.summarise_text_system_prompt_gpt_oss()

    cached_summary = await find_cached_summary(
        transcription_id=transcription_id,
        language_code=user['user_language'],
        llm_model=user['llm_model'],
        system_prompt=actual_system_prompt
    )
    file_name = None

    if cached_summary:
        logger.info(f"Cache HIT for summary: session={session_id}, summary_id={cached_summary['id']}")
        voice_summary = cached_summary['summary_text']
        file_name = cached_summary.get('generated_title')
    else:
        logger.info(f"Cache MISS for summary: session={session_id}, generating new summary")
        # Генерируем только саммари
        try:
            summary_data: dict = await summarise_text(text=raw_transcript, user=user, i18n=i18n, session_id=session_id,
                                                      transcription_id=transcription_id)
        except Exception as e:
            logger.error(f"Error generating summary: {e}")
            return {
                        'raw_transcript': raw_transcript,
                        'timecoded_transcript': timecoded_transcript,
                        'transcription_id': transcription_id,
                        'summary': None,
                        'file_name': None,
                        'audio_duration': audio_duration,
                        'original_file_size': original_file_size
                    }
        voice_summary = summary_data['summary_text']
        file_name = summary_data.get('generated_title')
    
    # Генерируем название если его нет в саммари
    if not file_name:
        try:
            file_name = await generate_title(text=raw_transcript, user=user, i18n=i18n)
        except Exception as e:
            logger.error(f"Failed to generate title for cached transcription: {e}")
            file_name = "transcript"

    return {
        'raw_transcript': raw_transcript,
        'timecoded_transcript': timecoded_transcript,
        'transcription_id': transcription_id,
        'summary': voice_summary,
        'file_name': file_name,
        'audio_duration': audio_duration,
        'original_file_size': original_file_size
    }


async def _process_uncached_transcription(waiting_message: Message, user: dict, i18n: TranslatorRunner, session_id: str,
                                    message: Message, state: FSMContext, progress_manager: DynamicProgressManager, language_code: str, is_link: bool = False, file_name: str = None,
                                    url: str = None, audio: types.Audio | types.Video | types.Document = None, transcript_id: str = None,
                                    is_document: bool = False, original_identifier: str | None = None, use_quality_model: bool = False, audio_file_source_type: str = None):
    """
    Обрабатывает некэшированную транскрипцию
    Возвращаем tuple[str, str, str, str, str] - raw_transcript, timecoded_transcript, summary, file_name
    """
    # Если кэш не найден, выполняем обычный процесс
    try:

        # Checkpoint 1. Download audio
        audio_duration: float | None = None
        file_size: int | None = None
        audio_buffer: bytes | None = None
        file_path: str | None = None
        temp_files: list = []

        if not is_link:
            await progress_manager.start_phase(ProgressPhase.DOWNLOADING, 5)

            file_path: str = await download_file(
                source_type='telegram',
                identifier=original_identifier,
                destination_type='disk',
                user_data=user,
                session_id=session_id,
                download_method='telegram',
                add_file_size_to_session=True,
            )

            # Быстрый кэш-поиск по хэшу файла до конвертаций
            try:
                cached_by_path = await find_cached_transcription_by_file_path(file_path)
            except Exception:
                cached_by_path = None
            if cached_by_path:
                # Обрабатываем как кэшированный кейс, очищаем временный файл и выходим
                result_cached = await _process_cached_transcription(
                    cached_transcription=cached_by_path,
                    user=user,
                    i18n=i18n,
                    session_id=session_id,
                    message=message,
                    state=state,
                    waiting_message=waiting_message,
                    progress_manager=progress_manager,
                    audio_file_source_type=audio_file_source_type
                )
                try:
                    await delete_file(file_path)
                except Exception:
                    pass
                return result_cached

            # Вычисляем file_hash для последующей записи в кэш
            try:
                source_file_hash: str | None = await generate_file_hash_async(file_path=file_path)
            except Exception:
                source_file_hash = None

            # Сохраняем путь к исходному временному файлу из Telegram для последующего удаления
            original_download_path = file_path

            if isinstance(audio, (types.Video, types.Document)) and audio.mime_type.startswith('video'):
                # Checkpoint 1.1. Extract audio from video
                await progress_manager.start_phase(ProgressPhase.EXTRACTING_AUDIO, 20)
                try:
                    file_data: dict = await convert_file_fedor_api(file_path=file_path, mode='video_to_audio', destination_type='disk', user_data=user, session_id=session_id)
                    file_path: str = file_data['file_path']
                    audio_duration: float | None = file_data['result_data'].get('source_duration_seconds', None)
                    # Точно ли это original file size, а не размер итового аудио?!!!
                    file_size: int | None = file_data['result_data'].get('file_size_mb', None)
                    # Удаляем исходный временный файл, полученный из Telegram
                    await delete_file(original_download_path)
                except Exception as e:
                    logger.error(f"Error converting audio to mp3 by fedor_api: {e}")
                    logger.error(f"Trying to convert audio to mp3 using convert_to_mp3")
                    file_path: str = await extract_audio_from_video(file_path=file_path, i18n=i18n, output='path')
                    # Удаляем исходный временный файл после успешного извлечения аудио
                    await delete_file(original_download_path)
            else:
                # Checkpoint 1.1. Convert audio to suitable format
                await progress_manager.start_phase(ProgressPhase.CONVERTING_AUDIO, 20)
                converted_path: str | bytes = await convert_to_mp3(file_path=file_path, output='path')
                # Удаляем исходный временный файл после успешной конвертации
                await delete_file(original_download_path)
                file_path = converted_path  # используем путь на диск для дальнейшей обработки
                audio_buffer = None
                # Checkpoint 2. Clean up temporary files
            await progress_manager.update_progress(35)
        else:
            await progress_manager.start_phase(ProgressPhase.DOWNLOADING, 5)
            try:
                file_data: dict = await download_file_fedor_api(file_url=url, user_data=user, session_id=session_id, result_content_type='audio', destination_type='disk', add_file_size_to_session=True)
                file_path: str = file_data['file_path']
                audio_duration: float | None = file_data['result_data'].get('duration_seconds', None)
                # Точно ли это original file size, а не размер итового аудио?!!!
                file_size: int | None = file_data['result_data'].get('file_size_mb', None)
            except Exception as e:
                logger.error(f"Error downloading file from Fedor API: {e}")
                logger.error(f"Trying to download file from URL to disk: {url}")

                retries = 0
                file_path = None
                while retries < 5:
                    try:
                        # Увеличиваем счетчик попыток загрузки в сессии
                        if session_id:
                            await increment_download_attempts(session_id)
                        audio_buffer: bytes | str = await get_audio_from_url(url, user_data=user, session_id=session_id)
                        if isinstance(audio_buffer, str):
                            file_path = audio_buffer
                        break
                    except Exception as e:
                        retries += 1
                        logger.warning(f"Download attempt {retries} failed for URL {url}: {e}")
                        continue
                if not file_path and not audio_buffer:
                    raise Exception('Failed to download file to disk or buffer')

            # Update progress after successful URL download
            await progress_manager.update_progress(35)

            # Быстрый кэш-поиск по хэшу файла для URL-кейса (если файл на диске)
            if file_path:
                try:
                    cached_by_path = await find_cached_transcription_by_file_path(file_path)
                except Exception:
                    cached_by_path = None
                if cached_by_path:
                    result_cached = await _process_cached_transcription(
                        cached_transcription=cached_by_path,
                        user=user,
                        i18n=i18n,
                        session_id=session_id,
                        message=message,
                        state=state,
                        waiting_message=waiting_message,
                        progress_manager=progress_manager,
                        audio_file_source_type=audio_file_source_type
                    )
                    try:
                        await delete_file(file_path)
                    except Exception:
                        pass
                    return result_cached

                # Вычисляем file_hash для последующей записи в кэш
                try:
                    source_file_hash: str | None = await generate_file_hash_async(file_path=file_path)
                except Exception:
                    source_file_hash = None
        # Обрабатываем переданный language_code
        if language_code == 'skip':
            language_code = None


        # Checkpoint 3. Process audio
        if audio_duration is None:
            try:
                audio_duration = await get_audio_duration(file_path=file_path) if file_path else await get_audio_duration(audio_bytes=audio_buffer)
            except Exception as e:
                logger.error(f"Error getting audio duration: {e}")
                audio_duration = 0
        if file_size is None:
            try:
                file_size = await get_file_size(file_path if file_path else (audio_buffer or b''))
            except Exception as e:
                logger.error(f"Error getting file size: {e}")
                file_size = 0

        # Нормализуем оригинальный размер исходного файла (для логов/БД)
        try:
            original_file_size = await get_file_size(file_path if file_path else (audio_buffer or b''))
        except Exception:
            original_file_size = file_size if isinstance(file_size, int) else 0

        # Start transcription phase with dynamic progress
        await progress_manager.start_phase(ProgressPhase.TRANSCRIBING, 39, audio_duration)

        processing_results: dict = await process_audio(file_bytes=io.BytesIO(audio_buffer) if audio_buffer else None,
                                                                                    file_path=file_path if file_path else None,
                                                                                    waiting_message=waiting_message,
                                                                                    user=user, i18n=i18n,
                                                                                    language_code=language_code,
                                                                                    session_id=session_id,
                                                                                    audio_length=audio_duration,
                                                                                    progress_manager=progress_manager,
                                                                                   file_data={'source_type': 'url' if is_link else 'telegram',
                                                                                              'original_identifier': original_identifier,
                                                                                              'specific_source': identify_url_source(url) if is_link else None,
                                                                                              'original_file_size': original_file_size,
                                                                                              'audio_duration': audio_duration,
                                                                                              'file_hash': source_file_hash if 'source_file_hash' in locals() else None},
                                                                                              use_quality_model=use_quality_model,
                                                                                              audio_file_source_type=audio_file_source_type if audio_file_source_type else None)
        await delete_file(file_path)
        voice_summary = processing_results.get('summary', None)
        raw_transcript = processing_results.get('raw_transcript', None)
        timecoded_transcript = processing_results.get('timecoded_transcript', None)
        transcription_id = processing_results.get('transcription_id', None)
        generated_title = processing_results.get('generated_title', None)

        if file_name is None:
            if is_link:
                # Instagram and some platforms can return None for title
                fetched_title: str | None = await get_video_title(url=url)
                if not fetched_title and not generated_title and raw_transcript:
                    # Генерируем название если нет ни fetched_title, ни generated_title
                    try:
                        generated_title = await generate_title(text=raw_transcript, user=user, i18n=i18n)
                    except Exception as e:
                        logger.error(f"Failed to generate title: {e}")
                        generated_title = None
                file_name = fetched_title or generated_title or "transcript"
            elif is_document:
                file_name = '.'.join(file_name.split('.')[:-1])
            else:
                if not generated_title and raw_transcript:
                    # Генерируем название если нет generated_title
                    try:
                        generated_title = await generate_title(text=raw_transcript, user=user, i18n=i18n)
                    except Exception as e:
                        logger.error(f"Failed to generate title: {e}")
                        generated_title = None
                file_name = generated_title or "transcript"

        if not voice_summary and raw_transcript:
            return {
                'raw_transcript': raw_transcript,
                'timecoded_transcript': timecoded_transcript,
                'transcription_id': transcription_id,
                'audio_duration': audio_duration,
                'file_name': file_name
            }

        # Checkpoint 4. Done! Send the result
        return {
            'raw_transcript': raw_transcript,
            'timecoded_transcript': timecoded_transcript,
            'summary': voice_summary,
            'file_name': file_name,
            'transcription_id': transcription_id,
            'audio_duration': audio_duration
        }
    except Exception as e:
        logger.error(f"Error processing audio with main workflow: {e}")
        # Проверяем, что url определена перед использованием
        if 'url' in locals() and url:
            try:
                result_data: dict | None = await process_audio_fedor_api(audio_url=url, session_id=session_id)
                if result_data:
                    raw_transcript = result_data['raw_transcript']
                    transcription_id = result_data['transcript_id']
                    timecoded_transcript = result_data['timecoded_transcript']
                else:
                    raise Exception(f'process_audio_fedor_api returned None. Result_data: {result_data}')
                original_file_size = 0
                summary_data: dict = await summarise_text(text=raw_transcript, user=user, i18n=i18n,
                                                          session_id=session_id,
                                                          transcription_id=transcription_id)
                voice_summary = summary_data['summary_text']
                generated_title = summary_data['generated_title']
                model_provider = summary_data['model_provider']
                model_name = summary_data['model_name']

                return {
                    'raw_transcript': raw_transcript,
                    'timecoded_transcript': timecoded_transcript,
                    'summary': voice_summary,
                    'file_name': file_name,
                    'transcription_id': transcription_id,
                    'audio_duration': 0
                }
            except Exception as fallback_error:
                logger.error(f"Fallback API also failed: {fallback_error}")
                # Не отправляем сообщение об ошибке здесь, так как это будет сделано в основном блоке except
                raise e  # Повторно вызываем оригинальное исключение
        else:
            # Если url не определена, это означает, что пользователь отправил файл, а не ссылку
            # В этом случае повторно вызываем исключение, так как fallback API работает только со ссылками
            logger.error(f"Fedor API also failed (Fedor API only for links)")
            raise e

@router.callback_query(F.data == 'notetaker_menu')
async def notetaker_menu(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    await callback.answer()
    await callback.message.answer(
        text=i18n.notetaker_menu(),
    reply_markup=notetaker_menu_keyboard(i18n=i18n))