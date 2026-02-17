import logging
import traceback
import aiohttp
import json
from typing import Optional, Dict, Any, List, Tuple
import os
import aiofiles
import mimetypes
from aiohttp import FormData
from fluentogram import TranslatorRunner
import asyncio
import time
from services.services import progress_bar, format_time

from services.init_bot import config

logger = logging.getLogger(__name__)


def calculate_timeout(audio_length: int) -> int:
    """
    Рассчитывает динамический таймаут на основе длительности аудио файла.
    
    Args:
        audio_length: Длительность аудио в секундах
        
    Returns:
        int: Таймаут в секундах
    """
    if not audio_length or audio_length <= 0:
        return 300  # Дефолтный таймаут 5 минут для неизвестной длительности
    
    # Преобразуем в минуты для удобства расчета
    audio_minutes = audio_length / 60
    
    if 5 <= audio_minutes < 15:
        # Для файлов от 5 до 15 минут: таймаут = 1/1.2 от длительности
        timeout = int(audio_length / 1.05)
    elif 15 <= audio_minutes < 30:
        # Для файлов от 15 до 30 минут: таймаут = 1/2 от длительности
        timeout = int(audio_length / 1.2)
    elif 30 <= audio_minutes < 60:
        # Для файлов от 30 до 60 минут: таймаут = 1/3.3 от длительности
        timeout = int(audio_length / 1.8)
    elif 60 <= audio_minutes < 180:
        # Для файлов от 60 минут: таймаут = 1/4 от длительности
        timeout = int(audio_length / 3)
    elif 180 <= audio_minutes:
        timeout = int(audio_length / 2)
    else:
        # Для файлов менее 5 минут: минимальный таймаут 2 минуты
        timeout = 120
    logger.info(f"Calculated dynamic timeout for ElevateAI: {timeout} seconds (from {audio_length}s audio)")
    # Минимальный таймаут 60 секунд, максимальный 3600 секунд (1 час)
    return int(max(60, min(timeout, 3600)))


class ElevateAIKeyManager:
    """
    Класс для управления API ключами Elevate AI и балансировки нагрузки между ними.
    """
    def __init__(self, api_keys: List[str]):
        """
        Инициализация менеджера API ключей.
        
        Args:
            api_keys: Список API ключей Elevate AI
        """
        if not api_keys:
            raise ValueError("No API keys provided for ElevateAI")
        print(api_keys)
        print('ElevateAI api keys:', len(api_keys))
        self.api_keys = api_keys
        self.request_counter = 0
        self.key_errors = {key: 0 for key in api_keys}
    
    def get_next_key(self) -> str:
        """
        Возвращает следующий API ключ по принципу round-robin.
        
        Returns:
            str: Выбранный API ключ
        """
        if not self.api_keys:
            raise ValueError("No API keys available for ElevateAI")
        
        selected_key = self.api_keys[self.request_counter % len(self.api_keys)]
        self.request_counter += 1
        
        # Маскируем ключ для безопасности в логах
        masked_key = selected_key[:8] + "..." + selected_key[-4:]
        logger.debug(f"Selected API key for request: {masked_key} (key #{self.request_counter % len(self.api_keys) + 1} of {len(self.api_keys)})")
        
        return selected_key
    
    def mark_key_error(self, api_key: str) -> None:
        """
        Отмечает ошибку для конкретного API ключа.
        
        Args:
            api_key: API ключ, с которым произошла ошибка
        """
        if api_key in self.key_errors:
            self.key_errors[api_key] += 1
    
    def get_all_keys(self) -> List[str]:
        """
        Возвращает все доступные API ключи.
        
        Returns:
            List[str]: Список всех API ключей
        """
        return self.api_keys.copy()


