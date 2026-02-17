"""
Модуль для нормализации источников и генерации хешей для системы кэширования
"""
import hashlib
import logging
from urllib.parse import urlparse, parse_qs, urlunparse
import re
import aiofiles

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    """
    Нормализует URL для поиска в кэше:
    - Убирает UTM метки и tracking параметры
    - Нормализует YouTube URLs (разные форматы к единому)
    - Нормализует Instagram URLs
    - Убирает лишние параметры

    Args:
        url: Исходный URL

    Returns:
        Нормализованный ключ источника
    """
    try:
        parsed = urlparse(url.lower().strip())

        # YouTube нормализация
        if 'youtube.com' in parsed.netloc or 'youtu.be' in parsed.netloc:
            # Извлекаем video ID
            if 'youtu.be' in parsed.netloc:
                video_id = parsed.path.strip('/')
            else:
                query_params = parse_qs(parsed.query)
                video_id = query_params.get('v', [''])[0]

            if video_id:
                return f"youtube:{video_id}"

        # Instagram нормализация
        if 'instagram.com' in parsed.netloc:
            # Извлекаем post/reel ID
            match = re.search(r'/(p|reel|tv)/([A-Za-z0-9_-]+)', parsed.path)
            if match:
                return f"instagram:{match.group(2)}"

        # TikTok нормализация
        if 'tiktok.com' in parsed.netloc:
            match = re.search(r'/video/(\d+)', parsed.path)
            if match:
                return f"tiktok:{match.group(1)}"

        # VK нормализация
        if 'vk.com' in parsed.netloc or 'vkvideo.ru' in parsed.netloc:
            match = re.search(r'video(-?\d+_\d+)', parsed.path)
            if match:
                return f"vk:{match.group(1)}"

        # Общая нормализация для других URL
        # Убираем UTM метки и tracking параметры
        query_params = parse_qs(parsed.query)
        tracking_params = [
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
            'fbclid', 'gclid', 'yclid', 'msclkid', '_ga', 'mc_cid', 'mc_eid'
        ]
        cleaned_params = {
            k: v for k, v in query_params.items()
            if not any(k.startswith(tp) for tp in tracking_params)
        }

        # Собираем обратно
        cleaned_query = '&'.join(f"{k}={v[0]}" for k, v in sorted(cleaned_params.items()))
        normalized = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip('/'),  # Убираем trailing slash
            '',
            cleaned_query,
            ''
        ))

        return normalized

    except Exception as e:
        logger.warning(f"Failed to normalize URL {url}: {e}")
        # В случае ошибки возвращаем исходный URL
        return url


def normalize_source_key(source_type: str, original_identifier: str) -> str:
    """
    Генерирует нормализованный ключ источника

    Args:
        source_type: Тип источника ('url' или 'telegram')
        original_identifier: Оригинальный идентификатор (URL или file_id)

    Returns:
        Нормализованный ключ
    """
    if source_type == 'url':
        return normalize_url(original_identifier)
    else:
        # Для telegram используем file_id как есть
        return f"telegram:{original_identifier}"


async def generate_file_hash_async(file_path: str = None, file_bytes: bytes = None) -> str:
    """
    Асинхронно генерирует SHA256 хэш файла

    Args:
        file_path: Путь к файлу на диске
        file_bytes: Байты файла в памяти

    Returns:
        SHA256 хэш в hex формате
    """
    sha = hashlib.sha256()

    try:
        if file_path:
            async with aiofiles.open(file_path, 'rb') as f:
                while chunk := await f.read(65536):  # 64KB chunks
                    sha.update(chunk)
        elif file_bytes:
            sha.update(file_bytes)
        else:
            raise ValueError("Either file_path or file_bytes must be provided")

        return sha.hexdigest()

    except Exception as e:
        logger.error(f"Failed to generate file hash: {e}")
        return None


def generate_file_hash_sync(file_path: str = None, file_bytes: bytes = None) -> str:
    """
    Синхронно генерирует SHA256 хэш файла (для небольших файлов)

    Args:
        file_path: Путь к файлу на диске
        file_bytes: Байты файла в памяти

    Returns:
        SHA256 хэш в hex формате
    """
    sha = hashlib.sha256()

    try:
        if file_path:
            with open(file_path, 'rb') as f:
                while chunk := f.read(65536):  # 64KB chunks
                    sha.update(chunk)
        elif file_bytes:
            sha.update(file_bytes)
        else:
            raise ValueError("Either file_path or file_bytes must be provided")

        return sha.hexdigest()

    except Exception as e:
        logger.error(f"Failed to generate file hash: {e}")
        return None


def generate_prompt_hash(system_prompt: str) -> str:
    """
    Генерирует SHA256 хэш системного промпта

    Args:
        system_prompt: Текст системного промпта

    Returns:
        SHA256 хэш в hex формате
    """
    return hashlib.sha256(system_prompt.encode('utf-8')).hexdigest()