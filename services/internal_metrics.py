"""
Internal Bot Metrics - метрики изнутри процесса бота.

Собирает метрики, которые невозможно получить снаружи:
- Event loop lag (задержка asyncio event loop)
- GC statistics (сборка мусора)
- Thread count
- HTTP request timing (если используется aiohttp ClientSession)
- Object counts

Использование:
1. Вызвать start_metrics_collector() при старте бота
2. Получать метрики через get_current_metrics() или HTTP endpoint /metrics
"""

import asyncio
import gc
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Dict, Any, List
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class EventLoopLagSample:
    """Один замер задержки event loop"""
    timestamp: datetime
    lag_ms: float


@dataclass
class InternalMetrics:
    """Контейнер для внутренних метрик бота"""
    # Event loop lag
    event_loop_lag_ms: float = 0.0  # Текущая задержка
    event_loop_lag_max_ms: float = 0.0  # Максимальная за период
    event_loop_lag_avg_ms: float = 0.0  # Средняя за период

    # GC statistics
    gc_count_gen0: int = 0  # Количество сборок поколения 0
    gc_count_gen1: int = 0  # Количество сборок поколения 1
    gc_count_gen2: int = 0  # Количество сборок поколения 2
    gc_objects_tracked: int = 0  # Количество отслеживаемых объектов
    gc_is_enabled: bool = True

    # Threading
    thread_count: int = 0
    active_thread_names: List[str] = field(default_factory=list)

    # Asyncio tasks
    asyncio_tasks_count: int = 0
    asyncio_tasks_pending: int = 0

    # Timing (последние измерения)
    last_sample_time: Optional[datetime] = None
    samples_collected: int = 0

    # Uptime
    uptime_seconds: float = 0.0
    start_time: Optional[datetime] = None

    # Telegram API latency (через Local Bot API Server)
    telegram_api_latency_ms: Optional[float] = None
    telegram_api_latency_avg_ms: Optional[float] = None
    telegram_api_latency_max_ms: Optional[float] = None
    telegram_api_last_check: Optional[datetime] = None
    telegram_api_error: Optional[str] = None


