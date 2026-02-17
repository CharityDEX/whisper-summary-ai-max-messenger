import asyncio
import io
import json
import logging
import random
import re
import sys
import traceback
from typing import Optional

import aiohttp
import yt_dlp
import urllib.request
import os
import aiofiles
from yarl import URL

from services.content_downloaders.fastsaver import download_video_via_fastsaver
from services.init_bot import config
from services.content_downloaders.file_handling import download_file, identify_url_source
from services.content_downloaders.vimeo_downloader import download_vimeo_video
from services.content_downloaders.vk_services import all_media_downloader_api
# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(stream=sys.stdout)
    ]
)

# {'http': 'http://brd-customer-hl_9048c59d-zone-mobile_proxy1:bhpn8x2cv81k@brd.superproxy.io:22225',
#             'https': 'http://brd-customer-hl_9048c59d-zone-mobile_proxy1:bhpn8x2cv81k@brd.superproxy.io:22225'}

logger = logging.getLogger(__name__)


async def get_content_from_url(url: str, user_data: dict, download_mode: str = 'audio', destination_type: str = 'buffer', session_id: str | None = None) -> bytes | str:
    """
    Получает контент из URL, пробуя различные методы загрузки.

    Args:
        url: URL видео
        user_data: данные пользователя
        download_mode: режим загрузки (audio, video)
        destination_type: тип назначения (buffer, disk)
    
    Returns:
        bytes: аудио данные в байтовом формате, если destination_type == 'buffer'
        str: путь к файлу, если destination_type == 'disk'

    Raises:
        Exception: если все методы загрузки не удались
    """
    logger.debug(f"Начинаем загрузку контента из URL: {url}, режим загрузки: {download_mode}, тип назначения: {destination_type}")

    if 'vk.' in url or 'vkvideo' in url:
        logger.debug(f'Обнаружена ссылка на VK видео: {url}')
        try:
            content_buffer: bytes | str = await all_media_downloader_api(url=url, download_mode=download_mode, user_data=user_data, destination_type=destination_type, session_id=session_id)
            return content_buffer
        except Exception as e:
            logger.error(f'Ошибка при загрузке аудио из VK: {e}')
            logger.debug(f'Трассировка: {traceback.format_exc()}')
    
    try:
        logger.debug(f'Пробуем загрузку через cobalt для URL: {url}')
        content_buffer: bytes | str = await cobalt_download_data(video_url=url, download_mode=download_mode, user_data=user_data, destination_type=destination_type, session_id=session_id)
        return content_buffer
    except Exception as e:
        if 'Downloaded file is empty' in str(e):
            pass
        else:
            logger.error(f'Ошибка при загрузке через cobalt: {e}')
            logger.debug(f'Трассировка: {traceback.format_exc()}')

        if 'you' in url:
            try:
                if download_mode == 'audio':
                    try:
                        content_buffer = await youtube_audio_to_buffer_api(video_url=url, user_data=user_data,
                                                                           destination_type=destination_type, session_id=session_id)
                    except:
                        content_buffer = await youtube_search_download_api(video_url=url,
                                                                           user_data=user_data,
                                                                           destination_type=destination_type, session_id=session_id)

                    return content_buffer
                else:
                    content_buffer = await youtube_search_download_api(video_url=url,
                                                                       user_data=user_data,
                                                                       destination_type=destination_type, session_id=session_id)
                    return content_buffer
            except:
                content_buffer = await all_media_downloader_api(url=url,
                                                                download_mode=download_mode,
                                                                user_data=user_data,
                                                                destination_type=destination_type, session_id=session_id)
                return content_buffer

        else:
            try:
                content_buffer = await all_media_downloader_api(url=url,
                                                                download_mode=download_mode,
                                                                user_data=user_data,
                                                                destination_type=destination_type, session_id=session_id)
                return content_buffer
            except Exception as e:
                logger.error(f'Ошибка при загрузке через rapidapi: {traceback.format_exc()}')
                raise Exception(f"Все методы загрузки не удались для URL: {url}")

