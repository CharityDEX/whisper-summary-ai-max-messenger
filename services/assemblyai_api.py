import logging
import aiohttp
import asyncio
import aiofiles
from typing import Optional, Dict, Tuple
from fluentogram import TranslatorRunner

from services.services import progress_bar, format_time
from services.init_bot import config

logger = logging.getLogger(__name__)


class AssemblyAIClient:
    """
    Клиент для работы с API AssemblyAI.
    """

    def __init__(self, api_key: str):
        """
        Инициализация клиента.

        Args:
            api_key: API ключ AssemblyAI
        """
        self.api_key = api_key
        self.base_url = "https://api.assemblyai.com/v2"
        self.headers = {
            "Authorization": api_key,
        }

    async def upload_audio(self, audio_bytes: bytes) -> Optional[str]:
        """
        Загружает аудио файл в AssemblyAI и возвращает URL для транскрипции.

        Args:
            audio_bytes: Содержимое аудио файла

        Returns:
            Optional[str]: URL загруженного файла или None при ошибке
        """
        endpoint = f"{self.base_url}/upload"

        try:
            logger.debug("Uploading audio file to AssemblyAI")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    headers=self.headers,
                    data=audio_bytes
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        upload_url = result.get("upload_url")
                        logger.debug(f"Successfully uploaded audio file. URL: {upload_url}")
                        return upload_url
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to upload audio file. Status: {response.status}, "
                            f"Response: {error_text}"
                        )
                        return None

        except Exception as e:
            logger.error(f"Exception occurred while uploading audio file: {str(e)}")
            return None

    async def submit_transcription(
        self,
        audio_url: str,
        language_code: Optional[str] = None
    ) -> Optional[str]:
        """
        Отправляет запрос на транскрипцию.

        Args:
            audio_url: URL аудио файла
            language_code: Код языка (например, 'en', 'ru'). Если None - автоопределение

        Returns:
            Optional[str]: ID транскрипции или None при ошибке
        """
        endpoint = f"{self.base_url}/transcript"

        # Формируем payload с оптимальными настройками для качества
        payload = {
            "audio_url": audio_url,
            "speaker_labels": True,  # Диаризация спикеров
            "punctuate": True,  # Пунктуация
            "format_text": True,  # Форматирование текста
            "disfluencies": False,  # Убираем "эм", "а" и т.д.
            "speech_model": "best",  # Лучшая модель
        }

        # Настройка языка
        if language_code:
            payload["language_code"] = language_code
        else:
            payload["language_detection"] = True

        headers = {
            **self.headers,
            "Content-Type": "application/json"
        }

        try:
            logger.debug(f"Submitting transcription request to AssemblyAI: {payload}")

            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, headers=headers, json=payload) as response:
                    if response.status in (200, 201):
                        result = await response.json()
                        transcript_id = result.get("id")
                        logger.debug(f"Successfully submitted transcription. ID: {transcript_id}")
                        return transcript_id
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to submit transcription. Status: {response.status}, "
                            f"Response: {error_text}"
                        )
                        return None

        except Exception as e:
            logger.error(f"Exception occurred while submitting transcription: {str(e)}")
            return None

    async def get_transcript(self, transcript_id: str) -> Optional[Dict]:
        """
        Получает статус и результат транскрипции.

        Args:
            transcript_id: ID транскрипции

        Returns:
            Optional[Dict]: Данные транскрипции или None при ошибке
        """
        endpoint = f"{self.base_url}/transcript/{transcript_id}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to get transcript. Status: {response.status}, "
                            f"Response: {error_text}"
                        )
                        return None

        except Exception as e:
            logger.error(f"Exception occurred while getting transcript: {str(e)}")
            return None

    async def get_sentences(self, transcript_id: str) -> Optional[Dict]:
        """
        Получает разбивку транскрипции по предложениям.

        Args:
            transcript_id: ID транскрипции

        Returns:
            Optional[Dict]: Данные с предложениями или None при ошибке
        """
        endpoint = f"{self.base_url}/transcript/{transcript_id}/sentences"

        try:
            logger.debug(f"Fetching sentences for transcript {transcript_id}")
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=self.headers) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.debug(f"Successfully fetched {len(result.get('sentences', []))} sentences")
                        return result
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to get sentences. Status: {response.status}, "
                            f"Response: {error_text}"
                        )
                        return None

        except Exception as e:
            logger.error(f"Exception occurred while getting sentences: {str(e)}")
            return None

    async def wait_for_transcript(
        self,
        transcript_id: str,
        delay_seconds: int = 5,
        max_timeout: int = 3600
    ) -> Optional[Dict]:
        """
        Ожидает готовности транскрипции, периодически опрашивая API.

        Args:
            transcript_id: ID транскрипции
            delay_seconds: Задержка между опросами в секундах
            max_timeout: Максимальное время ожидания в секундах

        Returns:
            Optional[Dict]: Данные транскрипции или None при ошибке
        """
        logger.debug(f"Waiting for transcript {transcript_id}")

        counter = 0
        while counter < max_timeout:
            transcript_data = await self.get_transcript(transcript_id)

            if not transcript_data:
                logger.error("Failed to get transcript status")
                return None

            status = transcript_data.get("status")

            if status == "completed":
                logger.debug("Transcription completed successfully")
                # logger.debug(f'AssemlyAI. Transcription data {transcript_data}')
                return transcript_data
            elif status == "error":
                error_message = transcript_data.get("error")
                logger.error(f"Transcription failed: {error_message}")
                return None
            elif status in ["queued", "processing"]:
                logger.debug(
                    f"Current status: {status}, waiting {delay_seconds} seconds... "
                    f"(elapsed: {counter}/{max_timeout}s)"
                )
                await asyncio.sleep(delay_seconds)
                counter += delay_seconds
            else:
                logger.error(f"Unexpected status: {status}")
                return None

        logger.error(f"Timeout reached while waiting for transcript: {max_timeout} seconds")
        return None

    def format_transcript(self, transcript_data: Dict, sentences_data: Optional[Dict] = None) -> Tuple[str, str]:
        """
        Форматирует данные транскрипции в timecoded и plain text.

        Args:
            transcript_data: Данные транскрипции от AssemblyAI
            sentences_data: Опциональные данные с предложениями (для лучшей сегментации)

        Returns:
            Tuple[str, str]: (timecoded_text, plain_text)
        """
        timecoded_text = ''
        plain_text = transcript_data.get("text", "")

        # Приоритет 1: Используем sentences для лучшей сегментации
        if sentences_data:
            sentences = sentences_data.get("sentences", [])
            logger.debug(f"Formatting transcript using {len(sentences)} sentences")

            for sentence in sentences:
                start_time = sentence.get("start", 0) / 1000  # Конвертируем из мс в секунды
                end_time = sentence.get("end", 0) / 1000
                text = sentence.get("text", "")
                speaker = sentence.get("speaker", "UNKNOWN")

                # Форматируем спикера
                speaker_label = f"SPEAKER_{speaker}" if speaker != "UNKNOWN" else "SPEAKER"

                timecoded_line = (
                    f"[{format_time(start_time)} - {format_time(end_time)}] {speaker_label}\n"
                    f"{text}\n\n"
                )

                timecoded_text += timecoded_line
        else:
            # Приоритет 2: Fallback на utterances (для обратной совместимости)
            utterances = transcript_data.get("utterances", [])
            logger.debug(f"Formatting transcript using {len(utterances)} utterances (fallback)")

            if utterances:
                for utterance in utterances:
                    start_time = utterance.get("start", 0) / 1000
                    end_time = utterance.get("end", 0) / 1000
                    text = utterance.get("text", "")
                    speaker = utterance.get("speaker", "UNKNOWN")

                    speaker_label = f"SPEAKER_{speaker}" if speaker != "UNKNOWN" else "SPEAKER"

                    timecoded_line = (
                        f"[{format_time(start_time)} - {format_time(end_time)}] {speaker_label}\n"
                        f"{text}\n\n"
                    )

                    timecoded_text += timecoded_line
            else:
                # Приоритет 3: Последний fallback - используем words с группировкой по спикерам
                logger.warning("No sentences or utterances available, falling back to word-level grouping")
                words = transcript_data.get("words", [])
                if words:
                    current_speaker = None
                    current_text = []
                    current_start = None
                    current_end = None

                    for word in words:
                        speaker = word.get("speaker")

                        # Если сменился спикер, сохраняем предыдущий сегмент
                        if speaker != current_speaker and current_text:
                            speaker_label = f"SPEAKER_{current_speaker}" if current_speaker else "SPEAKER"
                            timecoded_line = (
                                f"[{format_time(current_start)} - {format_time(current_end)}] {speaker_label}\n"
                                f"{' '.join(current_text)}\n\n"
                            )
                            timecoded_text += timecoded_line
                            current_text = []

                        # Обновляем текущий сегмент
                        current_speaker = speaker
                        current_text.append(word.get("text", ""))

                        if current_start is None:
                            current_start = word.get("start", 0) / 1000
                        current_end = word.get("end", 0) / 1000

                    # Добавляем последний сегмент
                    if current_text:
                        speaker_label = f"SPEAKER_{current_speaker}" if current_speaker else "SPEAKER"
                        timecoded_line = (
                            f"[{format_time(current_start)} - {format_time(current_end)}] {speaker_label}\n"
                            f"{' '.join(current_text)}\n\n"
                        )
                        timecoded_text += timecoded_line

        return timecoded_text.strip(), plain_text.strip()

    async def process_audio(
        self,
        audio_bytes: Optional[bytes] = None,
        file_path: Optional[str] = None,
        waiting_message=None,
        i18n: TranslatorRunner = None,
        language_code: Optional[str] = None,
        suppress_progress: bool = False
    ) -> Optional[Tuple[str, str]]:
        """
        Полный процесс обработки аудио файла.

        Args:
            audio_bytes: Содержимое аудио файла (опционально, если передан file_path)
            file_path: Путь к аудио файлу (опционально, если переданы audio_bytes)
            waiting_message: Сообщение для обновления прогресса
            i18n: Переводчик для интернационализации
            language_code: Код языка или None для автоопределения
            suppress_progress: Отключить обновление прогресса

        Returns:
            Optional[Tuple[str, str]]: (timecoded_text, plain_text) или None при ошибке
        """
        try:
            # Шаг 0: Получение аудио данных
            if file_path:
                logger.debug(f"Reading audio from file: {file_path}")
                async with aiofiles.open(file_path, 'rb') as f:
                    buffer_data = await f.read()
            elif audio_bytes:
                buffer_data = audio_bytes
            else:
                raise ValueError("Either audio_bytes or file_path must be provided")

            # Шаг 1: Загрузка аудио
            if not suppress_progress:
                try:
                    await waiting_message.edit_text(
                        text=i18n.transcribe_audio_progress(progress=progress_bar(35, i18n))
                    )
                except:
                    pass

            upload_url = await self.upload_audio(buffer_data)
            if not upload_url:
                logger.error("Failed to upload audio to AssemblyAI")
                return None

            # Шаг 2: Отправка на транскрипцию
            if not suppress_progress:
                try:
                    await waiting_message.edit_text(
                        text=i18n.transcribe_audio_progress_extracting(progress=progress_bar(40, i18n))
                    )
                except:
                    pass

            transcript_id = await self.submit_transcription(upload_url, language_code)
            if not transcript_id:
                logger.error("Failed to submit transcription to AssemblyAI")
                return None

            # Шаг 3: Ожидание результата
            transcript_data = await self.wait_for_transcript(transcript_id, delay_seconds=5)
            if not transcript_data:
                logger.error("Failed to get transcription result from AssemblyAI")
                return None

            # Шаг 4: Получение предложений для лучшей сегментации
            sentences_data = await self.get_sentences(transcript_id)
            if not sentences_data:
                logger.warning("Failed to fetch sentences, will use utterances as fallback")

            # Шаг 5: Форматирование результата
            timecoded_text, plain_text = self.format_transcript(transcript_data, sentences_data)

            return timecoded_text, plain_text

        except Exception as e:
            logger.error(f"Failed to process audio with AssemblyAI: {e}")
            return None


# Создаем глобальный экземпляр клиента
assemblyai_client = AssemblyAIClient(config.assemblyai.api_key)


# Функция-обертка для совместимости с существующим кодом
async def process_audio_assemblyai(
    audio_bytes: Optional[bytes] = None,
    file_path: Optional[str] = None,
    waiting_message=None,
    i18n: TranslatorRunner = None,
    language_code: Optional[str] = None,
    suppress_progress: bool = False
) -> Optional[Tuple[str, str]]:
    """
    Функция-обертка для обработки аудио через AssemblyAI.

    Args:
        audio_bytes: Содержимое аудио файла (опционально, если передан file_path)
        file_path: Путь к аудио файлу (опционально, если переданы audio_bytes)
        waiting_message: Сообщение для обновления прогресса
        i18n: Переводчик для интернационализации
        language_code: Код языка или None для автоопределения
        suppress_progress: Отключить обновление прогресса

    Returns:
        Optional[Tuple[str, str]]: (timecoded_text, plain_text) или None при ошибке
    """
    return await assemblyai_client.process_audio(
        audio_bytes=audio_bytes,
        file_path=file_path,
        waiting_message=waiting_message,
        i18n=i18n,
        language_code=language_code,
        suppress_progress=suppress_progress
    )
