"""
Модуль для A/B тестирования моделей транскрипции
"""
import os
import csv
import json
import time
import logging
import aiofiles
from datetime import datetime
from typing import Optional, Tuple

from services.private_module_stt import private_stt_client

logger = logging.getLogger(__name__)


async def test_model_comparison(
    audio_bytes: Optional[bytes],
    file_path: Optional[str],
    session_id: str,
    main_result: Tuple[str, str],  # (timecoded, plain)
    main_provider: str,
    main_processing_duration: float,
    audio_duration: float
):
    """
    Background task для тестирования private_stt_client и сравнения результатов.

    Запускается в фоне после завершения основной транскрипции.
    НЕ влияет на пользовательский опыт.

    ВСЕГДА записывает результат в CSV, даже при ошибках.

    Args:
        audio_bytes: Аудио в памяти (освобождается после использования)
        file_path: Путь к аудио файлу (не рекомендуется - может быть удален)
        session_id: ID сессии обработки
        main_result: (timecoded_text, plain_text) из основной транскрипции
        main_provider: Название провайдера основной транскрипции
        main_processing_duration: Длительность основной транскрипции в секундах
        audio_duration: Длительность аудио в секундах
    """
    start_time = time.time()
    test_success = False
    test_timecoded = ""
    test_plain = ""
    error_type = None
    error_message = None

    try:
        logger.info(f"Starting test model comparison for session {session_id}")
        
        # Логируем размер буфера для мониторинга памяти
        if audio_bytes:
            buffer_size_mb = len(audio_bytes) / 1024 / 1024
            logger.debug(f"Test comparison buffer size: {buffer_size_mb:.2f} MB")

        # Запускаем тестовую обработку через private_stt_client
        result = await private_stt_client.process_audio(
            file_buffer=audio_bytes,
            file_path=file_path,
            session_id=session_id,
            suppress_progress=True,  # Без обновлений прогресса
            group_transcription=True
        )

        if result:
            test_timecoded, test_plain = result
            test_success = True
            logger.info(f"Test model comparison completed successfully for session {session_id}")
        else:
            error_type = "empty_result"
            error_message = "Test model returned None"
            logger.error(f"Test model returned None for session {session_id}")

    except TimeoutError as e:
        error_type = "timeout"
        error_message = f"Timeout error: {str(e)}"
        logger.error(f"Test model timed out for session {session_id}: {e}")

    except Exception as e:
        # Определяем тип ошибки
        error_class = e.__class__.__name__
        if "connection" in str(e).lower() or "timeout" in str(e).lower():
            error_type = "connection_error"
        elif "api" in str(e).lower() or "http" in str(e).lower():
            error_type = "api_error"
        else:
            error_type = "unknown_error"

        error_message = f"{error_class}: {str(e)[:200]}"  # Ограничиваем длину сообщения
        logger.error(f"Test model comparison failed for session {session_id}: {e}", exc_info=True)

    finally:
        # ВСЕГДА записываем результат, даже при ошибках
        test_processing_duration = time.time() - start_time

        timestamp = datetime.now()
        save_params = {
            'timestamp': timestamp,
            'session_id': session_id,
            'main_provider': main_provider,
            'main_transcript_plain': main_result[1],
            'main_transcript_timecoded': main_result[0],
            'main_processing_duration': main_processing_duration,
            'test_transcript_plain': test_plain,
            'test_transcript_timecoded': test_timecoded,
            'test_processing_duration': test_processing_duration,
            'test_success': test_success,
            'test_error_type': error_type,
            'test_error_message': error_message,
            'audio_duration': audio_duration
        }
        
        # Сохраняем в CSV
        try:
            await save_comparison_to_csv(**save_params)
            logger.info(f"Comparison results saved to CSV for session {session_id}. Success: {test_success}, Duration: {test_processing_duration:.2f}s")
        except Exception as csv_error:
            logger.error(f"Failed to save comparison to CSV for session {session_id}: {csv_error}", exc_info=True)
        
        # Сохраняем в JSON
        try:
            await save_comparison_to_json(**save_params)
            logger.info(f"Comparison results saved to JSON for session {session_id}.")
        except Exception as json_error:
            logger.error(f"Failed to save comparison to JSON for session {session_id}: {json_error}", exc_info=True)
        
        # Явно освобождаем память от аудио буфера
        if audio_bytes:
            buffer_size_mb = len(audio_bytes) / 1024 / 1024
            audio_bytes = None  # Освобождаем ссылку для GC
            logger.debug(f"Released {buffer_size_mb:.2f} MB audio buffer for session {session_id}")