class InternalMetricsCollector:
    """
    Сборщик внутренних метрик бота.

    Запускает фоновую задачу, которая периодически замеряет:
    - Задержку event loop (насколько loop отстаёт от реального времени)
    - Статистику GC
    - Количество потоков
    - Количество asyncio tasks
    """

    def __init__(self, sample_interval_ms: int = 100, history_size: int = 100,
                 api_check_interval_sec: int = 30):
        """
        Args:
            sample_interval_ms: Интервал между замерами event loop (мс)
            history_size: Количество хранимых замеров для расчёта avg/max
            api_check_interval_sec: Интервал проверки Telegram API (сек)
        """
        self.sample_interval_ms = sample_interval_ms
        self.history_size = history_size
        self.api_check_interval_sec = api_check_interval_sec

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._api_check_task: Optional[asyncio.Task] = None
        self._start_time: Optional[datetime] = None

        # История замеров event loop lag
        self._lag_history: deque = deque(maxlen=history_size)

        # Счётчики
        self._samples_collected = 0

        # HTTP request timing (опционально, заполняется извне)
        self._http_request_times: deque = deque(maxlen=100)

        # Telegram API latency
        self._bot = None
        self._api_latency_history: deque = deque(maxlen=20)  # Последние 20 замеров
        self._api_latency_last: Optional[float] = None
        self._api_latency_error: Optional[str] = None
        self._api_last_check: Optional[datetime] = None

    def set_bot(self, bot):
        """
        Устанавливает ссылку на бота для замера API latency.

        Args:
            bot: Экземпляр aiogram Bot
        """
        self._bot = bot
        logger.info("Bot reference set for API latency measurement")

    async def start(self):
        """Запускает фоновый сбор метрик"""
        if self._running:
            logger.warning("Internal metrics collector is already running")
            return

        self._running = True
        self._start_time = datetime.utcnow()
        self._task = asyncio.create_task(self._sample_loop())

        # Запускаем проверку API если бот установлен
        if self._bot:
            self._api_check_task = asyncio.create_task(self._api_check_loop())
            logger.info(f"Internal metrics collector started (interval={self.sample_interval_ms}ms, api_check={self.api_check_interval_sec}s)")
        else:
            logger.info(f"Internal metrics collector started (interval={self.sample_interval_ms}ms, api_check=disabled - no bot set)")

    async def stop(self):
        """Останавливает сбор метрик"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._api_check_task:
            self._api_check_task.cancel()
            try:
                await self._api_check_task
            except asyncio.CancelledError:
                pass
        logger.info("Internal metrics collector stopped")

    async def _sample_loop(self):
        """Основной цикл сбора метрик"""
        interval_sec = self.sample_interval_ms / 1000.0

        while self._running:
            try:
                # Замеряем event loop lag
                lag_ms = await self._measure_event_loop_lag()
                self._lag_history.append(EventLoopLagSample(
                    timestamp=datetime.utcnow(),
                    lag_ms=lag_ms
                ))
                self._samples_collected += 1

                # Логируем если задержка большая
                if lag_ms > 100:  # > 100ms - это уже проблема
                    logger.warning(f"High event loop lag detected: {lag_ms:.1f}ms")

            except Exception as e:
                logger.debug(f"Error in metrics sample loop: {e}")

            await asyncio.sleep(interval_sec)

    async def _measure_event_loop_lag(self) -> float:
        """
        Измеряет задержку event loop.

        Идея: планируем callback на "немедленно" и замеряем,
        сколько реально прошло времени до его выполнения.
        """
        loop = asyncio.get_event_loop()
        start = time.perf_counter()

        # Создаём future и планируем его разрешение "немедленно"
        future = loop.create_future()
        loop.call_soon(future.set_result, None)

        await future

        end = time.perf_counter()
        lag_ms = (end - start) * 1000

        return lag_ms

    async def _api_check_loop(self):
        """Цикл проверки Telegram API latency"""
        while self._running:
            try:
                await self._measure_telegram_api_latency()
            except Exception as e:
                logger.debug(f"Error in API check loop: {e}")

            await asyncio.sleep(self.api_check_interval_sec)

    async def _measure_telegram_api_latency(self):
        """
        Измеряет latency Telegram API через getMe.

        Это измеряет путь: Bot → Local Bot API Server → Telegram → обратно
        """
        if not self._bot:
            return

        try:
            start = time.perf_counter()
            await self._bot.get_me()
            end = time.perf_counter()

            latency_ms = round((end - start) * 1000, 1)

            self._api_latency_history.append(latency_ms)
            self._api_latency_last = latency_ms
            self._api_latency_error = None
            self._api_last_check = datetime.utcnow()

            # Логируем если высокая латентность
            if latency_ms > 1000:
                logger.warning(f"High Telegram API latency: {latency_ms}ms")
            else:
                logger.debug(f"Telegram API latency: {latency_ms}ms")

        except Exception as e:
            self._api_latency_error = str(e)[:100]
            self._api_last_check = datetime.utcnow()
            logger.warning(f"Telegram API check failed: {e}")

    def get_metrics(self) -> InternalMetrics:
        """Возвращает текущие метрики"""
        metrics = InternalMetrics()

        # Event loop lag
        if self._lag_history:
            lags = [s.lag_ms for s in self._lag_history]
            metrics.event_loop_lag_ms = lags[-1] if lags else 0.0
            metrics.event_loop_lag_max_ms = max(lags)
            metrics.event_loop_lag_avg_ms = round(sum(lags) / len(lags), 2)

        # GC statistics
        gc_counts = gc.get_count()
        metrics.gc_count_gen0 = gc_counts[0]
        metrics.gc_count_gen1 = gc_counts[1]
        metrics.gc_count_gen2 = gc_counts[2]
        metrics.gc_objects_tracked = len(gc.get_objects())
        metrics.gc_is_enabled = gc.isenabled()

        # Threading
        threads = threading.enumerate()
        metrics.thread_count = len(threads)
        metrics.active_thread_names = [t.name for t in threads[:20]]  # Ограничиваем

        # Asyncio tasks
        try:
            all_tasks = asyncio.all_tasks()
            metrics.asyncio_tasks_count = len(all_tasks)
            metrics.asyncio_tasks_pending = sum(1 for t in all_tasks if not t.done())
        except RuntimeError:
            pass  # No running event loop

        # Timing
        metrics.last_sample_time = datetime.utcnow()
        metrics.samples_collected = self._samples_collected

        # Uptime
        if self._start_time:
            metrics.start_time = self._start_time
            metrics.uptime_seconds = (datetime.utcnow() - self._start_time).total_seconds()

        # Telegram API latency
        if self._api_latency_last is not None:
            metrics.telegram_api_latency_ms = self._api_latency_last
        if self._api_latency_history:
            lats = list(self._api_latency_history)
            metrics.telegram_api_latency_avg_ms = round(sum(lats) / len(lats), 1)
            metrics.telegram_api_latency_max_ms = max(lats)
        metrics.telegram_api_last_check = self._api_last_check
        metrics.telegram_api_error = self._api_latency_error

        return metrics

    def get_metrics_dict(self) -> Dict[str, Any]:
        """Возвращает метрики как словарь (для JSON)"""
        metrics = self.get_metrics()
        result = asdict(metrics)

        # Конвертируем datetime в ISO string
        if result.get('last_sample_time'):
            result['last_sample_time'] = result['last_sample_time'].isoformat()
        if result.get('start_time'):
            result['start_time'] = result['start_time'].isoformat()
        if result.get('telegram_api_last_check'):
            result['telegram_api_last_check'] = result['telegram_api_last_check'].isoformat()

        return result

    def record_http_request_time(self, duration_ms: float, url: str = "", success: bool = True):
        """
        Записывает время HTTP запроса (вызывается извне).

        Args:
            duration_ms: Длительность запроса в мс
            url: URL запроса
            success: Успешен ли запрос
        """
        self._http_request_times.append({
            'timestamp': datetime.utcnow().isoformat(),
            'duration_ms': duration_ms,
            'url': url[:100],  # Ограничиваем длину
            'success': success
        })


# Глобальный экземпляр
_collector: Optional[InternalMetricsCollector] = None


def get_collector() -> Optional[InternalMetricsCollector]:
    """Возвращает глобальный экземпляр сборщика"""
    return _collector


async def start_metrics_collector(
    sample_interval_ms: int = 100,
    bot=None,
    api_check_interval_sec: int = 30
) -> InternalMetricsCollector:
    """
    Запускает глобальный сборщик метрик.

    Вызывать при старте бота (в on_startup).

    Args:
        sample_interval_ms: Интервал замеров event loop (мс)
        bot: Экземпляр aiogram Bot для замера API latency (опционально)
        api_check_interval_sec: Интервал проверки Telegram API (сек)
    """
    global _collector

    if _collector is not None:
        logger.warning("Metrics collector already started")
        return _collector

    _collector = InternalMetricsCollector(
        sample_interval_ms=sample_interval_ms,
        api_check_interval_sec=api_check_interval_sec
    )

    if bot:
        _collector.set_bot(bot)

    await _collector.start()

    return _collector


def set_bot_for_metrics(bot):
    """
    Устанавливает бота для замера API latency.

    Можно вызвать после start_metrics_collector если бот был создан позже.
    """
    if _collector:
        _collector.set_bot(bot)
        # Запускаем API check loop если ещё не запущен
        if _collector._running and not _collector._api_check_task:
            _collector._api_check_task = asyncio.create_task(_collector._api_check_loop())
            logger.info("API check loop started after bot was set")
    else:
        logger.warning("Cannot set bot: metrics collector not started")


async def stop_metrics_collector():
    """Останавливает глобальный сборщик"""
    global _collector

    if _collector:
        await _collector.stop()
        _collector = None


def get_current_metrics() -> Optional[Dict[str, Any]]:
    """
    Возвращает текущие метрики как словарь.

    Удобно для вызова из любого места кода.
    """
    if _collector:
        return _collector.get_metrics_dict()
    return None


def record_http_request(duration_ms: float, url: str = "", success: bool = True):
    """
    Записывает время HTTP запроса.

    Использовать как wrapper для aiohttp requests:

    start = time.perf_counter()
    async with session.get(url) as resp:
        ...
    duration = (time.perf_counter() - start) * 1000
    record_http_request(duration, url, resp.status < 400)
    """
    if _collector:
        _collector.record_http_request_time(duration_ms, url, success)


# --- HTTP endpoint для aiohttp ---

async def metrics_handler(request):
    """
    HTTP handler для получения метрик.

    Добавить в aiohttp app:
    app.router.add_get('/metrics', metrics_handler)
    """
    from aiohttp import web

    metrics = get_current_metrics()
    if metrics is None:
        return web.json_response(
            {'error': 'Metrics collector not started'},
            status=503
        )

    return web.json_response(metrics)
