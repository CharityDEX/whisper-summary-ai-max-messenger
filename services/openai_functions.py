import io
from io import BytesIO
import logging

import aiofiles
import httpx
import requests
from fluentogram import TranslatorRunner
from openai import AsyncOpenAI

from lexicon import lexicon_ru
from services.init_bot import config
from services.services import calculate_progress, progress_bar, split_audio, convert_to_mp3, format_time



proxies = {'https://': config.proxy.proxy, 'http://': config.proxy.proxy}
http_client = httpx.AsyncClient(proxies=proxies, timeout=360)

client = AsyncOpenAI(
    api_key=config.openai.api_key,
    http_client=http_client,
)

logger = logging.getLogger(__name__)


def _build_timecoded_text_from_segments(segments: list[dict]) -> str:
    """Создает текст с таймкодами из segments OpenAI API"""
    timecoded_parts = []
    for segment in segments:
        start_time = segment.get('start', 0)
        end_time = segment.get('end', 0)
        text = segment.get('text', '').strip()

        if text:
            formatted_start = format_time(start_time)
            formatted_end = format_time(end_time)
            timecoded_parts.append(f'[{formatted_start} - {formatted_end}] SPEAKER\n{text}\n\n')

    return ''.join(timecoded_parts).strip()


async def audio_to_text(file_bytes: bytes, waiting_message, i18n: TranslatorRunner, suppress_progress: bool = False, file_path: str = None) -> tuple[str, str]:
    """
    Транскрибирует аудио через OpenAI Whisper API.

    Returns:
        tuple[str, str]: (timecoded_text, plain_text)
    """
    if file_path:
        async with aiofiles.open(file_path, 'rb') as f:
            file_bytes = await f.read()

    if len(file_bytes) > 26000000:
        if not suppress_progress:
            await waiting_message.edit_text(text=i18n.transcribe_audio_progress(progress=progress_bar(39, i18n)))
        # Контрольная точка 3.1.1. Разделяем аудио на части, если оно большое
        files_list: list[bytes] = await split_audio(file_bytes)
    else:
        files_list = [file_bytes]

    all_text = ''
    all_timecoded_text = ''

    # Контрольная точка 3.2. Отправляем по частям. Каждая часть отдельные проценты
    total_parts = len(files_list)
    for index, file_bytes in enumerate(files_list):
        progress = calculate_progress(index, total_parts)
        if not suppress_progress:
            if progress <= 39:
                await waiting_message.edit_text(text=i18n.transcribe_audio_progress(progress=progress_bar(progress, i18n)))
            elif progress == 40:
                await waiting_message.edit_text(text=i18n.transcribe_audio_progress_extracting(progress=progress_bar(progress, i18n)))
            elif 45 <= progress <= 79:
                await waiting_message.edit_text(text=i18n.transcribe_audio_progress_almost_done(progress=progress_bar(progress, i18n)))
            elif progress >= 80:
                await waiting_message.edit_text(text=i18n.transcribe_audio_progress_finishing(progress=progress_bar(progress, i18n)))

        file_bytes: bytes = await convert_to_mp3(file_bytes)
        with io.BytesIO(file_bytes) as audio_file:
            audio_file.seek(0)  # Сбрасываем указатель на начало буфера
            files = {'file': ('audio.mp3', audio_file, 'audio/mpeg')}

            # Использование HTTPX для выполнения запроса с таймкодами
            response = await http_client.post(
                'https://api.openai.com/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {config.openai.api_key}'},
                files=files,
                data={
                    'model': 'whisper-1',
                    'response_format': 'verbose_json',
                    'timestamp_granularities[]': 'segment'
                }
            )

            response_data = response.json()
            logger.debug(f'OpenAI response keys: {response_data.keys()}')

            # Получаем plain text
            plain_text = response_data.get('text', '')
            all_text += plain_text

            # Получаем segments и строим timecoded text
            segments = response_data.get('segments', [])
            if segments:
                timecoded_part = _build_timecoded_text_from_segments(segments)
                all_timecoded_text += timecoded_part + '\n\n'
            else:
                # Fallback если нет segments
                all_timecoded_text += f'[00:00 - 00:00] SPEAKER\n{plain_text}\n\n'

    if not suppress_progress:
        await waiting_message.edit_text(text=i18n.transcribe_audio_progress_finishing(progress=progress_bar(100, i18n)))

    logger.debug(f'OpenAI transcription completed. Text length: {len(all_text)}')

    return all_timecoded_text.strip(), all_text.strip()

