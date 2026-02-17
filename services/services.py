import io
import logging
import subprocess
import tempfile
import os
import asyncio
import aiofiles.os
import time
import uuid
from functools import wraps
import re
from datetime import datetime

from aiogram.types import BufferedInputFile
from pydub import AudioSegment
from fluentogram import TranslatorRunner

from services.word_service import create_enhanced_transcript_docx, create_simple_transcript_docx
from services.init_bot import config
from .txt_generator import create_enhanced_transcript_txt, create_simple_transcript_txt
from .markdown_service import create_markdown_buffer

# Настройка логгирования
logger = logging.getLogger(__name__)
logging.getLogger('fontTools').setLevel(logging.WARNING)

# Создаем форматтер для логов
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')

# Если обработчики не настроены, добавляем консольный обработчик
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)

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

# Декоратор для логгирования синхронных функций
def log_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        operation_id = str(uuid.uuid4())[:8]
        func_name = func.__name__
        logger.debug(f"[{operation_id}] Starting {func_name}")
        start_time = time.time()
        
        try:
            result = func(*args, **kwargs)
            execution_time = time.time() - start_time
            logger.debug(f"[{operation_id}] Completed {func_name} in {execution_time:.2f}s")
            return result
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"[{operation_id}] Failed {func_name} after {execution_time:.2f}s: {str(e)}")
            raise
    
    return wrapper

async def get_file_size(buffer_or_path: io.BytesIO | bytes | bytearray | memoryview | str | os.PathLike) -> int:
    """
    Возвращает размер в байтах для буфера или файла по пути (минимум операций).
    """
    if isinstance(buffer_or_path, (bytes, bytearray, memoryview)):
        return len(buffer_or_path)
    if isinstance(buffer_or_path, io.BytesIO):
        return buffer_or_path.getbuffer().nbytes
    st = await aiofiles.os.stat(buffer_or_path)
    return st.st_size