async def get_audio_from_url(url: str, user_data: dict, session_id: str | None = None) -> bytes | str | None:
    """
    Получает аудио из видео по URL, пробуя различные методы загрузки.

    Args:
        url: URL видео

    Returns:
        bytes: аудио данные в байтовом формате

    Raises:
        Exception: если все методы загрузки не удались
    """
    logger.debug(f"Начинаем загрузку аудио с URL: {url}")

    # Проверяем, является ли URL ссылкой на VK видео
    if 'vk.' in url or 'vkvideo' in url:
        logger.debug(f'Обнаружена ссылка на VK видео: {url}')
        try:
            audio_buffer: bytes = await all_media_downloader_api(url=url, download_mode='audio', user_data=user_data, session_id=session_id)
            logger.debug(f'Успешно загружено аудио из VK, размер: {len(audio_buffer)} байт')
            return audio_buffer
        except Exception as e:
            logger.error(f'Ошибка при загрузке аудио из VK: {e}')
            logger.debug(f'Трассировка: {traceback.format_exc()}')
            raise Exception(f"Не удалось загрузить аудио из VK для URL: {url}")

    try:
        logger.debug(f'Пробуем загрузку через cobalt для URL: {url}')
        audio_buffer: bytes = await cobalt_download_data(video_url=url, download_mode='audio', user_data=user_data, session_id=session_id)
        if len(audio_buffer) == 0:
            logger.error(f'Cobalt. Не удалось загрузить URL, вернулось 0 байт: {url}')
            raise Exception
        logger.debug(f'Успешно загружено через cobalt, размер: {len(audio_buffer)} байт')
        return audio_buffer
    except Exception as e:
        if 'Downloaded file is empty' in str(e):
            pass
        else:
            logger.error(f'Ошибка при загрузке через cobalt: {e}')
            logger.debug(f'Трассировка: {traceback.format_exc()}')

        if 'you' in url:
            try:
                logger.info(f"Trying FastSaver API for URL: {url}, session={session_id}, user={user_data.get('telegram_id') if user_data else None}")
                fastsaver_result = await download_video_via_fastsaver(
                    link=url,
                    user_data=user_data,
                    session_id=session_id,
                    attempt_number=1,
                    destination_type='buffer'
                )
                if isinstance(fastsaver_result, str):
                    # Read file bytes if a path was returned (safety net)
                    async with aiofiles.open(fastsaver_result, 'rb') as f:
                        audio_buffer = await f.read()
                else:
                    audio_buffer = fastsaver_result
                logger.info(f"FastSaver result: {'<bytes>' if isinstance(fastsaver_result, bytes) else fastsaver_result}, session={session_id}, user={user_data.get('telegram_id') if user_data else None}")
                return audio_buffer
            except Exception as e:
                print(e)
            try:
                logger.debug(f'Пробуем альтернативную загрузку через youtube_audio_to_buffer_api для URL: {url}')
                audio_buffer: bytes = await youtube_audio_to_buffer_api(video_url=url, user_data=user_data, session_id=session_id)
                logger.debug(f'Успешно загружено через API, размер: {len(audio_buffer)} байт')
                return audio_buffer
            except Exception as e:
                logger.error(f'Ошибка при загрузке через API: {e}')
                logger.debug(f'Трассировка: {traceback.format_exc()}')

                # try:
                #     random_int = random.randint(1000, 99999999999)
                #     proxy = f'http://brd-customer-hl_dc442fab-zone-web_unlocker1-session-{random_int}:ks3g15fqdzrr@brd.superproxy.io:22225'
                #     logger.info(f'Пробуем загрузку через прокси для URL: {url}')
                #     logger.debug(f'Используемый прокси: {proxy}')
                #     audio_buffer: bytes = await youtube_audio_to_buffer(video_url=url, proxy_url=proxy)
                #     logger.info(f'Успешно загружено через прокси, размер: {len(audio_buffer)} байт')
                #     return audio_buffer
                # except Exception as proxy_error:
                #     logger.error(f'Ошибка при загрузке через прокси: {proxy_error}')
                #     logger.debug(f'Трассировка: {traceback.format_exc()}')
                # raise Exception(f"Все методы загрузки не удались для URL: {url}")
        else:
            try:
                audio_buffer = await all_media_downloader_api(url=url, download_mode='video', user_data=user_data, session_id=session_id)
                return audio_buffer
            except Exception as e:
                logger.error(f'Ошибка при загрузке через rapidapi: {traceback.format_exc()}')
                raise Exception(f"Все методы загрузки не удались для URL: {url}")


