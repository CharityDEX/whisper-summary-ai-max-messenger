import io
import logging
import traceback
import aiohttp
import asyncio
import os
from typing import Optional, Union
import aiofiles
from fluentogram import TranslatorRunner
import subprocess
import json
from mutagen import File

from services.services import progress_bar
from services.telegram_alerts import send_alert
from services.transcription_grouper import extract_plain_text, group_transcription_smart
from services.init_bot import config
logger = logging.getLogger(__name__)


# Max allowed file size: 20 GB
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024 * 1024

# Upload configuration
MAX_UPLOAD_RETRIES = 5
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB chunks
CONNECT_TIMEOUT = 10  # seconds
READ_TIMEOUT = 300  # seconds (5 minutes)
BACKOFF_BASE = 2  # exponential backoff factor


class PrivateSTTClient:
    """
    Клиент для работы с приватным STT API.
    """
    
    def __init__(self, api_url: str = config.private_stt.api_url, api_key: str = config.private_stt.api_key):
        """
        Инициализация клиента.
        
        Args:
            api_url: URL API для получения подписанных ссылок
        """
        self.api_url = api_url
        self.api_key = api_key

    def get_audio_duration(self, file_bytes: bytes) -> Optional[float]:
        """
        Получает длительность аудио файла в секундах с помощью ffprobe.
        
        Args:
            file_path: Путь к аудио файлу
            
        Returns:
            float: Длительность в секундах или None при ошибке
        """
        try:
            audio = File(io.BytesIO(file_bytes))
            return audio.info.length
        except Exception as e:
            logger.error(f"Exception while getting audio duration: {e}")
            return None

    
    def calculate_dynamic_timeout(self, duration_seconds: float) -> int:
        """
        Рассчитывает динамический таймаут на основе длительности аудио.
        Таймаут = длительность / 15 (в секундах).
        
        Args:
            duration_seconds: Длительность аудио в секундах
            
        Returns:
            int: Таймаут в секундах
        """
        timeout = int(duration_seconds / 10)
        # Минимальный таймаут 2 минуты, максимальный 100 минут
        timeout = max(120, min(timeout, 6000))
        
        logger.debug(f"Calculated dynamic timeout: {timeout} seconds (from {duration_seconds}s audio)")
        return timeout

    async def check_jobs_status(self, job_ids: list[str]) -> dict[str, bool]:
        """
        Проверяет статусы задач по их ID.
        
        Args:
            job_ids: Список ID задач
        Returns:
            dict[str, bool]: Словарь статусов задач. True if failed, False if not failed (yet)
        """
        if not job_ids:
            logger.warning("No job IDs provided")
            return {}

        try:
            headers = {"x-api-key": self.api_key} if self.api_key else {}
            payload = {"job_ids": job_ids, 'check_failed_jobs': True}
            async with aiohttp.ClientSession() as session:
                    async with session.post(self.api_url, headers=headers, json=payload) as response:
                        if response.status == 200:
                            data = await response.json()
                            failed_jobs = data['failed_jobs']
                            result = {job_id: status for job_id, status in zip(job_ids, failed_jobs)}
                            return result
                        else:
                            error_text = await response.text()
                            logger.error(f"Failed to check jobs status. Status: {response.status}, Response: {error_text}")
                            return {}
        except Exception as e:
            logger.error(f"Exception occurred while checking jobs status: {str(e)}", exc_info=True)
            return {}

    async def cancel_job(self, job_id: str) -> bool:
        """
        Отменяет задачу по ее ID.

        Args:
            job_id: ID задачи
        Returns:
            bool: True если успешно, иначе False
        """
        try:
            headers = {"x-api-key": self.api_key} if self.api_key else {}
            payload = {"job_id": job_id, "cancel_job": True}
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data['status'] == 'cancel_request_successfull':
                            logger.info(f"Job {job_id} canceled successfully")
                            return True
                        else:
                            logger.error(f"Failed to cancel job. Status: {response.status}, Response: {data['status']}")
                            return False
        except Exception as e:
            logger.error(f"Exception occurred while canceling job: {str(e)}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return False

    async def request_signed_urls(self, file_name: str) -> Optional[tuple[str, str, Optional[str]]]:
        """
        Запрашивает подписанные URL для загрузки и скачивания.

        Returns:
            tuple[str, str, Optional[str]]: (upload_url, download_url, job_id) или None при ошибке
        """
        try:
            logger.debug(f"Requesting signed URLs from: {self.api_url} for file: {file_name}")
            headers = {"x-api-key": self.api_key} if self.api_key else {}
            payload = {"file_name": file_name}

            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        # Ответ может быть уже JSON с нужными полями или обёрнут в { body: "{...}" }
                        try:
                            data = await response.json(content_type=None)
                        except Exception:
                            text_payload = await response.text()
                            data = json.loads(text_payload)

                        if isinstance(data, dict) and "body" in data and isinstance(data["body"], str):
                            try:
                                data = json.loads(data["body"])  # распаковываем строковый body API Gateway
                            except Exception:
                                logger.error("Failed to parse 'body' field as JSON from API Gateway response")
                                return None

                        upload_url = data.get("upload_url")
                        download_url = data.get("download_url")
                        job_id = data.get("job_id")

                        if not upload_url or not download_url:
                            logger.error("Signed URLs response is missing required fields 'upload_url' or 'download_url'")
                            return None

                        logger.debug(f"Got signed URLs - Upload: {str(upload_url)[:50]}..., Download: {str(download_url)[:50]}..., job_id: {job_id}")
                        return upload_url, download_url, job_id
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to get signed URLs. Status: {response.status}, Response: {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Exception occurred while requesting signed URLs: {str(e)}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return None
    
    async def upload_audio_file(self, upload_url: str, file_path: str) -> tuple[bool, Optional[float]]:
        """
        Загружает аудио файл по подписанному URL с retry логикой и chunked upload.
        
        Args:
            upload_url: Подписанный URL для загрузки
            file_path: Путь к аудио файлу
            
        Returns:
            tuple[bool, Optional[float]]: (success, duration) - True если успешно, иначе False, и длительность аудио
        """
        try:
            if not os.path.exists(file_path):
                logger.error(f"File not found: {file_path}")
                return False, None
            
            # Проверка размера файла (макс. 20 ГБ)
            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE_BYTES:
                logger.error(f"File is too large (max 20 GB): {file_size:,} bytes")
                return False, None

            logger.debug(f"Uploading audio file: {file_path} ({file_size:,} bytes)")
            
            # Read file and get duration
            async with aiofiles.open(file_path, 'rb') as file:
                file_content = await file.read()
                duration: Optional[float] = self.get_audio_duration(file_content)
            
            # Retry loop with exponential backoff
            for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
                try:
                    logger.debug(f"Upload attempt {attempt}/{MAX_UPLOAD_RETRIES}...")
                    
                    headers = {"Content-Type": "application/octet-stream"}
                    timeout = aiohttp.ClientTimeout(
                        sock_connect=CONNECT_TIMEOUT,
                        sock_read=READ_TIMEOUT
                    )
                    
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        # Create chunked data generator
                        async def file_sender():
                            async with aiofiles.open(file_path, 'rb') as f:
                                while True:
                                    chunk = await f.read(CHUNK_SIZE)
                                    if not chunk:
                                        break
                                    yield chunk
                        
                        async with session.put(upload_url, data=file_sender(), headers=headers) as response:
                            # Only 200 or 204 mean success
                            if response.status in (200, 204):
                                logger.info(f"✅ Upload succeeded on attempt {attempt}")
                                return True, duration
                            else:
                                error_text = await response.text()
                                logger.warning(f"❌ Upload failed (HTTP {response.status}). Response: {error_text[:200]}")
                
                except asyncio.TimeoutError as e:
                    logger.warning(f"⏳ Timeout on attempt {attempt}: {e}")
                
                except (aiohttp.ClientError, aiohttp.ClientConnectionError) as e:
                    logger.warning(f"⏳ Network/connection error on attempt {attempt}: {e}")
                
                except Exception as e:
                    logger.warning(f"⚠️ Request exception on attempt {attempt}: {e}")
                
                # Retry with exponential backoff (skip sleep on last attempt)
                if attempt < MAX_UPLOAD_RETRIES:
                    sleep_time = BACKOFF_BASE ** attempt
                    logger.debug(f"🔁 Retrying in {sleep_time} seconds...")
                    await asyncio.sleep(sleep_time)
            
            logger.error(f"❌ Upload failed after {MAX_UPLOAD_RETRIES} retry attempts")
            return False, None
                            
        except Exception as e:
            logger.error(f"Exception occurred while uploading audio file: {str(e)}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return False, None
    
    async def upload_audio_from_buffer(self, upload_url: str, file_buffer: bytes) -> tuple[bool, Optional[float]]:
        """
        Загружает аудио из буфера по подписанному URL с retry логикой и chunked upload.
        
        Args:
            upload_url: Подписанный URL для загрузки
            file_buffer: Содержимое аудио файла
            
        Returns:
            tuple[bool, Optional[float]]: (success, duration) - True если успешно, иначе False, и длительность аудио
        """
        try:
            # Проверка размера буфера (макс. 20 ГБ)
            buffer_size = len(file_buffer) if file_buffer else 0
            if buffer_size > MAX_FILE_SIZE_BYTES:
                logger.error(f"Buffer is too large (max 20 GB): {buffer_size:,} bytes")
                return False, None

            logger.debug(f"Uploading audio from buffer ({buffer_size:,} bytes)")
            
            # Get audio duration
            duration: Optional[float] = self.get_audio_duration(file_buffer)
            
            # Retry loop with exponential backoff
            for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
                try:
                    logger.debug(f"Upload attempt {attempt}/{MAX_UPLOAD_RETRIES}...")
                    
                    headers = {"Content-Type": "application/octet-stream"}
                    timeout = aiohttp.ClientTimeout(
                        sock_connect=CONNECT_TIMEOUT,
                        sock_read=READ_TIMEOUT
                    )
                    
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        # Create chunked data generator
                        async def buffer_sender():
                            offset = 0
                            while offset < len(file_buffer):
                                chunk = file_buffer[offset:offset + CHUNK_SIZE]
                                yield chunk
                                offset += CHUNK_SIZE
                        
                        async with session.put(upload_url, data=buffer_sender(), headers=headers) as response:
                            # Only 200 or 204 mean success
                            if response.status in (200, 204):
                                logger.info(f"✅ Upload succeeded on attempt {attempt}")
                                return True, duration
                            else:
                                error_text = await response.text()
                                logger.warning(f"❌ Upload failed (HTTP {response.status}). Response: {error_text[:200]}")
                
                except asyncio.TimeoutError as e:
                    logger.warning(f"⏳ Timeout on attempt {attempt}: {e}")
                
                except (aiohttp.ClientError, aiohttp.ClientConnectionError) as e:
                    logger.warning(f"⏳ Network/connection error on attempt {attempt}: {e}")
                
                except Exception as e:
                    logger.warning(f"⚠️ Request exception on attempt {attempt}: {e}")
                
                # Retry with exponential backoff (skip sleep on last attempt)
                if attempt < MAX_UPLOAD_RETRIES:
                    sleep_time = BACKOFF_BASE ** attempt
                    logger.debug(f"🔁 Retrying in {sleep_time} seconds...")
                    await asyncio.sleep(sleep_time)
            
            logger.error(f"❌ Upload failed after {MAX_UPLOAD_RETRIES} retry attempts")
            return False, None
                        
        except Exception as e:
            logger.error(f"Exception occurred while uploading audio from buffer: {str(e)}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return False, None
    
    async def poll_for_transcript(
        self, 
        download_url: str,
        job_id: str,
        poll_interval: int = 5, 
        timeout_seconds: int = 300,
        session_id: Optional[str] = None,
        file_path: Optional[str] = None,
        file_buffer: Optional[str] = None,
        audio_duration: Optional[float] = None
    ) -> Optional[str]:
        """
        Опрашивает URL для получения готовой транскрипции с таймаутом.
        
        Args:
            download_url: URL для скачивания транскрипции
            poll_interval: Интервал опроса в секундах
            timeout_seconds: Общий таймаут в секундах
            session_id: ID сессии
        Returns:
            str: Текст транскрипции или None при ошибке
        """
        try:
            max_attempts = timeout_seconds // poll_interval
            logger.debug(f"Starting to poll for transcript. Timeout: {timeout_seconds}s, max attempts: {max_attempts}")
            
            start_time = asyncio.get_event_loop().time()
            chat_type = 'dev_chat'
            for attempt in range(max_attempts):
                # Проверяем, не истек ли таймаут
                elapsed_time = asyncio.get_event_loop().time() - start_time
                if elapsed_time >= timeout_seconds:
                    logger.error(f"Timeout reached after {elapsed_time:.1f} seconds")
                    error_message = f'🟢 Timeout error.\n\n Failed to get transcript after {max_attempts} attempts ({timeout_seconds}s timeout).\n\n<b>Audio duration:</b> {audio_duration} seconds.\n<b>Session ID:</b> {session_id}\n<b>Job ID:</b> {job_id}'
                    if file_path:
                        await send_alert(text=error_message, chat_type=chat_type,
                             file_path=file_path,
                             level='WARNING', topic='Local model')
                    elif file_buffer:
                        await send_alert(
                            text=error_message, chat_type=chat_type,
                            file_buffer=file_buffer,
                            level='WARNING', topic='Local model')
                    else:
                        await send_alert(text=error_message, chat_type=chat_type,
                             level='WARNING', topic='Local model')
                    await self.cancel_job(job_id)
                    raise TimeoutError(f"Failed to get transcript after {max_attempts} attempts ({timeout_seconds}s timeout)")
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(download_url) as response:
                        if response.status == 200:
                            transcript_text = await response.text()
                            logger.debug(f"Successfully received transcript after {attempt + 1} attempts ({elapsed_time:.1f}s)")
                            return transcript_text
                        elif response.status == 404:
                            # Транскрипция еще не готова
                            statuses: dict[str, bool] = await self.check_jobs_status([job_id])
                            if statuses.get(job_id, False):
                                logger.error(f"Private module. Job {job_id} failed")
                                await self.cancel_job(job_id)
                                error_message = f'🟢 Job {job_id} failed. Cancelled.\n\n<b>Audio duration:</b> {audio_duration} seconds.\n<b>Session ID:</b> {session_id}\n<b>Job ID:</b> {job_id}'
                                await send_alert(text=error_message, chat_type=chat_type,
                                 level='WARNING', topic='Local model',
                                 file_path=file_path,
                                 file_buffer=file_buffer)
                                return None
                            else:
                                logger.debug(f"Job {job_id} is not in the list of failed jobs. Retrying in {poll_interval}s...")
                                remaining_time = timeout_seconds - elapsed_time
                                logger.debug(f"Transcript not ready yet (attempt {attempt + 1}/{max_attempts}, {elapsed_time:.1f}s elapsed, {remaining_time:.1f}s remaining). Retrying in {poll_interval}s...")
                                await asyncio.sleep(poll_interval)
                        else:
                            error_text = await response.text()
                            logger.error(f"Unexpected response while polling. Status: {response.status}, Response: {error_text}")
                            return None
            
            error_message = f'🟢 Timeout error.\n\n Failed to get transcript after {max_attempts} attempts ({timeout_seconds}s timeout).\n\n<b>Audio duration:</b> {audio_duration} seconds.\n<b>Session ID:</b> {session_id}\n<b>Job ID:</b> {job_id}'
            logger.error(f"Local model timeout error: {error_message}")
            await self.cancel_job(job_id)
            await send_alert(text=error_message, chat_type=chat_type,
                             level='WARNING', topic='Local model',
                             file_path=file_path,
                             file_buffer=file_buffer)
            raise TimeoutError(f"Failed to get transcript after {max_attempts} attempts ({timeout_seconds}s timeout)")
            
        except Exception as e:
            logger.error(f"Exception occurred while polling for transcript: {str(e)}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return None
    
    async def process_audio(
        self,
        waiting_message=None,
        i18n: Optional[TranslatorRunner] = None,
        file_path: Optional[str] = None,
        file_buffer: Optional[bytes] = None,
        poll_interval: int = 2,
        group_transcription: bool = True,
        suppress_progress: bool = False,
        session_id: Optional[str] = None
    ) -> Optional[tuple[str, str]]:
        """
        Полный процесс обработки аудио файла через приватный STT API.
        
        Args:
            waiting_message: Сообщение для обновления прогресса
            i18n: Переводчик для интернационализации
            file_path: Путь к аудио файлу
            file_buffer: Содержимое аудио файла в памяти
            poll_interval: Интервал опроса готовности транскрипции
            group_transcription: Применять ли группировку транскрипции
            suppress_progress: Подавлять ли динамический прогресс
            session_id: ID сессии
            
        Returns:
            tuple[str, str]: (grouped_timecoded_transcript, clean_transcript) или None при ошибке
        """
        if not file_path and not file_buffer:
            raise ValueError("Either file_path or file_buffer must be provided")
        
        try:
            # Обновляем прогресс
            if waiting_message and i18n:
                try:
                    if not suppress_progress:
                        await waiting_message.edit_text(
                            text=i18n.transcribe_audio_progress_extracting(progress=progress_bar(10, i18n))
                        )
                except:
                    pass
            
            # 1. Получаем подписанные URL
            logger.debug("Step 1: Requesting signed URLs")
            # Формируем имя файла для запроса
            req_file_name = os.path.basename(file_path) if file_path else "buffer_audio.bin"
            urls_result = await self.request_signed_urls(req_file_name)
            if not urls_result:
                logger.error("Failed to get signed URLs")
                return None
            
            upload_url, download_url, job_id = urls_result
            if job_id:
                logger.info(f"Obtained STT job_id: {job_id}")
            
            # Обновляем прогресс
            if waiting_message and i18n:
                try:
                    if not suppress_progress:
                        await waiting_message.edit_text(
                            text=i18n.transcribe_audio_progress_extracting(progress=progress_bar(30, i18n))
                        )
                except:
                    pass
            
            # 2. Загружаем аудио файл
            logger.debug("Step 2: Uploading audio file")
            # Проверяем, существует ли файл, если передан путь
            if file_path and os.path.exists(file_path):
                upload_success, duration = await self.upload_audio_file(upload_url, file_path)
            elif file_buffer:
                logger.debug(f"File path {'not provided' if not file_path else f'does not exist ({file_path})'}, using buffer")
                upload_success, duration = await self.upload_audio_from_buffer(upload_url, file_buffer)
            else:
                logger.error(f"No valid file source available. file_path={file_path}, file_buffer={'provided' if file_buffer else 'None'}")
                return None
            
            if not upload_success:
                logger.error("Failed to upload audio file")
                return None
            
            # Обновляем прогресс
            if waiting_message and i18n:
                try:
                    if not suppress_progress:
                        await waiting_message.edit_text(
                            text=i18n.transcribe_audio_progress_extracting(progress=progress_bar(60, i18n))
                    )
                except:
                    pass
            
            # 3. Рассчитываем динамический таймаут на основе длительности аудио
            timeout_seconds = 300  # По умолчанию 5 минут
            if duration:
                timeout_seconds = self.calculate_dynamic_timeout(duration)
                logger.info(f"Using dynamic timeout: {timeout_seconds}s (audio duration: {duration:.1f}s)")
            else:
                logger.warning("Failed to get audio duration, using default timeout")
            
            # 4. Ожидаем готовности транскрипции
            logger.debug("Step 4: Polling for transcript")
            raw_transcript = await self.poll_for_transcript(
                download_url=download_url,
                job_id=job_id,
                poll_interval=poll_interval,
                timeout_seconds=timeout_seconds,
                session_id=session_id,
                file_path=file_path,
                file_buffer=file_buffer,
                audio_duration=duration
            )
            
            if not raw_transcript:
                return None
            
            # Обновляем прогресс
            if waiting_message and i18n:
                try:
                    if not suppress_progress:
                        await waiting_message.edit_text(
                        text=i18n.transcribe_audio_progress_extracting(progress=progress_bar(90, i18n))
                    )
                except:
                    pass
            clean_transcript = extract_plain_text(raw_transcript)
            # 5. Обрабатываем транскрипцию
            logger.debug("Step 5: Processing transcript")
            if group_transcription:
                try:
                    grouped_timecoded_transcript = group_transcription_smart(raw_transcript)
                    logger.debug("Successfully grouped transcription")
                    return grouped_timecoded_transcript, clean_transcript
                except Exception as e:
                    logger.warning(f"Failed to group transcription: {e}. Returning raw transcript.")
                    return raw_transcript, clean_transcript
            else:
                return raw_transcript, clean_transcript
                
        except Exception as e:
            logger.error(f"Exception occurred during audio processing: {str(e)}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return None


# Создаем глобальный экземпляр клиента
private_stt_client = PrivateSTTClient(api_key=config.private_stt.api_key)


# Функция-обертка для удобного использования
async def process_audio(
    waiting_message=None,
    i18n: Optional[TranslatorRunner] = None,
    file_path: Optional[str] = None,
    file_buffer: Optional[bytes] = None,
    poll_interval: int = 2,
    group_transcription: bool = True,
    suppress_progress: bool = False,
    session_id: Optional[str] = None
) -> Optional[tuple[str, str]]:
    """
    Функция-обертка для обработки аудио через приватный STT API.
    
    Args:
        waiting_message: Сообщение для обновления прогресса
        i18n: Переводчик для интернационализации
        file_path: Путь к аудио файлу
        file_buffer: Содержимое аудио файла в памяти
        poll_interval: Интервал опроса готовности транскрипции
        group_transcription: Применять ли группировку транскрипции
        suppress_progress: Подавлять ли динамический прогресс
        session_id: ID сессии
    Returns:
        tuple[str, str]: (grouped_timecoded_transcript, clean_transcript) или None при ошибке
    """
    return await private_stt_client.process_audio(
        waiting_message=waiting_message,
        i18n=i18n,
        file_path=file_path,
        file_buffer=file_buffer,
        poll_interval=poll_interval,
        group_transcription=group_transcription,
        suppress_progress=suppress_progress,
        session_id=session_id
    )


if __name__ == "__main__":
    async def main():
        # Тестовый пример использования
        audio_file = "/home/vadim/PycharmProjects/maxim_voice_summary/6 Северный Узел в знаке ЛЕВ.mp3"
        
        if os.path.exists(audio_file):
            print("Testing private STT API...")
            result = await process_audio(file_path=audio_file)
            
            if result:
                grouped_transcript, raw_transcript = result
                print("✅ Transcription successful!")
                print(f"Raw transcript length: {len(raw_transcript)}")
                print(f"Grouped transcript length: {len(grouped_transcript)}")
                print("\nFirst 500 characters of grouped transcript:")
                print(grouped_transcript[:500])
            else:
                print("❌ Transcription failed")
        else:
            print(f"Audio file not found: {audio_file}")
    
    # Запускаем тест
    asyncio.run(main())
