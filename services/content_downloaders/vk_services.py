import asyncio
import logging
import sys
import traceback
from typing import Optional

import aiohttp
from yarl import URL

from services.init_bot import config
from services.content_downloaders.file_handling import download_file

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(stream=sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

async def all_media_downloader_api(url: str, download_mode: str = 'audio', user_data: dict = None, destination_type: str = 'buffer', session_id: str | None = None) -> bytes | str:
    """
    Получает аудио из VK видео по URL.
    https://rapidapi.com/andryerics/api/all-media-downloader1/playground/apiendpoint_7604cbb9-1399-4613-8490-a2661edc37b0
    Работает с любыми источниками

    Args:
        url: URL видео на VK

    Returns:
        bytes: аудио данные в байтовом формате, если destination_type == 'buffer'
        str: путь к файлу, если destination_type == 'file'

    Raises:
        Exception: если произошла ошибка при запросе или обработке ответа
    """
    logger.debug(f"Начинаем загрузку из VK для URL: {url}")

    try:
        # Получаем JSON данные от API
        video_info = await fetch_vk_video_info(url)

        # Получаем URL лучшего аудио потока
        best_url = None
        if download_mode == 'audio':
            best_url = get_best_audio_url_vk(video_info)
        elif download_mode == 'video':

            best_url = video_info.get('url', None)
            if not best_url:
                best_url = get_best_video_url_vk(video_info)

        if not best_url:
            raise Exception("Не удалось найти подходящий аудио поток для VK видео")

        # Загружаем аудио
        logger.debug(f"Загружаем аудио из VK по URL: {best_url}")
        audio_data: bytes | str = await download_file(source_type='url', identifier=url, destination_type=destination_type, user_data=user_data, session_id=session_id, download_method='rapidapi_all_media')
        return audio_data
    except Exception as e:
        logger.error(f"Ошибка при загрузке аудио из VK: {e}")
        logger.debug(f"Трассировка: {traceback.format_exc()}")
        raise


async def fetch_vk_video_info(url: str) -> dict:
    """
    Получает информацию о видео VK через RapidAPI.
    https://rapidapi.com/andryerics/api/all-media-downloader1/playground/apiendpoint_7604cbb9-1399-4613-8490-a2661edc37b0
    Может работать с любыми видами видео

    Args:
        url: URL видео на VK

    Returns:
        dict: информация о видео

    Raises:
        Exception: если произошла ошибка при запросе или обработке ответа
    """
    logger.debug(f"Получаем информацию о VK видео для URL: {url}")

    api_url = "https://all-media-downloader1.p.rapidapi.com/all"
    headers = {
        "x-rapidapi-key": config.rapidapi.key,
        "x-rapidapi-host": "all-media-downloader1.p.rapidapi.com",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    payload = {"url": url}
    timeout = aiohttp.ClientTimeout(total=7200,  # 120 минут общий таймаут
                                    connect=60,  # 60 секунд на соединение
                                    sock_read=7200)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(api_url, data=payload, headers=headers) as response:
                if response.status != 200:
                    error_msg = f"RapidAPI вернул ошибку: HTTP {response.status}, {await response.text()}"
                    logger.error(error_msg)
                    response_text = await response.text()
                    logger.debug(f"Ответ: {response_text}")
                    raise Exception(error_msg)

                data = await response.json()
                logger.debug(f"Получена информация о VK видео {type(data)}: {data}")
                return data
    except Exception as e:
        logger.error(f"Ошибка при получении информации о VK видео: {e}")
        logger.debug(f"Трассировка: {traceback.format_exc()}")
        raise


def get_best_audio_url_vk(video_info: dict) -> str:
    """
    Находит URL для аудиопотока наилучшего качества из метаданных видео.

    Сначала ищет аудиоформаты с указанным средним битрейтом (abr)
    и выбирает URL с самым высоким abr. Если такие форматы не найдены,
    пытается найти любой формат, помеченный как аудио (по acodec или разрешению)
    и имеющий URL.

    Args:
        video_info (dict): Словарь, содержащий метаданные видео,
                           структурированный как в предоставленном примере.

    Returns:
        str: URL аудиопотока с наивысшим средним битрейтом (abr)
             или URL любого другого найденного аудиопотока, если форматы
             с abr отсутствуют.
        None: Если не найдено подходящих аудиопотоков с URL.
    """
    if not isinstance(video_info, dict) or 'formats' not in video_info:
        logger.error("Ошибка: Неверный ввод или отсутствует ключ 'formats'.")
        return None

    formats = video_info.get('formats', [])
    if not isinstance(formats, list):
        logger.error("Ошибка: Ключ 'formats' не содержит список.")
        return None

    audio_formats_with_abr = []
    potential_audio_formats = []

    for format_dict in formats:
        if not isinstance(format_dict, dict):
            continue  # Пропускаем невалидные записи форматов

        url = format_dict.get('url')
        if not url:
            continue  # Пропускаем форматы без URL

        # --- Критерии для основного поиска (с битрейтом) ---
        acodec = format_dict.get('acodec')
        abr = format_dict.get('abr')

        # 1. Должен быть аудио кодек (не 'none')
        # 2. Должен быть указан 'abr' (средний битрейт), и он должен быть числом
        is_primary_audio = (acodec and acodec != 'none' and
                            abr is not None)

        if is_primary_audio:
            try:
                current_bitrate = float(abr)
                # Добавляем кортеж (битрейт, url) для сортировки
                audio_formats_with_abr.append((current_bitrate, url))
            except (ValueError, TypeError):
                # Игнорируем, если abr нельзя преобразовать в число
                logger.warning(
                    f"Предупреждение: Не удалось обработать битрейт '{abr}' для формата ID {format_dict.get('format_id', 'N/A')}")
                # Тем не менее, это может быть аудио, добавим в резервный список
                if acodec and acodec != 'none':
                    potential_audio_formats.append(url)
        else:
            # --- Критерии для резервного поиска (без явного abr) ---
            # 3. Или формат явно помечен как 'audio only'
            # 4. Или у него есть аудио кодек, но нет abr (уже проверено выше)
            resolution = format_dict.get('resolution')
            format_note = format_dict.get('format_note', '').lower()  # Приводим к нижнему регистру для надежности

            is_potential_audio = (
                    (acodec and acodec != 'none') or
                    (resolution and 'audio only' in resolution.lower()) or
                    ('audio' in format_note)
            )
            # Дополнительно проверим, что это не видео формат
            vcodec = format_dict.get('vcodec')
            if is_potential_audio and (not vcodec or vcodec == 'none'):
                potential_audio_formats.append(url)

    # --- Выбор лучшего URL ---
    if audio_formats_with_abr:
        # Сортируем по битрейту (первый элемент кортежа) по убыванию
        audio_formats_with_abr.sort(key=lambda x: x[0], reverse=True)
        best_url = audio_formats_with_abr[0][1]  # Берем URL с самым высоким битрейтом
        logger.debug(f"Найден лучший аудио URL с битрейтом: {audio_formats_with_abr[0][0]:.3f} kbps")
        return best_url
    elif potential_audio_formats:
        # Если нет форматов с abr, возвращаем первый попавшийся из резервного списка
        fallback_url = potential_audio_formats[0]
        logger.warning(
            "Предупреждение: Не найдены аудиоформаты с битрейтом (abr). Возвращается первый найденный потенциальный аудио URL.")
        return fallback_url
    else:
        # Если ничего не найдено
        logger.error("Не найдено подходящих аудио потоков с URL.")
        return None


def get_best_video_url_vk(video_info: dict) -> str:
    """
    Находит URL для видеопотока наилучшего качества из метаданных видео.

    Args:
        video_info (dict): Словарь, содержащий метаданные видео

    Returns:
        str: URL видеопотока с наивысшим качеством
        None: Если не найдено подходящих видеопотоков с URL.
    """
    if not isinstance(video_info, dict) or 'formats' not in video_info:
        logger.error("Ошибка: Неверный ввод или отсутствует ключ 'formats'.")
        return None

    formats = video_info.get('formats', [])
    if not isinstance(formats, list):
        logger.error("Ошибка: Ключ 'formats' не содержит список.")
        return None

    # Ищем форматы с видео
    video_formats = []

    for format_dict in formats:
        if not isinstance(format_dict, dict):
            continue  # Пропускаем невалидные записи форматов

        url = format_dict.get('url')
        if not url:
            continue  # Пропускаем форматы без URL

        # Критерии для поиска видео
        vcodec = format_dict.get('vcodec')
        height = format_dict.get('height')
        width = format_dict.get('width')
        format_note = format_dict.get('format_note', '').lower()

        # Это видео формат, если:
        # 1. Есть видеокодек (не 'none')
        # 2. Есть высота или ширина
        # 3. Или содержит 'video' в format_note
        is_video = (
                (vcodec and vcodec != 'none') or
                (height and width) or
                ('video' in format_note)
        )

        if is_video:
            try:
                # Добавляем кортеж (высота, url) для сортировки
                # Если высота не указана, используем 0
                h = int(height) if height else 0
                video_formats.append((h, url))
            except (ValueError, TypeError):
                # Если высота не может быть преобразована в int
                video_formats.append((0, url))

    # Выбираем лучший URL
    if video_formats:
        # Сортируем по высоте (первый элемент кортежа) по убыванию
        video_formats.sort(key=lambda x: x[0], reverse=True)
        best_url = video_formats[0][1]  # Берем URL с наибольшей высотой
        logger.debug(f"Найден лучший видео URL с разрешением: {video_formats[0][0]} px")
        return best_url
    else:
        # Если не найдено видео форматов, пробуем взять URL из поля 'url' корневого объекта
        if 'url' in video_info:
            logger.warning("Не найдены видеоформаты, возвращается URL из корневого объекта.")
            return video_info['url']

        logger.error("Не найдено подходящих видео потоков с URL.")
        return None