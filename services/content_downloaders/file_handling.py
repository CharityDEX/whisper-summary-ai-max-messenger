import re
import aiohttp
import io
import tempfile
import os
import shutil # Added for file copying
import asyncio # Added for running sync I/O in threads
import time # Added for timing
import logging # Added for logging
import logging.handlers # Added for log rotation
import datetime # Added for log filenames with dates
from pathlib import Path # Added for path operations
import httpx # Added for httpx fallback
import functools # Added for functools.partial if needed, or general utility
import aiofiles # For async file operations with httpx
from services.init_bot import bot
from models.orm import get_user, add_download_record, update_download_record, \
    update_processing_session  # Added ORM functions
from models.model import DownloadStatus # Added Enum


# Configure logging to write to a separate file
def setup_file_logger():
    """Setup a dedicated logger for file_handling operations that writes to a separate log file.
    Returns the configured logger."""
    logger = logging.getLogger(__name__)
    
    # Don't re-create handlers if logger is already configured
    if logger.handlers:
        return logger
    
    # Create logs directory if it doesn't exist
    logs_dir = Path('logs')
    logs_dir.mkdir(exist_ok=True)
    
    # Create a log file with today's date in the name
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    log_file = logs_dir / f'file_downloads_{today}.log'
    
    # Create a file handler that logs everything (DEBUG level and above)
    # Use RotatingFileHandler to limit log file size (10MB) and keep 5 backup files
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    
    # Create a more detailed formatter for the file log
    file_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    
    # Add the handlers to the logger
    logger.addHandler(file_handler)
    
    # Set the logger level to control which messages are processed
    logger.setLevel(logging.INFO)
    
    # Debug message to confirm logger setup
    logger.debug(f"File logging configured to write to {log_file}")
    
    return logger

# Get and configure logger for this module
logger = setup_file_logger()