class ElevateAIClient:
    """
    Клиент для работы с API Elevate AI.
    """
    def __init__(self, key_manager: ElevateAIKeyManager):
        """
        Инициализация клиента с менеджером ключей.
        
        Args:
            key_manager: Менеджер API ключей
        """
        self.key_manager = key_manager
        self.base_url = "https://api.elevateai.com/v1"
    
    async def declare_audio_interaction(
        self,
        download_uri: Optional[str] = None,
        language_tag: str = "auto",
        original_filename: Optional[str] = None,
        external_identifier: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[Tuple[str, str]]:
        """
        Объявляет аудио взаимодействие в API Elevate AI.
        
        Args:
            download_uri: URL для загрузки аудио
            language_tag: Код языка (по умолчанию "auto")
            original_filename: Оригинальное имя файла
            external_identifier: Внешний идентификатор
            metadata: Метаданные для взаимодействия
            
        Returns:
            Tuple[str, str]: (interaction_id, api_key) в случае успеха, None при ошибке
        """
        endpoint = f"{self.base_url}/interactions"
        
        payload = {
            "type": "audio",
            "languageTag": language_tag,
            "vertical": "default",
            "model": "echo",
            "includeAiResults": True
        }
        
        if download_uri:
            payload["downloadUri"] = download_uri
        if original_filename:
            payload["originalFileName"] = original_filename
        if external_identifier:
            payload["externalIdentifier"] = external_identifier
        if metadata:
            payload["metadata"] = json.dumps(metadata)
        
        # Получаем ключ по round-robin
        api_key = self.key_manager.get_next_key()
        
        # Маскируем ключ для безопасности в логах
        masked_key = api_key[:8] + "..." + api_key[-4:]
        logger.debug(f"Declaring audio interaction using API key: {masked_key}")
        
        headers = {
            "X-API-Token": api_key,
            "Content-Type": "application/json"
        }
        
        try:
            logger.debug(f"Declaring audio interaction to ElevateAI: {payload}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, headers=headers, json=payload) as response:
                    if response.status == 201:
                        result = await response.json()
                        interaction_id = result.get("interactionIdentifier")
                        logger.debug(f"Successfully declared audio interaction. ID: {interaction_id}")
                        return interaction_id, api_key
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to declare audio interaction. Status: {response.status}, "
                            f"Response: {error_text}"
                        )
                        self.key_manager.mark_key_error(api_key)
                        return None
                        
        except Exception as e:
            logger.error(f"Exception occurred while declaring audio interaction: {str(e)}")
            self.key_manager.mark_key_error(api_key)
            return None
    
    async def upload_audio_file(
        self,
        interaction_id: str,
        file_path: str,
        api_key: str
    ) -> bool:
        """
        Загружает аудио файл в ElevateAI.
        
        Args:
            interaction_id: Идентификатор взаимодействия
            file_path: Путь к файлу
            api_key: API ключ для запроса
            
        Returns:
            bool: True если успешно, иначе False
        """
        endpoint = f"{self.base_url}/interactions/{interaction_id}/upload"
        
        # Маскируем ключ для безопасности в логах
        masked_key = api_key[:8] + "..." + api_key[-4:]
        logger.debug(f"Uploading audio file using API key: {masked_key}")
        
        headers = {
            "X-API-Token": api_key,
            # Ask server if it is ready before sending large body
            "Expect": "100-continue",
        }
        
        try:
            if not os.path.exists(file_path):
                logger.error(f"File not found: {file_path}")
                return False

            filename = os.path.basename(file_path)
            if not os.path.splitext(filename)[1]:
                filename = f"{filename}.mp3"
            logger.debug(f"Upload filename: {filename}")
            # Guess content type to help the server and avoid misclassification
            content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

            # Robust retry loop for transient TLS/connection errors
            max_retries = 4
            backoff = 1.5
            attempt = 0
            last_error = None

            while attempt <= max_retries:
                form_data = FormData()
                # IMPORTANT: stream file from disk; don't preload into memory
                with open(file_path, 'rb') as file_obj:
                    # Use a conventional field name 'file' for multipart upload
                    form_data.add_field(
                        'file',
                        file_obj,
                        filename=filename,
                        content_type=content_type
                    )
                    logger.debug(f"Uploading audio file (attempt {attempt+1}/{max_retries+1}): {filename} for interaction: {interaction_id}")

                    timeout = aiohttp.ClientTimeout(total=900, sock_connect=30, sock_read=600)
                    connector = aiohttp.TCPConnector(limit=10, force_close=True, enable_cleanup_closed=True)
                    try:
                        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                            async with session.post(endpoint, headers=headers, data=form_data) as response:
                                response_text = await response.text()
                                if response.status in (200, 201):
                                    logger.debug(f"Successfully uploaded audio file for interaction: {interaction_id}")
                                    return True
                                else:
                                    logger.error(
                                        f"Failed to upload audio file. Status: {response.status}, Response: {response_text}"
                                    )
                                    self.key_manager.mark_key_error(api_key)
                                    return False
                    except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError, aiohttp.ClientConnectorError, ConnectionError) as e:
                        last_error = e
                        attempt += 1
                        if attempt > max_retries:
                            break
                        sleep_s = backoff ** attempt
                        logger.warning(f"Upload attempt {attempt} failed due to connection error: {e}. Retrying in {sleep_s:.1f}s")
                        await asyncio.sleep(sleep_s)
                    except Exception as e:
                        logger.error(f"Unexpected error during upload: {e}")
                        self.key_manager.mark_key_error(api_key)
                        return False

            logger.error(f"All upload attempts failed. Last error: {last_error}")
            self.key_manager.mark_key_error(api_key)
            return False
                        
        except Exception as e:
            logger.error(f"Exception occurred while uploading audio file: {str(e)}")
            self.key_manager.mark_key_error(api_key)
            return False
    
    async def upload_audio_from_buffer(
        self,
        interaction_id: str,
        file_buffer: bytes,
        filename: str,
        api_key: str
    ) -> bool:
        """
        Загружает аудио файл из буфера в ElevateAI.
        
        Args:
            interaction_id: Идентификатор взаимодействия
            file_buffer: Содержимое файла
            filename: Имя файла
            api_key: API ключ для запроса
            
        Returns:
            bool: True если успешно, иначе False
        """
        endpoint = f"{self.base_url}/interactions/{interaction_id}/upload"
        
        # Маскируем ключ для безопасности в логах
        masked_key = api_key[:8] + "..." + api_key[-4:]
        logger.debug(f"Uploading audio buffer using API key: {masked_key}")
        
        headers = {
            "X-API-Token": api_key
        }
        
        try:
            form_data = FormData()
            form_data.add_field(
                f'filename{os.path.splitext(filename)[1]}',
                file_buffer,
                filename=filename
            )
            logger.debug(f"Uploading audio buffer as file: {filename} for interaction: {interaction_id}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, headers=headers, data=form_data) as response:
                    response_text = await response.text()
                    
                    if response.status in (200, 201):
                        logger.debug(f"Successfully uploaded audio buffer for interaction: {interaction_id}")
                        return True
                    else:
                        logger.error(
                            f"Failed to upload audio buffer. Status: {response.status}, "
                            f"Response: {response_text}"
                        )
                        self.key_manager.mark_key_error(api_key)
                        return False
                        
        except Exception as e:
            logger.error(f"Exception occurred while uploading audio buffer: {str(e)}")
            self.key_manager.mark_key_error(api_key)
            return False
    
    async def get_interaction_status(
        self,
        interaction_id: str,
        api_key: str
    ) -> Optional[Dict]:
        """
        Проверяет статус обработки взаимодействия.
        
        Args:
            interaction_id: Идентификатор взаимодействия
            api_key: API ключ для запроса
            
        Returns:
            Optional[Dict]: Словарь с информацией о статусе или None при ошибке
        """
        endpoint = f"{self.base_url}/interactions/{interaction_id}/status"
        
        # Маскируем ключ для безопасности в логах
        masked_key = api_key[:8] + "..." + api_key[-4:]
        logger.debug(f"Checking interaction status using API key: {masked_key}")
        
        headers = {
            "X-API-Token": api_key,
            "Accept-Encoding": "gzip, deflate, br"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=headers) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.debug(f"Status for interaction {interaction_id}: {result['status']}")
                        return result
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to get status. Status code: {response.status}, "
                            f"Response: {error_text}"
                        )
                        self.key_manager.mark_key_error(api_key)
                        return None
                        
        except Exception as e:
            logger.error(f"Exception occurred while getting status: {str(e)}")
            self.key_manager.mark_key_error(api_key)
            return None
    
    async def get_punctuated_transcript(
        self,
        interaction_id: str,
        api_key: str
    ) -> Optional[Dict]:
        """
        Получает транскрипцию с пунктуацией.
        
        Args:
            interaction_id: Идентификатор взаимодействия
            api_key: API ключ для запроса
            
        Returns:
            Optional[Dict]: Словарь с данными транскрипции или None при ошибке
        """
        endpoint = f"{self.base_url}/interactions/{interaction_id}/transcripts/punctuated"
        
        # Маскируем ключ для безопасности в логах
        masked_key = api_key[:8] + "..." + api_key[-4:]
        logger.debug(f"Getting punctuated transcript using API key: {masked_key}")
        
        headers = {
            "X-API-Token": api_key,
            "Accept-Encoding": "gzip, deflate, br"
        }
        
        try:
            logger.debug(f"Requesting punctuated transcript for interaction: {interaction_id}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=headers) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.debug(f"Successfully retrieved transcript for interaction: {interaction_id}")
                        return result
                    elif response.status == 204:
                        logger.warning(f"Transcript not ready yet for interaction: {interaction_id}")
                        return None
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to get transcript. Status: {response.status}, "
                            f"Response: {error_text}"
                        )
                        self.key_manager.mark_key_error(api_key)
                        return None
        except Exception as e:
            logger.error(f"Exception occurred while getting transcript: {str(e)}")
            self.key_manager.mark_key_error(api_key)
            return None
    
    async def wait_for_transcript(
        self,
        interaction_id: str,
        api_key: str,
        delay_seconds: int = 10,
        audio_length: int = None
    ) -> Optional[Dict]:
        """
        Ожидает готовности транскрипции, периодически опрашивая API.
        
        Args:
            interaction_id: Идентификатор взаимодействия
            api_key: API ключ для запроса
            delay_seconds: Задержка между опросами в секундах
            audio_length: Длительность аудио в секундах
        Returns:
            Optional[Dict]: Данные транскрипции или None при ошибке
        """
        # Маскируем ключ для безопасности в логах
        masked_key = api_key[:8] + "..." + api_key[-4:]
        logger.debug(f"Waiting for transcript using API key: {masked_key}")
        
        # Рассчитываем динамический таймаут
        timeout = calculate_timeout(audio_length)
        logger.info(f'Audio length: {audio_length} seconds, calculated timeout: {timeout} seconds')

        start_monotonic = time.monotonic()
        while True:
            elapsed = int(time.monotonic() - start_monotonic)
            if elapsed >= timeout:
                logger.error(f"Timeout reached while waiting for transcript: {timeout} seconds, audio length: {audio_length} seconds")
                raise TimeoutError(f"Timeout reached while waiting for transcript: {timeout} seconds, audio length: {audio_length} seconds")
            status_info = await self.get_interaction_status(interaction_id, api_key)
            
            if not status_info:
                logger.error("Failed to get interaction status")
                return None
                
            status = status_info.get("status")
            error_message = status_info.get("errorMessage")
            
            if status == "processed":
                logger.debug("Interaction processing completed, getting transcript...")
                return await self.get_punctuated_transcript(interaction_id, api_key)
            elif status == "processingFailed":
                logger.error(f"Processing failed: {error_message}")
                return None
            elif status in ["declared", "filePendingUpload", "fileUploading", "fileUploaded",
                           "filePendingDownload", "fileDownloading", "fileDownloaded",
                           "pendingProcessing", "processing"]:
                logger.debug(f"Current status: {status}, waiting {delay_seconds} seconds... (elapsed: {elapsed}/{timeout}s)")
                await asyncio.sleep(delay_seconds)
            else:
                logger.error(f"Unexpected status: {status}")
                if error_message:
                    logger.error(f"Error message: {error_message}")
                return None
    
    async def process_audio(
        self,
        waiting_message,
        i18n: TranslatorRunner,
        file_path: str = None,
        file_buffer: bytes = None,
        filename: str = None,
        delay_seconds: int = 5,
        language: str = 'ru',
        audio_length: int = None,
        suppress_progress: bool = False
    ) -> tuple[str, str] | None:
        """
        Полный процесс обработки аудио файла с использованием round-robin для API ключей.
        
        Args:
            waiting_message: Сообщение о ожидании
            i18n: Переводчик для интернационализации
            file_path: Путь к файлу на диске
            file_buffer: Содержимое файла в памяти
            filename: Имя файла при загрузке из буфера
            delay_seconds: Задержка между проверками статуса
            language: Код языка для транскрипции
            audio_length: Длительность аудио в секундах
        Returns:
            tuple[str, str]: (timecoded_text, plain_text) или None при ошибке
        """
        if not file_path and not (file_buffer and filename):
            raise ValueError("Either file_path or (file_buffer and filename) must be provided")
        
        # Используем имя файла из пути или переданное явно
        actual_filename = os.path.basename(file_path) if file_path else filename
        
        # Пробуем все доступные ключи в случае ошибок
        all_keys = self.key_manager.get_all_keys()
        logger.debug(f"Starting audio processing with {len(all_keys)} available API keys")
        print(f'FILE PATH: {file_path}')
        print(f'FILE BUFFER: {file_buffer}')
        print(f'FILENAME: {filename}')
        for attempt in range(len(all_keys)):
            try:
                # Обновляем сообщение о прогрессе
                try:
                    if not suppress_progress:
                        await waiting_message.edit_text(text=i18n.transcribe_audio_progress_extracting(progress=progress_bar(40, i18n)))
                except:
                    pass
                
                logger.debug(f"Processing attempt {attempt + 1} of {len(all_keys)}")
                
                # Объявляем взаимодействие с использованием round-robin выбора ключа
                result = await self.declare_audio_interaction(
                    original_filename=actual_filename,
                    language_tag=language
                )
                
                if not result:
                    continue
                    
                interaction_id, api_key = result
                
                # Загружаем файл
                if file_path:
                    upload_success = await self.upload_audio_file(
                        interaction_id=interaction_id,
                        file_path=file_path,
                        api_key=api_key
                    )
                else:
                    upload_success = await self.upload_audio_from_buffer(
                        interaction_id=interaction_id,
                        file_buffer=file_buffer,
                        filename=filename,
                        api_key=api_key
                    )
                
                if not upload_success:
                    continue
                
                # Ждем и получаем транскрипцию
                transcript = await self.wait_for_transcript(
                    interaction_id=interaction_id,
                    api_key=api_key,
                    delay_seconds=delay_seconds,
                    audio_length=audio_length
                )
                
                if not transcript:
                    continue
                
                # Обрабатываем транскрипцию
                timecoded_text = ''
                plain_text = ''
                
                if "sentenceSegments" in transcript:
                    for segment in transcript["sentenceSegments"]:
                        start_time = segment["startTimeOffset"] / 1000
                        end_time = segment["endTimeOffset"] / 1000
                        phrase = segment["phrase"]
                        participant = segment["participant"]
                        if participant == 'participantOne':
                            participant = 'SPEAKER_1'
                        elif participant == 'participantTwo':
                            participant = 'SPEAKER_2'
                        elif participant == 'participantThree':
                            participant = 'SPEAKER_3'
                        elif participant == 'participantFour':
                            participant = 'SPEAKER_4'
            
                        timecoded_line = (f"[{format_time(start_time)} - {format_time(end_time)}] {participant}\n"
                                          f"{phrase}\n\n")
                        
                        timecoded_text += timecoded_line
                        plain_text += phrase + " "
            
                return timecoded_text.strip(), plain_text.strip()
            except TimeoutError as e:
                logger.error(f"ElevateAI. Timeout reached while waiting for transcript: {e}.")
                break
            except Exception as e:
                logger.error(f"Failed to process audio on attempt {attempt + 1}: {e}")
                logger.debug(f"Traceback: {traceback.format_exc()}")
                continue
        
        # Если все попытки не удались
        logger.error("Elevateai. All processing attempts failed")
        return None


