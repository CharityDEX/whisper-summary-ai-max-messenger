import traceback
from datetime import datetime, timedelta
import io
import logging
import asyncio
import time
import uuid

from maxapi import Router, F
from maxapi.context import MemoryContext
from maxapi.types import MessageCreated, MessageCallback
from maxapi.types.message import Message
from maxapi.types.input_media import InputMedia, InputMediaBuffer
from maxapi.types.attachments.audio import Audio as MaxAudioAttachment
from maxapi.types.attachments.video import Video as MaxVideoAttachment
from maxapi.types.attachments.file import File as MaxFileAttachment
from maxapi.enums.parse_mode import ParseMode
from fluentogram import TranslatorRunner
import pytz

from models.orm import get_transcript_by_chat_session
from max_keyboards.user_keyboards import (
    continue_without_language, inline_change_model_menu,
    inline_change_specify_language_menu, inline_main_menu, inline_new_session, inline_cancel,
    bill_keyboard, inline_user_settings, subscription_menu, subscription_forward,
    inline_download_file, transcription_no_summary_keyboard, notetaker_menu_keyboard,
    inline_cancel_queue,
)
from models.orm import (
    change_user_setting, get_transcription_data, get_user, create_new_user, add_gpt_use, add_voice_use,
    renew_subscription_db, update_user_blocked_status, update_audio_log,
    create_processing_session, update_processing_session, create_audio_log_with_session,
    increment_download_attempts, log_anonymous_chat_message, count_user_chat_requests_by_session,
    get_processing_session_by_id, find_cached_transcription, find_cached_summary,
    find_cached_transcription_by_file_path, log_user_action_async,
)
from services.cache_normalization import generate_prompt_hash, generate_file_hash_async
from services.content_downloaders.file_handling import download_file, identify_url_source
from services.fedor_api import convert_file_fedor_api, download_file_fedor_api, process_audio_fedor_api
from services.general_functions import process_chat_request, process_audio, summarise_text, generate_title
from services.dynamic_progress_manager import DynamicProgressManager, create_progress_manager, ProgressPhase
from services.google_docs_service_lite import create_two_google_docs_lite
from services.init_max_bot import max_bot, config
from services.openai_functions import prepare_language_code
from services.services import (
    create_input_file_from_text, extract_audio_from_video, convert_to_mp3, delete_file, get_file_size,
    progress_bar, replace_markdown_bold_with_html, sanitize_html_for_telegram, split_title_and_summary, get_audio_duration,
)
from services.youtube_funcs import get_content_from_url, is_valid_video_url, get_audio_from_url
from max_states.states import UserAudioSession
from services.video_title_extractor import get_video_title
from services.max_audio_queue_service import max_audio_queue_manager
from services.max_static_files_cache import send_intro_video, edit_message_with_intro_video

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
# Compatibility wrapper for shared services
# ---------------------------------------------------------------------------

