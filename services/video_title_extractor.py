#!/usr/bin/env python3
"""
Универсальный скрипт для извлечения названия/заголовка видео с различных платформ
Поддерживаемые платформы: YouTube, Instagram, VK, Facebook, Rutube, Reddit, Twitter, Vimeo
Поддерживает несколько методов: yt-dlp, requests+beautifulsoup, pytube
"""

import sys
import re
import argparse
import asyncio
import logging
from urllib.parse import urlparse, parse_qs

from services.content_downloaders.vk_services import fetch_vk_video_info
from services.youtube_funcs import get_youtube_video_info, logger

# Настройка логгера для видео экстрактора
video_logger = logging.getLogger(__name__)
if not video_logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    video_logger.addHandler(handler)
    video_logger.setLevel(logging.INFO)


def detect_platform(url):
    """Определяет платформу по URL"""
    url_lower = url.lower()
    
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    elif any(domain in url_lower for domain in ['vk.com', 'vkontakte.ru', 'vkvideo.ru']):
        return 'vk'
    elif 'facebook.com' in url_lower or 'fb.com' in url_lower:
        return 'facebook'
    elif 'rutube.ru' in url_lower:
        return 'rutube'
    elif 'reddit.com' in url_lower:
        return 'reddit'
    elif 'twitter.com' in url_lower or 'x.com' in url_lower:
        return 'twitter'
    elif 'vimeo.com' in url_lower:
        return 'vimeo'
    else:
        return 'unknown'


async def get_title_with_yt_dlp(url):
    """Метод 1: Использование yt-dlp (асинхронная версия)"""
    try:
        import yt_dlp
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        
        # Выполняем только блокирующую операцию в executor
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            if info:
                return info.get('title', 'Название не найдено')
            return None
            
    except ImportError:
        return None
    except Exception as e:
        video_logger.warning(f"Ошибка yt-dlp: {e}")
        return None

def get_title_with_requests(url):
    """Метод 2: Парсинг HTML с помощью requests"""
    try:
        import requests
        from bs4 import BeautifulSoup
        import time
        
        # Улучшенные заголовки для имитации реального браузера
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0'
        }
        
        # Создаем сессию для сохранения cookies
        session = requests.Session()
        session.headers.update(headers)
        
        # Для VK - делаем небольшую задержку и добавляем реферер
        if 'vk' in url.lower():
            time.sleep(1)  # Задержка для имитации человека
            headers['Referer'] = 'https://vk.com/'
        
        response = session.get(url, timeout=15)
        response.raise_for_status()
        
        # Автоматическое определение кодировки для русских сайтов
        if response.encoding == 'ISO-8859-1' or response.apparent_encoding:
            response.encoding = response.apparent_encoding or 'utf-8'
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Специальная обработка для VK
        if 'vk' in url.lower():
            # Проверяем на защитные сообщения VK
            protective_messages = [
                'У вас большие запросы',
                'Подтвердите, что запрос отправили вы',
                'Confirm that you sent the request',
                'Пожалуйста, подтвердите',
                'Please confirm',
                'Проверка браузера'
            ]
            
            page_text = soup.get_text().strip()
            for msg in protective_messages:
                if msg.lower() in page_text.lower():
                    video_logger.warning(f"❌ VK заблокировал запрос: '{msg}' (требуется ручная проверка)")
                    return None
            
            # Попробуем найти специфические VK селекторы
            vk_selectors = [
                'meta[property="og:title"]',
                'meta[name="title"]',
                '.video_item_title',
                '.mv_title',
                'h1'
            ]
            
            for selector in vk_selectors:
                element = soup.select_one(selector)
                if element:
                    if element.name == 'meta':
                        content = element.get('content')
                        if isinstance(content, list):
                            title = content[0] if content else ''
                        else:
                            title = content if content else ''
                        title = str(title).strip()
                    else:
                        title = element.get_text().strip()
                    
                    # Дополнительная проверка на защитные сообщения в title
                    for msg in protective_messages:
                        if msg.lower() in title.lower():
                            video_logger.warning(f"❌ VK заблокировал запрос в title: '{title}'")
                            return None
                    
                    if title and title not in ['VK', 'ВКонтакте']:
                        return title
        
        # Общий поиск тега title
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.text.strip()
            # Удаляем суффиксы различных платформ
            suffixes = [' - YouTube', ' | ВКонтакте', ' - VK', ' | VK']
            for suffix in suffixes:
                if title.endswith(suffix):
                    title = title[:-len(suffix)]
                    break
            return title
            
        return None
        
    except ImportError:
        return None
    except Exception as e:
        video_logger.warning(f"Ошибка requests: {e}")
        return None

