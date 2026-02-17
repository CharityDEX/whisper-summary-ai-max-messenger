import io
import logging
import time
import uuid
from functools import wraps
from datetime import datetime
from fluentogram import TranslatorRunner

# Настройка логгирования
logger = logging.getLogger(__name__)

# Декоратор для логгирования асинхронных функций
def async_log_decorator(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        operation_id = str(uuid.uuid4())[:8]
        func_name = func.__name__
        logger.debug(f"[{operation_id}] Starting {func_name}")
        start_time = time.time()
        
        try:
            result = await func(*args, **kwargs)
            execution_time = time.time() - start_time
            logger.debug(f"[{operation_id}] Completed {func_name} in {execution_time:.2f}s")
            return result
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"[{operation_id}] Failed {func_name} after {execution_time:.2f}s: {str(e)}")
            raise
    
    return wrapper


@async_log_decorator
async def create_enhanced_transcript_txt(title: str, clean_transcript: str, full_transcript: str, i18n: TranslatorRunner) -> io.BytesIO:
    """
    Creates an enhanced TXT document with title, table of contents, and both clean and full transcript versions.
    Uses similar structure to create_enhanced_transcript_pdf but for plain text format.
    
    Args:
        title: Document title
        clean_transcript: Clean version of the transcript (without timestamps/speakers)
        full_transcript: Full version with timestamps and speakers
        i18n: TranslatorRunner instance for translations
    Returns:
        io.BytesIO: Buffer with enhanced TXT content
    """
    buffer = io.BytesIO()
    
    # Build the text content
    content_lines = []
    
    # ===== HEADER WITH WHISPER AI BRANDING =====
    content_lines.extend([
        "=" * 80,
        i18n.transcription_file_made_with_whisper(),
        i18n.transcription_file_bot_label(bot_link="https://t.me/WhisperSummaryAI_bot"),
        "=" * 80,
        "",
        i18n.transcription_file_title_txt(title=title),
        i18n.transcription_file_date_txt(date=datetime.now().strftime('%d.%m.%Y %H:%M')),
        "",
        "=" * 80,
        ""
    ])
    
    # ===== TABLE OF CONTENTS =====
    content_lines.extend([
        i18n.transcription_file_table_of_contents_title(),
        "",
        i18n.transcription_file_toc_clean_version(),
        i18n.transcription_file_toc_full_version(),
        "",
        "=" * 80,
        ""
    ])
    
    # ===== CLEAN VERSION =====
    content_lines.extend([
        i18n.transcription_file_clean_version_title(),
        "",
        i18n.transcription_file_clean_version_desc_1(),
        "",
        "-" * 40,
        ""
    ])
    
    # Add clean transcript content
    clean_lines = clean_transcript.strip().split('\n')
    content_lines.extend(clean_lines)
    
    content_lines.extend([
        "",
        "=" * 80,
        ""
    ])
    
    # ===== FULL VERSION =====
    content_lines.extend([
        i18n.transcription_file_full_version_title(),
        "",
        i18n.transcription_file_full_version_desc_1(),
        "",
        "-" * 40,
        ""
    ])
    
    # Add full transcript content
    full_lines = full_transcript.strip().split('\n')
    content_lines.extend(full_lines)
    
    # Add footer
    content_lines.extend([
        "",
        "",
        "=" * 80,
        i18n.transcription_file_footer_created_in(bot_link="https://t.me/WhisperSummaryAI_bot"),
        i18n.transcription_file_footer_created_date(date=datetime.now().strftime('%d.%m.%Y %H:%M:%S')),
        "=" * 80
    ])
    
    # Join all lines and encode to bytes
    full_content = '\n'.join(content_lines)
    buffer.write(full_content.encode('utf-8'))
    
    # Reset buffer position to start
    buffer.seek(0)
    return buffer


@async_log_decorator
async def create_simple_transcript_txt(transcript: str, i18n: TranslatorRunner, title: str = "Транскрипт") -> io.BytesIO:
    """
    Creates a simple TXT document with basic formatting.
    
    Args:
        transcript: The transcript text
        i18n: TranslatorRunner instance for translations
        title: Document title
        
    Returns:
        io.BytesIO: Buffer with TXT content
    """
    buffer = io.BytesIO()
    
    content_lines = [
        "=" * 80,
        i18n.transcription_file_made_with_whisper(),
        i18n.transcription_file_bot_label(bot_link="https://t.me/WhisperSummaryAI_bot"),
        "=" * 80,
        "",
        i18n.transcription_file_title_txt(title=title),
        i18n.transcription_file_date_txt(date=datetime.now().strftime('%d.%m.%Y %H:%M')),
        "",
        "=" * 80,
        ""
    ]
    
    # Add transcript content
    transcript_lines = transcript.strip().split('\n')
    content_lines.extend(transcript_lines)
    
    # Add footer
    content_lines.extend([
        "",
        "",
        "=" * 80,
        i18n.transcription_file_footer_created_in(bot_link="https://t.me/WhisperSummaryAI_bot"),
        i18n.transcription_file_footer_created_date(date=datetime.now().strftime('%d.%m.%Y %H:%M:%S')),
        "=" * 80
    ])
    
    # Join all lines and encode to bytes
    full_content = '\n'.join(content_lines)
    buffer.write(full_content.encode('utf-8'))
    
    # Reset buffer position to start
    buffer.seek(0)
    return buffer 