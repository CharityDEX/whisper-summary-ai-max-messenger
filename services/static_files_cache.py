"""
Централизованное кэширование статических файлов для Telegram Bot.

Этот модуль управляет file_id статических файлов (видео, PDF и т.д.),
чтобы избежать повторной загрузки на сервера Telegram при каждой отправке.

Принцип работы:
1. При первой отправке файла загружаем его как FSInputFile
2. Telegram возвращает file_id в ответе
3. Кэшируем file_id в глобальной переменной
4. При следующих отправках используем file_id вместо файла
5. Если file_id не работает (сброшен), автоматически загружаем файл заново
"""

import logging
from typing import Optional, Union

from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaVideo
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)

# ============================================================================
# ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ КЭШИРОВАНИЯ FILE_ID
# ============================================================================

# Приветственные видео
INTRO_VIDEO_RU_ID: Optional[str] = None
INTRO_VIDEO_EN_ID: Optional[str] = None

# FAQ документы
FAQ_PDF_RU_ID: Optional[str] = None
FAQ_PDF_EN_ID: Optional[str] = None

# Пути к файлам (константы)
INTRO_VIDEO_RU_PATH = 'resources/whisper_introduction_ru.mp4'
INTRO_VIDEO_EN_PATH = 'resources/whisper_introduction_en.mp4'
FAQ_PDF_RU_PATH = 'resources/WhisperSummary_FAQ_RU.pdf'
FAQ_PDF_EN_PATH = 'resources/WhisperSummary_FAQ_EN.pdf'

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def send_intro_video(
    message: Union[Message, CallbackQuery],
    lang: str,
    caption: str,
    reply_markup=None,
    **kwargs
) -> Message:
    """
    Отправляет приветственное видео с автоматическим кэшированием file_id.

    Args:
        message: Message или CallbackQuery объект для отправки
        lang: Язык пользователя ('ru', 'en', и т.д.)
        caption: Текст подписи к видео
        reply_markup: Клавиатура (optional)
        **kwargs: Дополнительные параметры для answer_video()

    Returns:
        Message: Отправленное сообщение
    """
    global INTRO_VIDEO_RU_ID, INTRO_VIDEO_EN_ID

    # Определяем какой file_id и путь использовать
    if lang == 'ru':
        cached_id = INTRO_VIDEO_RU_ID
        file_path = INTRO_VIDEO_RU_PATH
    else:
        cached_id = INTRO_VIDEO_EN_ID
        file_path = INTRO_VIDEO_EN_PATH

    # Если есть кэш, пробуем отправить по file_id
    if cached_id:
        try:
            logger.debug(f"Sending intro video ({lang}) using cached file_id: {cached_id}")
            msg = await message.answer_video(
                video=cached_id,
                caption=caption,
                reply_markup=reply_markup,
                **kwargs
            )
            return msg
        except TelegramBadRequest as e:
            logger.warning(f"Failed to send video using cached file_id ({lang}): {e}. Falling back to file upload.")
            # Сбрасываем кэш и отправляем файлом
            if lang == 'ru':
                INTRO_VIDEO_RU_ID = None
            else:
                INTRO_VIDEO_EN_ID = None

    # Отправляем файлом (первый раз или после ошибки)
    logger.info(f"Uploading intro video ({lang}) from file: {file_path}")
    video_file = FSInputFile(path=file_path, filename='whisper_introduction.gif')
    msg = await message.answer_video(
        video=video_file,
        caption=caption,
        reply_markup=reply_markup,
        **kwargs
    )

    # Кэшируем file_id для следующих отправок
    if msg.video:
        if lang == 'ru':
            INTRO_VIDEO_RU_ID = msg.video.file_id
            logger.info(f"Cached RU intro video file_id: {INTRO_VIDEO_RU_ID}")
        else:
            INTRO_VIDEO_EN_ID = msg.video.file_id
            logger.info(f"Cached EN intro video file_id: {INTRO_VIDEO_EN_ID}")

    return msg