async def summarise_text_openai(text: str, i18n: TranslatorRunner) -> str:
    message: str = i18n.summarise_text_base_system_prompt_openai() + i18n.text_prompt(text=text)

    resp = await client.responses.create(
        model="o4-mini",  # модель с 'Thinking' / reasoning
        input=[{"role": "user", "content": message}],
        reasoning={  # включаем размышление
            "effort": "low",  # 'none' | 'low' | 'medium' | 'high' | 'xhigh' (в 5.2 есть xhigh)
            "summary": "auto"  # попросить резюме рассуждений (auto / concise / detailed — см. доки)
        }
    )

    logger.debug(f'SUMMARY OPENAI: {resp.output_text}')

    return resp.output_text


async def chat_function(context: list[dict], i18n: TranslatorRunner,
                        model: str = 'gpt-4o',
                        ) -> str | bool:
    if context[0]['role'] != 'system':
        context.insert(0, {'role': 'system', 'content': i18n.chat_system_prompt_openai()})

    try:
        response = await client.responses.create(
            model="o4-mini",  # модель с 'Thinking' / reasoning
            input=context,
            reasoning={  # включаем размышление
                "effort": "low",  # 'none' | 'low' | 'medium' | 'high' | 'xhigh' (в 5.2 есть xhigh)
                "summary": "auto"  # попросить резюме рассуждений (auto / concise / detailed — см. доки)
            }
        )

        return response.output_text
    except Exception as e:
        logger.error(f'Ошибка с генерацией чата OpenAI: {e}')
        return False


async def prepare_language_code(text_language: str) -> str | None:
    message = ('Твоя задача - преобразовать название языка в код, который используется в Whisper. '
               'Название языка: ' + text_language + '\n'
               'Коды языков: ' + str(lexicon_ru.language_codes) + '\n'
               'Ты должен ответить только кодом языка.')
    response = await client.chat.completions.create(
        model='gpt-5-mini',
        messages=[{'role': 'user', 'content': message}],
    )
    result = response.choices[0].message.content
    logger.debug(f'PREPARE LANGUAGE CODE: {response.choices[0].message.content}')
    for language in lexicon_ru.language_codes.values():
        for code in language:
            if result == code:
                return code
    else:
        return None


async def generate_title_openai(text: str, i18n: TranslatorRunner) -> str:
    """
    Генерирует название для транскрипции через OpenAI.

    Args:
        text: Текст транскрипции
        i18n: TranslatorRunner для получения промпта на нужном языке

    Returns:
        str: Сгенерированное название
    """
    message = i18n.generate_title_system_prompt() + '\n' + i18n.title_prompt(text=text)

    resp = await client.chat.completions.create(
        model='gpt-5.2',
        messages=[{'role': 'user', 'content': message}],
        temperature=0.7,
        top_p=1.0
    )

    title = resp.choices[0].message.content.strip()
    logger.debug(f'TITLE GENERATION OPENAI: {title}')

    # Удаляем возможные префиксы, если LLM их добавила
    for prefix in ['TITLE:', 'Title:', 'НАЗВАНИЕ:', 'Название:', '"', "'"]:
        if title.startswith(prefix):
            title = title[len(prefix):].strip()
        if title.endswith('"') or title.endswith("'"):
            title = title[:-1].strip()

    # Ограничиваем длину до 90 символов
    if len(title) > 90:
        title = title[:87] + '...'

    return title



