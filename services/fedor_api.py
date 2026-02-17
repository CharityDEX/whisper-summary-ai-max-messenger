import os
import time
import uuid
import requests
import aiohttp
import asyncio
import logging
import re
import mimetypes
from typing import Optional, Union

from models.orm import save_transcription_cache
from services.content_downloaders.file_handling import download_file, identify_url_source
from config_data.config import get_config

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# Отключаем propagate, чтобы избежать дублирования логов с root logger
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Глобальный кеш токена
_cached_token: Optional[str] = None
_token_lock = asyncio.Lock()


async def convert_file_fedor_api(file_path: str | None = None, buffer: bytes | None = None, mode: str = 'video_to_audio', destination_type: str = 'disk', user_data: dict | None = None, session_id: str | None = None) -> dict:
    """
    Асинхронно конвертирует файл по пути или буферу в аудио.
    :param file_path: Путь к файлу
    :param buffer: Буфер
    :param mode: Режим конвертации ['mp4_to_mp3', 'video_to_audio', 'optimize_audio', 'change_format']
    :return: Словарь с результатам: путь к файлу и доп данные
    """
    token = await get_token_async()

    filename = 'input'
    content_type = 'application/octet-stream'

    if buffer is not None:
        buffer = buffer
    elif file_path is not None:
        filename = os.path.basename(file_path)
        guessed_type, _ = mimetypes.guess_type(file_path)
        if guessed_type:
            content_type = guessed_type
    else:
        raise ValueError("Either buffer or file_path must be provided")

    result_id: str | None = await _request_convert_api_fedor_api(buffer, mode, token, filename, content_type, file_path=file_path)
    if result_id is None:
        raise Exception("Fedor API: Failed to make convert request")
    # print(f"Result ID convert: {result_id}")
    result_data: dict | None = await _poll_convert_result_async(result_id, token)
    # print(f"Result Data convert: {result_data}")

    # Получаем актуальный токен из кеша (мог обновиться в _request_convert_api_fedor_api или _poll_convert_result_async)
    current_token = await get_token_async()

    file_path: str | bytes = await download_file(source_type='url', identifier=f"https://trywhisper.xyz/api/v1/media/convert/{result_id}/file/",
                                    destination_type=destination_type, specific_source=None,
                                    user_data=user_data, session_id=session_id, additional_data={'token': current_token},
                                    download_method='fedor_api')
    # print(f"File path convert: {file_path}")



    return {
        'file_path': file_path,
        'result_data': result_data
    }

