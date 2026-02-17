"""
Lightweight alert sender using Telethon for health monitor.

This module is independent from the main bot and can send alerts
even if the main bot is down.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from telethon import TelegramClient

logger = logging.getLogger(__name__)


class TelethonAlertSender:
    """
    Отправляет алерты через существующий Telethon клиент.

    Это обеспечивает надежность - если основной бот упал, алерты все равно придут.
    """

    def __init__(
        self,
        health_chat_id: str,
        client: Optional[TelegramClient] = None
    ):
        self.health_chat_id = health_chat_id
        self.client = client  # Будет установлен извне

    def set_client(self, client: TelegramClient):
        """Устанавливает Telethon клиент (вызывается извне)"""
        self.client = client
        logger.info(f"Alert sender will use shared Telethon client")

    async def send_alert(
        self,
        text: str,
        level: str = "INFO",
        topic: Optional[str] = None
    ) -> bool:
        """
        Отправляет алерт в health chat через постоянное Telethon соединение.

        Args:
            text: Текст сообщения
            level: Уровень алерта (INFO, WARNING, ERROR, CRITICAL)
            topic: Тема/категория алерта

        Returns:
            bool: True если успешно отправлено, False если ошибка
        """
        if not self.client or not self.client.is_connected():
            logger.error("Alert sender not connected")
            return False

        logger.info(f"Attempting to send {level} alert to chat {self.health_chat_id}")
        try:
            formatted_message = self._format_message(text, level, topic)

            await self.client.send_message(
                int(self.health_chat_id),
                formatted_message
            )

            logger.info(f"✓ Alert sent successfully to chat {self.health_chat_id}: {level} - {text[:50]}...")
            return True

        except ValueError as e:
            # Проблема с chat_id
            logger.error(f"Invalid health_chat_id: {self.health_chat_id}. Error: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send alert via Telethon: {e}", exc_info=True)
            return False

    def _format_message(self, text: str, level: str, topic: Optional[str]) -> str:
        """
        Форматирует сообщение с эмодзи и временной меткой.

        Args:
            text: Основной текст
            level: Уровень (INFO, WARNING, ERROR, CRITICAL)
            topic: Тема

        Returns:
            str: Отформатированное сообщение
        """
        # Эмодзи для разных уровней
        level_emojis = {
            'DEBUG': '🔍',
            'INFO': 'ℹ️',
            'WARNING': '⚠️',
            'ERROR': '❌',
            'CRITICAL': '🚨'
        }

        emoji = level_emojis.get(level.upper(), 'ℹ️')
        timestamp = datetime.utcnow().strftime("%H:%M:%S UTC")

        # Формируем header
        header = f"{emoji} {level.upper()}"
        if topic:
            header += f" [{topic}]"

        return f"{header}\n🕐 {timestamp}\n\n{text}"

    async def send_startup_alert(self, config_info: dict) -> bool:
        """Отправляет алерт о старте монитора"""
        text = (
            f"🟢 Health Monitor started\n\n"
            f"Check interval: {config_info.get('interval', 'N/A')} minutes\n"
            f"Check command: {config_info.get('command', 'N/A')}\n"
            f"Warning threshold: {config_info.get('warning', 'N/A')}s\n"
            f"Critical threshold: {config_info.get('critical', 'N/A')}s"
        )
        return await self.send_alert(text, level="INFO", topic="HEALTH_MONITOR")

    async def send_shutdown_alert(self) -> bool:
        """Отправляет алерт о выключении монитора"""
        text = "🔴 Health Monitor stopped"
        return await self.send_alert(text, level="INFO", topic="HEALTH_MONITOR")

    async def send_slow_response_alert(
        self,
        response_time_ms: int,
        warning_threshold: int,
        critical_threshold: int,
        level: str
    ) -> bool:
        """Отправляет алерт о медленном ответе"""
        response_time_sec = response_time_ms / 1000

        text = (
            f"Бот отвечает медленно!\n"
            f"Время ответа: {response_time_sec:.1f}s\n"
            f"Порог предупреждения: {warning_threshold}s\n"
            f"Критический порог: {critical_threshold}s"
        )

        return await self.send_alert(text, level=level, topic="HEALTH_MONITOR")

    async def send_failure_alert(
        self,
        error_message: str,
        is_first: bool = False
    ) -> bool:
        """Отправляет алерт о неудаче проверки"""
        if is_first:
            text = (
                f"Бот не ответил на проверку!\n"
                f"Ошибка: {error_message}\n"
                f"Это первая неудача, продолжаю мониторинг..."
            )
            level = "WARNING"
        else:
            text = f"Бот не ответил на проверку!\nОшибка: {error_message}"
            level = "ERROR"

        return await self.send_alert(text, level=level, topic="HEALTH_MONITOR")

    async def send_critical_alert(
        self,
        failures_count: int,
        error_message: str
    ) -> bool:
        """Отправляет критический алерт"""
        text = (
            f"🚨 КРИТИЧЕСКАЯ СИТУАЦИЯ!\n\n"
            f"Бот не отвечает {failures_count} проверок подряд!\n"
            f"Последняя ошибка: {error_message}\n\n"
            f"Требуется немедленная проверка!"
        )

        return await self.send_alert(text, level="CRITICAL", topic="HEALTH_MONITOR")

    async def send_recovery_alert(
        self,
        response_time_ms: int,
        previous_failures: int
    ) -> bool:
        """Отправляет алерт о восстановлении бота"""
        response_time_sec = response_time_ms / 1000

        text = (
            f"✅ Бот восстановился!\n"
            f"Текущее время ответа: {response_time_sec:.1f}s\n"
            f"Было неудачных проверок: {previous_failures}"
        )

        return await self.send_alert(text, level="INFO", topic="HEALTH_MONITOR")