# Создаем глобальный экземпляр клиента
key_manager = ElevateAIKeyManager(config.elevateai.api_key)
elevate_client = ElevateAIClient(key_manager)

# Функции-обертки для совместимости с существующим кодом

async def process_audio_elevateai(
    waiting_message,
    i18n: TranslatorRunner,
    file_path: str = None,
    file_buffer: bytes = None,
    filename: str = 'audio_file.mp3',
    delay_seconds: int = 5,
    language: str = 'ru',
    audio_length: int = None,
    suppress_progress: bool = False
) -> tuple[str, str] | None:
    """
    Функция-обертка для обратной совместимости, использующая объектно-ориентированный подход.
    """
    return await elevate_client.process_audio(
        waiting_message=waiting_message,
        i18n=i18n,
        file_path=file_path,
        file_buffer=file_buffer,
        filename=filename,
        delay_seconds=delay_seconds,
        language=language,
        audio_length=audio_length,
        suppress_progress=suppress_progress
    )

# Остальные функции-обертки для обратной совместимости
async def declare_audio_interaction(*args, **kwargs):
    result = await elevate_client.declare_audio_interaction(*args, **kwargs)
    if result:
        return result[0]  # Возвращаем только interaction_id для совместимости
    return None

async def upload_audio_file(interaction_id, file_path, api_key=config.elevateai.api_key[0]):
    return await elevate_client.upload_audio_file(interaction_id, file_path, api_key)