def get_title_with_pytube(url):
    """Метод 3: Использование pytube"""
    try:
        from pytube import YouTube
        
        yt = YouTube(url)
        return yt.title
        
    except ImportError:
        return None
    except Exception as e:
        video_logger.warning(f"Ошибка pytube: {e}")
        return None

def get_vk_title_embed(url):
    """Быстрый метод для VK: берём embed-страницу video_ext.php и читаем og:title без Selenium"""
    try:
        import re, requests
        from bs4 import BeautifulSoup
        
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }

        # 1. Попытка построить прямой URL video_ext.php из owner_id и video_id
        match = re.search(r'video(?P<owner>[-\d]+)_(?P<id>\d+)', url)
        candidate_urls = []
        if match:
            owner, vid = match.group('owner'), match.group('id')
            candidate_urls.append(f'https://vk.com/video_ext.php?oid={owner}&id={vid}')
            candidate_urls.append(f'https://vk.com/video_ext.php?oid={owner}&id={vid}&hd=1')

        # 2. На случай необычных ссылок добавляем сам оригинальный URL как fallback,
        #    чтобы позже вытащить из его HTML ссылку на video_ext.php
        candidate_urls.append(url)

        for link in candidate_urls:
            try:
                resp = requests.get(link, headers=headers, timeout=10, allow_redirects=True)
                resp.raise_for_status()
                html = resp.text

                # Если текущий link — не video_ext, пробуем найти его внутри HTML
                if 'video_ext.php' not in link:
                    m = re.search(r'https?://[^"\']*video_ext\.php[^"\']+', html)
                    if m:
                        link = m.group(0)
                        resp = requests.get(link, headers=headers, timeout=10)
                        resp.raise_for_status()
                        html = resp.text

                soup = BeautifulSoup(html, 'html.parser')
                meta = soup.find('meta', attrs={'property': 'og:title'})
                content = getattr(meta, 'attrs', {}).get('content') if meta else None  # безопасный доступ
                if content:
                    title = str(content).strip()
                    if title and title not in ['VK', 'ВКонтакте']:
                        return title
            except Exception:
                # Переходим к следующему варианту
                continue
        return None
    except Exception as e:
        video_logger.warning(f"Ошибка VK embed: {e}")
        return None

def get_vk_title_alternative(url):
    """Альтернативный метод для VK с попыткой обхода защиты"""
    try:
        import requests
        import time
        import random
        
        # Mobile User-Agent иногда помогает обойти защиту
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru,en;q=0.5',
            'Connection': 'keep-alive'
        }
        
        # Небольшая задержка
        time.sleep(random.uniform(1, 3))
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            # Простая проверка на наличие названия в HTML без BeautifulSoup
            html = response.text
            
            # Ищем og:title в сыром HTML
            import re
            og_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if og_match:
                title = og_match.group(1).strip()
                if title and 'запросы' not in title.lower() and 'подтвердите' not in title.lower():
                    return title
                    
        return None
        
    except ImportError:
        return None
    except Exception as e:
        video_logger.warning(f"Ошибка VK alternative: {e}")
        return None

async def get_video_title(url):
    """Основная функция для получения названия/заголовка видео с любой платформы"""
    
    platform = detect_platform(url)
    
    # Для YouTube можем показать ID видео
    if platform == 'youtube':
        try:
            video_info = await get_youtube_video_info(url)
            if video_info and 'title' in video_info:
                return video_info['title'].strip()
            else:
                logger.warning("YouTube API не вернуло название, пробуем другие методы...")
                # Если YouTube API не сработал, используем универсальные методы
        except Exception as e:
            video_logger.warning(f"Ошибка YouTube API: {e}, пробуем другие методы...")
            # Если ошибка, используем универсальные методы
    elif platform == 'instagram':
        return None

    # Пробуем разные методы по порядку
    # yt-dlp поддерживает все платформы, поэтому он первый
    methods = [
        ("yt-dlp", get_title_with_yt_dlp, True),  # True означает, что функция асинхронная
        ("requests + BeautifulSoup", get_title_with_requests, False),
    ]
    
    # Для VK добавляем специальные методы
    if platform == 'vk':
        methods = [
            ("yt-dlp", get_title_with_yt_dlp, True),
            ("VK embed video_ext", get_vk_title_embed, False),
            ("requests + BeautifulSoup", get_title_with_requests, False),
            ("VK альтернативный метод", get_vk_title_alternative, False)
        ]
    
    
    for method_name, method_func, is_async in methods:
        video_logger.debug(f"Пробуем метод: {method_name}...")
        if is_async:
            title = await method_func(url)
        else:
            # Выполняем синхронные методы в отдельном потоке, чтобы не блокировать event loop
            import asyncio as _asyncio
            title = await _asyncio.to_thread(method_func, url)
        if title:
            return title.strip()
    
    return None