async def edit_message_with_intro_video(
    message: Message,
    lang: str,
    caption: str,
    reply_markup=None
) -> Message:
    """
    Редактирует существующее сообщение, заменяя медиа на приветственное видео.

    Args:
        message: Message объект для редактирования
        lang: Язык пользователя ('ru', 'en', и т.д.)
        caption: Текст подписи к видео
        reply_markup: Клавиатура (optional)

    Returns:
        Message: Отредактированное сообщение
    """
    global INTRO_VIDEO_RU_ID, INTRO_VIDEO_EN_ID

    # Определяем какой file_id и путь использовать
    if lang == 'ru':
        cached_id = INTRO_VIDEO_RU_ID
        file_path = INTRO_VIDEO_RU_PATH
    else:
        cached_id = INTRO_VIDEO_EN_ID
        file_path = INTRO_VIDEO_EN_PATH

    # Если есть кэш, пробуем отправить по file_id
    if cached_id:
        try:
            logger.debug(f"Editing message with intro video ({lang}) using cached file_id: {cached_id}")
            media = InputMediaVideo(media=cached_id, caption=caption)
            msg = await message.edit_media(
                media=media,
                reply_markup=reply_markup
            )
            return msg
        except TelegramBadRequest as e:
            logger.warning(f"Failed to edit with cached file_id ({lang}): {e}. Falling back to file upload.")
            # Сбрасываем кэш
            if lang == 'ru':
                INTRO_VIDEO_RU_ID = None
            else:
                INTRO_VIDEO_EN_ID = None

    # Отправляем файлом
    logger.info(f"Editing message with intro video ({lang}) from file: {file_path}")
    video_file = FSInputFile(path=file_path, filename='whisper_introduction.gif')
    media = InputMediaVideo(media=video_file, caption=caption)
    msg = await message.edit_media(
        media=media,
        reply_markup=reply_markup
    )

    # Кэшируем file_id
    if msg.video:
        if lang == 'ru':
            INTRO_VIDEO_RU_ID = msg.video.file_id
            logger.info(f"Cached RU intro video file_id from edit: {INTRO_VIDEO_RU_ID}")
        else:
            INTRO_VIDEO_EN_ID = msg.video.file_id
            logger.info(f"Cached EN intro video file_id from edit: {INTRO_VIDEO_EN_ID}")

    return msg


async def send_faq_document(
    message: Union[Message, CallbackQuery],
    lang: str,
    **kwargs
) -> Message:
    """
    Отправляет FAQ PDF документ с автоматическим кэшированием file_id.

    Args:
        message: Message или CallbackQuery объект для отправки
        lang: Язык пользователя ('ru', 'en', и т.д.)
        **kwargs: Дополнительные параметры для answer_document()

    Returns:
        Message: Отправленное сообщение
    """
    global FAQ_PDF_RU_ID, FAQ_PDF_EN_ID

    # Определяем file_id и путь
    lang_upper = lang.upper()

    if lang_upper == 'RU':
        cached_id = FAQ_PDF_RU_ID
        file_path = FAQ_PDF_RU_PATH
        filename = 'WhisperSummary_FAQ_RU.pdf'
    elif lang_upper == 'EN':
        cached_id = FAQ_PDF_EN_ID
        file_path = FAQ_PDF_EN_PATH
        filename = 'WhisperSummary_FAQ_EN.pdf'
    else:
        # Fallback на русский для неизвестных языков
        logger.warning(f"Unknown language '{lang}' for FAQ, falling back to RU")
        cached_id = FAQ_PDF_RU_ID
        file_path = FAQ_PDF_RU_PATH
        filename = 'WhisperSummary_FAQ_RU.pdf'

    # Определяем куда отправлять (Message или CallbackQuery)
    if isinstance(message, CallbackQuery):
        target = message.message
        await message.answer()  # Закрываем callback
    else:
        target = message

    # Пробуем отправить по file_id
    if cached_id:
        try:
            logger.debug(f"Sending FAQ PDF ({lang_upper}) using cached file_id: {cached_id}")
            msg = await target.answer_document(
                document=cached_id,
                **kwargs
            )
            return msg
        except TelegramBadRequest as e:
            logger.warning(f"Failed to send FAQ PDF using cached file_id ({lang_upper}): {e}. Falling back to file upload.")
            # Сбрасываем кэш
            if lang_upper == 'RU':
                FAQ_PDF_RU_ID = None
            elif lang_upper == 'EN':
                FAQ_PDF_EN_ID = None

    # Отправляем файлом
    logger.info(f"Uploading FAQ PDF ({lang_upper}) from file: {file_path}")
    try:
        pdf_file = FSInputFile(file_path, filename=filename)
    except FileNotFoundError:
        # Если файл не найден, пробуем русский как fallback
        logger.error(f"FAQ PDF file not found: {file_path}, using RU as fallback")
        pdf_file = FSInputFile(FAQ_PDF_RU_PATH, filename='WhisperSummary_FAQ_RU.pdf')
        lang_upper = 'RU'  # Меняем язык для правильного кэширования

    msg = await target.answer_document(
        document=pdf_file,
        **kwargs
    )

    # Кэшируем file_id
    if msg.document:
        if lang_upper == 'RU':
            FAQ_PDF_RU_ID = msg.document.file_id
            logger.info(f"Cached RU FAQ PDF file_id: {FAQ_PDF_RU_ID}")
        elif lang_upper == 'EN':
            FAQ_PDF_EN_ID = msg.document.file_id
            logger.info(f"Cached EN FAQ PDF file_id: {FAQ_PDF_EN_ID}")

    return msg