async def _get_url_size(url: str) -> int | None:
    """Attempts to get the file size from a URL using a HEAD request."""
    try:
        # Try with aiohttp first
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200 and 'Content-Length' in response.headers:
                    try:
                        size = int(response.headers['Content-Length'])
                        logger.debug(f"Got Content-Length for {url} via aiohttp: {size}")
                        return size
                    except (ValueError, TypeError):
                        logger.warning(f"Could not parse Content-Length header for URL {url} via aiohttp. Header: '{response.headers.get('Content-Length')}'")
                else:
                    logger.debug(f"Could not get Content-Length for URL {url} via aiohttp. Status: {response.status}, Headers: {response.headers}")
                    # If aiohttp fails with 403, try httpx for HEAD as well, as it might handle headers differently
                    if response.status == 403: 
                        pass # Fall through to httpx attempt
                    else:
                        return None
    except asyncio.TimeoutError:
         logger.warning(f"Timeout while fetching headers for URL {url} via aiohttp.")
    except aiohttp.ClientError as e:
        logger.warning(f"ClientError while fetching headers for URL {url} via aiohttp: {e}")
    except Exception as e: # Catch other potential errors
        logger.warning(f"Unexpected error while fetching headers for URL {url} via aiohttp: {type(e).__name__}: {e}")

    # Fallback or direct attempt with httpx if aiohttp failed or was skipped for 403
    try:
        async with httpx.AsyncClient(http2=True, follow_redirects=True, timeout=10.0) as client:
            head_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': '*/*',
            }
            response = await client.head(url, headers=head_headers)
            if response.status_code == 200 and 'content-length' in response.headers:
                try:
                    size = int(response.headers['content-length'])
                    logger.debug(f"Got Content-Length for {url} via httpx: {size}")
                    return size
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse Content-Length header for URL {url} via httpx. Header: '{response.headers.get('content-length')}'")
                    return None
            else:
                logger.debug(f"Could not get Content-Length for URL {url} via httpx. Status: {response.status_code}, Headers: {response.headers}")
                return None
    except httpx.TimeoutException:
        logger.warning(f"Timeout while fetching headers for URL {url} via httpx.")
        return None
    except httpx.RequestError as e:
        logger.warning(f"RequestError while fetching headers for URL {url} via httpx: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error while fetching headers for URL {url} via httpx: {type(e).__name__}: {e}")
        return None


async def _download_to_disk(
    source_type: str,
    identifier: str,
    file_name: str | None,
    download_coroutine,
    specific_source: str | None = None,
    temp_dir: str | None = None,
    download_method: str | None = None,
    additional_data: dict | None = None
) -> str:
    """Helper to download content or copy existing local file to a temporary disk file."""
    suffix = f"_{file_name}" if file_name else None
    temp_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=temp_dir)
    temp_filepath = temp_file.name
    temp_file.close()

    try:
        if source_type == 'url':
            await download_coroutine(identifier, temp_filepath, specific_source=specific_source, download_method=download_method, additional_data=additional_data)
        else: # telegram
            await download_coroutine(identifier, temp_filepath)
        return temp_filepath
    except Exception as e:
        if os.path.exists(temp_filepath):
            # Clean up temp file on error
            try:
                os.remove(temp_filepath)
            except OSError as remove_err:
                 logger.error(f"Failed to remove temporary file {temp_filepath} after error: {remove_err}")
        logger.error(f"Error during {source_type} '{identifier}' download/copy to disk: {type(e).__name__}: {e}")
        raise


async def _download_to_buffer(source_type: str, identifier: str, download_coroutine, specific_source: str | None = None, download_method: str | None = None, additional_data: dict | None = None) -> bytes:
    """Helper to download content or read existing local file into a memory buffer."""
    buffer = io.BytesIO()
    try:
        if source_type == 'url':
            await download_coroutine(identifier, buffer, specific_source=specific_source, download_method=download_method, additional_data=additional_data)
        else: # telegram
            await download_coroutine(identifier, buffer)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"Error during {source_type} '{identifier}' download/read to buffer: {type(e).__name__}: {e}")
        raise


async def _download_url_content(url: str, destination: str | io.BytesIO, specific_source: str | None = None, download_method: str | None = None, additional_data: dict | None = None):
    """Coroutine to perform the actual URL download (to disk or buffer)."""
    # Reduced default timeout slightly, ensure it's less than any upstream timeout
    timeout = aiohttp.ClientTimeout(total=7000, connect=60, sock_read=7000)
    destination_info = f"path {destination}" if isinstance(destination, str) else "buffer"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    }

    if 'vk' in url or 'vkvideo' in url:
        headers['Referer'] = 'https://vk.com/'
    if 'okcdn.ru' in url or 'oneme.ru' in url or 'max.ru' in url:
        headers['Referer'] = 'https://max.ru/'
        headers['Origin'] = 'https://max.ru'
    if download_method == 'fedor_api':
        # print('fedor_api, AAAAAAAAAAAAAAAAAAAAAAA')
        headers = {'Accept': 'application/json'}
        if additional_data is not None:
            headers['Authorization'] = f'Token {additional_data.get("token")}'
        # print(headers)

    logger.debug(f"Starting URL download: {url} to {destination_info}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout, headers=headers) as response:
                response.raise_for_status() # Check for HTTP errors like 4xx, 5xx
                if isinstance(destination, str): # Path to disk file
                    logger.debug(f"Writing URL {url} content chunk by chunk to {destination}")
                    bytes_written = 0
                    async with aiofiles.open(destination, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            await f.write(chunk)
                            bytes_written += len(chunk)
                    logger.debug(f"Finished writing {bytes_written} bytes from {url} to {destination}")
                elif isinstance(destination, io.BytesIO): # Buffer
                    logger.debug(f"Reading entire URL {url} content into buffer")
                    content = await response.read()
                    destination.write(content)
                    logger.debug(f"Finished reading {len(content)} bytes from {url} into buffer")
                else:
                    # This case should ideally not be reached due to checks in calling functions
                    err_msg = "Invalid destination type for _download_url_content"
                    logger.error(err_msg)
                    raise TypeError(err_msg)
    except asyncio.TimeoutError as e:
        if specific_source == 'instagram':
            logger.warning(f"Got timeout for Instagram URL {url} with aiohttp. Trying with httpx.")
            try:
                # Pass original headers, httpx might use them or its own defaults
                await _download_url_content_httpx(url, destination, headers, specific_source, download_method)
                logger.debug(f"Successfully downloaded Instagram URL {url} with httpx after aiohttp timeout.")
                return # Exit if httpx download was successful
            except Exception as httpx_e:
                logger.error(f"httpx fallback failed for Instagram URL {url}: {type(httpx_e).__name__}: {httpx_e}")
                raise e # Re-raise the original aiohttp error if httpx also fails
        else:
            logger.error(f"Timeout during URL download: {url} to {destination_info}")
            raise
    except aiohttp.ClientConnectorError as e:
        logger.error(f"Connection error during URL download: {url} to {destination_info}. Error: {e}")
        if specific_source == 'instagram':
            logger.warning(f"Got connection error for Instagram URL {url} with aiohttp. Trying with httpx.")
            try:
                # Pass original headers, httpx might use them or its own defaults
                await _download_url_content_httpx(url, destination, headers, specific_source, download_method)
                logger.debug(f"Successfully downloaded Instagram URL {url} with httpx after aiohttp connection error.")
                return # Exit if httpx download was successful
            except (httpx.ConnectError, httpx.NetworkError) as httpx_conn_e:
                logger.warning(f"httpx also got connection error for Instagram URL {url}. Trying SOCKS5 proxy as last resort.")
                try:
                    await _download_url_content_httpx_with_proxy(url, destination, headers, download_method)
                    logger.debug(f"Successfully downloaded Instagram URL {url} with SOCKS5 proxy after both aiohttp and httpx connection errors.")
                    return # Exit if SOCKS5 proxy download was successful
                except Exception as proxy_e:
                    logger.error(f"SOCKS5 proxy fallback also failed for Instagram URL {url}: {type(proxy_e).__name__}: {proxy_e}")
                    raise e # Re-raise the original aiohttp error if all methods fail
            except Exception as httpx_e:
                logger.error(f"httpx fallback failed for Instagram URL {url}: {type(httpx_e).__name__}: {httpx_e}")
                raise e # Re-raise the original aiohttp error if httpx also fails
        raise
    except aiohttp.ClientResponseError as e:
        logger.error(f"HTTP error {e.status} during URL download: {url} to {destination_info}. Message: {e.message}")
        # okcdn.ru / Max messenger CDN URLs may return 400 due to IP restrictions;
        # retry with httpx (different connection handling) then with proxy
        if e.status == 400 and ('okcdn.ru' in url or 'oneme.ru' in url):
            logger.warning(f"Got 400 for Max CDN URL {url}. Trying httpx fallback.")
            try:
                await _download_url_content_httpx(url, destination, headers, specific_source, download_method)
                logger.info(f"Successfully downloaded Max CDN URL {url} with httpx after aiohttp 400.")
                return
            except Exception as httpx_e:
                logger.warning(f"httpx fallback also failed for Max CDN URL: {httpx_e}. Trying with proxy.")
                try:
                    await _download_url_content_httpx_with_proxy(url, destination, headers, download_method)
                    logger.info(f"Successfully downloaded Max CDN URL {url} via proxy.")
                    return
                except Exception as proxy_e:
                    logger.error(f"All download methods failed for Max CDN URL {url}: {proxy_e}")
                    raise e
        if e.status == 403 and specific_source == 'instagram':
            logger.warning(f"Got 403 for Instagram URL {url} with aiohttp. Trying with httpx.")
            try:
                # Pass original headers, httpx might use them or its own defaults
                await _download_url_content_httpx(url, destination, headers, specific_source, download_method)
                logger.debug(f"Successfully downloaded Instagram URL {url} with httpx after aiohttp 403.")
                return # Exit if httpx download was successful
            except httpx.HTTPStatusError as httpx_403_e:
                if httpx_403_e.response.status_code == 403:
                    logger.warning(f"httpx also got 403 for Instagram URL {url}. Trying SOCKS5 proxy as last resort.")
                    try:
                        await _download_url_content_httpx_with_proxy(url, destination, headers, download_method)
                        logger.debug(f"Successfully downloaded Instagram URL {url} with SOCKS5 proxy after both aiohttp and httpx 403.")
                        return # Exit if SOCKS5 proxy download was successful
                    except Exception as proxy_e:
                        logger.error(f"SOCKS5 proxy fallback also failed for Instagram URL {url}: {type(proxy_e).__name__}: {proxy_e}")
                        raise e # Re-raise the original aiohttp error if all methods fail
                else:
                    logger.error(f"httpx returned non-403 error for Instagram URL {url}: {httpx_403_e}")
                    raise e # Re-raise the original aiohttp error
            except Exception as httpx_e:
                logger.error(f"httpx fallback failed for Instagram URL {url}: {type(httpx_e).__name__}: {httpx_e}")
                raise e # Re-raise the original aiohttp error if httpx also fails
        raise
    except Exception as e:
        logger.error(f"Unexpected error during URL download ({url} to {destination_info}): {type(e).__name__}: {e}")
        raise


# --- Helper for reading local file to buffer --- Needed for asyncio.to_thread ---
def _read_local_file_sync(file_path: str) -> bytes:
    try:
        logger.debug(f"Reading local file {file_path} synchronously")
        with open(file_path, "rb") as f:
            data = f.read()
        logger.debug(f"Successfully read {len(data)} bytes from {file_path}")
        return data
    except FileNotFoundError:
        logger.error(f"Local file not found for reading: {file_path}")
        raise # Re-raise to be caught by the calling async function
    except Exception as e:
        logger.error(f"Error reading local file {file_path}: {type(e).__name__}: {e}")
        raise # Re-raise
# -----------------------------------------------------------------------------


async def _download_url_content_httpx(url: str, destination: str | io.BytesIO, request_headers: dict, specific_source: str | None = None, download_method: str | None = None):
    """Helper to download URL content using httpx, for fallback scenarios."""
    destination_info = f"path {destination}" if isinstance(destination, str) else "buffer"
    logger.debug(f"Starting URL download with httpx: {url} to {destination_info}")

    # httpx timeout configuration (can be adjusted)
    timeout_config = httpx.Timeout(connect=60.0, read=7000.0, write=60.0, pool=None) # total could be implicitly larger

    try:
        async with httpx.AsyncClient(http2=True, headers=request_headers, timeout=timeout_config, follow_redirects=True) as client:
            async with client.stream('GET', url) as response:
                response.raise_for_status() # Check for HTTP errors like 4xx, 5xx

                if isinstance(destination, str): # Path to disk file
                    logger.debug(f"Writing URL {url} content chunk by chunk to {destination} using httpx and aiofiles")
                    bytes_written = 0
                    async with aiofiles.open(destination, 'wb') as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            await f.write(chunk)
                            bytes_written += len(chunk)
                    logger.debug(f"Finished writing {bytes_written} bytes from {url} to {destination} using httpx")
                elif isinstance(destination, io.BytesIO): # Buffer
                    logger.debug(f"Reading entire URL {url} content into buffer using httpx")
                    content = await response.aread() # Reads entire body
                    destination.write(content)
                    logger.debug(f"Finished reading {len(content)} bytes from {url} into buffer using httpx")
                else:
                    err_msg = "Invalid destination type for _download_url_content_httpx"
                    logger.error(err_msg)
                    raise TypeError(err_msg)
    except httpx.TimeoutException as e:
        if specific_source == 'instagram':
            logger.warning(f"httpx got timeout for Instagram URL {url}. Trying SOCKS5 proxy as last resort.")
            try:
                await _download_url_content_httpx_with_proxy(url, destination, request_headers, download_method)
                logger.debug(f"Successfully downloaded Instagram URL {url} with SOCKS5 proxy after httpx timeout.")
                return # Exit if SOCKS5 proxy download was successful
            except Exception as proxy_e:
                logger.error(f"SOCKS5 proxy fallback also failed for Instagram URL {url}: {type(proxy_e).__name__}: {proxy_e}")
                # Re-raise the original httpx timeout error if SOCKS5 proxy also fails
                raise e 
        else:
            logger.error(f"Timeout during httpx URL download: {url} to {destination_info}")
            raise
    except httpx.HTTPStatusError as e: # Specific httpx error for non-2xx status codes
        if e.response.status_code == 403 and specific_source == 'instagram':
            logger.warning(f"httpx got 403 for Instagram URL {url}. Trying SOCKS5 proxy as last resort.")
            try:
                await _download_url_content_httpx_with_proxy(url, destination, request_headers, download_method)
                logger.debug(f"Successfully downloaded Instagram URL {url} with SOCKS5 proxy after httpx 403.")
                return # Exit if SOCKS5 proxy download was successful
            except Exception as proxy_e:
                logger.error(f"SOCKS5 proxy fallback also failed for Instagram URL {url}: {type(proxy_e).__name__}: {proxy_e}")
                # Re-raise the original httpx 403 error if SOCKS5 proxy also fails
                raise e 
        else:
            logger.error(f"HTTP error {e.response.status_code} during httpx URL download: {url} to {destination_info}. Response: {e.response.text[:200]}")
            raise
    except (httpx.ConnectError, httpx.NetworkError) as e: # Connection and network errors
        if specific_source == 'instagram':
            logger.warning(f"httpx got connection/network error for Instagram URL {url}. Trying SOCKS5 proxy as last resort.")
            try:
                await _download_url_content_httpx_with_proxy(url, destination, request_headers, download_method)
                logger.debug(f"Successfully downloaded Instagram URL {url} with SOCKS5 proxy after httpx connection error.")
                return # Exit if SOCKS5 proxy download was successful
            except Exception as proxy_e:
                logger.error(f"SOCKS5 proxy fallback also failed for Instagram URL {url}: {type(proxy_e).__name__}: {proxy_e}")
                # Re-raise the original httpx connection error if SOCKS5 proxy also fails
                raise e 
        else:
            logger.error(f"Connection/network error during httpx URL download: {url} to {destination_info}. Error: {e}")
            raise
    except httpx.RequestError as e: # General httpx request errors (other protocol errors)
        logger.error(f"Request error during httpx URL download: {url} to {destination_info}. Error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during httpx URL download ({url} to {destination_info}): {type(e).__name__}: {e}")
        raise


async def _download_telegram_content(file_id: str, destination: str | io.BytesIO, additional_data: dict | None = None):
    """Coroutine to handle Telegram files on a local Bot API server:
       Copies (for disk) or reads (for buffer) the file directly from the local path.
    """
    destination_info = f"path {destination}" if isinstance(destination, str) else "buffer"
    logger.debug(f"Handling local TG file ID {file_id} for {destination_info}")
    try:
        logger.debug(f"Getting file info for TG id: {file_id}")
        file_info = await bot.get_file(file_id)
        local_file_path = file_info.file_path # The direct path on the local server

        if not os.path.exists(local_file_path):
             logger.error(f"Telegram file path reported but not found on local disk: {local_file_path}")
             raise FileNotFoundError(f"Telegram file path reported but not found on local disk: {local_file_path}")

        logger.debug(f"TG file found locally at: {local_file_path}. Proceeding with local access.")

        if isinstance(destination, str): # Path to temporary disk file
            logger.debug(f"Copying local TG file {local_file_path} to temporary disk path: {destination}")
            try:
                # Run blocking shutil.copy in a separate thread
                await asyncio.to_thread(shutil.copy, local_file_path, destination)
                logger.debug(f"Finished copying local TG file to: {destination}")
            except Exception as e:
                logger.error(f"Error copying local file {local_file_path} to {destination}: {type(e).__name__}: {e}")
                # Attempt to clean up the potentially partially created destination file
                if os.path.exists(destination):
                    try:
                        os.remove(destination)
                        logger.debug(f"Removed partially copied file {destination}")
                    except OSError as remove_err:
                        logger.error(f"Could not remove temporary file {destination} after copy error: {remove_err}")
                raise # Re-raise the copy error

        elif isinstance(destination, io.BytesIO): # Buffer
            logger.debug(f"Reading local TG file into buffer: {local_file_path}")
            # Run blocking file read in a separate thread
            file_bytes = await asyncio.to_thread(_read_local_file_sync, local_file_path)
            destination.write(file_bytes)
            logger.debug(f"Finished reading {len(file_bytes)} bytes from local TG file into buffer.")

        else:
            err_msg = "Invalid destination type for _download_telegram_content"
            logger.error(err_msg)
            raise TypeError(err_msg)

    except Exception as e:
        # Catch errors from bot.get_file or file existence check
        logger.error(f"Error during local Telegram file handling for file_id {file_id}: {type(e).__name__}: {e}")
        raise # Re-raise the exception


# Helper to get file size async
def _get_local_file_size_sync(file_path: str) -> int | None:
    try:
        size = os.path.getsize(file_path)
        logger.debug(f"Got size for local file {file_path}: {size}")
        return size
    except Exception as e:
        logger.warning(f"Error getting size for local file {file_path}: {e}")
        return None


async def download_file(
    source_type: str, # 'url' или 'telegram'
    identifier: str,  # URL или file_id
    destination_type: str = 'disk', # 'disk' | 'buffer' | 'auto'
    specific_source: str | None = None, # 'youtube', 'instagram', etc.
    file_name: str | None = None, # Желаемое имя файла для диска
    user_data: dict | None = {},  # Для будущей логики
    session_id: str | None = None,  # UUID сессии обработки
    attempt_number: int = 1,  # Номер попытки в рамках сессии
    download_method: str | None = None,  # Метод загрузки
    max_buffer_bytes: int = 10 * 1024 * 1024, # Порог для auto-режима (10MB)
    temp_dir: str | None = None, # Кастомная директория для временных файлов
    additional_data: dict | None = None, # Дополнительные данные для загрузки
    add_file_size_to_session: bool = False
) -> str | bytes:
    """
    Загружает файл из указанного источника (URL или Telegram)
    в указанное назначение (диск или буфер памяти),
    логируя статистику в базу данных с поддержкой ProcessingSession.

    Args:
        source_type: Тип источника ('url' или 'telegram').
        identifier: Идентификатор ресурса (URL или file_id).
        destination_type: Тип назначения ('disk' или 'buffer').
        specific_source: Конкретный источник для URL (опционально).
        file_name: Предлагаемое имя файла (используется при сохранении на диск).
        user_data: Данные пользователя (опционально).
        session_id: UUID сессии обработки для связи с ProcessingSession.
        attempt_number: Номер попытки загрузки в рамках сессии (1, 2, 3...).
        download_method: Метод загрузки ('direct', 'rapidapi', 'cobalt', etc.).
        additional_data: Дополнительные данные для загрузки (опционально). Например, токен для Fedor API.
    Returns:
        Путь к файлу на диске, если destination_type='disk' или выбран 'auto'->'disk'.
        Содержимое файла в виде байтов, если destination_type='buffer' или выбран 'auto'->'buffer'.

    Raises:
        ValueError: Если некорректные типы источника/назначения или пользователь не найден.
        FileNotFoundError: Если локальный файл Telegram не найден.
        aiohttp.ClientError: При ошибках скачивания URL.
        # Другие возможные исключения (Telegram API, файловые, DB errors)

    """
    start_time = time.monotonic()
    record_id: int | None = None
    result: str | bytes | None = None
    identifier = str(identifier) # Так как иногда бывает как объект URL

    if user_data is not None:
        user_db_id: int = user_data.get('id', 0)
        telegram_id: int = user_data.get('telegram_id')
    else:
        user_db_id = 0
        telegram_id = 0

    if source_type == 'url' and specific_source is None:
        # Try to identify source automatically if not provided for URL
        identified_source = identify_url_source(identifier)
        if identified_source:
            logger.debug(f"Identified source for {identifier} as '{identified_source}'")
            specific_source = identified_source
        else:
            logger.debug(f"Could not identify specific source for URL: {identifier}")

    log_prefix = f"[User:{telegram_id}|Session:{session_id}|Download:{record_id if record_id else 'N/A'}]"
    logger.debug(f"{log_prefix} Initiating download: source={source_type}, id={identifier}, dest={destination_type}, specific_src={specific_source}, attempt={attempt_number}, method={download_method or 'direct'}")
    try:
        # --- Get file size beforehand if possible --- (Now uses logger)
        initial_file_size: int | None = None
        if source_type == 'url':
            initial_file_size = await _get_url_size(identifier)
            if initial_file_size is not None:
                logger.debug(f"{log_prefix} Source URL: Estimated file size = {initial_file_size} bytes.")
            else:
                logger.debug(f"{log_prefix} Source URL: Could not determine file size beforehand.")
        elif source_type == 'telegram':
            try:
                file_info = await bot.get_file(identifier)
                initial_file_size = file_info.file_size
                if initial_file_size is not None:
                    logger.debug(f"{log_prefix} Source Telegram: File size = {initial_file_size} bytes.")
                else:
                    logger.debug(f"{log_prefix} Source Telegram: File size not available in metadata.")
            except Exception as e:
                logger.warning(f"{log_prefix} Could not get Telegram file info for id {identifier}. Error: {e}")

        # TODO: Implement size check logic here
        # ---------------------------------------------

        # Определяем финальный тип назначения при destination_type='auto'
        if destination_type == 'auto':
            if initial_file_size is not None and initial_file_size <= max_buffer_bytes:
                destination_type = 'buffer'
            else:
                destination_type = 'disk'

        # --- Add initial record to DB --- 
        # Ensure user_db_id is valid before calling
        if not isinstance(user_db_id, int):
             err_msg = f"{log_prefix} Invalid user_db_id type: {type(user_db_id)}"
             logger.error(err_msg)
             raise TypeError(err_msg)
             
        record_id = await add_download_record(
            user_id=user_db_id,
            source_type=source_type,
            identifier=identifier,
            destination_type=destination_type,
            specific_source=specific_source,
            initial_file_size=initial_file_size,
            session_id=session_id,
            attempt_number=attempt_number,
            download_method=download_method or 'direct'  # По умолчанию 'direct'
        )
        if add_file_size_to_session:
            #Добавляем в сессию размер
            await update_processing_session(original_file_size=initial_file_size, session_id=session_id)
        if record_id is None:
            err_msg = f"{log_prefix} Failed to create download record in database."
            logger.error(err_msg)
            raise RuntimeError(err_msg)
        # Update log prefix with the new record_id
        log_prefix = f"[User:{telegram_id}|Session:{session_id}|Download:{record_id}]"
        logger.debug(f"{log_prefix} Created DB record.")

        # --- Select download coroutine --- 
        download_coroutine = None
        if source_type == 'url':
            download_coroutine = _download_url_content
        elif source_type == 'telegram':
            download_coroutine = _download_telegram_content
        else:
            # This error will be caught by the main try/except
            raise ValueError(f"Unknown source_type: {source_type}")

        # --- Perform download --- 
        logger.debug(f"{log_prefix} Starting main download operation...")
        if destination_type == 'disk':
            if source_type == 'telegram':
                # Optimization for local Bot API: return the local file path directly (no copy)
                try:
                    file_info = await bot.get_file(identifier)
                    local_file_path = file_info.file_path
                    if not os.path.isabs(local_file_path) or not os.path.exists(local_file_path):
                        # Fallback to legacy copy flow if path is not local/accessible
                        result = await _download_to_disk(
                            source_type,
                            identifier,
                            file_name,
                            download_coroutine,
                            specific_source=specific_source,
                            temp_dir=temp_dir,
                            download_method=download_method,
                            additional_data=additional_data
                        )
                        logger.debug(f"{log_prefix} Telegram fallback: copied to temp path: {result}")
                    else:
                        result = local_file_path
                        logger.debug(f"{log_prefix} Telegram local path returned without copy: {result}")
                except Exception as tg_err:
                    logger.warning(f"{log_prefix} Could not get local Telegram path directly: {tg_err}. Falling back to copy.")
                    result = await _download_to_disk(
                        source_type,
                        identifier,
                        file_name,
                        download_coroutine,
                        specific_source=specific_source,
                        temp_dir=temp_dir,
                        download_method=download_method,
                        additional_data=additional_data
                    )
                    logger.debug(f"{log_prefix} Telegram fallback: copied to temp path: {result}")
            else:
                result = await _download_to_disk(
                    source_type,
                    identifier,
                    file_name,
                    download_coroutine,
                    specific_source=specific_source,
                    temp_dir=temp_dir,
                    download_method=download_method,
                    additional_data=additional_data
                )
                logger.debug(f"{log_prefix} Download to disk finished. Path: {result}")
        elif destination_type == 'buffer':
            result = await _download_to_buffer(source_type, identifier, download_coroutine, specific_source=specific_source, download_method=download_method, additional_data=additional_data)
            logger.debug(f"{log_prefix} Download to buffer finished. Size: {len(result)} bytes")
        else:
            # This error will be caught by the main try/except
            raise ValueError(f"Unknown destination_type: {destination_type}")

        # --- Success: Update DB record --- 
        duration = time.monotonic() - start_time
        final_size = None
        temp_path = None
        if isinstance(result, str): # Disk download
             temp_path = result
             final_size = await asyncio.to_thread(_get_local_file_size_sync, temp_path)
             if final_size == 0:
                 raise ValueError(f"{log_prefix} Downloaded file is empty: {temp_path}")
             logger.debug(f"{log_prefix} Final size from disk: {final_size}")
        elif isinstance(result, bytes): # Buffer download
            final_size = len(result)
            if final_size == 0:
                raise ValueError(f"{log_prefix} Downloaded file is empty: {temp_path}")
            logger.debug(f"{log_prefix} Final size from buffer: {final_size}")

        logger.debug(f"{log_prefix} Download successful. Updating DB record. Duration: {duration:.2f}s")
        await update_download_record(
            record_id=record_id,
            status=DownloadStatus.DOWNLOADED, # Or COMPLETED
            final_file_size=final_size,
            duration_seconds=duration,
            temp_file_path=temp_path
        )
        return result # Return path or bytes on success

    except Exception as e:
        duration = time.monotonic() - start_time
        error_message = f"{type(e).__name__}: {str(e)}"
        # Ensure traceback is logged for unexpected errors
        if not isinstance(e, (ValueError, FileNotFoundError, aiohttp.ClientError, asyncio.TimeoutError)):
             logger.exception(f"{log_prefix} Unexpected error during download: {error_message}")
        else:
            if 'Downloaded file is empty' in error_message:
                error_message = 'Downloaded file is empty'
            else:
                logger.error(f"{log_prefix} Download failed: {error_message}")

        if record_id:
            # --- Failure: Update DB record --- 
            logger.debug(f"{log_prefix} Updating DB record to ERROR status. Duration: {duration:.2f}s")
            await update_download_record(
                record_id=record_id,
                status=DownloadStatus.ERROR,
                duration_seconds=duration,
                error_message=error_message # Store error message
            )
        else:
             logger.error(f"{log_prefix} Download failed before DB record could be created.")

        # Re-raise the exception so the caller knows about the failure
        raise


def identify_url_source(url: str) -> str | None:
    """Identifies the specific source platform from a given URL.

    Args:
        url: The URL string.

    Returns:
        A string representing the source (e.g., 'youtube', 'instagram', 'twitter',
        'vk', 'facebook', 'rutube', 'reddit', 'vimeo') or None if not identified.
    """
    url = str(url)

    if 'vk.com' in url or 'vkvideo.ru' in url or 'vkvd' in url:
        return 'vk'
    elif 'youtube.com' in url or 'youtu.be' in url:
        return 'youtube'
    elif 'instagram.com' in url:
        return 'instagram'
    elif 'facebook.com' in url:
        return 'facebook'
    elif 'rutube.ru' in url:
        return 'rutube'
    elif 'twitter.com' in url or 'x.com' in url:
        return 'twitter'
    elif 'reddit.com' in url:
        return 'reddit'
    elif 'vimeo.com' in url:
        return 'vimeo'
    elif 'tiktok.com' in url:
        return 'tiktok'
    elif 'drive.google.com' in url:
        return 'google_drive'

    # Use logger instead of print for consistency
    logger.debug(f"Could not identify source for URL: {url}")
    return None


async def _download_url_content_httpx_with_proxy(url: str, destination: str | io.BytesIO, request_headers: dict, download_method: str | None = None):
    """Helper to download Instagram URL content using httpx with SOCKS5 proxy as last resort."""
    destination_info = f"path {destination}" if isinstance(destination, str) else "buffer"
    logger.warning(f"Starting Instagram URL download with SOCKS5 proxy (last resort): {url} to {destination_info}")

    # SOCKS5 proxy configuration - настройка из test.py
    proxies = {
        "all://": "socks5://localhost:9052"
    }
    
    # Увеличенный таймаут для туннеля
    timeout_config = httpx.Timeout(connect=60.0, read=120.0, write=60.0, pool=None)

    try:
        async with httpx.AsyncClient(
            http2=True, 
            headers=request_headers, 
            timeout=timeout_config, 
            follow_redirects=True, 
            proxies=proxies
        ) as client:
            async with client.stream('GET', url) as response:
                response.raise_for_status()

                if isinstance(destination, str):
                    logger.debug(f"Writing Instagram URL {url} content via SOCKS5 proxy to {destination}")
                    bytes_written = 0
                    async with aiofiles.open(destination, 'wb') as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            await f.write(chunk)
                            bytes_written += len(chunk)
                    logger.debug(f"Successfully downloaded Instagram URL via SOCKS5 proxy: {bytes_written} bytes from {url}")
                elif isinstance(destination, io.BytesIO):
                    logger.debug(f"Reading Instagram URL {url} content via SOCKS5 proxy into buffer")
                    content = await response.aread()
                    destination.write(content)
                    logger.debug(f"Successfully downloaded Instagram URL via SOCKS5 proxy: {len(content)} bytes from {url}")
                else:
                    err_msg = "Invalid destination type for _download_url_content_httpx_with_proxy"
                    logger.error(err_msg)
                    raise TypeError(err_msg)
                    
    except httpx.ConnectError as e:
        logger.error(f"SOCKS5 proxy connection error for {url}: {e}. Is the SSH tunnel running on localhost:9052?")
        raise
    except httpx.TimeoutException:
        logger.error(f"Timeout during SOCKS5 proxy download: {url} to {destination_info}")
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error {e.response.status_code} during SOCKS5 proxy download: {url}. Response: {e.response.text[:200]}")
        raise
    except httpx.RequestError as e:
        logger.error(f"Request error during SOCKS5 proxy download: {url}. Error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during SOCKS5 proxy download ({url} to {destination_info}): {type(e).__name__}: {e}")
        raise


# --- Конец функции ---

# Пример использования (нужно будет адаптировать):
# async def main():
#     try:
#         # Скачать из URL на диск
#         filepath = await download_file('url', 'https://httpbin.org/image/png', 'disk', file_name='image.png')
#         print(f"File downloaded to: {filepath}")
#         # ... обработать файл ...
#         if filepath and os.path.exists(filepath):
#              os.remove(filepath) # Не забыть удалить временный файл после использования!

#         # Скачать из Telegram в буфер (если файл маленький)
#         file_id = "YOUR_TELEGRAM_FILE_ID"
#         file_bytes = await download_file('telegram', file_id, 'buffer')
#         print(f"Downloaded {len(file_bytes)} bytes from Telegram.")
#         # ... обработать байты ...

#     except Exception as e:
#         print(f"An error occurred: {e}")

# if __name__ == "__main__":
#     import asyncio
#     # Пример URL для теста
#     TEST_URL = 'https://via.placeholder.com/150'
#     TEST_TG_ID = 'simulated_telegram_file_id_123'

#     async def main():
#         temp_files_to_clean = []
#         try:
#             print("Testing URL download to disk...")
#             filepath_disk = await download_file('url', TEST_URL, 'disk', file_name='url_disk_test.png')
#             print(f"URL -> Disk: {filepath_disk} (Exists: {os.path.exists(filepath_disk)})")
#             if filepath_disk: temp_files_to_clean.append(filepath_disk)

#             print("Testing URL download to buffer...")
#             file_bytes_url = await download_file('url', TEST_URL, 'buffer')
#             print(f"URL -> Buffer: {len(file_bytes_url)} bytes")

#             print("Testing Telegram download to disk...")
#             filepath_tg_disk = await download_file('telegram', TEST_TG_ID, 'disk', file_name='tg_disk_test.dat')
#             print(f"TG -> Disk: {filepath_tg_disk} (Exists: {os.path.exists(filepath_tg_disk)})")
#             if filepath_tg_disk: temp_files_to_clean.append(filepath_tg_disk)

#             print("Testing Telegram download to buffer...")
#             file_bytes_tg = await download_file('telegram', TEST_TG_ID, 'buffer')
#             print(f"TG -> Buffer: {len(file_bytes_tg)} bytes")

#             # Test invalid source/destination
#             try:
#                 print("Testing invalid source...")
#                 await download_file('invalid_source', 'some_id', 'disk')
#             except ValueError as e:
#                 print(f"Caught expected error: {e}")

#             try:
#                 print("Testing invalid destination...")
#                 await download_file('url', TEST_URL, 'invalid_destination')
#             except ValueError as e:
#                 print(f"Caught expected error: {e}")

#         except Exception as e:
#             print(f"An unexpected error occurred during testing: {e}")
#         finally:
#             print("Cleaning up temporary files...")
#             for f_path in temp_files_to_clean:
#                 if os.path.exists(f_path):
#                     try:
#                         os.remove(f_path)
#                         print(f"Removed: {f_path}")
#                     except OSError as e:
#                         print(f"Error removing {f_path}: {e}")

#     asyncio.run(main())