@async_log_decorator
async def create_input_file_from_text(full_transcript: str, 
                                      clean_transcript: str, filename: str, 
                                      i18n: TranslatorRunner, 
                                      format_type: str = "pdf") -> BufferedInputFile:
    """
    Создает файл из текста.
    Доступные форматы: pdf, md, txt, docx
    """
    text_length = len(full_transcript)
    logger.debug(f"Creating input file from text of length {text_length} characters")
    try:
        if format_type.lower() == "txt":
            logger.debug("Converting text to TXT format")
            try:
                buffer = await create_enhanced_transcript_txt(title=filename,
                                                            clean_transcript=clean_transcript,
                                                            full_transcript=full_transcript,
                                                              i18n=i18n)
                content = buffer.read()
                filename = filename + '.txt'
                logger.debug(f"Created enhanced TXT file, size: {len(content)/1024:.2f} KB")
            except:
                logger.error("Falling back to simple text format")
                buffer = io.BytesIO(full_transcript.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.txt'
                logger.debug(f"Created simple text file, size: {len(content)/1024:.2f} KB")
        elif format_type.lower() in ["md", "markdown"]:
            logger.debug("Converting text to Markdown format")
            try:
                buffer = await create_markdown_buffer(title=filename,
                                                    clean_transcript=clean_transcript,
                                                    full_transcript=full_transcript,
                                                    i18n=i18n)
                content = buffer.read()
                filename = filename + '.md'
                logger.debug(f"Created enhanced Markdown file, size: {len(content)/1024:.2f} KB")
            except Exception as e:
                logger.error(f"Falling back to simple markdown format: {str(e)}")
                # Fallback to simple markdown
                simple_markdown = f"# {filename}\n\n## Расшифровка\n\n{full_transcript}"
                buffer = io.BytesIO(simple_markdown.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.md'
                logger.debug(f"Created simple markdown file, size: {len(content)/1024:.2f} KB")
        elif format_type.lower() in ["docx", "doc"]:
            logger.info(f"Converting text to DOCX format: {full_transcript}, {clean_transcript}")
            try:
                buffer = await create_enhanced_transcript_docx(full_transcript=full_transcript,
                                                              clean_transcript=clean_transcript,
                                                              title=filename,
                                                              i18n=i18n)
                content = buffer.read()
                filename = filename + '.docx'
                logger.debug(f"Created enhanced DOCX file, size: {len(content)/1024:.2f} KB")
            except:
                logger.error("Falling back to plain text format")
                buffer = io.BytesIO(full_transcript.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.txt'
                logger.debug(f"Created simple text file, size: {len(content)/1024:.2f} KB")
        else:
            logger.debug("Converting text to PDF")
            try:
                buffer = await create_enhanced_transcript_pdf(full_transcript=full_transcript,
                                                              clean_transcript=clean_transcript,
                                                              title=filename,
                                                              i18n=i18n)
            except:
                logger.error("Falling back to plain text format")
                buffer = await text_to_pdf_buffer(full_transcript)

            content = buffer.read()
            
            # Check if content is valid PDF
            if content.startswith(b'%PDF'):
                filename = filename + '.pdf'
                logger.debug(f"Created valid PDF file, size: {len(content)/1024:.2f} KB")
            else:
                filename = filename + '.txt'
                logger.debug(f"Created text file, size: {len(content)/1024:.2f} KB")
            
        logger.debug(f"Successfully created input file: {filename}")
        return BufferedInputFile(content, filename=filename)
        
    except Exception as e:
        error_msg = i18n.create_file_error(error=str(e))
        logger.error(f"Error creating file from text: {error_msg}")
        
        # Fallback to plain text
        logger.debug("Falling back to plain text format")
        text_buffer = io.BytesIO(full_transcript.encode('utf-8'))
        text_content = text_buffer.read()
        filename = filename + '.txt'
        
        logger.debug(f"Created fallback text file: {filename}, size: {len(text_content)/1024:.2f} KB")
        return BufferedInputFile(text_content, filename=filename)


@async_log_decorator
async def create_single_input_file_from_text(transcript: str, 
                                           filename: str, 
                                           i18n: TranslatorRunner, 
                                           format_type: str = "pdf",
                                           transcript_type: str = "clean") -> BufferedInputFile:
    """
    Создает один файл из одного типа транскрипции.
    Доступные форматы: pdf, md, txt, docx
    Доступные типы: clean, full
    """
    text_length = len(transcript)
    logger.debug(f"Creating single input file from {transcript_type} transcript of length {text_length} characters")
    try:
        if format_type.lower() == "txt":
            logger.debug("Converting text to TXT format")
            try:
                buffer = await create_simple_transcript_txt(transcript=transcript, title=filename, i18n=i18n)
                content = buffer.read()
                filename = filename + '.txt'
                logger.debug(f"Created simple TXT file, size: {len(content)/1024:.2f} KB")
            except:
                logger.error("Falling back to basic text format")
                buffer = io.BytesIO(transcript.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.txt'
                logger.debug(f"Created basic text file, size: {len(content)/1024:.2f} KB")
        elif format_type.lower() in ["md", "markdown"]:
            logger.debug("Converting text to Markdown format")
            try:
                # Create simple markdown
                title_text = i18n.google_docs_clean_version_title() if transcript_type == 'clean' else i18n.google_docs_full_version_title()
                current_date = datetime.now().strftime("%d.%m.%Y")
                simple_markdown = f"""# {filename}

{title_text}. {i18n.google_docs_creation_date(date=current_date)}

---

{transcript}

---

*{i18n.google_docs_made_with_prefix()} [Whisper AI]({config.tg_bot.bot_url})*
"""
                buffer = io.BytesIO(simple_markdown.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.md'
                logger.debug(f"Created simple markdown file, size: {len(content)/1024:.2f} KB")
            except Exception as e:
                logger.error(f"Falling back to basic markdown format: {str(e)}")
                basic_markdown = f"# {filename}\n\n{transcript}"
                buffer = io.BytesIO(basic_markdown.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.md'
                logger.debug(f"Created basic markdown file, size: {len(content)/1024:.2f} KB")
        elif format_type.lower() in ["docx", "doc"]:
            logger.debug("Converting text to DOCX format")
            try:
                buffer = await create_simple_transcript_docx(transcript=transcript, title=filename, i18n=i18n, transcript_type=transcript_type)
                content = buffer.read()
                filename = filename + '.docx'
                logger.debug(f"Created simple DOCX file, size: {len(content)/1024:.2f} KB")
            except:
                logger.error("Falling back to plain text format")
                buffer = io.BytesIO(transcript.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.txt'
                logger.debug(f"Created simple text file, size: {len(content)/1024:.2f} KB")
        else:
            logger.debug("Converting text to PDF")
            try:
                buffer = await create_simple_transcript_pdf(transcript=transcript, title=filename, i18n=i18n, transcript_type=transcript_type)
                content = buffer.read()
                filename = filename + '.pdf'
                logger.debug(f"Created simple PDF file, size: {len(content)/1024:.2f} KB")
            except:
                logger.error("Falling back to plain text format")
                buffer = io.BytesIO(transcript.encode('utf-8'))
                content = buffer.read()
                filename = filename + '.txt'
                logger.debug(f"Created simple text file, size: {len(content)/1024:.2f} KB")
            
        logger.debug(f"Successfully created single input file: {filename}")
        return BufferedInputFile(content, filename=filename)
        
    except Exception as e:
        error_msg = i18n.create_file_error(error=str(e))
        logger.error(f"Error creating single file from text: {error_msg}")
        
        # Fallback to plain text
        logger.debug("Falling back to plain text format")
        text_buffer = io.BytesIO(transcript.encode('utf-8'))
        text_content = text_buffer.read()
        filename = filename + '.txt'
        
        logger.debug(f"Created fallback text file: {filename}, size: {len(text_content)/1024:.2f} KB")
        return BufferedInputFile(text_content, filename=filename)


@async_log_decorator
async def create_two_input_files_from_text(full_transcript: str, 
                                         clean_transcript: str, 
                                         filename: str, 
                                         i18n: TranslatorRunner, 
                                         format_type: str = "pdf") -> tuple[BufferedInputFile, BufferedInputFile]:
    """
    Создает два отдельных файла из текста - один для чистой транскрипции, другой для полной.
    Доступные форматы: pdf, md, txt, docx
    """
    logger.debug(f"Creating two separate input files for: {filename}")
    
    try:
        # Create titles for both files (without version in title, version info is in date line)
        clean_title = filename
        full_title = filename
        
        # Create both files
        clean_file = await create_single_input_file_from_text(
            transcript=clean_transcript,
            filename=clean_title,
            i18n=i18n,
            format_type=format_type,
            transcript_type='clean'
        )
        
        full_file = await create_single_input_file_from_text(
            transcript=full_transcript,
            filename=full_title,
            i18n=i18n,
            format_type=format_type,
            transcript_type='full'
        )
        
        logger.debug(f"Successfully created two separate files for: {filename}")
        return clean_file, full_file
        
    except Exception as e:
        error_msg = i18n.create_file_error(error=str(e))
        logger.error(f"Error creating two files from text: {error_msg}")
        
        # Fallback to simple text files
        clean_filename = f"{filename}_clean.txt"
        full_filename = f"{filename}_full.txt"
        
        clean_file = BufferedInputFile(clean_transcript.encode('utf-8'), filename=clean_filename)
        full_file = BufferedInputFile(full_transcript.encode('utf-8'), filename=full_filename)
        
        logger.debug(f"Created fallback text files for: {filename}")
        return clean_file, full_file


@async_log_decorator
async def split_audio(audio_data: bytes, chunk_size_ms: int = 600000, i18n: TranslatorRunner = None) -> list:
    """
    Split audio into parts of specified size.

    :param audio_data: Audio data as bytes
    :param chunk_size_ms: Size of each part in milliseconds (default 60 seconds)
    :param i18n: Translator instance for localization
    :return: List of audio parts as bytes
    """
    logger.debug(f"Splitting audio of size {len(audio_data)/1024/1024:.2f} MB with chunk size {chunk_size_ms} ms")
    try:
        # Создаем временный файл для входных данных
        # Используем NamedTemporaryFile только для генерации имени, но не открываем его
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as temp_input:
            temp_input_path = temp_input.name
        
        # Асинхронно записываем данные
        async with aiofiles.open(temp_input_path, 'wb') as f:
            await f.write(audio_data)
            logger.debug(f"Created temporary input file: {temp_input_path}")
        
        try:
            # Асинхронно загружаем аудио
            logger.debug(f"Loading audio from temporary file")
            audio = await asyncio.to_thread(AudioSegment.from_file, temp_input_path)
            logger.debug(f"Audio loaded, duration: {len(audio)/1000:.2f} seconds")
            audio_chunks = []

            total_chunks = (len(audio) + chunk_size_ms - 1) // chunk_size_ms
            logger.debug(f"Will create {total_chunks} chunks")

            for i in range(0, len(audio), chunk_size_ms):
                chunk_start_time = i / 1000  # в секундах
                chunk_end_time = min((i + chunk_size_ms) / 1000, len(audio) / 1000)  # в секундах
                logger.debug(f"Processing chunk {i//chunk_size_ms + 1}/{total_chunks} ({chunk_start_time:.2f}s - {chunk_end_time:.2f}s)")
                
                chunk = audio[i:i + chunk_size_ms]
                
                # Создаем временный файл для каждого чанка
                temp_chunk_path = f"{temp_input_path}_chunk_{i}.mp3"
                logger.debug(f"Exporting chunk to {temp_chunk_path}")
                
                # Экспортируем чанк во временный файл
                await asyncio.to_thread(chunk.export, temp_chunk_path, format="mp3")
                
                # Асинхронно читаем данные
                async with aiofiles.open(temp_chunk_path, 'rb') as chunk_file:
                    chunk_data = await chunk_file.read()
                    chunk_size_mb = len(chunk_data) / 1024 / 1024
                    logger.debug(f"Chunk {i//chunk_size_ms + 1} size: {chunk_size_mb:.2f} MB")
                    audio_chunks.append(chunk_data)
                
                # Удаляем временный файл чанка
                await aiofiles.os.remove(temp_chunk_path)
                logger.debug(f"Removed temporary chunk file: {temp_chunk_path}")

            logger.debug(f"Successfully split audio into {len(audio_chunks)} chunks")
            return audio_chunks
        finally:
            # Удаляем входной временный файл
            if os.path.exists(temp_input_path):
                await aiofiles.os.remove(temp_input_path)
                logger.debug(f"Removed temporary input file: {temp_input_path}")
    except Exception as e:
        if i18n:
            error_msg = i18n.audio_processing_error(service='AudioSegment', error=str(e))
            logger.error(error_msg)
        else:
            logger.error(f"Error splitting audio: {str(e)}")
        raise


@async_log_decorator
async def extract_audio_from_video(
    i18n: TranslatorRunner,
    video_buffer: io.BytesIO | None = None,
    file_path: str | None = None,
    output: str = 'bytes',  # 'bytes' | 'path'
    output_file_path: str | None = None,
    ffmpeg_timeout_seconds: int = 600
) -> bytes | str:
    """
    Извлекает аудио-дорожку из видео при минимальном использовании RAM.

    Поддерживает два режима входа:
    - video_buffer: io.BytesIO — видео в памяти (будет записано на диск по частям)
    - file_path: str — путь к видео на диске (предпочтительно для экономии RAM)

    И два режима выхода:
    - output='bytes' — вернуть байты аудио (mp3). Временные файлы удаляются
    - output='path' — вернуть путь к файлу на диске. Файл НЕ удаляется

    Возвращает: bytes | str (в зависимости от параметра output)
    """

    if not video_buffer and not file_path:
        raise ValueError("Either video_buffer or file_path must be provided")

    created_input_temp = False
    input_video_path = None
    audio_output_path = None
    created_audio_temp = False

    try:
        # Готовим входной файл на диске, избегая .getvalue() чтобы не копировать буфер целиком в RAM
        if file_path:
            # Проверяем, что файл существует и доступен
            try:
                st = await aiofiles.os.stat(file_path)
                logger.debug(f"Extracting audio from file: {file_path} (size {st.st_size/1024/1024:.2f} MB)")
            except FileNotFoundError:
                logger.error(f"Video file not found: {file_path}")
                raise
            input_video_path = file_path
        else:
            # Сохраняем BytesIO на диск ПО ЧАСТЯМ
            # Используем NamedTemporaryFile только для генерации имени
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_video:
                input_video_path = temp_video.name
            created_input_temp = True
            logger.debug(f"Created temporary video file: {input_video_path}")
            try:
                video_buffer.seek(0)
            except Exception:
                pass
            
            # Асинхронная запись
            async with aiofiles.open(input_video_path, 'wb') as f:
                while True:
                    chunk = video_buffer.read(1024 * 1024)
                    if not chunk:
                        break
                    await f.write(chunk)

        # Готовим выходной файл
        if output_file_path:
            audio_output_path = output_file_path
        else:
            # Создаем временный выходной путь
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as temp_audio:
                audio_output_path = temp_audio.name
                created_audio_temp = True
        logger.debug(f"Audio will be extracted to: {audio_output_path}")

        async def _run_ffmpeg(args: list[str]) -> tuple[int, bytes, bytes]:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=ffmpeg_timeout_seconds)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                return 124, b'', b'Timeout'
            return process.returncode, stdout, stderr

        # Базовые флаги для устойчивой работы
        base_flags = [
            'ffmpeg',
            '-y',               # overwrite output
            '-nostdin',         # не читать stdin
            '-hide_banner',
            '-loglevel', 'error',
            '-threads', '1',    # меньше шансов на SEGV в некоторых окружениях
            '-fflags', '+genpts',
            '-i', input_video_path,
            '-vn', '-sn', '-dn',
            '-ac', '2',
            '-ar', '44100',
            '-c:a', 'libmp3lame',
            '-b:a', '192k'
        ]

        # Попытка 1: Явная первая аудио-дорожка
        args1 = base_flags + ['-map', '0:a:0', audio_output_path]
        logger.info(f"Starting ffmpeg (attempt 1) to extract audio: {audio_output_path}")
        rc, _so, se = await _run_ffmpeg(args1)

        # Попытка 2: Без жесткой привязки к 0:a:0
        if rc != 0 or not (os.path.exists(audio_output_path) and os.path.getsize(audio_output_path) > 0):
            if os.path.exists(audio_output_path):
                try:
                    os.remove(audio_output_path)
                except Exception:
                    pass
            args2 = base_flags + ['-map', '0:a?', audio_output_path]
            logger.warning(f"ffmpeg attempt 1 failed (rc={rc}). Retrying with relaxed mapping")
            rc, _so, se = await _run_ffmpeg(args2)

        # Проверяем результат
        if rc != 0 or not (os.path.exists(audio_output_path) and os.path.getsize(audio_output_path) > 0):
            error_message = (se or b'').decode(errors='ignore')
            logger.error(f"ffmpeg failed (rc={rc}): {error_message}")
            raise RuntimeError(i18n.ffmpeg_failed())

        # Возвращаем согласно требуемому типу
        if output == 'path':
            logger.debug("Audio extraction completed, returning file path")
            return audio_output_path

        # Иначе читаем байты и чистим файлы
        logger.debug("Reading extracted audio file into memory")
        async with aiofiles.open(audio_output_path, 'rb') as audio_file:
            audio_data = await audio_file.read()
        audio_size_mb = len(audio_data) / 1024 / 1024
        logger.debug(f"Audio extraction completed. Audio size: {audio_size_mb:.2f} MB")
        return audio_data
    except Exception as e:
        logger.error(f"Error extracting audio from video: {str(e)}")
        raise
    finally:
        # Удаляем временный входной видеофайл (если создавали)
        if created_input_temp and input_video_path and os.path.exists(input_video_path):
            try:
                await aiofiles.os.remove(input_video_path)
                logger.debug(f"Removed temporary video file: {input_video_path}")
            except Exception as e:
                logger.warning(f"Failed to remove temporary video file {input_video_path}: {str(e)}")
        # Удаляем временный аудиофайл, если мы не возвращаем путь наружу
        if created_audio_temp and audio_output_path and os.path.exists(audio_output_path) and output != 'path':
            try:
                await aiofiles.os.remove(audio_output_path)
                logger.debug(f"Removed temporary audio file: {audio_output_path}")
            except Exception as e:
                logger.warning(f"Failed to remove temporary audio file {audio_output_path}: {str(e)}")


@async_log_decorator
async def get_audio_duration(audio_bytes: bytes | None = None, file_path: str | None = None, timeout_seconds: int = 60) -> float:
    """
    Возвращает длительность аудио в секундах, используя ffprobe (без декодирования всего файла в RAM).

    Предпочитает file_path. Если переданы только bytes, временно пишет на диск и вызывает ffprobe.
    """
    temp_file_path = None
    try:
        load_path = None

        if file_path:
            try:
                file_stat = await aiofiles.os.stat(file_path)
                logger.debug(f"Extracting duration via ffprobe from file: {file_path} (size {file_stat.st_size/1024/1024:.2f} MB)")
                load_path = file_path
            except FileNotFoundError:
                logger.error(f"File not found for duration extraction: {file_path}")
                return 0.0
        else:
            if not audio_bytes:
                logger.error("Neither audio bytes nor file path provided for duration extraction")
                return 0.0

            logger.debug(f"Extracting duration from audio bytes of size {len(audio_bytes)/1024/1024:.2f} MB via ffprobe")
            # Используем NamedTemporaryFile только для генерации имени
            with tempfile.NamedTemporaryFile(delete=False, suffix='.audio') as temp_file:
                temp_file_path = temp_file.name
            
            # Асинхронно записываем данные
            buffer = io.BytesIO(audio_bytes)
            buffer.seek(0)
            async with aiofiles.open(temp_file_path, 'wb') as f:
                while True:
                    chunk = buffer.read(1024 * 1024)
                    if not chunk:
                        break
                    await f.write(chunk)
            load_path = temp_file_path

        # Запускаем ffprobe
        args = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            load_path
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            logger.error("ffprobe timed out while getting duration")
            return 0.0

        if process.returncode != 0:
            logger.error(f"ffprobe failed (rc={process.returncode}): {(stderr or b'').decode(errors='ignore')}")
            return 0.0

        try:
            duration_seconds = float(stdout.decode().strip())
        except Exception:
            logger.error(f"ffprobe returned non-float output: {stdout!r}")
            return 0.0

        logger.debug(f"Audio duration extracted: {duration_seconds:.2f} seconds")
        return duration_seconds
    except Exception as e:
        logger.error(f"Error extracting audio duration: {str(e)}")
        return 0.0
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                await aiofiles.os.remove(temp_file_path)
                logger.debug(f"Removed temporary file: {temp_file_path}")
            except Exception as e:
                logger.warning(f"Failed to remove temporary file {temp_file_path}: {str(e)}")


@async_log_decorator
async def convert_to_mp3(
    input_audio: bytes | None = None,
    file_path: str | None = None,
    output: str = 'bytes'  # 'bytes' | 'path'
) -> bytes | str:
    """
    Конвертирует входной аудиофайл в MP3, минимизируя использование RAM.

    Вход:
    - input_audio: байты входного файла (нежелательно для больших файлов)
    - file_path: путь к входному файлу на диске (предпочтительно)

    Выход:
    - output='bytes': возвращает байты MP3
    - output='path': возвращает путь к MP3 файлу (временный файл, удалить снаружи)
    """

    if not input_audio and not file_path:
        raise ValueError("Either input_audio or file_path must be provided")

    temp_input_path = None
    created_input_temp = False
    temp_output_path = None
    created_output_temp = False

    try:
        if file_path:
            temp_input_path = file_path
            try:
                st = await aiofiles.os.stat(file_path)
                logger.debug(f"Converting file to MP3: {file_path} (size {st.st_size/1024/1024:.2f} MB)")
            except FileNotFoundError:
                logger.error(f"Input file not found for MP3 conversion: {file_path}")
                raise
        else:
            # Сохраняем входные байты на диск
            # Используем NamedTemporaryFile только для генерации имени
            with tempfile.NamedTemporaryFile(delete=False, suffix='.audio') as temp_input:
                temp_input_path = temp_input.name
            
            created_input_temp = True
            logger.debug(f"Created temporary input file: {temp_input_path}")
            
            buffer = io.BytesIO(input_audio)
            buffer.seek(0)
            async with aiofiles.open(temp_input_path, 'wb') as f:
                while True:
                    chunk = buffer.read(1024 * 1024)
                    if not chunk:
                        break
                    await f.write(chunk)

        # Готовим выходной путь
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as temp_output:
            temp_output_path = temp_output.name
            created_output_temp = True
        logger.debug(f"Output will be saved to: {temp_output_path}")

        # Конвертация через pydub/ffmpeg
        logger.debug("Loading audio file for conversion")
        input_audio_segment = await asyncio.to_thread(AudioSegment.from_file, temp_input_path)
        logger.debug(f"Audio loaded, duration: {len(input_audio_segment)/1000:.2f} seconds")

        logger.debug("Exporting audio to MP3 format")
        await asyncio.to_thread(input_audio_segment.export, temp_output_path, format="mp3")
        logger.debug("Export completed")

        if output == 'path':
            logger.debug("Returning output file path for MP3")
            return temp_output_path

        # Возвращаем байты и чистим временный выходной файл
        logger.debug("Reading converted MP3 bytes")
        async with aiofiles.open(temp_output_path, 'rb') as output_file:
            output_bytes = await output_file.read()
        output_size_mb = len(output_bytes) / 1024 / 1024
        logger.debug(f"Conversion completed. Output size: {output_size_mb:.2f} MB")
        return output_bytes
    except Exception as e:
        logger.error(f"Error converting audio to MP3: {str(e)}")
        raise
    finally:
        # Удаляем временные входные данные, если создавали
        if created_input_temp and temp_input_path and os.path.exists(temp_input_path):
            try:
                await aiofiles.os.remove(temp_input_path)
                logger.debug(f"Removed temporary input file: {temp_input_path}")
            except Exception as e:
                logger.warning(f"Failed to remove temporary input file {temp_input_path}: {str(e)}")
        # Удаляем временный выходной файл только если мы возвращали байты (output != 'path')
        if created_output_temp and temp_output_path and os.path.exists(temp_output_path) and output != 'path':
            try:
                await aiofiles.os.remove(temp_output_path)
                logger.debug(f"Removed temporary output file: {temp_output_path}")
            except Exception as e:
                logger.warning(f"Failed to remove temporary output file {temp_output_path}: {str(e)}")

@log_decorator
def sources_to_str(sources: list[tuple[str, int]], i18n: TranslatorRunner, subscription: bool = False) -> str:
    if subscription:
        stat_label = i18n.source_subscriptions()
    else:
        stat_label = i18n.source_visits()
    source_link = i18n.source_link()
    return '\n'.join([
        i18n.source_item(
            number=i,
            link_label=source_link,
            source=source[0],
            stat_label=stat_label,
            count=source[1]
        ) for i, source in enumerate(sources, 1)
    ])


@log_decorator
def sources_to_str_paginated(sources: list[tuple[str, int]], page: int, per_page: int, i18n: TranslatorRunner, subscription: bool = False) -> tuple[str, int, bool, bool]:
    """
    Форматирует источники с пагинацией.
    
    Args:
        sources: Список источников в формате (source, count)
        page: Номер текущей страницы (начиная с 1)
        per_page: Количество элементов на странице
        i18n: Переводчик
        subscription: Флаг подписки
    
    Returns:
        tuple: (formatted_text, total_pages, has_previous, has_next)
    """
    if not sources:
        return i18n.no_sources_found(), 1, False, False
    
    total_items = len(sources)
    total_pages = (total_items + per_page - 1) // per_page  # Округление вверх
    
    # Проверяем границы страницы
    page = max(1, min(page, total_pages))
    
    # Вычисляем индексы для текущей страницы
    start_idx = (page - 1) * per_page
    end_idx = min(start_idx + per_page, total_items)
    
    # Получаем источники для текущей страницы
    page_sources = sources[start_idx:end_idx]
    
    # Форматируем источники
    if subscription:
        stat_label = i18n.source_subscriptions()
    else:
        stat_label = i18n.source_visits()
    source_link = i18n.source_link()
    
    formatted_sources = '\n'.join([
        i18n.source_item(
            number=start_idx + i + 1,  # Глобальная нумерация
            link_label=source_link,
            source=source[0],
            stat_label=stat_label,
            count=source[1]
        ) for i, source in enumerate(page_sources)
    ])
    
    # Информация о пагинации
    has_previous = page > 1
    has_next = page < total_pages
    
    return formatted_sources, total_pages, has_previous, has_next

@async_log_decorator
async def delete_file(file_path: str):
    logger.debug(f"Attempting to delete file: {file_path}")
    
    try:
        # Безопасная проверка пустых значений
        if not file_path:
            logger.debug("Skip deletion: empty or None path")
            return

        # Нормализуем путь (безопасно для None уже проверено)
        path_str = str(file_path)

        # Проверяем существование файла
        try:
            is_file = await aiofiles.os.path.isfile(path_str)
        except Exception:
            # Некоторые окружения aiofiles.os.path.isfile могут вести себя нестабильно; fallback на os.path
            is_file = os.path.isfile(path_str)

        if is_file:
            try:
                file_stat = await aiofiles.os.stat(path_str)
                file_size_kb = file_stat.st_size / 1024
                logger.debug(f"File exists, size: {file_size_kb:.2f} KB")
            except Exception:
                # Если не удалось прочитать stat, продолжаем попытку удаления
                logger.debug("Could not stat file before deletion; proceeding to remove")

            try:
                await aiofiles.os.remove(path_str)
            except Exception:
                # Fallback на синхронное удаление в отдельном потоке
                await asyncio.to_thread(os.remove, path_str)
            logger.debug(f"Successfully deleted file: {path_str}")
        else:
            logger.debug(f"Skip deletion: file does not exist: {path_str}")
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {str(e)}")


def progress_bar(progress, i18n: TranslatorRunner = None) -> str:
    # Define progress bar length
    bar_length = 20

    # Limit progress value between 0 and 100
    progress = max(0, min(100, progress))

    # Calculate number of filled characters
    filled_length = int(bar_length * progress // 100)

    # Form progress bar
    bar = '█' * filled_length + '-' * (bar_length - filled_length)

    # Return progress bar string with percentage
    return f'[{bar}] {progress}%'


def calculate_progress(index, total, i18n: TranslatorRunner = None) -> int:
    """
    Calculate progress percentage for a given index in a sequence.

    :param index: Current index
    :param total: Total number of items
    :param i18n: Translator instance for localization
    :return: Progress percentage (45-79)
    """
    start = 45
    end = 79

    # If list has only one element, return 45%
    if total == 1:
        return start

    # Calculate progress step
    step = (end - start) / (total - 1)

    # Calculate progress for current element
    progress = int(start + step * index)

    if i18n:
        logger.debug(i18n.progress_bar(bar=progress_bar(progress), progress=progress))

    return progress

from fpdf import FPDF
import re
import threading

# Thread-safe font initialization to prevent concurrent cache generation/writes
_FONT_INIT_LOCK = threading.Lock()
_FONT_CACHE_WARMED = False
_FONT_FAMILY_NAME = 'DejaVu'
_FONT_TTF_PATH = 'resources/fonts/Arial Unicode MS.TTF'

def _warm_font_cache_once() -> None:
    """Ensure the TTF -> pickle cache is created exactly once per process.

    FPDF/fontTools may write cache files alongside the TTF. Doing that
    concurrently from multiple threads can corrupt the cache and/or crash
    the interpreter. This function serializes the initial cache build.
    """
    global _FONT_CACHE_WARMED
    if _FONT_CACHE_WARMED:
        return
    with _FONT_INIT_LOCK:
        if _FONT_CACHE_WARMED:
            return
        try:
            tmp_pdf = FPDF()
            # Build cache files once
            tmp_pdf.add_font(_FONT_FAMILY_NAME, '', _FONT_TTF_PATH, uni=True)
            tmp_pdf.add_font(_FONT_FAMILY_NAME, 'B', _FONT_TTF_PATH, uni=True)
        except Exception as e:
            logger.warning(f"Font cache warm-up failed: {e}")
        else:
            _FONT_CACHE_WARMED = True

def _add_unicode_fonts(pdf: FPDF) -> None:
    """Safely register unicode fonts on a specific PDF instance.

    Uses a process-wide warm-up and serializes add_font to avoid races.
    """
    if not _FONT_CACHE_WARMED:
        _warm_font_cache_once()
    # After warm-up, adding fonts should read from cache and is safe to run concurrently
    pdf.add_font(_FONT_FAMILY_NAME, '', _FONT_TTF_PATH, uni=True)
    pdf.add_font(_FONT_FAMILY_NAME, 'B', _FONT_TTF_PATH, uni=True)

def clean_text_for_pdf(text: str) -> str:
    """
    Removes Unicode directional control characters and other problematic characters for PDF generation.
    
    Args:
        text: Input text to clean
        
    Returns:
        Cleaned text safe for PDF generation
    """
    # Remove Unicode directional control characters
    directional_chars = [
        '\u2068',  # First Strong Isolate (FSI)
        '\u2069',  # Pop Directional Isolate (PDI)
        '\u202A',  # Left-to-Right Embedding (LRE)
        '\u202B',  # Right-to-Left Embedding (RLE)
        '\u202C',  # Pop Directional Formatting (PDF)
        '\u202D',  # Left-to-Right Override (LRO)
        '\u202E',  # Right-to-Left Override (RLO)
        '\u061C',  # Arabic Letter Mark (ALM)
        '\u200E',  # Left-to-Right Mark (LRM)
        '\u200F',  # Right-to-Left Mark (RLM)
        '\u2066',  # Left-to-Right Isolate (LRI)
        '\u2067',  # Right-to-Left Isolate (RLI)
    ]
    
    # Remove each directional character
    for char in directional_chars:
        text = text.replace(char, '')
    
    # Remove any other control characters except newlines and tabs
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', text)
    
    return text

async def text_to_pdf_buffer(text: str, line_height: int = 10) -> io.BytesIO:
    """
    Asynchronously converts text to PDF and returns it as a memory buffer.
    
    Args:
        text: Text to convert
    Returns:
        io.BytesIO: Buffer with PDF content
    """
    def _build_pdf_sync() -> io.BytesIO:
        local_buffer = io.BytesIO()
        pdf = FPDF()
        pdf.add_page()
        _add_unicode_fonts(pdf)
        pdf.set_font(_FONT_FAMILY_NAME, '', 14)
        # Clean text to remove problematic Unicode characters
        cleaned_text = clean_text_for_pdf(text)
        pdf.multi_cell(0, 6, cleaned_text)
        pdf.output(local_buffer)
        local_buffer.seek(0)
        return local_buffer

    return await asyncio.to_thread(_build_pdf_sync)


async def create_simple_transcript_pdf(transcript: str, title: str, i18n: TranslatorRunner, transcript_type: str = 'clean') -> io.BytesIO:
    """
    Creates a beautifully formatted PDF with a single transcript (keeping all original styling).
    Based on create_enhanced_transcript_pdf but without table of contents and dual versions.
    
    Args:
        transcript: The transcript text
        title: Document title
        i18n: TranslatorRunner instance for translations
        transcript_type: Type of transcript ('clean' or 'full')
        
    Returns:
        io.BytesIO: Buffer with enhanced PDF content
    """
    def _build_simple_pdf_sync() -> io.BytesIO:
        local_buffer = io.BytesIO()
        pdf = EnhancedPDF()
        _add_unicode_fonts(pdf)
        pdf.add_page()
        try:
            logo_width = 25
            logo_height = 25
            logo_x = pdf.l_margin
            logo_y = pdf.t_margin
            pdf.image('resources/whisper_logo.jpg', logo_x, logo_y, logo_width, logo_height)
            pdf.set_font(_FONT_FAMILY_NAME, '', 12)
            pdf.set_text_color(100, 100, 100)
            text_x = logo_x + logo_width + 5
            text_y = logo_y + 8
            pdf.set_xy(text_x, text_y)
            made_with_text = i18n.transcription_file_made_with_prefix()
            first_part_width = pdf.get_string_width(made_with_text)
            pdf.cell(first_part_width, 8, made_with_text, 0, 0, 'L')
            pdf.set_text_color(0, 0, 255)
            bot_link = 'https://t.me/WhisperSummaryAI_bot'
            whisper_ai_text = i18n.transcription_file_whisper_ai_text()
            whisper_width = pdf.get_string_width(whisper_ai_text)
            pdf.set_xy(text_x + first_part_width, text_y)
            pdf.cell(whisper_width, 8, whisper_ai_text, 0, 0, 'L', link=bot_link)
        except Exception:
            pdf.set_font(_FONT_FAMILY_NAME, '', 12)
            pdf.set_text_color(100, 100, 100)
            made_with_text = i18n.transcription_file_made_with_prefix()
            first_part_width = pdf.get_string_width(made_with_text)
            pdf.cell(first_part_width, 8, made_with_text, 0, 0, 'L')
            pdf.set_text_color(0, 0, 255)
            bot_link = 'https://t.me/WhisperSummaryAI_bot'
            whisper_ai_text = i18n.transcription_file_whisper_ai_text()
            whisper_width = pdf.get_string_width(whisper_ai_text)
            current_x = pdf.get_x()
            pdf.set_x(current_x)
            pdf.cell(whisper_width, 8, whisper_ai_text, 0, 0, 'L', link=bot_link)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(20)
        pdf.set_font(_FONT_FAMILY_NAME, 'B', 22)
        # Clean title text
        cleaned_title = clean_text_for_pdf(title)
        pdf.multi_cell(0, 12, cleaned_title, 0, 'L')
        pdf.ln(10)
        pdf.set_font(_FONT_FAMILY_NAME, '', 12)
        current_date = datetime.now().strftime("%d.%m.%Y")
        version_title = i18n.google_docs_clean_version_title() if transcript_type == 'clean' else i18n.google_docs_full_version_title()
        version_date_text = f"{version_title}. {i18n.google_docs_creation_date(date=current_date)}"
        # Clean version date text
        cleaned_version_date_text = clean_text_for_pdf(version_date_text)
        pdf.cell(0, 8, cleaned_version_date_text, 0, 1, 'L')
        pdf.ln(20)
        pdf.set_font(_FONT_FAMILY_NAME, '', 14)
        # Clean transcript text
        cleaned_transcript = clean_text_for_pdf(transcript)
        pdf.multi_cell(0, 6, cleaned_transcript)
        pdf.output(local_buffer)
        local_buffer.seek(0)
        return local_buffer

    return await asyncio.to_thread(_build_simple_pdf_sync)


class EnhancedPDF(FPDF):
    """Enhanced PDF class with page numbers in footer"""
    
    def footer(self):
        """Add page number in bottom right corner"""
        self.set_y(-15)
        self.set_font('DejaVu', '', 10)
        # Add page number in bottom right
        self.cell(0, 10, f'{self.page_no()}', 0, 0, 'R')


async def create_enhanced_transcript_pdf(title: str, clean_transcript: str, full_transcript: str,
                                         i18n: TranslatorRunner) -> io.BytesIO:
    """
    Creates an enhanced PDF with title, table of contents, and both clean and full transcript versions.
    Uses the same font and approach as the main text_to_pdf_buffer function for maximum compatibility.
    
    Args:
        title: Document title
        clean_transcript: Clean version of the transcript (without timestamps/speakers)
        full_transcript: Full version with timestamps and speakers
        
    Returns:
        io.BytesIO: Buffer with enhanced PDF content
    """
    def _build_enhanced_pdf_sync() -> io.BytesIO:
        local_buffer = io.BytesIO()
        pdf = EnhancedPDF()
        _add_unicode_fonts(pdf)
        pdf.add_page()
        try:
            logo_width = 25
            logo_height = 25
            logo_x = pdf.l_margin
            logo_y = pdf.t_margin
            pdf.image('resources/whisper_logo.jpg', logo_x, logo_y, logo_width, logo_height)
            pdf.set_font(_FONT_FAMILY_NAME, '', 12)
            pdf.set_text_color(100, 100, 100)
            text_x = logo_x + logo_width + 5
            text_y = logo_y + 8
            pdf.set_xy(text_x, text_y)
            made_with_text = i18n.transcription_file_made_with_prefix()
            first_part_width = pdf.get_string_width(made_with_text)
            pdf.cell(first_part_width, 8, made_with_text, 0, 0, 'L')
            pdf.set_text_color(0, 0, 255)
            bot_link = 'https://t.me/WhisperSummaryAI_bot'
            whisper_ai_text = i18n.transcription_file_whisper_ai_text()
            whisper_width = pdf.get_string_width(whisper_ai_text)
            pdf.set_xy(text_x + first_part_width, text_y)
            pdf.cell(whisper_width, 8, whisper_ai_text, 0, 0, 'L', link=bot_link)
        except Exception:
            pdf.set_font(_FONT_FAMILY_NAME, '', 12)
            pdf.set_text_color(100, 100, 100)
            made_with_text = i18n.transcription_file_made_with_prefix()
            first_part_width = pdf.get_string_width(made_with_text)
            pdf.cell(first_part_width, 8, made_with_text, 0, 0, 'L')
            pdf.set_text_color(0, 0, 255)
            bot_link = 'https://t.me/WhisperSummaryAI_bot'
            whisper_ai_text = i18n.transcription_file_whisper_ai_text()
            whisper_width = pdf.get_string_width(whisper_ai_text)
            current_x = pdf.get_x()
            pdf.set_x(current_x)
            pdf.cell(whisper_width, 8, whisper_ai_text, 0, 0, 'L', link=bot_link)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(20)
        pdf.set_font(_FONT_FAMILY_NAME, 'B', 22)
        # Clean title text
        cleaned_title = clean_text_for_pdf(title)
        pdf.multi_cell(0, 12, cleaned_title, 0, 'L')
        from datetime import datetime as _dt
        pdf.ln(10)
        pdf.set_font(_FONT_FAMILY_NAME, '', 12)
        current_date = _dt.now().strftime("%d.%m.%Y")
        pdf.cell(0, 8, i18n.transcription_file_date_pdf(date=current_date), 0, 1, 'L')
        pdf.ln(20)
        pdf.set_font(_FONT_FAMILY_NAME, 'B', 18)
        pdf.cell(0, 12, i18n.transcription_file_table_of_contents_title(), 0, 1, 'L')
        pdf.ln(5)
        pdf.set_font(_FONT_FAMILY_NAME, '', 14)
        clean_link = pdf.add_link()
        pdf.set_link(clean_link, page=2)
        pdf.set_text_color(0, 0, 255)
        pdf.cell(0, 10, i18n.transcription_file_toc_clean_version(), 0, 1, 'L', link=clean_link)
        full_link = pdf.add_link()
        pdf.set_text_color(0, 0, 255)
        pdf.cell(0, 10, i18n.transcription_file_toc_full_version(), 0, 1, 'L', link=full_link)
        pdf.set_text_color(0, 0, 0)
        pdf.add_page()
        pdf.set_font(_FONT_FAMILY_NAME, '', 18)
        pdf.multi_cell(0, 15, i18n.transcription_file_clean_version_title(), 0, 'L')
        pdf.ln(5)
        pdf.set_font(_FONT_FAMILY_NAME, '', 11)
        pdf.set_text_color(80, 80, 80)
        clean_description = i18n.transcription_file_clean_version_desc_pdf()
        pdf.multi_cell(0, 5, clean_description)
        pdf.ln(8)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font(_FONT_FAMILY_NAME, '', 14)
        # Clean transcript text
        cleaned_clean_transcript = clean_text_for_pdf(clean_transcript)
        pdf.multi_cell(0, 6, cleaned_clean_transcript)
        current_page = pdf.page_no() + 1
        pdf.set_link(full_link, page=current_page)
        pdf.add_page()
        pdf.set_font(_FONT_FAMILY_NAME, '', 18)
        pdf.multi_cell(0, 15, i18n.transcription_file_full_version_title(), 0, 'L')
        pdf.ln(5)
        pdf.set_font(_FONT_FAMILY_NAME, '', 11)
        pdf.set_text_color(80, 80, 80)
        full_description = i18n.transcription_file_full_version_desc_pdf()
        pdf.multi_cell(0, 5, full_description)
        pdf.ln(8)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font(_FONT_FAMILY_NAME, '', 14)
        # Clean full transcript text
        cleaned_full_transcript = clean_text_for_pdf(full_transcript)
        pdf.multi_cell(0, 6, cleaned_full_transcript)
        pdf.output(local_buffer)
        local_buffer.seek(0)
        return local_buffer

    return await asyncio.to_thread(_build_enhanced_pdf_sync)

def format_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = seconds % 60

    if hours > 0:
        return f'{hours:02}:{minutes:02}:{seconds:05.2f}'
    elif minutes > 0:
        return f'{minutes:02}:{seconds:05.2f}'
    else:
        return f'{seconds:.2f}'

async def sanitize_html_for_telegram(text: str) -> str:
    """
    Очищает HTML-теги для отправки в Telegram.
    Удаляет неподдерживаемые теги и экранирует специальные символы.
    
    Telegram поддерживает только: <b>, <strong>, <i>, <em>, <u>, <ins>, 
    <s>, <strike>, <del>, <a>, <code>, <pre>, <tg-spoiler>, <span class="tg-spoiler">
    
    Args:
        text: Текст с возможными HTML-тегами
        
    Returns:
        Очищенный текст, безопасный для отправки в Telegram
    """
    import html
    
    # Сначала заменяем <br> и <br/> на переносы строк
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    
    # Также обрабатываем <p> теги
    text = re.sub(r'<p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    
    # Паттерны для поддерживаемых тегов (сохраним их временно)
    # Формат: (pattern, replacement, flags)
    supported_tags = [
        (r'<b>(.*?)</b>', '<!B_START!>\\1<!B_END!>', re.IGNORECASE),
        (r'<strong>(.*?)</strong>', '<!STRONG_START!>\\1<!STRONG_END!>', re.IGNORECASE),
        (r'<i>(.*?)</i>', '<!I_START!>\\1<!I_END!>', re.IGNORECASE),
        (r'<em>(.*?)</em>', '<!EM_START!>\\1<!EM_END!>', re.IGNORECASE),
        (r'<u>(.*?)</u>', '<!U_START!>\\1<!U_END!>', re.IGNORECASE),
        (r'<ins>(.*?)</ins>', '<!INS_START!>\\1<!INS_END!>', re.IGNORECASE),
        (r'<s>(.*?)</s>', '<!S_START!>\\1<!S_END!>', re.IGNORECASE),
        (r'<strike>(.*?)</strike>', '<!STRIKE_START!>\\1<!STRIKE_END!>', re.IGNORECASE),
        (r'<del>(.*?)</del>', '<!DEL_START!>\\1<!DEL_END!>', re.IGNORECASE),
        (r'<code>(.*?)</code>', '<!CODE_START!>\\1<!CODE_END!>', re.IGNORECASE),
        (r'<pre>(.*?)</pre>', '<!PRE_START!>\\1<!PRE_END!>', re.IGNORECASE | re.DOTALL),
        (r'<a\s+href=["\']([^"\']+)["\']>(.*?)</a>', '<!A_START_\\1!>\\2<!A_END!>', re.IGNORECASE),
        (r'<tg-spoiler>(.*?)</tg-spoiler>', '<!SPOILER_START!>\\1<!SPOILER_END!>', re.IGNORECASE),
    ]
    
    # Временно заменяем поддерживаемые теги на плейсхолдеры
    for pattern, replacement, flags in supported_tags:
        text = re.sub(pattern, replacement, text, flags=flags)
    
    # Экранируем все оставшиеся HTML-символы
    text = html.escape(text)
    
    # Восстанавливаем поддерживаемые теги
    text = text.replace('&lt;!B_START!&gt;', '<b>').replace('&lt;!B_END!&gt;', '</b>')
    text = text.replace('&lt;!STRONG_START!&gt;', '<strong>').replace('&lt;!STRONG_END!&gt;', '</strong>')
    text = text.replace('&lt;!I_START!&gt;', '<i>').replace('&lt;!I_END!&gt;', '</i>')
    text = text.replace('&lt;!EM_START!&gt;', '<em>').replace('&lt;!EM_END!&gt;', '</em>')
    text = text.replace('&lt;!U_START!&gt;', '<u>').replace('&lt;!U_END!&gt;', '</u>')
    text = text.replace('&lt;!INS_START!&gt;', '<ins>').replace('&lt;!INS_END!&gt;', '</ins>')
    text = text.replace('&lt;!S_START!&gt;', '<s>').replace('&lt;!S_END!&gt;', '</s>')
    text = text.replace('&lt;!STRIKE_START!&gt;', '<strike>').replace('&lt;!STRIKE_END!&gt;', '</strike>')
    text = text.replace('&lt;!DEL_START!&gt;', '<del>').replace('&lt;!DEL_END!&gt;', '</del>')
    text = text.replace('&lt;!CODE_START!&gt;', '<code>').replace('&lt;!CODE_END!&gt;', '</code>')
    text = text.replace('&lt;!PRE_START!&gt;', '<pre>').replace('&lt;!PRE_END!&gt;', '</pre>')
    text = text.replace('&lt;!SPOILER_START!&gt;', '<tg-spoiler>').replace('&lt;!SPOILER_END!&gt;', '</tg-spoiler>')
    
    # Восстанавливаем ссылки
    text = re.sub(r'&lt;!A_START_([^!]+)!&gt;(.*?)&lt;!A_END!&gt;', r'<a href="\1">\2</a>', text)
    
    return text

async def replace_markdown_bold_with_html(text: str) -> str:
    """
    Заменяет двойные звездочки (маркдаун-выделение жирным) на HTML-теги жирного шрифта.
    
    Пример: **текст** => <b>текст</b>
    
    Args:
        text: Исходный текст, возможно содержащий двойные звездочки
        
    Returns:
        Текст с заменой двойных звездочек на HTML-теги <b></b>
    """
    # Ищем текст между двойными звездочками и заменяем на <b>текст</b>
    pattern = r'\*\*(.*?)\*\*'
    return re.sub(pattern, r'<b>\1</b>', text)

async def split_title_and_summary(text: str, i18n: TranslatorRunner) -> tuple[str, str]:
    """
    Извлекает заголовок из текста

    Args:
        text: Исходный текст

    Returns:
        Заголовок
        :param text:
        :param i18n:
    """
    text = text.strip('#').strip()
    first_line = text.splitlines()[0]

    if first_line.__contains__('НАЗВАНИЕ:') or first_line.__contains__('TITLE:'):
        return first_line.replace('НАЗВАНИЕ:', '').replace('TITLE:', '').strip(), text[len(first_line):].strip()
    return i18n.transcript_blank(), text

@log_decorator
def split_long_message(text: str, max_length: int = 4000) -> list[str]:
    """
    Разбивает длинный текст на части, подходящие для отправки в Telegram.
    
    Args:
        text: Исходный текст для разбивки
        max_length: Максимальная длина одного сообщения (по умолчанию 4000 для Telegram)
        
    Returns:
        Список строк, каждая из которых не превышает max_length символов
    """
    if len(text) <= max_length:
        return [text]
    
    messages = []
    lines = text.split('\n')
    current_message = ""
    
    for line in lines:
        # Если даже одна строка превышает лимит, придется ее обрезать
        if len(line) > max_length:
            # Сначала добавляем накопленное сообщение, если оно есть
            if current_message:
                messages.append(current_message.strip())
                current_message = ""
            
            # Разбиваем длинную строку на части
            while len(line) > max_length:
                messages.append(line[:max_length])
                line = line[max_length:]
            
            # Остаток строки становится началом нового сообщения
            if line:
                current_message = line + '\n'
        else:
            # Проверяем, поместится ли строка в текущее сообщение
            test_message = current_message + line + '\n'
            if len(test_message) <= max_length:
                current_message = test_message
            else:
                # Сохраняем текущее сообщение и начинаем новое
                if current_message:
                    messages.append(current_message.strip())
                current_message = line + '\n'
    
    # Добавляем последнее сообщение, если оно есть
    if current_message:
        messages.append(current_message.strip())
    
    return messages