async def save_comparison_to_csv(
    timestamp: datetime,
    session_id: str,
    main_provider: str,
    main_transcript_plain: str,
    main_transcript_timecoded: str,
    main_processing_duration: float,
    test_transcript_plain: str,
    test_transcript_timecoded: str,
    test_processing_duration: float,
    audio_duration: float,
    test_success: bool = True,
    test_error_type: Optional[str] = None,
    test_error_message: Optional[str] = None
):
    """
    Сохраняет результаты сравнения в CSV файл.

    Файл создаётся по дате: model_comparison_2025-10-27.csv
    Каждый день - новый файл.

    ВСЕГДА записывает результат, даже при ошибках тестовой модели.

    Args:
        timestamp: Время запуска обработки
        session_id: ID сессии
        main_provider: Провайдер основной транскрипции
        main_transcript_plain: Чистый текст основной транскрипции
        main_transcript_timecoded: Транскрипция с таймкодами (основная)
        main_processing_duration: Длительность основной обработки (сек)
        test_transcript_plain: Чистый текст тестовой транскрипции (пустой при ошибке)
        test_transcript_timecoded: Транскрипция с таймкодами (тест, пустой при ошибке)
        test_processing_duration: Длительность тестовой обработки (сек)
        test_success: True если тест успешен, False при ошибке
        test_error_type: Тип ошибки (timeout, api_error, connection_error, etc.)
        test_error_message: Сообщение об ошибке
        audio_duration: Длительность аудио в секундах
    """
    try:
        # Формируем имя файла по дате
        date_str = timestamp.strftime("%Y-%m-%d")
        csv_filename = f"model_comparison_{date_str}.csv"

        # Путь к файлу (сохраняем в корне проекта)
        csv_path = os.path.join(os.getcwd(), csv_filename)

        # Проверяем, существует ли файл
        file_exists = os.path.exists(csv_path)

        # Формируем данные для записи
        row_data = {
            'timestamp': timestamp.isoformat(),
            'session_id': session_id,
            'main_provider': main_provider,
            'main_processing_duration_sec': f"{main_processing_duration:.2f}",
            'audio_duration_sec': f"{audio_duration:.2f}",
            'test_success': str(test_success),
            'test_processing_duration_sec': f"{test_processing_duration:.2f}",
            'test_error_type': test_error_type or '',
            'test_error_message': test_error_message or '',
            'main_transcript_plain': main_transcript_plain,
            'main_transcript_timecoded': main_transcript_timecoded,
            'test_transcript_plain': test_transcript_plain,
            'test_transcript_timecoded': test_transcript_timecoded,
        }

        # Используем синхронный CSV writer через aiofiles
        # CSV автоматически экранирует спецсимволы, переносы строк и кавычки
        fieldnames = [
            'timestamp',
            'session_id',
            'main_provider',
            'main_processing_duration_sec',
            'audio_duration_sec',
            'test_success',
            'test_processing_duration_sec',
            'test_error_type',
            'test_error_message',
            'main_transcript_plain',
            'main_transcript_timecoded',
            'test_transcript_plain',
            'test_transcript_timecoded'
        ]

        # Открываем файл для добавления
        async with aiofiles.open(csv_path, mode='a', newline='', encoding='utf-8') as f:
            # Если файл новый, записываем заголовки
            if not file_exists:
                header_line = ','.join(fieldnames) + '\n'
                await f.write(header_line)
                logger.info(f"Created new CSV file: {csv_path}")

            # Форматируем строку CSV с правильным экранированием
            # Используем csv.writer для правильной обработки спецсимволов
            import io
            output = io.StringIO()
            writer = csv.DictWriter(
                output,
                fieldnames=fieldnames,
                quoting=csv.QUOTE_ALL,  # Экранируем все поля
                lineterminator=''  # Убираем лишний перенос строки
            )
            writer.writerow(row_data)
            csv_line = output.getvalue() + '\n'

            # Записываем в файл
            await f.write(csv_line)

        logger.debug(f"Saved comparison results to {csv_path}")

    except Exception as e:
        logger.error(f"Failed to save comparison to CSV: {e}", exc_info=True)