async def cobalt_download_data(video_url: str, download_mode: str = 'audio', user_data: dict = None, destination_type: str = 'buffer', session_id: str | None = None) -> bytes | str:
    """
    Загружает аудио через сервис cobalt.

    Args:
        video_url: URL видео

    Returns:
        bytes: аудио данные

    Raises:
        Exception: если произошла ошибка при запросе или обработке ответа
        :param download_mode:
    """
    logger.debug(f"Начинаем загрузку через cobalt для URL: {video_url}")

    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            }

            data: dict = {
                'url': video_url
            }

            if download_mode == 'audio':
                data['downloadMode'] = 'audio'

            if 'vkvideo' in video_url:
                data['downloadMode'] = 'auto'

            data_json: str = json.dumps(data)

            logger.debug(f"Отправляем запрос к cobalt с данными: {data}")
            async with session.post('http://31.130.151.218/', headers=headers, data=data_json) as response:
                if response.status != 200:
                    error_msg = f"cobalt вернул ошибку: HTTP {response.status}, {await response.text()}"
                    logger.error(error_msg)
                    response_text = await response.text()
                    logger.debug(f"Ответ: {response_text}")
                    raise Exception(error_msg)

                result = await response.json()
                logger.debug(f'Получен ответ от cobalt: {result}')

                if 'url' not in result:
                    error_msg = f"cobalt не вернул URL для загрузки: {result}"
                    logger.error(error_msg)
                    raise Exception(error_msg)

            logger.debug(f"Скачиваем аудио с URL: {result['url']}")
            signed_url = result['url']
            url = URL(signed_url, encoded=True)

            source = identify_url_source(video_url)
            data: bytes | str = await download_file(source_type='url', identifier=str(url), destination_type=destination_type, user_data=user_data, specific_source=source, session_id=session_id, download_method='cobalt')
            if len(data) == 0:
                logger.error(f'Cobalt. Не удалось загрузить URL, вернулось 0 байт: {url}')
                raise Exception('Cobalt. Не удалось загрузить URL, вернулось 0 байт')
            
            logger.debug(f'Успешно загружено через cobalt, размер: {len(data)} байт')
            return data
    
    except Exception as e:
        if 'Downloaded file is empty' in str(e):
            raise
        else:
            logger.error(f"Ошибка при работе с cobalt: {e}")
            logger.debug(f"Трассировка: {traceback.format_exc()}")
            raise




async def youtube_audio_to_buffer(video_url: str, proxy_url: Optional[str] = None) -> bytes:
    """
    Загружает аудио с YouTube с помощью yt-dlp.

    Args:
        video_url: URL видео
        proxy_url: URL прокси (опционально)

    Returns:
        bytes: аудио данные

    Raises:
        Exception: если произошла ошибка при загрузке или обработке
    """
    logger.debug(f"Начинаем загрузку аудио через yt-dlp для URL: {video_url}")
    if proxy_url:
        logger.debug(f"Используемый прокси: {proxy_url}")

    temp_file = f'temp_audio_{random.randint(1000, 9999)}'
    temp_file_path = f'{temp_file}.mp3'

    logger.debug(f"Временный файл для сохранения: {temp_file_path}")

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': temp_file,
        'logtostderr': True,
        'proxy': proxy_url,
        'nocheckcertificate': True,
    }

    try:
        loop = asyncio.get_event_loop()
        logger.debug(f"Запускаем загрузку через yt-dlp")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await loop.run_in_executor(None, lambda: ydl.download([video_url]))
        
        logger.debug(f"Загрузка и конвертация завершены, читаем файл: {temp_file_path}")

        if not os.path.exists(temp_file_path):
            raise FileNotFoundError(f"Файл не создан после загрузки: {temp_file_path}")

        async with aiofiles.open(temp_file_path, 'rb') as f:
            audio_data = await f.read()
        
        logger.debug(f"Удаляем временный файл: {temp_file_path}")
        await loop.run_in_executor(None, os.remove, temp_file_path)
        
        logger.debug(f"Аудио успешно загружено через yt-dlp, размер: {len(audio_data)} байт")
        return audio_data
    except Exception as e:
        logger.error(f"Ошибка при скачивании аудио через yt-dlp: {e}")
        logger.debug(f"Трассировка: {traceback.format_exc()}")

        # Очистка временных файлов в случае ошибки
        try:
            if os.path.exists(temp_file_path):
                logger.debug(f"Удаляем временный файл после ошибки: {temp_file_path}")
                await loop.run_in_executor(None, os.remove, temp_file_path)
        except Exception as cleanup_error:
            logger.warning(f"Не удалось удалить временный файл: {cleanup_error}")

        raise