# Обратная совместимость
def get_youtube_title(url):
    """Устаревшая функция для обратной совместимости"""
    import asyncio
    return asyncio.run(get_video_title(url))

def interactive_mode():
    """Интерактивный режим для работы с множественными ссылками"""
    print("=== Универсальный извлекатель названий видео ===")
    print("Поддерживаемые платформы: YouTube, Instagram, VK, Facebook, Rutube, Reddit, Twitter, Vimeo")
    print("Введите ссылку на видео (или 'exit'/'quit' для выхода):")
    print()
    
    while True:
        try:
            # Получаем ввод от пользователя
            url = input("🔗 Ссылка: ").strip()
            
            # Проверяем команды выхода
            if url.lower() in ['exit', 'quit', 'выход', 'q']:
                print("👋 До свидания!")
                break
            
            # Проверяем, что введена не пустая строка
            if not url:
                print("❌ Пожалуйста, введите ссылку или 'exit' для выхода")
                continue
            
            # Проверяем, что это похоже на URL
            if not (url.startswith('http://') or url.startswith('https://')):
                print("❌ Ссылка должна начинаться с http:// или https://")
                continue
            
            print("⏳ Получаем название видео...")
            
            # Получаем название
            title = asyncio.run(get_video_title(url))
            
            # Выводим результат
            if title and title != "Не удалось получить название видео":
                print(f"✅ Название: {title}")
            else:
                print("❌ Не удалось получить название видео")
            
            print("-" * 50)
            
        except KeyboardInterrupt:
            print("\n👋 Получен сигнал прерывания. До свидания!")
            break
        except EOFError:
            print("\n👋 До свидания!")
            break
        except Exception as e:
            print(f"❌ Произошла ошибка: {e}")
            print("-" * 50)

def main():
    title = None
    parser = argparse.ArgumentParser(
        description='Извлечение названия/заголовка видео с различных платформ',
        epilog='Поддерживаемые платформы: YouTube, Instagram, VK, Facebook, Rutube, Reddit, Twitter, Vimeo'
    )
    parser.add_argument('url', nargs='?', help='Ссылка на видео с любой поддерживаемой платформы (если не указана, запускается интерактивный режим)')
    parser.add_argument('--method', choices=['yt-dlp', 'requests', 'pytube'], 
                       help='Принудительно использовать определенный метод')
    parser.add_argument('--platform', choices=['youtube', 'instagram', 'vk', 'facebook', 'rutube', 'reddit', 'twitter', 'vimeo'],
                       help='Принудительно указать платформу (автоопределение по умолчанию)')
    parser.add_argument('--interactive', '-i', action='store_true',
                       help='Запустить интерактивный режим')
    
    args = parser.parse_args()
    
    # Если не указан URL или явно запрошен интерактивный режим
    if not args.url or args.interactive:
        interactive_mode()
        return 0
    
    if args.method:
        # Используем только указанный метод
        if args.method == 'yt-dlp':
            title = asyncio.run(get_title_with_yt_dlp(args.url))
        elif args.method == 'requests':
            title = get_title_with_requests(args.url)
        elif args.method == 'pytube':
            # pytube работает только с YouTube
            detected_platform = args.platform or detect_platform(args.url)
            if detected_platform != 'youtube':
                video_logger.error(f"Ошибка: pytube работает только с YouTube, обнаружена платформа: {detected_platform}")
                return 1
            title = get_title_with_pytube(args.url)
        
        if not title:
            video_logger.warning(f"Метод {args.method} не доступен или не сработал")
            return 1
    else:
        # Пробуем все методы
        title = asyncio.run(get_video_title(args.url))
    
    print(f"\nНазвание видео: {title}")
    return 0

if __name__ == "__main__":
    sys.exit(main()) 