async def save_comparison_to_json(
    timestamp: datetime,
    session_id: str,
    main_provider: str,
    main_transcript_plain: str,
    main_transcript_timecoded: str,
    main_processing_duration: float,
    test_transcript_plain: str,
    test_transcript_timecoded: str,
    test_processing_duration: float,
    audio_duration: float,
    test_success: bool = True,
    test_error_type: Optional[str] = None,
    test_error_message: Optional[str] = None
):
    """
    Сохраняет результаты сравнения в JSON файл.

    Файл создаётся по дате: model_comparison_2025-10-27.json
    Каждый день - новый файл.

    ВСЕГДА записывает результат, даже при ошибках тестовой модели.

    Args:
        timestamp: Время запуска обработки
        session_id: ID сессии
        main_provider: Провайдер основной транскрипции
        main_transcript_plain: Чистый текст основной транскрипции
        main_transcript_timecoded: Транскрипция с таймкодами (основная)
        main_processing_duration: Длительность основной обработки (сек)
        test_transcript_plain: Чистый текст тестовой транскрипции (пустой при ошибке)
        test_transcript_timecoded: Транскрипция с таймкодами (тест, пустой при ошибке)
        test_processing_duration: Длительность тестовой обработки (сек)
        test_success: True если тест успешен, False при ошибке
        test_error_type: Тип ошибки (timeout, api_error, connection_error, etc.)
        test_error_message: Сообщение об ошибке
        audio_duration: Длительность аудио в секундах
    """
    try:
        # Формируем имя файла по дате
        date_str = timestamp.strftime("%Y-%m-%d")
        json_filename = f"model_comparison_{date_str}.json"

        # Путь к файлу (сохраняем в корне проекта)
        json_path = os.path.join(os.getcwd(), json_filename)

        # Формируем данные для записи (те же поля что и в CSV)
        row_data = {
            'timestamp': timestamp.isoformat(),
            'session_id': session_id,
            'main_provider': main_provider,
            'main_processing_duration_sec': round(main_processing_duration, 2),
            'audio_duration_sec': round(audio_duration, 2),
            'test_success': test_success,
            'test_processing_duration_sec': round(test_processing_duration, 2),
            'test_error_type': test_error_type or '',
            'test_error_message': test_error_message or '',
            'main_transcript_plain': main_transcript_plain,
            'main_transcript_timecoded': main_transcript_timecoded,
            'test_transcript_plain': test_transcript_plain,
            'test_transcript_timecoded': test_transcript_timecoded,
        }

        # Читаем существующий файл или создаём новый массив
        existing_data = []
        if os.path.exists(json_path):
            try:
                async with aiofiles.open(json_path, mode='r', encoding='utf-8') as f:
                    content = await f.read()
                    if content.strip():
                        existing_data = json.loads(content)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Could not read existing JSON file {json_path}, creating new: {e}")
                existing_data = []

        # Добавляем новую запись
        existing_data.append(row_data)

        # Записываем обновлённые данные
        async with aiofiles.open(json_path, mode='w', encoding='utf-8') as f:
            await f.write(json.dumps(existing_data, ensure_ascii=False, indent=2))

        logger.debug(f"Saved comparison results to {json_path}")

    except Exception as e:
        logger.error(f"Failed to save comparison to JSON: {e}", exc_info=True)