def is_valid_video_url(url: str) -> bool:
    """
    Проверяет, является ли URL действительным URL видео поддерживаемой платформы.

    Args:
        url: URL для проверки

    Returns:
        bool: True, если URL валиден, иначе False
    """
    logger.debug(f"Проверка URL на валидность: {url}")

    # Regex pattern explanation:
    # 1. ^(https?:\/\/)?             -> optional http:// or https:// at the start
    # 2. ([\w\-]+\.)?                -> optional subdomain (like www.)
    # 3. (instagram\.com|facebook\.com|rutube\.ru|youtube\.com|youtu\.be|
    #     twitter\.com|x\.com|reddit\.com|vimeo\.com)
    #                                -> allowed domains (note escaping of dots)
    # 4. \/.+$                       -> a slash followed by at least one character until the end.
    pattern = re.compile(
        r'^(https?:\/\/)?([\w\-]+\.)?'
        r'(instagram\.com|facebook\.com|rutube\.ru|youtube\.com|youtu\.be|'
        r'twitter\.com|x\.com|reddit\.com|vimeo\.com|vkvideo\.ru|vk\.com|'
        r'tiktok\.com|drive\.google\.com|disk\.yandex\.ru|loom\.com)\/.+$',
        re.IGNORECASE
    )
    result = bool(pattern.match(url))

    logger.debug(f"Результат проверки URL {url}: {'валидный' if result else 'невалидный'}")
    return result