class MaxMessageCompat:
    """Wraps a maxapi Message to provide aiogram-compatible methods.

    Shared services (STT providers, DynamicProgressManager, etc.) call
    ``waiting_message.edit_text(text=...)`` — an aiogram method that doesn't
    exist on maxapi Message.  This wrapper bridges the gap until Phase 6
    adapts the shared services to be platform-agnostic.
    """

    def __init__(self, message: Message):
        self._message = message

    async def edit_text(self, text: str, **kwargs):
        """aiogram-compatible edit_text → delegates to maxapi message.edit()."""
        try:
            return await self._message.edit(text=text)
        except Exception as e:
            if "message is not modified" not in str(e):
                logger.warning(f"MaxMessageCompat.edit_text failed: {e}")

    async def delete(self):
        return await self._message.delete()

    @property
    def message_id(self):
        return self._message.body.mid

    @property
    def text(self):
        return self._message.body.text if self._message.body else None

    @property
    def chat(self):
        """Aiogram-compatible .chat with .id — maps to maxapi recipient.chat_id."""
        class _Chat:
            def __init__(self, chat_id):
                self.id = chat_id
        return _Chat(self._message.recipient.chat_id)

    # Allow attribute access to fall through to the wrapped message
    def __getattr__(self, name):
        return getattr(self._message, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_domain_from_url(url: str) -> str:
    """Extract domain from URL for safe logging."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or 'unknown'
    except Exception:
        return 'unknown'


def _bf_to_max(bf) -> InputMediaBuffer:
    """Convert an aiogram BufferedInputFile to maxapi InputMediaBuffer.
    Used as a bridge until services.py is adapted (Phase 5)."""
    return InputMediaBuffer(buffer=bf.data, filename=bf.filename)



async def _reply(message: Message, **kwargs) -> Message | None:
    """Send a reply and return the inner Message with bot reference set."""
    result = await message.reply(**kwargs)
    if result:
        result.message.bot = max_bot
        return result.message
    return None


async def _answer(message: Message, **kwargs) -> Message | None:
    """Send an answer and return the inner Message with bot reference set."""
    result = await message.answer(**kwargs)
    if result:
        result.message.bot = max_bot
        return result.message
    return None


async def _send(chat_id: int = None, user_id: int = None, **kwargs) -> Message | None:
    """Send a message via bot and return the inner Message with bot reference set."""
    result = await max_bot.send_message(chat_id=chat_id, user_id=user_id, **kwargs)
    if result:
        result.message.bot = max_bot
        return result.message
    return None


def _get_attachment_url(attachment) -> str | None:
    """Extract download URL from a Max attachment payload."""
    if hasattr(attachment, 'payload') and attachment.payload is not None:
        if hasattr(attachment.payload, 'url'):
            return attachment.payload.url
    # Video attachments have urls field with quality variants
    if isinstance(attachment, MaxVideoAttachment) and attachment.urls:
        for quality in ('mp4_720', 'mp4_480', 'mp4_360', 'mp4_1080', 'mp4_240', 'mp4_144'):
            url = getattr(attachment.urls, quality, None)
            if url:
                return url
    return None


# ---------------------------------------------------------------------------
# Media extraction from Max messages
# ---------------------------------------------------------------------------

async def extract_media_from_message(message: Message, context: MemoryContext, i18n: TranslatorRunner, user: dict | None = None) -> dict | None:
    """
    Extract media data from a Max messenger message.

    Max attachment types:
      - Audio  → voice messages (type="audio")
      - Video  → video attachments (type="video")
      - File   → any other file (type="file") – may be audio/video by extension
      - Text   → URL links checked via is_valid_video_url()

    Returns a dict compatible with the Telegram version's extract_media_from_message,
    or None if the content is unsupported.
    """
    if user is None:
        user = await get_user(telegram_id=message.sender.user_id)

    result = {
        'audio': None,          # The attachment object (or None for links)
        'is_link': False,
        'is_document': False,
        'video_url': None,
        'file_name': None,
        'url': None,            # Download URL (attachment URL or video link)
        'audio_file_source_type': None,
    }

    body = message.body
    text = body.text if body else None
    attachments = body.attachments if body else []

    # Forwarded messages: attachments live in message.link.message, not message.body
    if not attachments and hasattr(message, 'link') and message.link:
        linked_body = getattr(message.link, 'message', None)
        if linked_body:
            attachments = linked_body.attachments if linked_body.attachments else []
            if not text:
                text = linked_body.text if hasattr(linked_body, 'text') else None

    # Check attachments first
    audio_att = None
    video_att = None
    file_att = None
    logger.info(f"extract_media: attachments count={len(attachments or [])}, types={[type(a).__name__ for a in (attachments or [])]}")
    for att in (attachments or []):
        att_type = getattr(att, 'type', None)
        logger.info(
            f"  Attachment: class={type(att).__name__}, type={att_type}, "
            f"isinstance_audio={isinstance(att, MaxAudioAttachment)}, "
            f"isinstance_video={isinstance(att, MaxVideoAttachment)}, "
            f"isinstance_file={isinstance(att, MaxFileAttachment)}, "
            f"filename={getattr(att, 'filename', 'N/A')}"
        )
        if isinstance(att, MaxAudioAttachment) and audio_att is None:
            audio_att = att
        elif isinstance(att, MaxVideoAttachment) and video_att is None:
            video_att = att
        elif isinstance(att, MaxFileAttachment) and file_att is None:
            file_att = att
        # Fallback: if isinstance fails but type string matches
        elif att_type in ('audio',) and audio_att is None:
            logger.info(f"  -> Fallback match: audio (class was {type(att).__name__})")
            audio_att = att
        elif att_type in ('video',) and video_att is None:
            logger.info(f"  -> Fallback match: video (class was {type(att).__name__})")
            video_att = att
        elif att_type in ('file',) and file_att is None:
            logger.info(f"  -> Fallback match: file (class was {type(att).__name__})")
            file_att = att

    if audio_att:
        # Voice / audio message
        result['audio'] = audio_att
        result['url'] = _get_attachment_url(audio_att)
        result['audio_file_source_type'] = 'voice'
        # Debug: log full payload for diagnosing CDN download issues
        if hasattr(audio_att, 'payload') and audio_att.payload:
            logger.info(f"  Audio payload: url={getattr(audio_att.payload, 'url', 'N/A')}, token={getattr(audio_att.payload, 'token', 'N/A')}")
    elif text and is_valid_video_url(text):
        if 'playlist' in text:
            await message.answer(text=i18n.no_playlists_please())
            raise ValueError(f'WARNING: Url to playlist: {text}')
        result['url'] = text
        result['video_url'] = text
        result['is_link'] = True
        await context.update_data(video_url=result['video_url'])
        result['audio_file_source_type'] = 'video_link'
    elif video_att:
        result['audio'] = video_att
        result['url'] = _get_attachment_url(video_att)
        result['audio_file_source_type'] = 'video'
    elif file_att:
        filename = file_att.filename or ''
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        supported_extensions = ('wav', 'mp3', 'mp4', 'webm', 'm4a', 'avi', 'mov', 'ogg', 'aac', 'flac')
        if ext in supported_extensions:
            result['audio'] = file_att
            result['is_document'] = True
            result['file_name'] = filename
            result['url'] = _get_attachment_url(file_att)
            result['audio_file_source_type'] = 'document'
        else:
            # Unsupported file type
            await log_user_action_async(
                user_id=user['id'],
                action_type='unsupported_content_sent',
                action_category='bot_interaction',
                metadata={
                    'file_extension': ext,
                    'file_name': filename,
                    'content_type': 'document',
                    'file_size': file_att.size,
                }
            )
            await message.answer(text=i18n.please_send_audio_or_video())
            return None
    else:
        # Unsupported content (text that is not a URL, photo, sticker, etc.)
        metadata = {'content_type': 'unknown'}
        if text:
            metadata['content_type'] = 'text'
            metadata['text'] = text
            if text.startswith('http://') or text.startswith('https://'):
                metadata['content_type'] = 'url'
                metadata['url'] = text
                metadata['domain'] = extract_domain_from_url(text)

        await log_user_action_async(
            user_id=user['id'],
            action_type='unsupported_content_sent',
            action_category='bot_interaction',
            metadata=metadata,
        )
        await message.answer(text=i18n.please_send_audio_or_video())
        return None

    return result


# ---------------------------------------------------------------------------
# Subscription menu (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'subscription_menu')
async def process_subscription_menu(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    await event.answer()
    # TODO: Wire to max_handlers/balance_handlers.py when created (Phase 4)
    await event.message.answer(text=i18n.subscription_menu() if i18n else "Subscription menu")


# ---------------------------------------------------------------------------
# Start new audio session
# ---------------------------------------------------------------------------

async def process_new_audio_start(event_or_message, context: MemoryContext, i18n: TranslatorRunner):
    """Initiate a new audio processing session. Accepts MessageCreated, MessageCallback, or Message."""
    if isinstance(event_or_message, MessageCallback):
        await event_or_message.answer()
        is_callback = True
        user: dict = await get_user(telegram_id=event_or_message.callback.user.user_id)
        message = event_or_message.message
    elif isinstance(event_or_message, MessageCreated):
        is_callback = False
        user: dict = await get_user(telegram_id=event_or_message.message.sender.user_id)
        message = event_or_message.message
    else:
        # Assume it's a Message object
        is_callback = False
        user: dict = await get_user(telegram_id=event_or_message.sender.user_id)
        message = event_or_message

    if user['subscription'] != 'True' and user['subscription'] != 'trial' and user['audio_uses'] >= 50:
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_limit_exceeded',
            action_category='conversion',
            metadata={
                'audio_uses': user['audio_uses'],
                'limit': 50,
                'source': 'new_audio_start',
                'shown_subscription_menu': True,
            }
        )
        await message.answer(text=i18n.free_audio_limit_exceeded(), attachments=[subscription_menu(i18n)])
        return

    await message.answer(text=i18n.send_new_audio())
    await context.clear()
    await context.set_state(UserAudioSession.waiting_user_audio)


# ---------------------------------------------------------------------------
# Core audio processing (internal)
# ---------------------------------------------------------------------------

async def _process_audio_internal(
    message: Message,
    context: MemoryContext,
    i18n: TranslatorRunner,
    language_code: str | None = None,
    queue_message: Message | None = None,
    media_data: dict | None = None,
):
    """Internal audio processing function (Max version)."""
    user: dict = await get_user(telegram_id=message.sender.user_id)

    # Extract media data from message
    if media_data is None:
        media_data = await extract_media_from_message(message, context, i18n, user)
    if media_data is None:
        return

    # Unpack
    audio = media_data['audio']
    is_link = media_data['is_link']
    is_document = media_data['is_document']
    video_url = media_data['video_url']
    file_name = media_data['file_name']
    url = media_data['url']
    audio_file_source_type = media_data['audio_file_source_type']

    data = await context.get_data()

    if data.get('ask_language_message_mid'):
        try:
            await max_bot.delete_message(message_id=data['ask_language_message_mid'])
        except Exception:
            pass
        await context.update_data(ask_language_message_mid=None)

    if queue_message:
        raw_waiting = await _reply(message, text=i18n.wait_for_your_response())
        try:
            await queue_message.delete()
        except Exception:
            pass
    else:
        raw_waiting = await _reply(message, text=i18n.wait_for_your_response())

    if raw_waiting is None:
        logger.error("Failed to send waiting message")
        return

    # Wrap for aiogram-compatible edit_text() used by shared services
    waiting_message = MaxMessageCompat(raw_waiting)

    await context.set_state(UserAudioSession.user_wait)

    # === POINT 1: Create ProcessingSession ===
    start_time = time.time()
    # For Max, non-link files use 'url' source_type since we download via attachment URL
    source_type = 'url' if is_link else 'max'
    original_identifier = url if url else 'unknown'

    if is_link:
        specific_source = identify_url_source(url)
    else:
        specific_source = 'max_attachment'

    session_id = await create_processing_session(
        user_id=user['id'],
        original_identifier=original_identifier,
        source_type=source_type,
        specific_source=specific_source,
        waiting_message_id=None,         # Max message IDs are strings; DB column is BIGINT
        user_original_message_id=None,  # Max message IDs are strings; DB column is BIGINT
    )

    logger.info(f'Starting processing session {session_id} for user {user["id"]}')
    await context.update_data(session_id=session_id)

    if not session_id:
        await waiting_message.edit(text=i18n.something_went_wrong())
        return

    await context.update_data(session_id=session_id)
    audio_log_id = await create_audio_log_with_session(
        session_id=session_id,
        user_id=user['id'],
    )

    try:
        # === CHECK TRANSCRIPTION CACHE ===
        cached_transcription: dict | None = await find_cached_transcription(
            source_type=source_type,
            original_identifier=original_identifier,
        )

        raw_transcript = None
        timecoded_transcript = None
        voice_summary = None
        transcription_id = None
        generated_file_name = None
        audio_duration = None
        original_file_size = 0
        use_quality_model = False
        temp_files: list[str] = []

        # "same audio within an hour from same user" → use deepgram
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
                cached_transcription = None

        # Initialize dynamic progress manager
        progress_manager = await create_progress_manager(waiting_message, progress_bar, i18n, session_id=session_id)

        if cached_transcription:
            result: dict | None = await _process_cached_transcription(
                cached_transcription=cached_transcription, user=user, i18n=i18n,
                session_id=session_id, message=message, context=context,
                waiting_message=waiting_message, progress_manager=progress_manager,
                audio_file_source_type=audio_file_source_type,
            )
        else:
            result: dict | None = await _process_uncached_transcription(
                user=user, i18n=i18n, session_id=session_id, message=message,
                context=context, file_name=file_name, url=url, audio=audio,
                is_document=is_document, transcript_id=transcription_id,
                progress_manager=progress_manager, waiting_message=waiting_message,
                language_code=language_code, original_identifier=original_identifier,
                is_link=is_link, use_quality_model=use_quality_model,
                audio_file_source_type=audio_file_source_type,
            )

        if result:
            raw_transcript = result.get('raw_transcript')
            timecoded_transcript = result.get('timecoded_transcript')
            voice_summary = result.get('summary')
            generated_file_name = result.get('file_name')
            transcription_id = result.get('transcription_id')
            audio_duration = result.get('audio_duration')
        else:
            raise ValueError('Result is empty from _process_cached/_process_uncached')

        try:
            await progress_manager.start_phase(ProgressPhase.FINALIZING, 95)
        except Exception:
            pass

        await context.update_data(transcript=raw_transcript)
        await context.update_data(context=[
            {'role': 'user', 'content': f"{i18n.audio_text_prefix()} {raw_transcript}"}
        ])

        anonymous_chat_session = str(uuid.uuid4())
        await context.update_data(anonymous_chat_session=anonymous_chat_session)

        try:
            await log_anonymous_chat_message(
                chat_session=anonymous_chat_session,
                message_from='user',
                text=raw_transcript,
                message_order=1,
            )
        except Exception as e:
            logger.warning(f"Failed to log anonymous chat message: {e}")

        # Send transcription
        if not voice_summary and raw_transcript:
            await send_transcription(
                message=message, i18n=i18n,
                transcription_raw=raw_transcript,
                transcription_timecoded=timecoded_transcript,
                file_name=file_name if file_name else generated_file_name,
                transcription_format=user['transcription_format'],
                chat_session=anonymous_chat_session,
                session_id=session_id,
                no_summary=True,
                audio_file_source_type=audio_file_source_type,
                is_link=is_link,
            )
        else:
            await send_transcription(
                message=message, i18n=i18n,
                transcription_raw=raw_transcript,
                transcription_timecoded=timecoded_transcript,
                file_name=file_name if file_name else generated_file_name,
                transcription_format=user['transcription_format'],
                chat_session=anonymous_chat_session,
                session_id=session_id,
                no_summary=False,
                audio_file_source_type=audio_file_source_type,
                is_link=is_link,
            )
            await send_summary(
                message=message, context=context, i18n=i18n, summary=voice_summary,
                is_link=is_link, chat_session=anonymous_chat_session, session_id=session_id,
            )

        await add_voice_use(message.sender.user_id)

        # === POINT 2A: Successful completion ===
        try:
            if audio_log_id:
                await update_audio_log(
                    audio_log_id=audio_log_id,
                    length=audio_duration,
                    file_size_bytes=original_file_size,
                    processing_duration=time.time() - start_time,
                    success=True,
                )
        except Exception as log_error:
            logger.warning(f"Failed to update session logs {session_id}: {log_error}")

        await update_processing_session(
            session_id=session_id,
            completed_at=datetime.utcnow(),
            total_duration=time.time() - start_time,
            final_status='success',
        )
        logger.info(f"Processing session {session_id} for user {message.sender.user_id} completed successfully")
        await context.set_state(UserAudioSession.dialogue)

        if 'progress_manager' in locals():
            await progress_manager.stop()

        await waiting_message.delete()

        # Notify queue manager
        await max_audio_queue_manager.finish_processing(message.sender.user_id)

    except Exception as e:
        import traceback
        logger.error(f"_process_audio_internal EXCEPTION: {type(e).__name__}: {e}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        logger.error(i18n.process_audio_error(error=str(e)))
        if 'progress_manager' in locals():
            await progress_manager.stop()
        await max_audio_queue_manager.finish_processing(message.sender.user_id)

        # === POINT 2B: Error handling ===
        try:
            error_stage = 'download'
            if 'audio_buffer' in locals():
                error_stage = 'audio_extraction'
            if 'raw_transcript' in locals() and raw_transcript:
                error_stage = 'transcription'
            if 'voice_summary' in locals() and voice_summary:
                error_stage = 'summary'

            if audio_log_id:
                await update_audio_log(
                    audio_log_id=audio_log_id,
                    processing_duration=time.time() - start_time,
                    success=False,
                    error_message=str(e),
                )

            await update_processing_session(
                session_id=session_id,
                completed_at=datetime.utcnow(),
                total_duration=time.time() - start_time,
                final_status='failed',
                error_stage=error_stage,
                error_message=str(e),
            )
        except Exception as log_error:
            logger.warning(f"Failed to update session logs {session_id} with error: {log_error}")

        try:
            await waiting_message.edit(text=i18n.something_went_wrong())
        except Exception as edit_error:
            if "message is not modified" not in str(edit_error):
                logger.warning(f"Failed to edit waiting message: {edit_error}")


# ---------------------------------------------------------------------------
# Audio message extraction from state
# ---------------------------------------------------------------------------

async def _extract_audio_message(
    message: Message | None,
    context: MemoryContext,
    audio_key: str | None = None,
    user_id: int | None = None,
) -> Message:
    """Extract the audio message from direct message or from context state."""
    if message:
        return message

    if not audio_key and user_id:
        data = await context.get_data()
        audio_keys: list = [key for key in data.keys() if key.startswith(f'audio_msg_{user_id}_')]
        if not audio_keys:
            logger.error(f"No audio keys found for user {user_id}, state keys: {list(data.keys())}")
            raise ValueError("No audio message found")
        audio_key = max(audio_keys, key=lambda x: int(x.split('_')[-1]) if x.split('_')[-1].isdigit() else 0)

    if audio_key:
        data = await context.get_data()
        audio_message: Message = data.get(audio_key)
        if not audio_message:
            logger.error(f"No audio_message found for key {audio_key}")
            raise ValueError("No audio message found")
        return audio_message

    raise ValueError("No valid source for audio message")


async def _prepare_and_process_audio(
    message: Message | None,
    context: MemoryContext,
    i18n: TranslatorRunner,
    language_code: str | None,
    audio_key: str | None = None,
    user_id: int | None = None,
    delete_message_mid: str | None = None,
):
    """Universal function for preparing and processing audio."""
    # 1. Extract audio message
    audio_message = await _extract_audio_message(message, context, audio_key, user_id)

    # 2. Cleanup state data
    cleanup_data = {'language_code': None}
    if audio_key:
        cleanup_data[audio_key] = None
    if delete_message_mid:
        cleanup_data['ask_language_message_mid'] = None
    await context.update_data(**cleanup_data)

    # 3. Switch state back
    await context.set_state(UserAudioSession.waiting_user_audio)

    try:
        media_data = await extract_media_from_message(audio_message, context, i18n)
    except ValueError:
        return

    if media_data is None:
        return

    # 4. Add to queue or process directly
    is_queued, queue_message = await max_audio_queue_manager.add_to_queue(
        user_id=audio_message.sender.user_id,
        message=audio_message,
        context=context,
        i18n=i18n,
        language_code=language_code,
        media_data=media_data,
    )

    if is_queued:
        return

    # 5. Delete message if needed
    if delete_message_mid:
        try:
            await max_bot.delete_message(message_id=delete_message_mid)
        except Exception:
            pass

    # 6. Process directly (as background task so dispatcher can handle next events)
    asyncio.create_task(_process_audio_internal(
        message=audio_message,
        context=context,
        i18n=i18n,
        language_code=language_code,
        queue_message=None,
        media_data=media_data,
    ))


# ---------------------------------------------------------------------------
# Handler: Accept files while another is processing (state: user_wait)
# ---------------------------------------------------------------------------

@router.message_created(UserAudioSession.user_wait)
async def process_audio_while_waiting(event: MessageCreated, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    """Handle messages while another file is being processed.

    Text messages are routed to dialogue if a transcript context exists;
    otherwise everything is treated as new audio input queued for processing.
    """
    message = event.message
    text = message.body.text if message.body else None

    if text and not is_valid_video_url(text):
        data = await context.get_data()
        if 'context' in data:
            logger.info(f"process_audio_while_waiting: text message with dialogue context — routing to dialogue")
            await _handle_dialogue(message, context, user, i18n)
            return

    await _handle_audio_in_dialogue(event.message, context, i18n)


# ---------------------------------------------------------------------------
# Handler: Process new audio (state: waiting_user_audio)
# ---------------------------------------------------------------------------

@router.message_created(UserAudioSession.waiting_user_audio)
async def process_new_audio(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    """Main audio processing handler with queue support."""
    message = event.message
    user: dict = await get_user(telegram_id=message.sender.user_id)
    if user:
        username = user.get('username', '') or ''
        if 'fastsaver' in username:
            logger.warning(f'Request from fastsaver: telegram_id - {user["telegram_id"]}, username - {user["username"]}')
            return

    # Check subscription limits
    if user['subscription'] != 'True' and user['audio_uses'] >= 50 and user['subscription'] != 'trial':
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_limit_exceeded',
            action_category='conversion',
            metadata={
                'audio_uses': user['audio_uses'],
                'limit': 50,
                'source': 'audio_upload',
                'shown_subscription_menu': True,
            }
        )
        await message.answer(text=i18n.free_audio_limit_exceeded(), attachments=[subscription_menu(i18n)])
        return

    # Check if language specification is needed BEFORE adding to queue
    data = await context.get_data()
    logger.debug(f"Processing audio from user {message.sender.user_id}, current state data keys: {list(data.keys())}")

    if user['specify_audio_language'] and data.get('language_code') is None and data.get('language_code') != 'skip':
        audio_key = f"audio_msg_{message.sender.user_id}_{message.body.mid}_{int(time.time())}"
        await context.update_data(**{audio_key: message})

        ask_lang_msg = await _reply(message, text=i18n.enter_language(),
                                    attachments=[continue_without_language(i18n=i18n, audio_key=audio_key)])
        if ask_lang_msg:
            await context.update_data(ask_language_message_mid=ask_lang_msg.body.mid)
        await context.set_state(UserAudioSession.enter_language)
        return

    # Extract language_code
    language_code = data.get('language_code')
    await context.update_data(language_code=None)

    # Universal processing
    await _prepare_and_process_audio(
        message=message,
        context=context,
        i18n=i18n,
        language_code=language_code,
    )


# ---------------------------------------------------------------------------
# Handler: Enter language (state: enter_language)
# ---------------------------------------------------------------------------

@router.message_created(UserAudioSession.enter_language)
async def process_enter_language(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    message = event.message
    text = message.body.text if message.body else None

    try:
        language_code: str | None = await prepare_language_code(text)
    except Exception as e:
        logger.error(i18n.language_error(error=str(traceback.format_exc())))
        language_code = None

    if language_code:
        await _reply(message, text=i18n.language_specified(language=language_code))

    try:
        await _prepare_and_process_audio(
            message=None,
            context=context,
            i18n=i18n,
            language_code=language_code,
            user_id=message.sender.user_id,
        )
    except ValueError as e:
        logger.error(f"Failed to process audio: {e}")
        await message.answer(text=i18n.something_went_wrong())
        await context.clear()


# ---------------------------------------------------------------------------
# Handler: Continue without language (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('cont_lang_'))
async def process_continue_without_language(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await event.answer()
    language_code = 'skip'

    # Extract audio_key from callback payload
    audio_key = event.callback.payload.replace('cont_lang_', '')

    try:
        await _prepare_and_process_audio(
            message=None,
            context=context,
            i18n=i18n,
            language_code=language_code,
            audio_key=audio_key,
            delete_message_mid=event.message.body.mid,
        )
    except ValueError as e:
        logger.error(f"Failed to process audio: {e}")
        await event.message.answer(text=i18n.something_went_wrong())
        await context.clear()


# ---------------------------------------------------------------------------
# Handler: Non-text in dialogue state → redirect to audio processing
# ---------------------------------------------------------------------------

@router.message_created(UserAudioSession.dialogue)
async def process_dialogue_or_media(event: MessageCreated, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    """Handle messages in dialogue state. Text → chat, non-text → audio processing."""
    message = event.message
    text = message.body.text if message.body else None
    logger.info(f"process_dialogue_or_media: state=dialogue, has_text={bool(text)}, user={message.sender.user_id}")

    if text and not is_valid_video_url(text):
        # Text message → process as dialogue
        await _handle_dialogue(message, context, user, i18n)
    else:
        # Non-text or URL → redirect to audio processing
        await _handle_audio_in_dialogue(message, context, i18n)


async def _handle_audio_in_dialogue(message: Message, context: MemoryContext, i18n: TranslatorRunner):
    """Handle non-text messages in dialogue state as audio input."""
    user: dict = await get_user(telegram_id=message.sender.user_id)
    if user:
        username = user.get('username', '') or ''
        if 'fastsaver' in username:
            return

    if user['subscription'] != 'True' and user['audio_uses'] >= 50 and user['subscription'] != 'trial':
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_limit_exceeded',
            action_category='conversion',
            metadata={'audio_uses': user['audio_uses'], 'limit': 50, 'source': 'audio_upload', 'shown_subscription_menu': True},
        )
        await message.answer(text=i18n.free_audio_limit_exceeded(), attachments=[subscription_menu(i18n)])
        return

    data = await context.get_data()
    language_code = data.get('language_code')
    await context.update_data(language_code=None)

    await _prepare_and_process_audio(
        message=message, context=context, i18n=i18n, language_code=language_code,
    )


async def _handle_dialogue(message: Message, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    """Handle text dialogue with the LLM in dialogue state."""
    text = message.body.text

    data = await context.get_data()
    ctx = data.get('context')
    if not ctx:
        logger.warning(f"_handle_dialogue: no 'context' in FSM data for user {message.sender.user_id} — sending unsupported format")
        await message.answer(text=i18n.please_send_audio_or_video())
        return
    anonymous_chat_session = data.get('anonymous_chat_session')
    session_id = data.get('session_id')

    # Check chat message limit (20 per session)
    if session_id:
        chat_requests_count = await count_user_chat_requests_by_session(user['id'], session_id)
        if chat_requests_count >= 20:
            await message.answer(text=i18n.warning_message_limit())
            await context.set_state(None)
            return

    message_order = len(ctx) + 1

    # Log user question
    if anonymous_chat_session:
        try:
            await log_anonymous_chat_message(
                chat_session=anonymous_chat_session,
                message_from='user',
                text=text,
                message_order=message_order,
            )
        except Exception as e:
            logger.warning(f"Failed to log anonymous user message: {e}")

    ctx.append({'role': 'user', 'content': text})
    raw_waiting = await _reply(message, text=i18n.wait_for_chat_response(dots="."))
    if raw_waiting is None:
        return
    waiting_message = MaxMessageCompat(raw_waiting)
    await context.set_state(UserAudioSession.user_wait)

    # Dots animation parallel to LLM request
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
                await waiting_message.edit(text=i18n.wait_for_chat_response(dots=dots))
            except Exception:
                pass

    animation_task = asyncio.create_task(animate_dots())

    try:
        model_answer: str = await process_chat_request(context=ctx, user=user, i18n=i18n, session_id=data.get('session_id'))
    finally:
        animation_running = False
        animation_task.cancel()
        try:
            await animation_task
        except asyncio.CancelledError:
            pass

    if model_answer is False or model_answer == '':
        await message.answer(text=i18n.something_went_wrong())
        return

    try:
        ctx = await send_dialogue_result(message=waiting_message, i18n=i18n, model_answer=model_answer, context=ctx)
    except Exception as e:
        logger.error(f"Error sending dialogue result with HTML: {e}")
        try:
            await waiting_message.delete()
            import re
            plain_text = re.sub(r'<[^>]+>', '', model_answer)
            if len(plain_text) > 4096:
                parts = [plain_text[i:i + 4000] for i in range(0, len(plain_text), 4000)]
                for part in parts[:-1]:
                    await message.answer(text=part)
                await message.answer(text=parts[-1])
            else:
                await message.answer(text=plain_text)
            if ctx:
                ctx.append({'role': 'assistant', 'content': model_answer})
        except Exception as e2:
            logger.error(f"Error sending dialogue result as plain text: {e2}")
            await message.answer(text=i18n.something_went_wrong())
            return

    # Log assistant response
    if anonymous_chat_session and model_answer:
        try:
            await log_anonymous_chat_message(
                chat_session=anonymous_chat_session,
                message_from='assistant',
                text=model_answer,
                message_order=message_order + 1,
            )
        except Exception as e:
            logger.warning(f"Failed to log anonymous assistant message: {e}")

    await add_gpt_use(message.sender.user_id)
    await context.set_state(UserAudioSession.dialogue)
    await context.update_data(context=ctx)


# ---------------------------------------------------------------------------
# Handler: Ask questions (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('ask_questions'))
async def process_you_can_ask(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    payload = event.callback.payload

    if '|' in payload:
        callback_parts = payload.split('|')
        session_id = callback_parts[1]
        transcript_data: dict = await get_transcription_data(session_id=session_id)

        chat_session = str(uuid.uuid4())
        await context.update_data(anonymous_chat_session=chat_session)

        try:
            await log_anonymous_chat_message(
                chat_session=chat_session,
                message_from='user',
                text=transcript_data['raw_transcript'],
                message_order=1,
            )
        except Exception as e:
            logger.warning(f"Failed to log anonymous chat message: {e}")
    elif ':' in payload:
        callback_parts = payload.split(':')
        chat_session = callback_parts[1]
    else:
        chat_session = None

    await event.answer()

    if chat_session:
        transcript = await get_transcript_by_chat_session(chat_session)
        if transcript:
            new_context = [{'role': 'user', 'content': f"{i18n.audio_text_prefix()} {transcript}"}]
            await context.update_data(
                context=new_context,
                anonymous_chat_session=chat_session,
                session_id=None,
            )
            await context.set_state(UserAudioSession.dialogue)
            await _reply(event.message, text=i18n.you_can_ask_questions())
        else:
            await event.message.answer(text=i18n.something_went_wrong())
    else:
        await event.message.answer(text=i18n.you_can_ask_questions())


# ---------------------------------------------------------------------------
# Handler: Get video (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('get_video'))
async def process_get_video(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    payload = event.callback.payload
    callback_parts = payload.split(':')

    if len(callback_parts) > 1:
        session_id = callback_parts[1]
        session_data = await get_processing_session_by_id(session_id)
        if not session_data:
            await event.message.answer(text=i18n.video_send_error_no_data())
            return
        if session_data['user_id'] != user['id']:
            await event.message.answer(text=i18n.video_send_error_no_data())
            return
        url = session_data['original_identifier']
        source_type = session_data['source_type']
        if source_type not in ('url', 'max'):
            await event.message.answer(text=i18n.video_send_error_no_data())
            return
    else:
        data = await context.get_data()
        url: str | None = data.get('video_url')
        session_id = None
        source_type = 'url'
        logger.warning('No session id')

    if url:
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
            session_id=session_id if session_id else None,
        )

        video_message = await _answer(event.message, text=i18n.downloading_video())
        retries = 0
        video_path: str | None = None
        download_method = 'fedor_api'

        try:
            video_data: dict = await download_file_fedor_api(
                url, user_data=user, session_id=session_id,
                result_content_type='video', destination_type='disk', add_file_size_to_session=True,
            )
            video_path = video_data['file_path']
        except Exception as e:
            logger.error(f"Error downloading video {url} from Fedor API (process_get_video): {e}")

        if not video_path:
            download_method = 'fallback'
            while retries < 5:
                try:
                    video_path: str = await get_content_from_url(url, user_data=user, download_mode='video', destination_type='disk')
                    break
                except Exception as e:
                    logger.error(f"Error downloading video {url}. Attempt {retries + 1}: {e}")
                    retries += 1
                    continue

        if retries == 5:
            logger.error(f"Error downloading full video: {url}")
            await log_user_action_async(
                user_id=user['id'], action_type='video_download_failed',
                action_category='content_processing',
                metadata={'trigger_source': 'get_video_callback', 'url_domain': extract_domain_from_url(url),
                          'download_method': download_method, 'retries': retries,
                          'error_message': 'Max retries exceeded',
                          'duration_ms': int((time.time() - start_time) * 1000)},
                session_id=session_id if session_id else None,
            )
            if video_message:
                try:
                    await video_message.edit(text=i18n.video_download_error())
                except Exception:
                    pass
            return

        try:
            if len(callback_parts) > 1:
                filename = "video"
            else:
                data = await context.get_data()
                filename = data.get("file_name", "video")

            video_to_send = InputMedia(path=video_path)
            download_keyboard = inline_download_file(i18n=i18n, session_id=callback_parts[1] if len(callback_parts) > 1 else None)
            await event.message.answer(text=i18n.requested_video(), attachments=[video_to_send, download_keyboard])

            await log_user_action_async(
                user_id=user['id'], action_type='video_download_completed',
                action_category='content_processing',
                metadata={'trigger_source': 'get_video_callback', 'url_domain': extract_domain_from_url(url),
                          'download_method': download_method, 'retries': retries,
                          'duration_ms': int((time.time() - start_time) * 1000)},
                session_id=session_id if session_id else None,
            )
        except Exception as e:
            logger.error(f"Error sending video {url}: {e}")
            await log_user_action_async(
                user_id=user['id'], action_type='video_download_failed',
                action_category='content_processing',
                metadata={'trigger_source': 'get_video_callback', 'url_domain': extract_domain_from_url(url),
                          'download_method': download_method, 'retries': retries,
                          'error_message': f'Send error: {str(e)[:150]}',
                          'duration_ms': int((time.time() - start_time) * 1000)},
                session_id=session_id if session_id else None,
            )
            if video_message:
                try:
                    await video_message.edit(text=i18n.video_send_error())
                except Exception:
                    pass
        finally:
            await delete_file(video_path)
    else:
        await event.message.answer(text=i18n.video_send_error_no_data())


# ---------------------------------------------------------------------------
# Handler: Download file (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('download_file'))
async def process_download_video(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    payload = event.callback.payload
    callback_parts = payload.split(':')

    if len(callback_parts) > 1:
        session_id = callback_parts[1]
        session_data = await get_processing_session_by_id(session_id)
        if not session_data:
            await event.message.answer(text=i18n.video_send_error_no_data())
            return
        if session_data['user_id'] != user['id']:
            await event.message.answer(text=i18n.video_send_error_no_data())
            return
        url = session_data['original_identifier']
        source_type = session_data['source_type']
        if source_type not in ('url', 'max'):
            await event.message.answer(text=i18n.video_send_error_no_data())
            return
    else:
        data = await context.get_data()
        url: str | None = data.get('video_url')
        session_id = None
        source_type = 'url'
        logger.warning('No session id')

    # Remove keyboard from original message
    try:
        await event.message.edit(text=event.message.body.text, attachments=[])
    except Exception:
        pass

    if url:
        start_time = time.time()
        await log_user_action_async(
            user_id=user['id'],
            action_type='file_download_started',
            action_category='content_processing',
            metadata={'trigger_source': 'download_file_callback', 'url_domain': extract_domain_from_url(url), 'source_type': source_type},
            session_id=session_id if session_id else None,
        )

        video_message = await _answer(event.message, text=i18n.downloading_video())
        retries = 0
        video_path: str | None = None
        download_method = 'fedor_api'

        try:
            video_data: dict = await download_file_fedor_api(
                url, user_data=user, session_id=session_id,
                result_content_type='video', destination_type='disk', add_file_size_to_session=True,
            )
            video_path = video_data['file_path']
        except Exception as e:
            logger.error(f"Error downloading video {url} from Fedor API (process_download_video): {e}")

        if not video_path:
            download_method = 'fallback'
            while retries < 5:
                try:
                    video_path: str = await get_content_from_url(url, user_data=user, download_mode='video', destination_type='disk')
                    break
                except Exception as e:
                    logger.error(f"Error downloading video {url}. Attempt {retries + 1}: {e}")
                    retries += 1
                    continue

        if retries == 5:
            logger.error(f"Error downloading full video: {url}")
            await log_user_action_async(
                user_id=user['id'], action_type='file_download_failed',
                action_category='content_processing',
                metadata={'trigger_source': 'download_file_callback', 'url_domain': extract_domain_from_url(url),
                          'download_method': download_method, 'retries': retries,
                          'error_message': 'Max retries exceeded',
                          'duration_ms': int((time.time() - start_time) * 1000)},
                session_id=session_id if session_id else None,
            )
            if video_message:
                try:
                    await video_message.edit(text=i18n.video_download_error())
                except Exception:
                    pass
            return

        try:
            if len(callback_parts) > 1:
                filename = "video"
            else:
                data = await context.get_data()
                filename = data.get("file_name", "video")

            video_to_send = InputMedia(path=video_path)
            await event.message.answer(text=i18n.requested_video(), attachments=[video_to_send])

            await log_user_action_async(
                user_id=user['id'], action_type='file_download_completed',
                action_category='content_processing',
                metadata={'trigger_source': 'download_file_callback', 'url_domain': extract_domain_from_url(url),
                          'download_method': download_method, 'retries': retries,
                          'duration_ms': int((time.time() - start_time) * 1000)},
                session_id=session_id if session_id else None,
            )
        except Exception as e:
            logger.error(f"Error sending video {url}: {e}")
            await log_user_action_async(
                user_id=user['id'], action_type='file_download_failed',
                action_category='content_processing',
                metadata={'trigger_source': 'download_file_callback', 'url_domain': extract_domain_from_url(url),
                          'download_method': download_method, 'retries': retries,
                          'error_message': f'Send error: {str(e)[:150]}',
                          'duration_ms': int((time.time() - start_time) * 1000)},
                session_id=session_id if session_id else None,
            )
            if video_message:
                try:
                    await video_message.edit(text=i18n.video_send_error())
                except Exception:
                    pass
        finally:
            await delete_file(video_path)
    else:
        await event.message.answer(text=i18n.video_send_error_no_data())


# ---------------------------------------------------------------------------
# Handler: Get full transcription (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('get_full_transcription|'))
async def process_get_full_transcription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    payload = event.callback.payload
    callback_parts = payload.split('|')
    session_id = callback_parts[1]
    transcription_data: dict | None = await get_transcription_data(session_id)

    if not transcription_data:
        await event.message.answer(text=i18n.transcription_not_found())
        return

    try:
        file_name = await generate_title(text=transcription_data['raw_transcript'], user=user, i18n=i18n)
    except Exception as e:
        logger.error(f"Failed to generate title for transcription: {e}")
        file_name = transcription_data['raw_transcript'][:25]

    # TODO: Remove the specific button from keyboard (Max doesn't support inline_keyboard mutation the same way)

    await send_transcription(
        message=event.message, i18n=i18n,
        transcription_raw=transcription_data['raw_transcript'],
        transcription_timecoded=transcription_data['timecoded_transcript'],
        file_name=file_name,
        transcription_format=user['transcription_format'],
        session_id=session_id,
        no_summary=False,
    )

    await log_user_action_async(
        user_id=user['id'],
        action_type='full_transcription_requested',
        action_category='feature',
        session_id=session_id if session_id else None,
    )


# ---------------------------------------------------------------------------
# Handler: Get summary (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('get_summary|'))
async def process_get_summary(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    payload = event.callback.payload
    callback_parts = payload.split('|')
    session_id = callback_parts[1]

    try:
        transcription_data: dict | None = await get_transcription_data(session_id)
        if not transcription_data:
            await event.message.answer(text=i18n.transcription_not_found())
            return

        # TODO: Remove specific buttons from keyboard

        waiting_message = await _reply(event.message, text=i18n.waiting_message_summary_in_process())

        summary_data: dict = await summarise_text(
            text=transcription_data['raw_transcript'], user=user, i18n=i18n,
            session_id=session_id, transcription_id=transcription_data['transcription_id'],
        )

        summary = summary_data['summary_text']
        if waiting_message:
            await waiting_message.delete()
        await send_summary(
            message=event.message, context=context, i18n=i18n, summary=summary,
            is_link=False, session_id=session_id,
        )
    except Exception:
        await event.message.answer(text=i18n.something_went_wrong())


# ---------------------------------------------------------------------------
# Handler: Cancel queue item (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('cancel_queue|'))
async def process_cancel_queue(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await event.answer()
    message_id = event.callback.payload.replace('cancel_queue|', '')
    await max_audio_queue_manager.remove_from_queue(user_id=event.callback.user.user_id, message_id=message_id)
    await max_audio_queue_manager.update_queue_count_in_messages(user_id=event.callback.user.user_id, i18n=i18n)
    await event.message.delete()
    await event.answer(text=i18n.queue_cancelled())


# ---------------------------------------------------------------------------
# Handler: Notetaker menu (callback)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'notetaker_menu')
async def notetaker_menu(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await event.answer()
    await event.message.answer(
        text=i18n.notetaker_menu(),
        attachments=[notetaker_menu_keyboard(i18n=i18n)],
    )


# ---------------------------------------------------------------------------
# Handler: Catch-all for messages without state
# ---------------------------------------------------------------------------

@router.message_created()
async def process_text_last(event: MessageCreated, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    """Catch-all handler: route text to dialogue if context exists, else treat as audio input."""
    message = event.message
    text = message.body.text if message.body else None

    if text and not is_valid_video_url(text):
        data = await context.get_data()
        if 'context' in data:
            logger.info(f"process_text_last: text message with dialogue context — routing to dialogue")
            await _handle_dialogue(message, context, user, i18n)
            return

    await _handle_audio_in_dialogue(event.message, context, i18n)


# ############################################################################
# Output functions
# ############################################################################

async def send_dialogue_result(message: Message, i18n: TranslatorRunner, model_answer: str, context: list[dict] | None = None):
    """Send dialogue result to the user, splitting if too long."""
    await message.delete()
    if context:
        context.append({'role': 'assistant', 'content': model_answer})

    model_answer_formatted = await replace_markdown_bold_with_html(model_answer)
    model_answer_formatted = await sanitize_html_for_telegram(model_answer_formatted)
    final_response = model_answer_formatted

    if len(final_response) > 4096:
        parts = await split_summary(final_response)
        for part in parts[:-1]:
            await message.answer(text=part, parse_mode=ParseMode.HTML)
        await message.answer(
            text=parts[-1],
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            text=final_response,
            parse_mode=ParseMode.HTML,
        )

    return context


async def send_summary(
    message: Message, context: MemoryContext, i18n: TranslatorRunner,
    summary: str, is_link: bool,
    chat_session: str = None, session_id: str = None,
):
    """Send summary to user, splitting if too long."""
    if summary == '':
        await _reply(message, text=i18n.no_voice_summary(),
                     attachments=[inline_new_session(i18n, is_summary=True, download_button=is_link,
                                                     chat_session=chat_session, session_id=session_id)])
        return

    full_message_formatted = await replace_markdown_bold_with_html(summary)
    full_message_formatted = await sanitize_html_for_telegram(full_message_formatted)

    # TODO: Replace with Max bot URL when available
    made_by_text = 'Whisper AI'
    full_message = i18n.voice_summary(summary=full_message_formatted, bot_link=made_by_text)

    if len(full_message) > 4096:
        parts = await split_summary(summary)

        first_part_formatted = await replace_markdown_bold_with_html(parts[0])
        first_part_formatted = await sanitize_html_for_telegram(first_part_formatted)
        await message.answer(text=i18n.voice_summary_first_part(summary=first_part_formatted, bot_link=made_by_text))

        for part in parts[1:-1]:
            part_formatted = await replace_markdown_bold_with_html(part)
            part_formatted = await sanitize_html_for_telegram(part_formatted)
            await message.answer(text=i18n.voice_summary_next_part(summary=part_formatted))

        last_part_formatted = await replace_markdown_bold_with_html(parts[-1])
        last_part_formatted = await sanitize_html_for_telegram(last_part_formatted)
        await message.answer(
            text=i18n.voice_summary_last_part(summary=last_part_formatted),
            attachments=[inline_new_session(i18n, is_summary=True, download_button=is_link,
                                            chat_session=chat_session, session_id=session_id)],
        )
    else:
        await message.answer(
            text=full_message,
            attachments=[inline_new_session(i18n, is_summary=True, download_button=is_link,
                                            chat_session=chat_session, session_id=session_id)],
        )


async def split_summary(text: str, max_length: int = 3500) -> list[str]:
    """Split text into parts of up to max_length, preferring sentence boundaries."""
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
    """Split text by sentences up to max_length."""
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


async def send_transcription(
    message: Message,
    i18n: TranslatorRunner,
    transcription_raw: str,
    transcription_timecoded: str,
    file_name: str,
    transcription_format: str = 'google_docs',
    chat_session: str = None,
    no_summary: bool = False,
    session_id: str = None,
    audio_file_source_type: str = None,
    is_link: bool = False,
):
    """Send transcription to user in the specified format."""
    if transcription_raw == '':
        await _reply(
            message, text=i18n.no_voice_summary(),
            attachments=[inline_new_session(i18n, is_summary=True, download_button=False, chat_session=chat_session)],
        )
        return

    if no_summary and len(transcription_raw) < 2000 and audio_file_source_type in ['voice', 'video_note']:
        # Short transcription — send inline without file
        made_with_hyperlink = f'{i18n.transcription_file_made_with_prefix()} Whisper AI'
        await _reply(
            message,
            text=i18n.short_transcription_template(transcription_text=transcription_raw, made_with_hyperlink=made_with_hyperlink),
            attachments=[transcription_no_summary_keyboard(i18n, session_id, show_video_button=is_link)],
        )
        return

    if transcription_format == 'google_docs':
        try:
            clean_doc_url, full_doc_url = await create_two_google_docs_lite(
                title=file_name,
                clean_transcript=transcription_raw,
                full_transcript=transcription_timecoded,
                i18n=i18n,
            )
            if clean_doc_url and full_doc_url:
                google_docs_hyperlink_1 = f'<a href="{clean_doc_url}">— Google Docs</a>'
                google_docs_hyperlink_2 = f'<a href="{full_doc_url}">— Google Docs</a>'

                clean_link_text = i18n.google_docs_first_transcription(google_docs_hyperlink=google_docs_hyperlink_1)
                full_link_text = i18n.google_docs_second_transcription(google_docs_hyperlink=google_docs_hyperlink_2)

                await _reply(message, text=clean_link_text)
                if no_summary:
                    await message.answer(
                        text=full_link_text,
                        attachments=[transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link)],
                    )
                else:
                    await message.answer(text=full_link_text)
                return
            else:
                logger.warning("Google Docs creation failed, falling back to file")
        except Exception as e:
            logger.error(f"Error creating Google Doc: {e}")

    elif transcription_format in ['pdf', 'docx', 'md', 'txt']:
        from services.services import create_two_input_files_from_text

        clean_file_tg, full_file_tg = await create_two_input_files_from_text(
            full_transcript=transcription_timecoded,
            clean_transcript=transcription_raw,
            filename=file_name,
            i18n=i18n,
            format_type=transcription_format,
        )
        clean_file = _bf_to_max(clean_file_tg)
        full_file = _bf_to_max(full_file_tg)

        await _reply(message, text=i18n.file_transcription_ready_1(), attachments=[clean_file])
        if no_summary:
            await message.answer(
                text=i18n.file_transcription_ready_2(),
                attachments=[full_file, transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link)],
            )
        else:
            await message.answer(text=i18n.file_transcription_ready_2(), attachments=[full_file])
        return

    # Fallback to file format (docx → txt)
    from services.services import create_two_input_files_from_text

    try:
        try:
            clean_file_tg, full_file_tg = await create_two_input_files_from_text(
                full_transcript=transcription_timecoded,
                clean_transcript=transcription_raw,
                filename=file_name,
                i18n=i18n,
                format_type='docx',
            )
            clean_file = _bf_to_max(clean_file_tg)
            full_file = _bf_to_max(full_file_tg)

            await _reply(message, text=i18n.file_transcription_ready_clean(), attachments=[clean_file])
            if no_summary:
                await message.answer(
                    text=i18n.file_transcription_ready_full(),
                    attachments=[full_file, transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link)],
                )
            else:
                await message.answer(text=i18n.file_transcription_ready_full(), attachments=[full_file])
        except Exception:
            clean_file_tg, full_file_tg = await create_two_input_files_from_text(
                full_transcript=transcription_timecoded,
                clean_transcript=transcription_raw,
                filename=file_name,
                i18n=i18n,
                format_type='txt',
            )
            clean_file = _bf_to_max(clean_file_tg)
            full_file = _bf_to_max(full_file_tg)

            await _reply(message, text=i18n.file_transcription_ready_1(), attachments=[clean_file])
            if no_summary:
                await message.answer(
                    text=i18n.file_transcription_ready_2(),
                    attachments=[full_file, transcription_no_summary_keyboard(i18n, session_id, get_file_button=False, show_video_button=is_link)],
                )
            else:
                await message.answer(text=i18n.file_transcription_ready_2(), attachments=[full_file])
            return
    except Exception as e:
        logger.error(f"Failed to send transcript files: {e}")
        await _reply(message, text=i18n.something_went_wrong())


# ############################################################################
# Cached / uncached transcription processing
# ############################################################################

async def _process_cached_transcription(
    cached_transcription: dict, user: dict, i18n: TranslatorRunner, session_id: str,
    message: Message, context: MemoryContext, waiting_message: Message,
    progress_manager: DynamicProgressManager, audio_file_source_type: str = None,
):
    """Process a cached transcription."""
    LLM_TIMEOUT = 120  # seconds — prevents 53-minute OS-level TCP timeouts

    logger.info(f"Cache HIT for transcription: session={session_id}, transcription_id={cached_transcription['id']}")
    raw_transcript = cached_transcription['transcript_raw']
    timecoded_transcript = cached_transcription['transcript_timecoded']
    transcription_id = cached_transcription['id']
    audio_duration = cached_transcription.get('audio_duration')
    original_file_size = cached_transcription.get('file_size_bytes', 0)

    await update_processing_session(
        session_id=session_id,
        transcription_id=transcription_id,
    )
    logger.info(f"[cached] session={session_id}: updated processing session")

    target_date = datetime(2025, 12, 9, 7, 0, 0)
    should_skip_summary = False

    if user['created_at'] > target_date:
        should_skip_summary = True

    if len(raw_transcript) < 2000 and audio_file_source_type in ['voice', 'video_note']:
        should_skip_summary = True

    logger.info(f"[cached] session={session_id}: should_skip_summary={should_skip_summary}")

    if should_skip_summary:
        try:
            logger.info(f"[cached] session={session_id}: calling generate_title (timeout={LLM_TIMEOUT}s)")
            file_name = await asyncio.wait_for(
                generate_title(text=raw_transcript, user=user, i18n=i18n),
                timeout=LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"generate_title timed out after {LLM_TIMEOUT}s for session={session_id}")
            file_name = "transcript"
        except Exception as e:
            logger.error(f"Failed to generate title for cached transcription: {e}")
            file_name = "transcript"

        logger.info(f"[cached] session={session_id}: skip_summary path done, file_name={file_name!r}")
        return {
            'raw_transcript': raw_transcript,
            'timecoded_transcript': timecoded_transcript,
            'transcription_id': transcription_id,
            'audio_duration': audio_duration,
            'summary': None,
            'file_name': file_name,
        }

    # Check summary cache
    actual_system_prompt = i18n.summarise_text_base_system_prompt_openai() if user['llm_model'] == 'gpt-4o' else i18n.summarise_text_system_prompt_gpt_oss()

    logger.info(f"[cached] session={session_id}: checking summary cache")
    cached_summary = await find_cached_summary(
        transcription_id=transcription_id,
        language_code=user['user_language'],
        llm_model=user['llm_model'],
        system_prompt=actual_system_prompt,
    )
    file_name = None

    if cached_summary:
        logger.info(f"Cache HIT for summary: session={session_id}, summary_id={cached_summary['id']}")
        voice_summary = cached_summary['summary_text']
        file_name = cached_summary.get('generated_title')
    else:
        logger.info(f"Cache MISS for summary: session={session_id}, generating new summary (timeout={LLM_TIMEOUT}s)")
        try:
            summary_data: dict = await asyncio.wait_for(
                summarise_text(
                    text=raw_transcript, user=user, i18n=i18n,
                    session_id=session_id, transcription_id=transcription_id,
                ),
                timeout=LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"summarise_text timed out after {LLM_TIMEOUT}s for session={session_id}")
            return {
                'raw_transcript': raw_transcript,
                'timecoded_transcript': timecoded_transcript,
                'transcription_id': transcription_id,
                'summary': None,
                'file_name': None,
                'audio_duration': audio_duration,
                'original_file_size': original_file_size,
            }
        except Exception as e:
            logger.error(f"Error generating summary: {e}")
            return {
                'raw_transcript': raw_transcript,
                'timecoded_transcript': timecoded_transcript,
                'transcription_id': transcription_id,
                'summary': None,
                'file_name': None,
                'audio_duration': audio_duration,
                'original_file_size': original_file_size,
            }
        voice_summary = summary_data['summary_text']
        file_name = summary_data.get('generated_title')

    if not file_name:
        try:
            logger.info(f"[cached] session={session_id}: generating title (timeout={LLM_TIMEOUT}s)")
            file_name = await asyncio.wait_for(
                generate_title(text=raw_transcript, user=user, i18n=i18n),
                timeout=LLM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(f"generate_title timed out after {LLM_TIMEOUT}s for session={session_id}")
            file_name = "transcript"
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
        'original_file_size': original_file_size,
    }


async def _process_uncached_transcription(
    waiting_message: Message, user: dict, i18n: TranslatorRunner, session_id: str,
    message: Message, context: MemoryContext, progress_manager: DynamicProgressManager,
    language_code: str, is_link: bool = False, file_name: str = None,
    url: str = None, audio=None, transcript_id: str = None,
    is_document: bool = False, original_identifier: str | None = None,
    use_quality_model: bool = False, audio_file_source_type: str = None,
):
    """Process an uncached transcription (Max version).

    Key difference from Telegram version: Max attachments provide download URLs,
    so we always use source_type='url' for download_file (no 'telegram' source_type).
    """
    try:
        audio_duration: float | None = None
        file_size: int | None = None
        audio_buffer: bytes | None = None
        file_path: str | None = None
        temp_files: list = []

        # Determine download URL for non-link content (Max attachment URL)
        download_url = url  # For links, this is the user-provided URL
        if not is_link and audio:
            download_url = _get_attachment_url(audio)
            if not download_url:
                raise ValueError("Could not extract download URL from Max attachment")

        if not is_link:
            await progress_manager.start_phase(ProgressPhase.DOWNLOADING, 5)

            file_path: str = await download_file(
                source_type='url',
                identifier=download_url,
                destination_type='disk',
                user_data=user,
                session_id=session_id,
                download_method='max_attachment',
                add_file_size_to_session=True,
            )

            # Quick cache lookup by file hash
            try:
                cached_by_path = await find_cached_transcription_by_file_path(file_path)
            except Exception:
                cached_by_path = None
            if cached_by_path:
                result_cached = await _process_cached_transcription(
                    cached_transcription=cached_by_path,
                    user=user, i18n=i18n, session_id=session_id,
                    message=message, context=context,
                    waiting_message=waiting_message,
                    progress_manager=progress_manager,
                    audio_file_source_type=audio_file_source_type,
                )
                try:
                    await delete_file(file_path)
                except Exception:
                    pass
                return result_cached

            # Compute file hash for cache
            try:
                source_file_hash: str | None = await generate_file_hash_async(file_path=file_path)
            except Exception:
                source_file_hash = None

            original_download_path = file_path

            # Check if we need to extract audio from video
            is_video_file = audio_file_source_type in ('video', 'document')
            if is_video_file and file_name:
                ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
                is_video_file = ext in ('mp4', 'webm', 'avi', 'mov', 'mkv')

            if is_video_file:
                await progress_manager.start_phase(ProgressPhase.EXTRACTING_AUDIO, 20)
                try:
                    file_data: dict = await convert_file_fedor_api(
                        file_path=file_path, mode='video_to_audio',
                        destination_type='disk', user_data=user, session_id=session_id,
                    )
                    file_path: str = file_data['file_path']
                    audio_duration: float | None = file_data['result_data'].get('source_duration_seconds')
                    file_size: int | None = file_data['result_data'].get('file_size_mb')
                    await delete_file(original_download_path)
                except Exception as e:
                    logger.error(f"Error converting to mp3 by fedor_api: {e}")
                    logger.error("Trying convert_to_mp3 fallback")
                    file_path: str = await extract_audio_from_video(file_path=file_path, i18n=i18n, output='path')
                    await delete_file(original_download_path)
            else:
                await progress_manager.start_phase(ProgressPhase.CONVERTING_AUDIO, 20)
                converted_path: str | bytes = await convert_to_mp3(file_path=file_path, output='path')
                await delete_file(original_download_path)
                file_path = converted_path
                audio_buffer = None

            await progress_manager.update_progress(35)
        else:
            # URL link processing
            await progress_manager.start_phase(ProgressPhase.DOWNLOADING, 5)
            try:
                file_data: dict = await download_file_fedor_api(
                    file_url=download_url, user_data=user, session_id=session_id,
                    result_content_type='audio', destination_type='disk',
                    add_file_size_to_session=True,
                )
                file_path: str = file_data['file_path']
                audio_duration: float | None = file_data['result_data'].get('duration_seconds')
                file_size: int | None = file_data['result_data'].get('file_size_mb')
            except Exception as e:
                logger.error(f"Error downloading file from Fedor API: {e}")
                logger.error(f"Trying to download file from URL to disk: {download_url}")

                retries = 0
                file_path = None
                while retries < 5:
                    try:
                        if session_id:
                            await increment_download_attempts(session_id)
                        audio_buffer: bytes | str = await get_audio_from_url(download_url, user_data=user, session_id=session_id)
                        if isinstance(audio_buffer, str):
                            file_path = audio_buffer
                        break
                    except Exception as e:
                        retries += 1
                        logger.warning(f"Download attempt {retries} failed for URL {download_url}: {e}")
                        continue
                if not file_path and not audio_buffer:
                    raise Exception('Failed to download file to disk or buffer')

            await progress_manager.update_progress(35)

            # Quick cache lookup for URL case
            if file_path:
                try:
                    cached_by_path = await find_cached_transcription_by_file_path(file_path)
                except Exception:
                    cached_by_path = None
                if cached_by_path:
                    result_cached = await _process_cached_transcription(
                        cached_transcription=cached_by_path,
                        user=user, i18n=i18n, session_id=session_id,
                        message=message, context=context,
                        waiting_message=waiting_message,
                        progress_manager=progress_manager,
                        audio_file_source_type=audio_file_source_type,
                    )
                    try:
                        await delete_file(file_path)
                    except Exception:
                        pass
                    return result_cached

                try:
                    source_file_hash: str | None = await generate_file_hash_async(file_path=file_path)
                except Exception:
                    source_file_hash = None

        # Process language code
        if language_code == 'skip':
            language_code = None

        # Get audio duration and file size
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

        try:
            original_file_size = await get_file_size(file_path if file_path else (audio_buffer or b''))
        except Exception:
            original_file_size = file_size if isinstance(file_size, int) else 0

        # Transcription phase
        await progress_manager.start_phase(ProgressPhase.TRANSCRIBING, 39, audio_duration)

        processing_results: dict = await process_audio(
            file_bytes=io.BytesIO(audio_buffer) if audio_buffer else None,
            file_path=file_path if file_path else None,
            waiting_message=waiting_message,
            user=user, i18n=i18n,
            language_code=language_code,
            session_id=session_id,
            audio_length=audio_duration,
            progress_manager=progress_manager,
            file_data={
                'source_type': 'url' if is_link else 'max',
                'original_identifier': original_identifier,
                'specific_source': identify_url_source(url) if is_link else 'max_attachment',
                'original_file_size': original_file_size,
                'audio_duration': audio_duration,
                'file_hash': source_file_hash if 'source_file_hash' in locals() else None,
            },
            use_quality_model=use_quality_model,
            audio_file_source_type=audio_file_source_type if audio_file_source_type else None,
        )
        await delete_file(file_path)

        voice_summary = processing_results.get('summary')
        raw_transcript = processing_results.get('raw_transcript')
        timecoded_transcript = processing_results.get('timecoded_transcript')
        transcription_id = processing_results.get('transcription_id')
        generated_title = processing_results.get('generated_title')

        if file_name is None:
            if is_link:
                fetched_title: str | None = await get_video_title(url=url)
                if not fetched_title and not generated_title and raw_transcript:
                    try:
                        generated_title = await generate_title(text=raw_transcript, user=user, i18n=i18n)
                    except Exception as e:
                        logger.error(f"Failed to generate title: {e}")
                        generated_title = None
                file_name = fetched_title or generated_title or "transcript"
            elif is_document:
                file_name = '.'.join(file_name.split('.')[:-1]) if file_name else "transcript"
            else:
                if not generated_title and raw_transcript:
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
                'file_name': file_name,
            }

        return {
            'raw_transcript': raw_transcript,
            'timecoded_transcript': timecoded_transcript,
            'summary': voice_summary,
            'file_name': file_name,
            'transcription_id': transcription_id,
            'audio_duration': audio_duration,
        }

    except Exception as e:
        logger.error(f"Error processing audio with main workflow: {e}")
        if 'url' in locals() and url:
            try:
                result_data: dict | None = await process_audio_fedor_api(audio_url=url, session_id=session_id)
                if result_data:
                    raw_transcript = result_data['raw_transcript']
                    transcription_id = result_data['transcript_id']
                    timecoded_transcript = result_data['timecoded_transcript']
                else:
                    raise Exception(f'process_audio_fedor_api returned None')
                original_file_size = 0
                summary_data: dict = await summarise_text(
                    text=raw_transcript, user=user, i18n=i18n,
                    session_id=session_id, transcription_id=transcription_id,
                )
                voice_summary = summary_data['summary_text']

                return {
                    'raw_transcript': raw_transcript,
                    'timecoded_transcript': timecoded_transcript,
                    'summary': voice_summary,
                    'file_name': file_name,
                    'transcription_id': transcription_id,
                    'audio_duration': 0,
                }
            except Exception as fallback_error:
                logger.error(f"Fallback API also failed: {fallback_error}")
                raise e
        else:
            logger.error("Fedor API fallback only works for links")
            raise e
