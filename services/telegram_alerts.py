import asyncio
import logging
import os
from asyncio import Queue
from datetime import datetime
from typing import Optional

from aiogram.types import BufferedInputFile, FSInputFile

try:
    from maxapi.types.input_media import InputMedia as MaxInputMedia, InputMediaBuffer as MaxInputMediaBuffer
    _HAS_MAXAPI = True
except ImportError:
    _HAS_MAXAPI = False

from config_data.config import load_config

logger = logging.getLogger(__name__)
config = load_config('.env')


class TelegramLogger:
    """Простой логгер для отправки сообщений в Telegram чат через очередь"""
    
    def __init__(self, bot_instance, log_chat_id: str, max_queue_size: int = 1000):
        self.bot = bot_instance
        self.log_chat_id = log_chat_id
        self.queue: Queue = Queue(maxsize=max_queue_size)
        self.worker_task: Optional[asyncio.Task] = None
        self.is_running = False
        
    async def start(self):
        """Запуск фонового воркера"""
        if not self.is_running:
            self.is_running = True
            self.worker_task = asyncio.create_task(self._worker())
            logger.info("TelegramLogger started")
            
    async def stop(self):
        """Остановка воркера с обработкой оставшихся сообщений"""
        logger.info("Stopping TelegramLogger...")
        self.is_running = False
        
        if self.worker_task:
            try:
                # Ждем завершения обработки очереди
                await asyncio.wait_for(self.worker_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("TelegramLogger stop timeout")
                
        logger.info("TelegramLogger stopped")
            
    async def send_alert(self, text: str, file_path: str | None = None, file_buffer: str | None = None, level: str = "INFO", topic: str = None, chat_type: str = 'main_chat', file_name: str | None = 'output.mp3', fingerprint: str = None):
        """Неблокирующая отправка сообщения в очередь"""
        try:
            message = {
                'text': text,
                'level': level,
                'topic': topic,
                'timestamp': datetime.utcnow(),
                'fingerprint': fingerprint,
                'file_path': file_path,
                'file_buffer': file_buffer,
                'chat_type': chat_type,
                'file_name': file_name
            }
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            # Если очередь переполнена - логируем локально, но не блокируем
            logger.warning(f"Telegram log queue is full, dropping message: {text[:100]}")
            
    async def _worker(self):
        """Фоновый воркер для обработки очереди"""
        while self.is_running or not self.queue.empty():
            try:
                # Ждем сообщение с таймаутом
                message = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                await self._send_message(message)
                self.queue.task_done()
            except asyncio.TimeoutError:
                continue  # Проверяем is_running
            except Exception as e:
                logger.error(f"Error in telegram logger worker: {e}")
                
    async def _send_message(self, message: dict):
        """Отправка сообщения (supports both aiogram and maxapi bots)."""
        try:
            file_path = message.get('file_path', None)
            file_buffer = message.get('file_buffer', None)
            file_name = message.get('file_name', 'default_file_name')

            if message.get('chat_type', 'main_chat') == 'main_chat':
                log_chat_id = self.log_chat_id
            elif message.get('chat_type', 'main_chat') == 'dev_chat':
                log_chat_id = config.tg_bot.dev_log_chat_id
            elif message.get('chat_type', 'main_chat') == 'health_chat':
                log_chat_id = config.health_monitor.health_chat_id
            else:
                log_chat_id = self.log_chat_id

            formatted_text = self._format_message(message)

            # Detect bot type: aiogram has send_document, maxapi does not
            is_aiogram = hasattr(self.bot, 'send_document')

            if is_aiogram:
                await self._send_message_aiogram(
                    log_chat_id, formatted_text, file_path, file_buffer, file_name
                )
            else:
                await self._send_message_maxapi(
                    log_chat_id, formatted_text, file_path, file_buffer, file_name
                )
        except Exception as e:
            logger.error(f"Failed to send telegram alert: {e}")

    async def _send_message_aiogram(self, log_chat_id, formatted_text, file_path, file_buffer, file_name):
        """Send via aiogram Bot (Telegram)."""
        file_to_send = None
        if file_path is not None:
            if not os.path.exists(file_path):
                logger.warning(f"File not found for alert: {file_path}")
            else:
                file_to_send = FSInputFile(path=file_path, filename=file_name or file_path.split('/')[-1])
        elif file_buffer is not None:
            file_to_send = BufferedInputFile(file=file_buffer, filename=file_name)

        if file_to_send:
            try:
                await self.bot.send_document(
                    chat_id=log_chat_id,
                    document=file_to_send,
                    caption=formatted_text
                )
            except Exception as e:
                logger.error(f"Failed to send document alert: {e}. Retrying with text only.")
                if file_path:
                    try:
                        size = os.path.getsize(file_path)
                        logger.error(f"Failed file info: Path={file_path}, Size={size/1024/1024:.2f} MB")
                    except Exception:
                        pass
                await self.bot.send_message(
                    chat_id=log_chat_id,
                    text=f"{formatted_text}\n\n⚠️ Failed to send attachment: {str(e)[:200]}...",
                    parse_mode='HTML'
                )
        else:
            await self.bot.send_message(
                chat_id=log_chat_id,
                text=formatted_text,
                parse_mode='HTML'
            )

    async def _send_message_maxapi(self, log_chat_id, formatted_text, file_path, file_buffer, file_name):
        """Send via maxapi Bot (Max messenger)."""
        attachments = []
        if file_path is not None and os.path.exists(file_path):
            attachments.append(MaxInputMedia(path=file_path))
        elif file_buffer is not None:
            attachments.append(MaxInputMediaBuffer(buffer=file_buffer, filename=file_name))

        if attachments:
            try:
                await self.bot.send_message(
                    chat_id=log_chat_id,
                    text=formatted_text,
                    attachments=attachments,
                )
            except Exception as e:
                logger.error(f"Failed to send document alert via Max: {e}. Retrying text only.")
                await self.bot.send_message(
                    chat_id=log_chat_id,
                    text=f"{formatted_text}\n\n⚠️ Failed to send attachment: {str(e)[:200]}...",
                )
        else:
            await self.bot.send_message(
                chat_id=log_chat_id,
                text=formatted_text,
            )
            
    def _format_message(self, message: dict) -> str:
        """Форматирование сообщения"""
        timestamp = message['timestamp'].strftime("%H:%M:%S")
        level = message['level']
        topic = message['topic']
        text = message['text']
        
        # Эмодзи для уровней
        level_emojis = {
            'DEBUG': '🔍',
            'INFO': 'ℹ️',
            'WARNING': '⚠️',
            'ERROR': '❌',
            'CRITICAL': '🚨'
        }
        
        emoji = level_emojis.get(level.upper(), 'ℹ️')
        header = f"{emoji} {level.upper()}"
        
        if topic:
            header += f" [{topic}]"
            
        return f"{header}\n🕐 {timestamp}\n\n{text}"


# Глобальный экземпляр для удобства использования
_telegram_logger: Optional[TelegramLogger] = None


async def init_telegram_logger(bot_instance, log_chat_id: str):
    """Инициализация глобального логгера"""
    global _telegram_logger
    if _telegram_logger is None:
        _telegram_logger = TelegramLogger(bot_instance, log_chat_id)
        await _telegram_logger.start()
    return _telegram_logger


def get_telegram_logger() -> Optional[TelegramLogger]:
    """Получение глобального логгера"""
    return _telegram_logger


async def send_alert(text: str, file_path: str | None = None, file_buffer: str | None = None, level: str = "INFO", topic: str = None, chat_type: str = 'main_chat', fingerprint: str = None):
    """Глобальная функция для отправки алертов"""
    logger_instance = get_telegram_logger()
    if logger_instance:
        await logger_instance.send_alert(text=text, file_path=file_path, file_buffer=file_buffer, level=level, topic=topic, chat_type=chat_type, fingerprint=fingerprint)
    else:
        # Если логгер не инициализирован, просто логируем локально
        logger.warning(f"TelegramLogger not initialized. Message: {text}")