async def _poll_convert_result_async(request_id: str, token: str, retry_on_auth_error: bool = True) -> dict | None:
    """
    Асинхронно опрашивает результат конвертации файла.

    :param request_id: ID запроса на конвертацию
    :param token: Аутентификационный токен
    :param retry_on_auth_error: Разрешить повторную попытку при ошибке аутентификации
    :return: Словарь с результатами конвертации
    """
    url = f'https://trywhisper.xyz/api/v1/media/convert/{request_id}/status/'
    headers = {
        'Authorization': f'Token {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    poll_count = 0
    while poll_count < 120:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    response.raise_for_status()
                    result = await response.json()
                    if result['status'] == 'completed':
                        return result
                    elif result['status'] == 'failed':
                        logger.error(f"Fedor API: Failed to poll convert result: {result}")
                        return None
                    else:
                        poll_count += 1
                        logger.debug(f"Fedor API: Poll convert result: {result} (attempt {poll_count})")
                        await asyncio.sleep(10)
                        continue
        except aiohttp.ClientResponseError as e:
            # Если ошибка аутентификации и разрешен retry, обновляем токен и повторяем
            if e.status in [401, 403] and retry_on_auth_error:
                logger.warning(f"Ошибка аутентификации при опросе ({e.status}), обновляем токен и повторяем...")
                invalidate_token()
                new_token = await get_token_async(force_refresh=True)
                return await _poll_convert_result_async(request_id, new_token, retry_on_auth_error=False)
            else:
                raise

async def _request_convert_api_fedor_api(buffer: bytes | None, mode: str, token: str, filename: str = 'input', content_type: str = 'application/octet-stream', file_path: str | None = None, retry_on_auth_error: bool = True) -> str | None:
    url = "https://trywhisper.xyz/api/v1/media/convert/"
    headers = {
        'Authorization': f'Token {token}',
        'Accept': 'application/json'
    }
    # Добавляем опциональный CSRF-токен (совместимость с curl примером)
    csrf_token = os.getenv('FEDOR_CSRF_TOKEN')
    if csrf_token:
        headers['X-CSRFTOKEN'] = csrf_token
    # Отправляем файл и параметры как multipart/form-data
    form = aiohttp.FormData()
    # Если file_path не задан, используем буфер
    if file_path is None:   
        form.add_field('file', buffer, filename=filename, content_type=content_type)
    else:
        fobj = open(file_path, 'rb')
        form.add_field('file', fobj, filename=filename, content_type=content_type)
    
    # API ожидает поле conversion_type
    form.add_field('conversion_type', mode)
    # print('mode: ', mode)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=form) as response:
                # print(response.status)
                # print(await response.text())
                response.raise_for_status()
                result = await response.json()
                if result['status'] == 'pending':
                    request_id = result['id']
                    return request_id
                else:
                    logger.error(f"Fedor API: Failed to make convert request: {result}")
                    return None
    except aiohttp.ClientResponseError as e:
        # Если ошибка аутентификации и разрешен retry, обновляем токен и повторяем
        if e.status in [401, 403] and retry_on_auth_error:
            logger.warning(f"Ошибка аутентификации ({e.status}), обновляем токен и повторяем...")
            invalidate_token()
            new_token = await get_token_async(force_refresh=True)
            return await _request_convert_api_fedor_api(buffer, mode, new_token, filename, content_type, file_path, retry_on_auth_error=False)
        else:
            raise
    finally:
        try:
            fobj.close()
        except Exception:
            pass


async def download_file_fedor_api(file_url: str, result_content_type: str = 'audio', user_data: dict | None = None, session_id: str | None = None, destination_type: str = 'disk', add_file_size_to_session: bool = False) -> dict:
    """
    Асинхронно скачивает файл по URL из Fedor API.
    :param file_url: URL файла для скачивания
    :param result_content_type: Тип результата ('audio' или 'video')
    :param user_data: Данные пользователя
    :param processing_session_id: ID сессии обработки
    :param audio_url: URL файла для скачивания
    :param result_type: Тип результата ('audio' или 'video')
    :return: Словарь с результатам: путь к файлу и доп данные
    """
    # Получаем токен из кеша
    token = await get_token_async()
    request_id = await _make_download_request_async(file_url, result_content_type, token)
    if request_id is None:
        raise Exception("Fedor API: Failed to make download request")
    result_data = await _poll_download_result_async(request_id, token)
    # print(f"Result data: {result_data}")
    # print('request_id: ', request_id)

    specific_source = identify_url_source(file_url)

    # Получаем актуальный токен из кеша (мог обновиться в _make_download_request_async или _poll_download_result_async)
    current_token = await get_token_async()

    file_path = await download_file(source_type='url', identifier=f"https://trywhisper.xyz/api/v1/media/download/{request_id}/file/",
                                    destination_type=destination_type, specific_source=specific_source,
                                    user_data=user_data, session_id=session_id, additional_data={'token': current_token},
                                    download_method='fedor_api', add_file_size_to_session=add_file_size_to_session)
    # print(f"File path: {file_path}")


    return {
        'file_path': file_path,
        'result_data': result_data
    }




async def _poll_download_result_async(request_id: str, token: str, retry_on_auth_error: bool = True) -> dict | None:
    """
    Асинхронно опрашивает результат скачивания файла.

    :param request_id: ID запроса на скачивание файла
    :param token: Аутентификационный токен
    :param retry_on_auth_error: Разрешить повторную попытку при ошибке аутентификации
    :return: Словарь с результатам: ссылка на файл и доп данные
    """
    url = f'https://trywhisper.xyz/api/v1/media/download/{request_id}/status/'
    headers = {
        'Authorization': f'Token {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    poll_count = 0
    while poll_count < 120: # 1200 seconds = 20 minutes
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    response.raise_for_status()
                    result = await response.json()
                    if result['status'] == 'completed':
                        return result
                    elif result['status'] == 'failed':
                        logger.error(f"Fedor API: Failed to poll download result: {result}")
                        return None
                    else:
                        poll_count += 1
                        logger.debug(f"Fedor API: Poll download result: {result} (attempt {poll_count})")
                        await asyncio.sleep(10)
                        continue
        except aiohttp.ClientResponseError as e:
            # Если ошибка аутентификации и разрешен retry, обновляем токен и повторяем
            if e.status in [401, 403] and retry_on_auth_error:
                logger.warning(f"Ошибка аутентификации при опросе ({e.status}), обновляем токен и повторяем...")
                invalidate_token()
                new_token = await get_token_async(force_refresh=True)
                return await _poll_download_result_async(request_id, new_token, retry_on_auth_error=False)
            else:
                raise
    return None

async def _make_download_request_async(file_url: str, result_content_type: str = 'audio', token: str | None = None, retry_on_auth_error: bool = True) -> str | None:

    body = {
        'url': file_url,
        'content_type': result_content_type
    }
    if token is None:
        token = await get_token_async()
    headers = {
        'Authorization': f'Token {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    # Добавляем CSRF-токен, если он задан в окружении (для совместимости с curl-примером)
    csrf_token = os.getenv('FEDOR_CSRF_TOKEN')
    if csrf_token:
        headers['X-CSRFTOKEN'] = csrf_token

    url = "https://trywhisper.xyz/api/v1/media/download/"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers) as response:
                response.raise_for_status()
                result = await response.json()

                if result['status'] == 'pending':
                    request_id = result['id']
                    return request_id
                else:
                    logger.error(f"Fedor API: Failed to make download request: {result}")
                    return None
    except aiohttp.ClientResponseError as e:
        # Если ошибка аутентификации и разрешен retry, обновляем токен и повторяем
        if e.status in [401, 403] and retry_on_auth_error:
            logger.warning(f"Ошибка аутентификации ({e.status}), обновляем токен и повторяем...")
            invalidate_token()
            new_token = await get_token_async(force_refresh=True)
            return await _make_download_request_async(file_url, result_content_type, new_token, retry_on_auth_error=False)
        else:
            raise

async def get_token_async(force_refresh: bool = False) -> str:
    """
    Асинхронно получает токен по логину и паролю с кешированием.

    :param force_refresh: Принудительно обновить токен, игнорируя кеш
    :return: Аутентификационный токен
    """
    global _cached_token

    async with _token_lock:
        # Если токен уже есть в кеше и не требуется принудительное обновление
        if _cached_token and not force_refresh:
            logger.debug("Используется кешированный токен")
            return _cached_token

        logger.info("Получение нового токена аутентификации...")
        config = get_config()
        url = "https://trywhisper.xyz/api/v1/user/token/"
        data = {
            "username": config.fedor_api.username,
            "password": config.fedor_api.password
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as response:
                    response.raise_for_status()
                    result = await response.json()
                    _cached_token = result["token"]
                    logger.info("Новый токен успешно получен и сохранен в кеш")
                    return _cached_token
        except Exception as e:
            logger.error(f"Ошибка при получении токена: {e}")
            raise

def invalidate_token():
    """Инвалидирует кешированный токен."""
    global _cached_token
    _cached_token = None
    logger.info("Кешированный токен инвалидирован")

async def process_audio_fedor_api(audio_url: str, session_id: str) -> dict:
    """
    Асинхронно обрабатывает аудио по URL и возвращает транскрипцию.
    
    :param audio_url: URL аудио или видео для транскрибации
    :param session_id: ID сессии обработки
    :return: Текст транскрипции без спикеров и временных меток и Текст транскрипции со спикерами и временными метками
    """
    logger.info(f"Начало обработки аудио по URL: {audio_url}")
    
    try:
        # Получаем токен
        logger.debug("Запрос токена аутентификации...")
        token = await get_token_async()
        
        # Создаем запрос на транскрибацию
        logger.info("Создание запроса на транскрибацию...")
        transcription_request_id = await create_transcription_request_async(audio_url, token)
        logger.info(f"Запрос создан с ID: {transcription_request_id}")
        
        # Опрашиваем результат до завершения
        logger.info("Ожидание завершения транскрибации...")
        transcription_text = await poll_transcription_result_async(transcription_request_id, token)

        transcription_id = await save_transcription_cache(
            source_type='url',
            original_identifier=audio_url,
            transcript_raw=transcription_text,
            transcript_timecoded=transcription_text,
            transcription_provider='fedor_api',
            session_id=session_id,
            specific_source=None,
            file_size_bytes=None,
            audio_duration=None
        )
        
        logger.info(f"Транскрибация успешно завершена. Длина текста: {len(transcription_text)} символов")
        clean_transcription = clean_transcription_text(transcription_text)
        return {
            'raw_transcript': clean_transcription,
            'timecoded_transcript': transcription_text,
            'transcription_id': transcription_id
        }
    
    except Exception as e:
        logger.error(f"Ошибка при обработке аудио через Fedor API: {e}")
        raise

async def create_transcription_request_async(audio_url: str, token: str, retry_on_auth_error: bool = True) -> str:
    """
    Асинхронно создаёт запрос на транскрибацию по URL.
    
    :param audio_url: URL аудио для транскрибации
    :param token: Аутентификационный токен
    :param retry_on_auth_error: Разрешить повторную попытку при ошибке аутентификации
    :return: ID запроса на транскрибацию
    """
    logger.debug(f"Отправка запроса на транскрибацию для URL: {audio_url}")
    url = "https://trywhisper.xyz/api/v1/elevate/transcriptions/"
    headers = {"Authorization": f"Token {token}"}
    data = {"user_input": audio_url}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as response:
                logger.debug(f"Ответ сервера: статус {response.status}")
                
                # Логируем тело ответа для отладки
                response_text = await response.text()
                logger.debug(f"Тело ответа сервера: {response_text}")
                
                response.raise_for_status()
                result = await response.json()
                request_id = result["id"]
                logger.info(f"Запрос на транскрибацию успешно создан с ID: {request_id}")
                return request_id
    except aiohttp.ClientResponseError as e:
        # Если ошибка аутентификации и разрешен retry, обновляем токен и повторяем
        if e.status in [401, 403] and retry_on_auth_error:
            logger.warning(f"Ошибка аутентификации ({e.status}), обновляем токен и повторяем...")
            invalidate_token()
            new_token = await get_token_async(force_refresh=True)
            return await create_transcription_request_async(audio_url, new_token, retry_on_auth_error=False)
        else:
            logger.error(f"HTTP ошибка при создании запроса на транскрибацию: {e.status} - {e.message}")
            # Логируем тело ответа при ошибке
            try:
                error_text = await e.response.text()
                logger.error(f"Тело ответа при ошибке: {error_text}")
            except:
                pass
            raise
    except Exception as e:
        logger.error(f"Ошибка при создании запроса на транскрибацию: {e}")
        raise

async def poll_transcription_result_async(
    request_id: Union[str, uuid.UUID],
    token: str,
    timeout: int = 12000,
    interval: int = 20,
    retry_on_auth_error: bool = True
) -> str:
    """
    Асинхронно опрашивает сервер до получения результата транскрибации.

    :param request_id: ID запроса на транскрибацию
    :param token: Аутентификационный токен
    :param timeout: Максимальное время ожидания в секундах
    :param interval: Интервал между запросами в секундах
    :param retry_on_auth_error: Разрешить повторную попытку при ошибке аутентификации
    :return: Текст транскрипции
    """
    logger.info(f"Начало опроса результата для запроса ID: {request_id}")
    logger.debug(f"Параметры: timeout={timeout}с, interval={interval}с")

    url = f"https://trywhisper.xyz/api/v1/elevate/transcriptions/{request_id}/"
    headers = {"Authorization": f"Token {token}"}

    start_time = time.time()
    poll_count = 0

    async with aiohttp.ClientSession() as session:
        while True:
            poll_count += 1
            elapsed_time = time.time() - start_time

            logger.debug(f"Опрос #{poll_count}, прошло времени: {elapsed_time:.1f}с")

            try:
                async with session.get(url, headers=headers) as response:
                    if response.status == 404:
                        logger.error(f"Транскрибация с ID {request_id} не найдена")
                        raise Exception("Транскрибация не найдена.")

                    response.raise_for_status()
                    result = await response.json()

                    status = result["status"]
                    logger.debug(f"Статус транскрибации: {status}")

                    if status == "completed":
                        transcription_text = result['transcription_text']
                        logger.info(f"Транскрипция успешно завершена за {elapsed_time:.1f}с. Длина: {len(transcription_text)} символов")
                        logger.debug(f"Первые 100 символов транскрипции: {transcription_text[:100]}...")
                        return transcription_text

                    elif status == "failed":
                        error_msg = result.get('error', 'Неизвестная ошибка')
                        # Логируем полный ответ для отладки
                        logger.error(f"Транскрибация завершена с ошибкой. Полный ответ: {result}")
                        logger.error(f"Транскрибация завершена с ошибкой: {error_msg}")

                        # Если ошибка неизвестная, добавляем дополнительную информацию
                        if error_msg == 'Неизвестная ошибка':
                            error_msg = f"Неизвестная ошибка (статус: failed, полный ответ: {result})"

                        raise Exception(f"Транскрибация завершена с ошибкой: {error_msg}")

                    elif status in ["processing", "pending"]:
                        logger.debug(f"Транскрибация в процессе... (статус: {status})")
                    else:
                        logger.warning(f"Неизвестный статус транскрибации: {status}")

                    if elapsed_time > timeout:
                        logger.error(f"Превышено время ожидания: {elapsed_time:.1f}с > {timeout}с")
                        raise TimeoutError(f"Превышено время ожидания транскрибации (>{timeout} сек).")

                    logger.debug(f"Ожидание {interval}с до следующего опроса...")
                    await asyncio.sleep(interval)

            except aiohttp.ClientResponseError as e:
                # Если ошибка аутентификации и разрешен retry, обновляем токен и повторяем
                if e.status in [401, 403] and retry_on_auth_error:
                    logger.warning(f"Ошибка аутентификации при опросе транскрипции ({e.status}), обновляем токен и повторяем...")
                    invalidate_token()
                    new_token = await get_token_async(force_refresh=True)
                    return await poll_transcription_result_async(request_id, new_token, timeout, interval, retry_on_auth_error=False)
                else:
                    logger.error(f"Ошибка HTTP при опросе результата: {e}")
                    # Продолжаем попытки при других HTTP ошибках
                    await asyncio.sleep(interval)
            except aiohttp.ClientError as e:
                logger.error(f"Ошибка сети при опросе результата: {e}")
                # Продолжаем попытки при сетевых ошибках
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"Неожиданная ошибка при опросе результата: {e}")
                raise

def clean_transcription_text(transcription_text: str) -> str:
    """
    Очищает текст транскрипции от временных меток и идентификаторов участников.
    
    Пример входного текста:
    "[00:21-00:42] Participant 1: Well lately, a lot of gaming..."
    
    Результат:
    "Well lately, a lot of gaming..."
    
    :param transcription_text: Исходный текст транскрипции с метками
    :return: Очищенный текст без временных меток и участников
    """
    logger.debug("Начало очистки текста транскрипции")
    
    if not transcription_text:
        logger.warning("Получен пустой текст для очистки")
        return ""
    
    try:
        # Паттерн для поиска временных меток и участников
        # Ищем: [время] Participant X: или [время] Speaker X: и т.д.
        pattern = r'\[\d{2}:\d{2}-\d{2}:\d{2}\]\s*(?:Participant|Speaker|Спикер|Участник)\s*\d*\s*:\s*'
        
        # Разбиваем текст по строкам
        lines = transcription_text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Удаляем временные метки и идентификаторы участников
            cleaned_line = re.sub(pattern, '', line, flags=re.IGNORECASE)
            cleaned_line = cleaned_line.strip()
            
            if cleaned_line:
                cleaned_lines.append(cleaned_line)
        
        # Объединяем очищенные строки
        result = ' '.join(cleaned_lines)
        
        # Дополнительная очистка: убираем лишние пробелы
        result = re.sub(r'\s+', ' ', result).strip()
        
        logger.debug(f"Текст очищен. Исходная длина: {len(transcription_text)}, итоговая: {len(result)}")
        logger.debug(f"Первые 100 символов очищенного текста: {result[:100]}...")
        
        return result
        
    except Exception as e:
        logger.error(f"Ошибка при очистке текста транскрипции: {e}")
        # В случае ошибки возвращаем исходный текст
        return transcription_text



# Пример использования
if __name__ == "__main__":
    # Асинхронный пример (новая функция)
    async def main():
        test_url = "https://www.youtube.com/watch?v=jtATbpMqbL4"
        
        print("=== Тест обычной транскрипции ===")
        transcription, clean_transcription = await process_audio_fedor_api(test_url)
        print(f"Сырая транскрипция: {transcription[:2000]}...")
        print(f"Очищенная транскрипция: {clean_transcription[:2000]}...")
        
    
    # Раскомментируйте для тестирования с реальным API:
    asyncio.run(main())
    