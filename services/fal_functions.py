import asyncio
import logging
import os

import aiofiles
import fal_client
import base64
from io import BytesIO

from fluentogram import TranslatorRunner

from services.init_bot import config
from services.services import format_time, progress_bar

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

async def process_audio_fal(audio_bytes: bytes, waiting_message,
                            i18n: TranslatorRunner, language_code: str = None, suppress_progress: bool = False,
                            file_path: str = None) -> tuple[str, str]:
    os.environ['FAL_KEY'] = config.fal.api_key
    
    # Convert bytes to base64 string (without data URI prefix)
    if file_path:
        async with aiofiles.open(file_path, 'rb') as f:
            audio_bytes = await f.read()
    if not suppress_progress:
        await waiting_message.edit_text(text=i18n.transcribe_audio_progress(progress=progress_bar(39, i18n)))
    url = await fal_client.upload_async(data=audio_bytes, content_type='audio/wav', file_name='audio.wav')

    

    def on_queue_update(update):
        if isinstance(update, fal_client.InProgress):
            # print(f"In progress: {update}")
            if update.logs:
                for log in update.logs:
                    logger.info(log["message"])
    
    try:
        if not suppress_progress:
            await waiting_message.edit_text(text=i18n.transcribe_audio_progress(progress=progress_bar(39, i18n)))
    except:
        pass
    result = await fal_client.subscribe_async(
        "fal-ai/whisper",
        arguments={
            "audio_url": url,  # Pass the base64 data URI instead of URL
            'language_code': language_code,
            'diarize': True,
        },
        on_queue_update=on_queue_update,
    )

    # logger.info(f'Fal result: {result}')

    timecoded_text = ''

    for chunk in result['chunks']:
        start_time, end_time = format_time(chunk['timestamp'][0]), format_time(chunk['timestamp'][1])
        text = chunk['text']
        speaker = chunk['speaker']
        timecoded_text += f'[{start_time} - {end_time}] {speaker}\n{text}\n\n'

    timecoded_text.replace('Редактор субтитров А.Семкин Корректор А.Егорова', '')
    result['text'].replace('Редактор субтитров А.Семкин Корректор А.Егорова', '')
    
    return timecoded_text, result['text']