async def send_intro_video_to_chat(
    bot,
    chat_id: int,
    lang: str,
    caption: str,
    **kwargs
):
    """
    Отправляет приветственное видео напрямую в чат через bot.send_video с кэшированием.

    Используется для рассылок и других случаев, когда нет объекта Message.

    Args:
        bot: Bot instance
        chat_id: ID чата для отправки
        lang: Язык пользователя ('ru', 'en', и т.д.)
        caption: Текст подписи к видео
        **kwargs: Дополнительные параметры для bot.send_video()

    Returns:
        Message: Отправленное сообщение
    """
    global INTRO_VIDEO_RU_ID, INTRO_VIDEO_EN_ID

    # Определяем какой file_id и путь использовать
    if lang == 'ru':
        cached_id = INTRO_VIDEO_RU_ID
        file_path = INTRO_VIDEO_RU_PATH
    else:
        cached_id = INTRO_VIDEO_EN_ID
        file_path = INTRO_VIDEO_EN_PATH

    # Если есть кэш, пробуем отправить по file_id
    if cached_id:
        try:
            logger.debug(f"Sending intro video to chat {chat_id} ({lang}) using cached file_id: {cached_id}")
            msg = await bot.send_video(
                chat_id=chat_id,
                video=cached_id,
                caption=caption,
                **kwargs
            )
            return msg
        except TelegramBadRequest as e:
            logger.warning(f"Failed to send video to chat using cached file_id ({lang}): {e}. Falling back to file upload.")
            # Сбрасываем кэш и отправляем файлом
            if lang == 'ru':
                INTRO_VIDEO_RU_ID = None
            else:
                INTRO_VIDEO_EN_ID = None

    # Отправляем файлом (первый раз или после ошибки)
    logger.info(f"Uploading intro video to chat {chat_id} ({lang}) from file: {file_path}")
    video_file = FSInputFile(path=file_path, filename='whisper_introduction.gif')
    msg = await bot.send_video(
        chat_id=chat_id,
        video=video_file,
        caption=caption,
        **kwargs
    )

    # Кэшируем file_id для следующих отправок
    if msg.video:
        if lang == 'ru':
            INTRO_VIDEO_RU_ID = msg.video.file_id
            logger.info(f"Cached RU intro video file_id from chat send: {INTRO_VIDEO_RU_ID}")
        else:
            INTRO_VIDEO_EN_ID = msg.video.file_id
            logger.info(f"Cached EN intro video file_id from chat send: {INTRO_VIDEO_EN_ID}")

    return msg


def get_intro_video_file_id(lang: str) -> Optional[str]:
    """
    Возвращает кэшированный file_id приветственного видео (или None).
    Используется для кастомной логики отправки.
    """
    if lang == 'ru':
        return INTRO_VIDEO_RU_ID
    else:
        return INTRO_VIDEO_EN_ID


def get_intro_video_file_path(lang: str) -> str:
    """
    Возвращает путь к файлу приветственного видео.
    Используется для кастомной логики отправки.
    """
    if lang == 'ru':
        return INTRO_VIDEO_RU_PATH
    else:
        return INTRO_VIDEO_EN_PATH


def set_intro_video_file_id(lang: str, file_id: str) -> None:
    """
    Устанавливает file_id приветственного видео в кэш.
    Используется для кастомной логики отправки.
    """
    global INTRO_VIDEO_RU_ID, INTRO_VIDEO_EN_ID

    if lang == 'ru':
        INTRO_VIDEO_RU_ID = file_id
        logger.info(f"Set RU intro video file_id: {file_id}")
    else:
        INTRO_VIDEO_EN_ID = file_id
        logger.info(f"Set EN intro video file_id: {file_id}")


def get_cache_stats() -> dict:
    """
    Возвращает статистику кэшированных file_id.
    Полезно для мониторинга и отладки.
    """
    return {
        'intro_video_ru': INTRO_VIDEO_RU_ID is not None,
        'intro_video_en': INTRO_VIDEO_EN_ID is not None,
        'faq_pdf_ru': FAQ_PDF_RU_ID is not None,
        'faq_pdf_en': FAQ_PDF_EN_ID is not None,
        'file_ids': {
            'intro_video_ru': INTRO_VIDEO_RU_ID,
            'intro_video_en': INTRO_VIDEO_EN_ID,
            'faq_pdf_ru': FAQ_PDF_RU_ID,
            'faq_pdf_en': FAQ_PDF_EN_ID,
        }
    }