async def upload_audio_from_buffer(interaction_id, file_buffer, filename, api_key=config.elevateai.api_key[0]):
    return await elevate_client.upload_audio_from_buffer(interaction_id, file_buffer, filename, api_key)

async def get_interaction_status(interaction_id, api_key=config.elevateai.api_key[0]):
    return await elevate_client.get_interaction_status(interaction_id, api_key)

async def get_punctuated_transcript(interaction_id, api_key=config.elevateai.api_key[0]):
    return await elevate_client.get_punctuated_transcript(interaction_id, api_key)

async def wait_for_transcript(interaction_id, delay_seconds=10, api_key=config.elevateai.api_key[0]):
    return await elevate_client.wait_for_transcript(interaction_id, api_key, delay_seconds)

async def process_audio_elevateai_with_key(*args, **kwargs):
    # Эта функция больше не используется напрямую
    logger.warning("process_audio_elevateai_with_key is deprecated, use process_audio_elevateai instead")
    return await process_audio_elevateai(*args, **kwargs)

if __name__ == "__main__":
    async def main():
        # Путь к вашему аудио файлу
        audio_file = "/home/vadim/PycharmProjects/maxim_voice_summary/6 Северный Узел в знаке ЛЕВ.mp3"
        # await transcribe_audio_test(audio_file)
    
    # Запускаем тест
    asyncio.run(main())