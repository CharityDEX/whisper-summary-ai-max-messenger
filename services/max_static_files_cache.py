"""
Static file sending for Max messenger bot.

Max doesn't have Telegram's file_id caching concept.
Files are read from disk and sent as attachments each time.
"""

import logging
import os
from typing import Optional

from maxapi.types.input_media import InputMedia, InputMediaBuffer

from services.init_max_bot import max_bot

logger = logging.getLogger(__name__)

# File paths (same as Telegram version)
INTRO_VIDEO_RU_PATH = 'resources/whisper_introduction_ru.mp4'
INTRO_VIDEO_EN_PATH = 'resources/whisper_introduction_en.mp4'
FAQ_PDF_RU_PATH = 'resources/WhisperSummary_FAQ_RU.pdf'
FAQ_PDF_EN_PATH = 'resources/WhisperSummary_FAQ_EN.pdf'


def _get_intro_video_path(lang: str) -> str:
    return INTRO_VIDEO_RU_PATH if lang == 'ru' else INTRO_VIDEO_EN_PATH


def _get_faq_path(lang: str) -> tuple[str, str]:
    """Returns (file_path, filename) for FAQ PDF."""
    lang_upper = lang.upper() if lang else 'RU'
    if lang_upper == 'EN':
        return FAQ_PDF_EN_PATH, 'WhisperSummary_FAQ_EN.pdf'
    return FAQ_PDF_RU_PATH, 'WhisperSummary_FAQ_RU.pdf'


async def send_intro_video(
    message,
    lang: str,
    caption: str,
    keyboard=None,
    **kwargs,
):
    """
    Send intro video as an attachment.

    Max doesn't support sending video with caption+keyboard in one message
    the way Telegram does (answer_video). We send the video as an attachment
    alongside the caption text.

    Args:
        message: maxapi Message to reply to
        lang: User language ('ru', 'en')
        caption: Text caption
        keyboard: Optional keyboard Attachment
    """
    video_path = _get_intro_video_path(lang)

    if not os.path.exists(video_path):
        logger.warning(f"Intro video not found: {video_path}. Sending text only.")
        attachments = [keyboard] if keyboard else []
        await message.answer(text=caption, attachments=attachments or None)
        return

    attachments = [InputMedia(path=video_path)]
    if keyboard:
        attachments.append(keyboard)

    try:
        await message.answer(text=caption, attachments=attachments)
    except Exception as e:
        logger.error(f"Failed to send intro video: {e}. Falling back to text only.")
        attachments = [keyboard] if keyboard else []
        await message.answer(text=caption, attachments=attachments or None)


async def edit_message_with_intro_video(
    message,
    lang: str,
    caption: str,
    keyboard=None,
):
    """
    Max doesn't support replacing message media type via edit.
    We delete the old message and send a new one with the video.
    """
    try:
        await message.delete()
    except Exception:
        pass

    await send_intro_video(message, lang=lang, caption=caption, keyboard=keyboard)


async def send_faq_document(
    event_or_message,
    lang: str,
    **kwargs,
):
    """
    Send FAQ PDF document.

    Args:
        event_or_message: A maxapi Message or MessageCallback event
        lang: User language ('ru', 'en')
    """
    # If it's a callback event, acknowledge and get the message
    if hasattr(event_or_message, 'answer') and hasattr(event_or_message, 'callback'):
        await event_or_message.answer()
        target = event_or_message.message
    else:
        target = event_or_message

    file_path, filename = _get_faq_path(lang)

    if not os.path.exists(file_path):
        logger.warning(f"FAQ PDF not found: {file_path}. Trying RU fallback.")
        file_path, filename = _get_faq_path('ru')
        if not os.path.exists(file_path):
            logger.error(f"FAQ PDF RU fallback also not found: {file_path}")
            return

    try:
        await target.answer(attachments=[InputMedia(path=file_path)])
    except Exception as e:
        logger.error(f"Failed to send FAQ document: {e}")


async def send_intro_video_to_chat(
    bot,
    chat_id: int,
    lang: str,
    caption: str,
    **kwargs,
):
    """Send intro video directly to a chat via bot.send_message."""
    video_path = _get_intro_video_path(lang)

    if not os.path.exists(video_path):
        logger.warning(f"Intro video not found: {video_path}. Sending text only.")
        await bot.send_message(chat_id=chat_id, text=caption)
        return

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            attachments=[InputMedia(path=video_path)],
        )
    except Exception as e:
        logger.error(f"Failed to send intro video to chat {chat_id}: {e}")
        await bot.send_message(chat_id=chat_id, text=caption)
