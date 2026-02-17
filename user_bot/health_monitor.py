"""
Health Monitor Service for monitoring bot availability and response time.

This service:
- Sends test commands to the bot using Telethon user-bot
- Measures response time
- Logs results to database
- Sends alerts independently via Telethon (no dependency on main bot)
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List

from telethon import TelegramClient, events
from telethon.tl.types import Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import update

from config_data.config import Config
from models.model import BotHealthCheck
from user_bot.telethon_alert_sender import TelethonAlertSender
from user_bot.metrics_collector import MetricsCollector

import aiohttp

logger = logging.getLogger(__name__)

# Direct Telegram API URL
TELEGRAM_API_URL = "https://api.telegram.org"

# Префикс для ping сообщений через Direct API
DIRECT_PING_PREFIX = "__DIRECT_PING_"

__version__ = '1.7.0'  # Added Direct API ping test and Telegram API latency metrics


@dataclass
class ResponseTimeSample:
    """Один замер времени ответа"""
    timestamp: float  # time.time()
    response_time_ms: int


class HealthMonitorService:
    """Service for monitoring bot health with persistent Telethon connection"""

    def __init__(
        self,
        config: Config,
        async_session: sessionmaker,
        bot_username: str,
        alert_sender: TelethonAlertSender,
        client: Optional[TelegramClient] = None
    ):
        self.config = config
        self.async_session = async_session
        self.bot_username = bot_username
        self.alert_sender = alert_sender

        # Счетчик последовательных неудач
        self.consecutive_failures = 0

        # Shared Telethon client (будет установлен через set_client())
        self.client: Optional[TelegramClient] = client

        # Для отслеживания pending проверок (включая запоздалые ответы)
        # {sent_message_id: {'db_check_id': int, 'send_time': float, 'started_at': datetime,
        #                     'future': Future, 'timed_out': bool}}
        self._pending_checks: dict = {}

        # Сборщик метрик сервера
        self.metrics_collector: Optional[MetricsCollector] = None
        if config.health_monitor.collect_metrics:
            self.metrics_collector = MetricsCollector(config, async_session)
            logger.info("✓ Metrics collector initialized")

        # Rolling average для response time (последние 30 секунд)
        self._response_time_history: deque = deque(maxlen=100)  # Максимум 100 замеров
        self._rolling_window_seconds = 30  # Окно для расчёта avg/max

        # Для Direct API ping теста
        # {ping_id: {'send_time': float, 'api_time': float, 'future': Future}}
        self._pending_direct_pings: dict = {}

    def _record_response_time(self, response_time_ms: int):
        """
        Записывает время ответа в историю для rolling average.

        Args:
            response_time_ms: Время ответа в миллисекундах
        """
        sample = ResponseTimeSample(
            timestamp=time.time(),
            response_time_ms=response_time_ms
        )
        self._response_time_history.append(sample)
        logger.debug(f"Recorded response time sample: {response_time_ms}ms (history size: {len(self._response_time_history)})")

    def _get_rolling_latency_stats(self) -> dict:
        """
        Вычисляет статистику времени ответа за последние N секунд.

        Returns:
            dict с ключами:
                - response_time_avg_30s: средняя задержка за 30 сек
                - response_time_max_30s: максимальная задержка за 30 сек
                - response_time_min_30s: минимальная задержка за 30 сек
                - response_time_samples_30s: количество замеров в окне
        """
        if not self._response_time_history:
            return {}

        now = time.time()
        cutoff = now - self._rolling_window_seconds

        # Фильтруем замеры за последние N секунд
        recent_samples = [
            s.response_time_ms
            for s in self._response_time_history
            if s.timestamp >= cutoff
        ]

        if not recent_samples:
            return {}

        return {
            'response_time_avg_30s': round(sum(recent_samples) / len(recent_samples), 1),
            'response_time_max_30s': max(recent_samples),
            'response_time_min_30s': min(recent_samples),
            'response_time_samples_30s': len(recent_samples)
        }

    async def perform_direct_api_ping_test(self) -> dict:
        """
        Тестирует отправку сообщения через Direct Telegram API (обходя Local Server).

        Отправляет ping сообщение напрямую через api.telegram.org и ждёт
        его получения через Telethon.

        Returns:
            dict с ключами:
                - direct_api_send_ms: время ответа API на отправку
                - direct_api_delivery_ms: полное время до получения Telethon
                - direct_api_error: ошибка (если есть)
        """
        result = {
            'direct_api_send_ms': None,
            'direct_api_delivery_ms': None,
            'direct_api_error': None
        }

        if not self.client or not self.client.is_connected():
            result['direct_api_error'] = 'Telethon client not connected'
            return result

        # Генерируем уникальный ID для ping
        ping_id = str(int(time.time() * 1000))
        ping_text = f"{DIRECT_PING_PREFIX}{ping_id}__"

        # Получаем chat_id из конфига (куда бот будет отправлять)
        # Используем health_chat_id - туда бот отправит, и Telethon получит
        health_chat_id = self.config.health_monitor.health_chat_id
        bot_token = self.config.tg_bot.token

        # Создаём Future для ожидания получения
        delivery_future = asyncio.Future()
        send_time = time.time()

        self._pending_direct_pings[ping_id] = {
            'send_time': send_time,
            'api_time': None,
            'future': delivery_future
        }

        try:
            # Отправляем через Direct API
            timeout = aiohttp.ClientTimeout(total=30.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                api_start = time.time()
                async with session.post(
                    f"{TELEGRAM_API_URL}/bot{bot_token}/sendMessage",
                    json={
                        'chat_id': health_chat_id,
                        'text': ping_text
                    }
                ) as resp:
                    api_end = time.time()
                    api_latency_ms = round((api_end - api_start) * 1000, 1)
                    result['direct_api_send_ms'] = api_latency_ms

                    if resp.status != 200:
                        resp_text = await resp.text()
                        result['direct_api_error'] = f'API error: {resp.status} - {resp_text[:100]}'
                        del self._pending_direct_pings[ping_id]
                        return result

                    self._pending_direct_pings[ping_id]['api_time'] = api_end

            # Ждём получения сообщения через Telethon (макс 30 сек)
            try:
                await asyncio.wait_for(delivery_future, timeout=30.0)
                delivery_end = time.time()
                delivery_ms = round((delivery_end - send_time) * 1000, 1)
                result['direct_api_delivery_ms'] = delivery_ms

                logger.info(
                    f"Direct API ping test: send={api_latency_ms}ms, delivery={delivery_ms}ms"
                )

            except asyncio.TimeoutError:
                result['direct_api_error'] = 'Delivery timeout (30s)'
                logger.warning(f"Direct API ping test: delivery timeout for ping_id={ping_id}")

        except Exception as e:
            result['direct_api_error'] = str(e)[:100]
            logger.warning(f"Direct API ping test error: {e}")

        finally:
            # Очищаем pending
            if ping_id in self._pending_direct_pings:
                del self._pending_direct_pings[ping_id]

        return result

    def _handle_direct_ping_message(self, text: str) -> bool:
        """
        Проверяет, является ли сообщение Direct API ping и обрабатывает его.

        Args:
            text: Текст сообщения

        Returns:
            True если это ping сообщение и оно обработано
        """
        if not text or not text.startswith(DIRECT_PING_PREFIX):
            return False

        # Извлекаем ping_id
        try:
            # Формат: __DIRECT_PING_{ping_id}__
            ping_id = text[len(DIRECT_PING_PREFIX):-2]  # Убираем префикс и __

            if ping_id in self._pending_direct_pings:
                ping_data = self._pending_direct_pings[ping_id]
                if 'future' in ping_data and not ping_data['future'].done():
                    ping_data['future'].set_result(True)
                    logger.debug(f"Direct API ping received: ping_id={ping_id}")
                    return True

        except Exception as e:
            logger.debug(f"Error parsing direct ping message: {e}")

        return False

    def set_client(self, client: TelegramClient):
        """Устанавливает shared Telethon клиент"""
        self.client = client

        # Регистрируем event handler для сообщений от бота
        @self.client.on(events.NewMessage(from_users=self.bot_username))
        async def message_handler(event):
            await self._handle_bot_message(event)

        # Регистрируем handler для Direct API ping сообщений в health chat
        health_chat_id = int(self.config.health_monitor.health_chat_id)

        @self.client.on(events.NewMessage(
            chats=health_chat_id,
            from_users=self.bot_username
        ))
        async def direct_ping_handler(event):
            """Обработчик для Direct API ping сообщений"""
            text = event.message.text or ""
            if self._handle_direct_ping_message(text):
                return  # Обработано как ping
            # Иначе игнорируем - это какое-то другое сообщение от бота

        # Регистрируем event handler для ручной команды проверки (если включено)
        if self.config.health_monitor.manual_check_enabled:
            command = self.config.health_monitor.manual_check_command

            @self.client.on(events.NewMessage(
                chats=health_chat_id,
                pattern=f'^{command}$'
            ))
            async def manual_check_handler(event):
                await self._handle_manual_check_command(event)

            logger.info(
                f"✓ Manual check enabled: '{command}' command registered for chat {health_chat_id}"
            )

        logger.info(f"✓ Health monitor will use shared client. Monitoring {self.bot_username}")

    async def _handle_manual_check_command(self, event):
        """
        Обработчик ручной команды проверки.
        Проверяет права доступа и запускает внеплановую проверку.
        """
        sender = await event.get_sender()
        sender_id = sender.id

        # Проверяем права доступа
        allowed_users = self.config.health_monitor.manual_check_allowed_users
        if allowed_users is not None and sender_id not in allowed_users:
            logger.warning(f"Manual check denied for user {sender_id} (not in allowed list)")
            await event.reply(
                "❌ У вас нет прав для выполнения этой команды.\n"
                f"User ID: {sender_id}"
            )
            return

        logger.info(f"Manual check requested by user {sender_id} ({sender.first_name})")

        # Отправляем подтверждение
        await event.reply("🔄 Запускаю проверку бота...")

        # Выполняем проверку
        try:
            result = await self.perform_health_check()

            # Формируем отчет
            if result['success']:
                status_emoji = "✅"
                status_text = "Успешно"
            else:
                status_emoji = "❌"
                status_text = "Ошибка"

            response_time = result.get('response_time_ms', 0)
            error_msg = result.get('error_message', '')

            report = (
                f"{status_emoji} **Результат проверки**\n\n"
                f"**Статус:** {status_text}\n"
                f"**Время ответа:** {response_time}ms ({response_time / 1000:.2f}s)\n"
                f"**Бот ответил:** {'Да' if result.get('bot_responded') else 'Нет'}\n"
            )

            if error_msg:
                report += f"\n**Ошибка:** {error_msg}\n"

            # Добавляем пороги
            warning_threshold = self.config.health_monitor.response_warning_seconds * 1000
            critical_threshold = self.config.health_monitor.response_critical_seconds * 1000

            if result['success']:
                if response_time >= critical_threshold:
                    report += "\n⚠️ **КРИТИЧНО**: Время ответа превышает критический порог!"
                elif response_time >= warning_threshold:
                    report += "\n⚠️ **ВНИМАНИЕ**: Время ответа превышает порог предупреждения"
                else:
                    report += "\n✅ Время ответа в пределах нормы"

            # Добавляем ключевые метрики сервера
            metrics = result.get('server_metrics')
            if metrics:
                report += "\n\n📊 **Метрики сервера:**\n"
                report += f"• CPU: {metrics.get('cpu_percent', 'N/A')}%\n"
                report += f"• RAM: {metrics.get('memory_percent', 'N/A')}%\n"
                report += f"• Load: {metrics.get('load_avg_1m', 'N/A')}\n"

                # Swap - критичная метрика!
                if 'swap_percent' in metrics and metrics['swap_percent'] > 0:
                    report += f"• ⚠️ Swap: {metrics['swap_percent']}% ({metrics.get('swap_used_mb', 0):.0f}MB)\n"

                # Disk I/O
                if 'iowait_percent' in metrics:
                    iowait = metrics['iowait_percent']
                    iowait_marker = "⚠️ " if iowait > 10 else ""
                    report += f"• {iowait_marker}IOWait: {iowait}%\n"

                if 'bot_fd_count' in metrics:
                    fd_info = f"• Bot FD: {metrics['bot_fd_count']}"
                    if 'bot_fd_used_percent' in metrics:
                        fd_info += f" ({metrics['bot_fd_used_percent']}% of limit)"
                    report += fd_info + "\n"

                if 'close_wait_count' in metrics:
                    cw = metrics['close_wait_count']
                    cw_marker = "⚠️ " if cw > 50 else ""
                    report += f"• {cw_marker}CLOSE_WAIT: {cw}\n"

                if 'pg_connections_total' in metrics:
                    report += f"• PG conn: {metrics['pg_connections_total']} (active: {metrics.get('pg_connections_active', 'N/A')})\n"
                if 'ffmpeg_processes' in metrics:
                    report += f"• ffmpeg: {metrics['ffmpeg_processes']}\n"
                if 'active_processing_sessions' in metrics:
                    report += f"• Active sessions: {metrics['active_processing_sessions']}\n"

                # Event loop lag - критичная метрика из внутренних метрик бота!
                if 'bot_event_loop_lag_ms' in metrics:
                    lag = metrics['bot_event_loop_lag_ms']
                    lag_max = metrics.get('bot_event_loop_lag_max_ms', lag)
                    lag_marker = "⚠️ " if lag > 50 else ""
                    report += f"• {lag_marker}Event loop lag: {lag:.1f}ms (max: {lag_max:.1f}ms)\n"

                # Asyncio tasks
                if 'bot_asyncio_tasks' in metrics:
                    report += f"• Asyncio tasks: {metrics['bot_asyncio_tasks']} (pending: {metrics.get('bot_asyncio_pending', 0)})\n"

                # Network errors (если есть)
                if metrics.get('net_errin', 0) > 0 or metrics.get('net_errout', 0) > 0:
                    report += f"• ⚠️ Net errors: in={metrics.get('net_errin')}, out={metrics.get('net_errout')}\n"

                # Network throughput (KB/s)
                if 'net_rx_kb_s' in metrics or 'net_tx_kb_s' in metrics:
                    rx = metrics.get('net_rx_kb_s', 0)
                    tx = metrics.get('net_tx_kb_s', 0)
                    report += f"• Net: ↓{rx} KB/s ↑{tx} KB/s\n"

                # Rolling average latency (за последние 30 сек)
                if 'response_time_avg_30s' in metrics:
                    avg = metrics['response_time_avg_30s']
                    max_rt = metrics.get('response_time_max_30s', avg)
                    samples = metrics.get('response_time_samples_30s', 0)
                    report += f"• Rolling avg (30s): {avg}ms (max: {max_rt}ms, samples: {samples})\n"

                # Direct Telegram API (обходит Local Server)
                if 'direct_api_getme_ms' in metrics:
                    direct_getme = metrics['direct_api_getme_ms']
                    connectivity = metrics.get('direct_api_connectivity_ms', 'N/A')
                    report += f"• Direct API: getMe={direct_getme}ms, ping={connectivity}ms\n"

            # Direct API ping тест (send + delivery) - при высокой задержке
            if response_time and response_time > 5000:
                report += "\n🔬 **Direct API Ping Test:**\n"
                ping_result = await self.perform_direct_api_ping_test()
                if ping_result.get('direct_api_error'):
                    report += f"• ❌ Error: {ping_result['direct_api_error']}\n"
                else:
                    send_ms = ping_result.get('direct_api_send_ms', 'N/A')
                    delivery_ms = ping_result.get('direct_api_delivery_ms', 'N/A')
                    report += f"• API send: {send_ms}ms\n"
                    report += f"• Delivery: {delivery_ms}ms\n"

                    # Сравнение с response_time
                    if delivery_ms and response_time:
                        if delivery_ms < response_time / 2:
                            report += "• 💡 Direct API быстрее → возможно проблема в Local Server\n"

            await event.reply(report)

            logger.info(f"Manual check completed for user {sender_id}: success={result['success']}")

        except Exception as e:
            logger.error(f"Failed to perform manual check: {e}", exc_info=True)
            await event.reply(
                f"❌ Ошибка при выполнении проверки:\n{str(e)}"
            )

    async def _handle_bot_message(self, event):
        """
        Обработчик сообщений от бота.
        Проверяет reply_to и обрабатывает как своевременные, так и запоздалые ответы.
        """
        message = event.message

        # КРИТИЧНО: принимаем ТОЛЬКО личные сообщения (не из групп)
        if not message.is_private:
            logger.debug(f"Ignoring non-private message (id={message.id}) from chat {message.chat_id}")
            return

        # Проверяем что это reply на наше сообщение
        if not message.reply_to or not message.reply_to.reply_to_msg_id:
            logger.debug(f"Ignoring message without reply_to (id={message.id})")
            return

        reply_to_id = message.reply_to.reply_to_msg_id

        # Ищем pending check для этого reply_to_id
        if reply_to_id not in self._pending_checks:
            logger.debug(f"Ignoring reply to unknown message (reply_to={reply_to_id})")
            return

        check = self._pending_checks[reply_to_id]

        # Проверка 1: Своевременный ответ (в течение 60 секунд)
        if 'future' in check and not check['future'].done():
            logger.info(f"✓ Received timely reply to message {reply_to_id}")
            check['future'].set_result(message)
            return

        # Проверка 2: Запоздалый ответ (после таймаута)
        if check.get('timed_out', False):
            response_time_ms = int((time.time() - check['send_time']) * 1000)
            logger.warning(
                f"⚠ Received LATE reply to message {reply_to_id} after {response_time_ms}ms "
                f"(timeout was at 60000ms)"
            )
            await self._update_late_response(check, message, response_time_ms)
            # Удаляем из pending после обработки
            del self._pending_checks[reply_to_id]

    async def perform_health_check(self) -> dict:
        """
        Выполняет проверку здоровья бота через постоянное Telethon соединение.
        Использует reply_to для точного сопоставления запроса и ответа.

        Returns:
            dict: результаты проверки с ключами:
                - success: bool
                - response_time_ms: int
                - error_message: str (если есть)
                - bot_responded: bool
        """
        if not self.client or not self.client.is_connected():
            return {
                'success': False,
                'response_time_ms': None,
                'error_message': 'Telethon client is not connected',
                'bot_responded': False,
                'actual_response': None
            }

        started_at = datetime.utcnow()
        result = {
            'success': False,
            'response_time_ms': None,
            'error_message': None,
            'bot_responded': False,
            'actual_response': None
        }

        sent_message_id = None
        server_metrics = None

        # Собираем метрики ДО отправки команды (чтобы зафиксировать состояние системы)
        if self.metrics_collector:
            try:
                # Определяем, нужны ли расширенные метрики
                # Если threshold = 0, всегда собираем расширенные
                threshold = self.config.health_monitor.extended_metrics_threshold_ms
                collect_extended = (threshold == 0)

                server_metrics = await self.metrics_collector.collect_metrics(extended=collect_extended)
                logger.debug(f"Collected server metrics: {len(server_metrics)} fields")
            except Exception as e:
                logger.warning(f"Failed to collect server metrics: {e}")
                server_metrics = {'collection_error': str(e)}

        try:
            # Отправляем команду боту
            logger.info(f"Sending command '{self.config.health_monitor.check_command}' to {self.bot_username}")

            sent_message = await self.client.send_message(
                self.bot_username,
                self.config.health_monitor.check_command
            )
            sent_message_id = sent_message.id

            # Создаем Future для ответа и добавляем в pending checks
            response_future = asyncio.Future()
            check_start_time = time.time()

            self._pending_checks[sent_message_id] = {
                'db_check_id': None,  # Будет установлен после сохранения в БД
                'send_time': check_start_time,
                'started_at': started_at,
                'future': response_future,
                'timed_out': False
            }

            logger.debug(f"Added pending check for message {sent_message_id}")

            # Ждем ответ через event handler (максимум 60 секунд)
            try:
                response = await asyncio.wait_for(response_future, timeout=60.0)

                # Измеряем время ответа
                check_end_time = time.time()
                actual_response_time_ms = int((check_end_time - check_start_time) * 1000)

                # Проверяем текст ответа
                result['bot_responded'] = True
                result['actual_response'] = response.text if response else None
                result['response_time_ms'] = actual_response_time_ms

                # Записываем в историю для rolling average
                self._record_response_time(actual_response_time_ms)

                expected_text = "bop"
                actual_text = (response.text or "").strip().lower()

                if actual_text == expected_text:
                    result['success'] = True
                    logger.info(
                        f"✓ Bot responded correctly in {actual_response_time_ms}ms "
                        f"(message.id={response.id}, reply_to={sent_message_id}, text='{actual_text}')"
                    )
                else:
                    result['success'] = False
                    result['error_message'] = f'Unexpected response text: expected "bop", got "{response.text}"'
                    logger.warning(
                        f"⚠ Bot responded in {actual_response_time_ms}ms but with wrong text: "
                        f"expected 'bop', got '{response.text}'"
                    )

                # Если время ответа превысило threshold и мы ещё не собирали расширенные метрики
                # - дособираем их сейчас
                threshold = self.config.health_monitor.extended_metrics_threshold_ms
                if (threshold > 0 and actual_response_time_ms >= threshold
                        and self.metrics_collector and server_metrics):
                    try:
                        extended = await self.metrics_collector.collect_metrics(extended=True)
                        # Добавляем расширенные метрики к уже собранным
                        server_metrics.update(extended)
                        server_metrics['extended_collected_after_delay'] = True
                        logger.info(
                            f"Collected extended metrics due to slow response ({actual_response_time_ms}ms >= {threshold}ms)"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to collect extended metrics: {e}")

                # Удаляем из pending после получения ответа
                if sent_message_id in self._pending_checks:
                    del self._pending_checks[sent_message_id]

            except asyncio.TimeoutError:
                result['error_message'] = 'Timeout: bot did not respond within 60 seconds'
                result['response_time_ms'] = None  # Нет ответа = нет времени ответа
                logger.error(result['error_message'])

                # ВАЖНО: Помечаем как timed_out, но НЕ удаляем из pending
                # Если ответ придет позже, мы обновим БД
                if sent_message_id in self._pending_checks:
                    self._pending_checks[sent_message_id]['timed_out'] = True
                    logger.info(f"Marked message {sent_message_id} as timed_out, will wait for late response")

        except Exception as e:
            result['error_message'] = f'Error during health check: {str(e)}'
            result['response_time_ms'] = None  # Ошибка = нет ответа
            logger.error(result['error_message'], exc_info=True)

            # Удаляем из pending при ошибке
            if sent_message_id and sent_message_id in self._pending_checks:
                del self._pending_checks[sent_message_id]

        # Добавляем rolling average stats в метрики
        if server_metrics is not None:
            rolling_stats = self._get_rolling_latency_stats()
            if rolling_stats:
                server_metrics.update(rolling_stats)

        # Сохраняем результат в БД
        completed_at = datetime.utcnow()
        db_check_id = await self._save_check_result(started_at, completed_at, result, server_metrics)

        # Сохраняем db_check_id в pending check (для будущего late update)
        if sent_message_id and sent_message_id in self._pending_checks:
            self._pending_checks[sent_message_id]['db_check_id'] = db_check_id
            logger.debug(f"Saved db_check_id={db_check_id} for pending message {sent_message_id}")

        # Обрабатываем алерты
        await self._handle_alerts(result)

        # Добавляем метрики в результат для доступа из вызывающего кода
        result['server_metrics'] = server_metrics

        return result

    # OBSOLETE: Removed polling-based _wait_for_response() method
    # Now using event-based approach with @client.on(events.NewMessage()) in perform_health_check()

    async def _update_late_response(self, check: dict, message: Message, response_time_ms: int):
        """
        Обновляет запись в БД для запоздалого ответа.

        Args:
            check: Данные pending check
            message: Полученное сообщение
            response_time_ms: Время ответа в миллисекундах
        """
        db_check_id = check.get('db_check_id')
        if not db_check_id:
            logger.error("Cannot update late response: db_check_id is not set")
            return

        # Проверяем текст ответа
        expected_text = "bop"
        actual_text = (message.text or "").strip().lower()
        text_is_correct = actual_text == expected_text

        try:
            async with self.async_session() as session:
                # Формируем error_message с учетом проверки текста
                error_parts = [f'Late response after {response_time_ms}ms (original timeout: 60000ms)']
                if not text_is_correct:
                    error_parts.append(f'Wrong text: expected "bop", got "{message.text}"')
                error_message = '; '.join(error_parts)

                # Обновляем запись в БД
                stmt = update(BotHealthCheck).where(
                    BotHealthCheck.id == db_check_id
                ).values(
                    # НЕ меняем success=False (проверка все равно failed из-за таймаута)
                    # Но обновляем информацию о запоздалом ответе
                    bot_responded=True,
                    actual_response=message.text if message else None,
                    response_time_ms=response_time_ms,
                    error_message=error_message
                )

                await session.execute(stmt)
                await session.commit()

                logger.info(
                    f"✓ Updated DB for late response: check_id={db_check_id}, "
                    f"response_time={response_time_ms}ms, text_correct={text_is_correct}"
                )

                # Отправляем алерт о запоздалом ответе
                alert_text = (
                    f"⚠️ Получен запоздалый ответ от бота!\n\n"
                    f"Время ответа: {response_time_ms / 1000:.1f}s\n"
                    f"Таймаут был: 60s\n"
                    f"Check ID: {db_check_id}\n"
                )
                if not text_is_correct:
                    alert_text += f"\n⚠️ Неправильный текст: ожидалось 'bop', получено '{message.text}'"

                await self.alert_sender.send_alert(
                    text=alert_text,
                    level="WARNING",
                    topic="Late Response"
                )

        except Exception as e:
            logger.error(f"Failed to update late response in DB: {e}", exc_info=True)

    async def _save_check_result(
        self,
        started_at: datetime,
        completed_at: datetime,
        result: dict,
        server_metrics: Optional[dict] = None
    ) -> Optional[int]:
        """
        Сохраняет результат проверки в БД.

        Args:
            started_at: Время начала проверки
            completed_at: Время завершения проверки
            result: Результаты проверки
            server_metrics: Метрики сервера (CPU, RAM, FD, etc.)

        Returns:
            int: ID созданной записи или None при ошибке
        """
        try:
            async with self.async_session() as session:
                check = BotHealthCheck(
                    check_type='command',
                    check_command=self.config.health_monitor.check_command,
                    started_at=started_at,
                    completed_at=completed_at,
                    response_time_ms=result.get('response_time_ms'),
                    success=result.get('success', False),
                    error_message=result.get('error_message'),
                    bot_responded=result.get('bot_responded', False),
                    actual_response=result.get('actual_response'),
                    monitor_version=__version__,
                    server_metrics=server_metrics
                )

                session.add(check)
                await session.commit()
                await session.refresh(check)  # Получаем ID

                metrics_info = f", metrics_fields={len(server_metrics)}" if server_metrics else ""
                logger.debug(f"Health check result saved to DB: id={check.id}, success={result.get('success')}{metrics_info}")
                return check.id

        except Exception as e:
            logger.error(f"Failed to save health check result to DB: {e}", exc_info=True)
            return None

    async def _handle_alerts(self, result: dict):
        """Обрабатывает алерты на основе результатов проверки"""
        response_time_ms = result.get('response_time_ms', 0)
        success = result.get('success', False)
        error_message = result.get('error_message', '')

        # Обновляем счетчик последовательных неудач
        if not success:
            self.consecutive_failures += 1
        else:
            # Если бот восстановился после неудач
            if self.consecutive_failures >= self.config.health_monitor.max_consecutive_failures:
                await self.alert_sender.send_recovery_alert(
                    response_time_ms=response_time_ms,
                    previous_failures=self.consecutive_failures
                )

            self.consecutive_failures = 0

        # Критическая ситуация: превышен порог неудач
        if self.consecutive_failures >= self.config.health_monitor.max_consecutive_failures:
            await self.alert_sender.send_critical_alert(
                failures_count=self.consecutive_failures,
                error_message=error_message
            )
            return

        # Проверка времени ответа
        if success:
            response_time_sec = response_time_ms / 1000

            if response_time_sec >= self.config.health_monitor.response_critical_seconds:
                await self.alert_sender.send_slow_response_alert(
                    response_time_ms=response_time_ms,
                    warning_threshold=self.config.health_monitor.response_warning_seconds,
                    critical_threshold=self.config.health_monitor.response_critical_seconds,
                    level='CRITICAL'
                )
            elif response_time_sec >= self.config.health_monitor.response_warning_seconds:
                await self.alert_sender.send_slow_response_alert(
                    response_time_ms=response_time_ms,
                    warning_threshold=self.config.health_monitor.response_warning_seconds,
                    critical_threshold=self.config.health_monitor.response_critical_seconds,
                    level='WARNING'
                )
        else:
            # Единичная неудача (но еще не критично)
            if self.consecutive_failures == 1:
                await self.alert_sender.send_failure_alert(
                    error_message=error_message,
                    is_first=True
                )