async def youtube_audio_to_buffer_api(video_url: str, user_data: dict = None,
                                      destination_type: str = 'buffer', session_id: str | None = None) -> bytes | str:
    """
    Загружает аудио с YouTube через RapidAPI.
     YouTube MP3 Audio Video downloader - https://rapidapi.com/nikzeferis/api/youtube-mp3-audio-video-downloader/
    Args:
        video_url: URL видео YouTube
        user_data: Словарь с данными юзера. В основном для статистики

    Returns:
        bytes: аудио данные

    Raises:
        Exception: если произошла ошибка при запросе или обработке ответа
    """
    logger.debug(f"Начинаем загрузку через RapidAPI для URL: {video_url}")

    try:
        video_id: str = await get_youtube_video_id(video_url)
        logger.debug(f"Извлеченный ID видео: {video_id}")

        url = f"https://youtube-mp3-audio-video-downloader.p.rapidapi.com/get_m4a_download_link/{video_id}"
        headers = {
            'x-rapidapi-key': config.rapidapi.key,
            'x-rapidapi-host': "youtube-mp3-audio-video-downloader.p.rapidapi.com"
        }

        logger.debug(f"Отправляем запрос к RapidAPI: {url}")

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    # TODO: Добавить обработку 403 - {"body": "Video is restricted by YouTube - for adults only."}
                    # TODO: Добавить обработку 403 - {"body": "Upcoming content is not available for download"}
                    # TODO: Добавить обработку 429 - {"body": "maximum proxy tries"}

                    if response.status == 429:
                        logger.debug(f"Получен ответ 429, повторная попытка через 30 секунд")
                        tries = 0
                        while tries < 5:
                            await asyncio.sleep(30)
                            tries += 1
                            response = await session.get(url, headers=headers)
                            if response.status == 200:
                                break
                        if tries == 5:
                            logger.error(f"RapidAPI вернул ошибку: HTTP {response.status}, {await response.text()} после 5 попыток")
                            raise Exception(f"RapidAPI 429")
                    else:
                        error_msg = f"RapidAPI вернул ошибку: HTTP {response.status}, {await response.text()}"
                        logger.error(error_msg)
                        response_text = await response.text()
                        logger.debug(f"Ответ: {response_text}")
                        raise Exception(error_msg)

                # Получаем текст ответа
                response_text = await response.text()

                try:
                    # Пробуем распарсить как JSON
                    data = json.loads(response_text)
                except json.JSONDecodeError:
                    # Если не получилось, пробуем найти JSON в HTML
                    import re
                    json_match = re.search(r'({.*})', response_text)
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))
                        except json.JSONDecodeError:
                            logger.error(f"Не удалось извлечь JSON из ответа: {response_text}")
                            raise Exception("Не удалось извлечь JSON из ответа")
                    else:
                        logger.error(f"Не удалось найти JSON в ответе: {response_text}")
                        raise Exception("Не удалось найти JSON в ответе")

                if 'file' not in data:
                    error_msg = f"RapidAPI не вернул URL файла: {data}"
                    logger.error(error_msg)
                    raise Exception(error_msg)

                logger.debug(f"Получен URL для скачивания: {data['file']}")
                
                audio_data: str | bytes = await download_file(source_type='url', identifier=data['file'],
                                                 destination_type=destination_type,
                                                 user_data=user_data, session_id=session_id, download_method='rapidapi_audio')
                return audio_data
    except Exception as e:
        logger.error(f"Ошибка при работе с RapidAPI: {e}")
        logger.debug(f"Трассировка: {traceback.format_exc()}")
        raise

async def get_youtube_video_id(url: str) -> str:
    if 'shorts' in url:
        video_id = url.split('/')[-1]
    elif 'live' in url:
        video_id = url.split('/')[-1]
    elif 'youtu.be' in url:
        video_id = url.split('/')[-1]
    else:
        video_id = url.split('v=')[-1]

    if '?si=' in video_id:
        video_id = video_id.split('?si=')[0]

    return video_id



async def youtube_search_download_api(video_url: str, user_data: dict, destination_type: str = 'buffer', session_id: str | None = None) -> bytes | str:
    """
    Загружает видео с YouTube через RapidAPI.
    Используем, когда человек просит видео - https://rapidapi.com/boztek-technology-boztek-technology-default/api/Youtube%20Search%20&%20Download

    Args:
        video_url: URL видео YouTube

    Returns:
        bytes: видео данные

    Raises:
        Exception: если произошла ошибка при запросе или обработке ответа
    """
    logger.debug(f"Начинаем загрузку видео через RapidAPI для URL: {video_url}")

    try:
        video_id: str = await get_youtube_video_id(video_url)
        logger.debug(f"Извлеченный ID видео: {video_id}")

        url = "https://youtube-search-download3.p.rapidapi.com/v1/download"
        
        querystring = {"v": video_id, "type": "mp4"}
        
        headers = {
            "x-rapidapi-key": config.rapidapi.key,
            "x-rapidapi-host": "youtube-search-download3.p.rapidapi.com"
        }

        logger.debug(f"Отправляем запрос к RapidAPI для видео: {url}")

        # Увеличенные значения таймаута для больших файлов
        timeout = aiohttp.ClientTimeout(total=1800,  # 30 минут общий таймаут
                                      connect=60,  # 60 секунд на соединение
                                      sock_read=1800)  # 30 минут на чтение данных

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=querystring) as response:
                if response.status != 200:
                    error_msg = f"RapidAPI вернул ошибку: HTTP {response.status}, {await response.text()}"
                    logger.error(error_msg)
                    response_text = await response.text()
                    logger.debug(f"Ответ: {response_text}")
                    raise Exception(error_msg)

                data: dict = await response.json()
                logger.debug(f"Ответ от RapidAPI: {data}")

                if "url" not in data:
                    error_msg = f"RapidAPI не вернул URL для скачивания: {data}"
                    logger.error(error_msg)
                    raise Exception(error_msg)

                download_url = data["url"]
                logger.debug(f"Получен URL для скачивания видео: {download_url}")

                # Логируем начало скачивания
                logger.debug(f"Начало скачивания данных видео по URL: {download_url}")
                # async with session.get(download_url) as video_resp:
                #     if video_resp.status != 200:
                #         error_msg = f"Ошибка при скачивании видео: HTTP {video_resp.status}"
                #         logger.error(error_msg)
                #         raise Exception(error_msg)

                #     video_data = await video_resp.read()
                #     logger.info(f"Видео успешно скачано через RapidAPI, размер: {len(video_data)} байт")
                
                video_data: bytes | str = await download_file(source_type='url', identifier=download_url,
                                                 destination_type=destination_type,
                                                 user_data=user_data,
                                                 session_id=session_id,
                                                 download_method='rapidapi_video')
                return video_data
    except Exception as e:
        logger.error(f"Ошибка при работе с RapidAPI для видео: {e}")
        logger.debug(f"Трассировка: {traceback.format_exc()}")
        raise

