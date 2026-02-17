"""
Markdown Service Module

Создание Markdown документов с расшифровками.
Адаптировано из PDF сервиса для поддержания единого стиля оформления.
"""

import io
from datetime import datetime
from typing import Optional
from fluentogram import TranslatorRunner


async def create_enhanced_transcript_markdown(
    title: str, 
    clean_transcript: str, 
    full_transcript: str,
    i18n: TranslatorRunner
) -> str:
    """
    Создает улучшенный Markdown документ с заголовком, оглавлением и двумя версиями расшифровки.
    Использует тот же принцип структуры и оформления, что и функция create_enhanced_transcript_pdf.
    
    Args:
        title: Заголовок документа
        clean_transcript: Чистая версия расшифровки (без таймкодов/спикеров)
        full_transcript: Полная версия с таймкодами и спикерами
        i18n: TranslatorRunner instance for translations
        
    Returns:
        str: Markdown содержимое документа
    """
    
    # ===== ЗАГОЛОВОК И МЕТА-ИНФОРМАЦИЯ =====
    current_date = datetime.now().strftime("%d.%m.%Y")
    
    markdown_content = []
    
    # Добавляем ссылку на бота в начале документа
    markdown_content.append(i18n.transcription_file_made_with_markdown(bot_link="https://t.me/WhisperSummaryAI_bot"))
    markdown_content.append("")
    
    # Основной заголовок
    markdown_content.append(f"# {title}")
    markdown_content.append("")
    
    # Дата создания
    markdown_content.append(i18n.transcription_file_date_markdown(date=current_date))
    markdown_content.append("")
    
    # Разделитель
    markdown_content.append("---")
    markdown_content.append("")
    
    # ===== ОГЛАВЛЕНИЕ =====
    markdown_content.append(f"## {i18n.transcription_file_table_of_contents_title()}")
    markdown_content.append("")
    markdown_content.append(i18n.transcription_file_toc_clean_version_markdown())
    markdown_content.append(i18n.transcription_file_toc_full_version_markdown())
    markdown_content.append("")
    
    # Разделитель
    markdown_content.append("---")
    markdown_content.append("")
    
    # ===== ЧИСТАЯ ВЕРСИЯ =====
    markdown_content.append('<a name="clean-version"></a>')
    markdown_content.append(i18n.transcription_file_clean_version_header_markdown())
    markdown_content.append("")
    
    # Описание чистой версии в блоке цитаты
    clean_description = i18n.transcription_file_clean_version_desc_markdown()
    markdown_content.append(f"> {clean_description}")
    markdown_content.append("")
    
    # Содержимое чистой версии
    markdown_content.append(clean_transcript)
    markdown_content.append("")
    
    # Разделитель
    markdown_content.append("---")
    markdown_content.append("")
    
    # ===== ПОЛНАЯ ВЕРСИЯ =====
    markdown_content.append('<a name="full-version"></a>')
    markdown_content.append(i18n.transcription_file_full_version_header_markdown())
    markdown_content.append("")
    
    # Описание полной версии в блоке цитаты
    full_description = i18n.transcription_file_full_version_desc_markdown()
    markdown_content.append(f"> {full_description}")
    markdown_content.append("")
    
    # Содержимое полной версии в блоке кода для лучшего форматирования
    markdown_content.append("```")
    markdown_content.append(full_transcript)
    markdown_content.append("```")
    markdown_content.append("")
    
    # Финальный разделитель и подпись
    markdown_content.append("---")
    markdown_content.append("")
    markdown_content.append(i18n.transcription_file_footer_markdown(bot_link="https://t.me/WhisperSummaryAI_bot"))
    
    return "\n".join(markdown_content)


async def save_transcript_to_markdown_file(
    title: str,
    clean_transcript: str,
    full_transcript: str,
    i18n: TranslatorRunner,
    filepath: Optional[str] = None
) -> str:
    """
    Создает и сохраняет Markdown файл с расшифровкой.
    
    Args:
        title: Заголовок документа
        clean_transcript: Чистая версия расшифровки
        full_transcript: Полная версия расшифровки
        i18n: TranslatorRunner instance for translations
        filepath: Путь для сохранения файла (если не указан, создается автоматически)
        
    Returns:
        str: Путь к созданному файлу
    """
    
    # Создаем Markdown содержимое
    markdown_content = await create_enhanced_transcript_markdown(
        title, clean_transcript, full_transcript, i18n
    )
    
    # Определяем путь к файлу
    if filepath is None:
        # Создаем безопасное имя файла из заголовка
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
        safe_title = safe_title.replace(' ', '_')[:50]  # Ограничиваем длину
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = f"transcript_{safe_title}_{timestamp}.md"
    
    # Сохраняем файл
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(markdown_content)
    
    return filepath


async def create_markdown_buffer(
    title: str,
    clean_transcript: str,
    full_transcript: str,
    i18n: TranslatorRunner
) -> io.BytesIO:
    """
    Создает буфер с Markdown содержимым для отправки в Telegram.
    Аналог функции для PDF, но для Markdown формата.
    
    Args:
        title: Заголовок документа
        clean_transcript: Чистая версия расшифровки
        full_transcript: Полная версия расшифровки
        i18n: TranslatorRunner instance for translations
        
    Returns:
        io.BytesIO: Буфер с Markdown содержимым
    """
    
    # Создаем Markdown содержимое
    markdown_content = await create_enhanced_transcript_markdown(
        title, clean_transcript, full_transcript, i18n
    )
    
    # Создаем буфер
    buffer = io.BytesIO()
    buffer.write(markdown_content.encode('utf-8'))
    buffer.seek(0)
    
    return buffer 