async def get_youtube_video_info(video_url: str, use_proxy: bool = False) -> dict:
    """
    Получает информацию о видео с YouTube c помощью RapidAPI: ⚡ YouTube MP3 Audio Video downloader.
    """
    video_id: str = await get_youtube_video_id(video_url)
    url = f'https://youtube-mp3-audio-video-downloader.p.rapidapi.com/get-video-info/{video_id}'
    headers = {
        'x-rapidapi-key': config.rapidapi.key,
        'x-rapidapi-host': "youtube-mp3-audio-video-downloader.p.rapidapi.com"
    }
    proxy = config.proxy.proxy

    async with aiohttp.ClientSession() as session:
        if not use_proxy:
            try:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        logger.error(f"Не удалось получить информацию о видео: HTTP {response.status}")
                        raise Exception(f"Не удалось получить информацию о видео: HTTP {response.status}")
                    return await response.json()
            except Exception as e:
                logger.error(f"Не удалось получить информацию о видео без прокси: {e}")
                return await get_youtube_video_info(video_url, use_proxy=True)
        else:
            async with session.get(url, headers=headers, proxy=proxy) as response:
                return await response.json()

if __name__ == "__main__":
    print(asyncio.run(get_youtube_video_id('https://youtu.be/SASDkIQjouI?si=iZRR509JfPbssyne')))
    # test_urls = [
    #     "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    #     "http://youtu.be/dQw4w9WgXcQ",
    #     "https://instagram.com/p/xyz123",
    #     "https://facebook.com/video.php?v=123456789",
    #     "https://rutube.ru/video/abcdefg",
    #     "https://twitter.com/someuser/status/1234567890",
    #     "https://x.com/someuser/status/1234567890",
    #     "https://www.reddit.com/r/videos/comments/abcdef/video_title/",
    #     "https://vimeo.com/123456789",
    #     "https://example.com/video"
    # ]
    #
    # for url in test_urls:
    #     print(f"{url} -> {is_valid_video_url(url)}")
    # print(is_valid_video_url('https://www.youtube.com/watch?v=jtATbpMqbL4 абоба'))
    # print(asyncio.run(get_video_title('https://rutube.ru/video/145edccab182d533c8f426e33cf15a5f/')))
    pass
    # asyncio.run(cobalt_download_audio('https://rutube.ru/video/145edccab182d533c8f426e33cf15a5f/', 'aboba.mp3'))
# curl -X POST http://localhost:9000 \
#      -H 'Content-Type': 'application/json' \
#      - H 'Accept': 'application/json' \
#      -d '{"url": "https://www.youtube.com/watch?v=E3T3rSYrRAc"}'