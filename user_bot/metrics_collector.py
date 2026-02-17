"""
Metrics Collector for Health Monitor.

Собирает метрики сервера для диагностики причин задержек бота:

Базовые метрики (собираются всегда):
- Системные: CPU, RAM, Load Average
- Disk I/O: iowait, read/write bytes, disk usage
- Swap: usage, sin/sout (критично для задержек!)
- Network: throughput (bytes sent/recv), errors, drops
- Network: соединения, CLOSE_WAIT, TCP stats
- PostgreSQL: соединения (total, active, idle, waiting)
- PostgreSQL диагностика (через async_session):
  - pg_blocked_queries: заблокированные запросы
  - pg_long_queries: запросы > 5 секунд
  - pg_idle_in_transaction: зависшие транзакции
  - pg_lock_waits: ожидающие блокировки
  - pg_oldest_query_seconds: время самого долгого запроса
- Процессы: ffmpeg/ffprobe count
- File Descriptors: для бота и local server
- Сессии обработки: активные, за последний час
- Bot internal (через HTTP /metrics):
  - Event loop lag (критично для диагностики!)
  - GC statistics
  - Thread count
  - Asyncio tasks count
- Direct Telegram API (обходит Local Bot API Server):
  - Connectivity ping к api.telegram.org
  - getMe latency через прямой API

Расширенные метрики (по threshold или всегда если threshold=0):
- lsof top: топ-15 процессов по количеству FD
- FD types: типы FD у процесса бота
- FD limits: лимиты и % использования для процессов
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional

import aiohttp
import psutil
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from config_data.config import Config

# URL для получения внутренних метрик бота
BOT_METRICS_URL = "http://127.0.0.1:3000/metrics"

# Direct Telegram API (обходит Local Bot API Server)
TELEGRAM_API_URL = "https://api.telegram.org"

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Сборщик метрик сервера для health monitor"""

    def __init__(
        self,
        config: Config,
        async_session: sessionmaker
    ):
        self.config = config
        self.async_session = async_session
        self._bot_pid: Optional[int] = None
        self._local_server_pid: Optional[int] = None

        # Для расчёта network delta (KB/s)
        self._prev_net_bytes_recv: Optional[int] = None
        self._prev_net_bytes_sent: Optional[int] = None
        self._prev_net_time: Optional[float] = None

    async def collect_metrics(self, extended: bool = False) -> dict:
        """
        Собирает все метрики.

        Args:
            extended: Собирать расширенные метрики (lsof, подробный вывод)

        Returns:
            dict с метриками
        """
        metrics = {}

        try:
            # Параллельно собираем независимые метрики
            results = await asyncio.gather(
                self._collect_system_metrics(),
                self._collect_process_metrics(),
                self._collect_network_metrics(),
                self._collect_postgres_metrics(),
                self._collect_postgres_diagnostics(),  # Блокировки, долгие запросы
                self._collect_ffmpeg_metrics(),
                self._collect_processing_sessions(),
                self._collect_disk_io_metrics(),
                self._collect_swap_metrics(),
                self._collect_network_throughput(),
                self._collect_bot_internal_metrics(),  # Event loop lag, GC, threads
                self._collect_direct_api_metrics(),  # Direct Telegram API (обходит Local Server)
                return_exceptions=True
            )

            # Объединяем результаты
            for result in results:
                if isinstance(result, dict):
                    metrics.update(result)
                elif isinstance(result, Exception):
                    logger.warning(f"Error collecting metrics: {result}")

            # Расширенные метрики (более тяжелые)
            if extended:
                extended_results = await asyncio.gather(
                    self._collect_lsof_top(),
                    self._collect_detailed_fd_info(),
                    self._collect_fd_limits(),
                    return_exceptions=True
                )
                for result in extended_results:
                    if isinstance(result, dict):
                        metrics.update(result)

        except Exception as e:
            logger.error(f"Error in collect_metrics: {e}", exc_info=True)
            metrics['collection_error'] = str(e)

        return metrics

    async def _collect_system_metrics(self) -> dict:
        """Системные метрики: CPU, RAM, Load Average"""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            load_avg = os.getloadavg()

            return {
                'cpu_percent': round(cpu_percent, 1),
                'memory_percent': round(memory.percent, 1),
                'memory_available_mb': round(memory.available / (1024 * 1024), 0),
                'load_avg_1m': round(load_avg[0], 2),
                'load_avg_5m': round(load_avg[1], 2),
                'load_avg_15m': round(load_avg[2], 2),
            }
        except Exception as e:
            logger.warning(f"Failed to collect system metrics: {e}")
            return {'system_error': str(e)}

    async def _collect_process_metrics(self) -> dict:
        """Метрики процессов бота и local server"""
        metrics = {}

        # Ищем PID бота
        bot_pattern = self.config.health_monitor.bot_process_pattern
        bot_pid = await self._find_process_pid(bot_pattern)

        if bot_pid:
            self._bot_pid = bot_pid
            try:
                proc = psutil.Process(bot_pid)
                metrics['bot_pid'] = bot_pid
                metrics['bot_memory_mb'] = round(proc.memory_info().rss / (1024 * 1024), 1)
                metrics['bot_threads'] = proc.num_threads()
                metrics['bot_fd_count'] = proc.num_fds()
                metrics['bot_cpu_percent'] = round(proc.cpu_percent(interval=0.1), 1)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"Cannot get bot process info: {e}")
                metrics['bot_error'] = str(e)

        # Ищем PID local server
        local_pattern = self.config.health_monitor.local_server_process_pattern
        local_pid = await self._find_process_pid(local_pattern)

        if local_pid:
            self._local_server_pid = local_pid
            try:
                proc = psutil.Process(local_pid)
                metrics['local_server_pid'] = local_pid
                metrics['local_server_memory_mb'] = round(proc.memory_info().rss / (1024 * 1024), 1)
                metrics['local_server_fd_count'] = proc.num_fds()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"Cannot get local server process info: {e}")
                metrics['local_server_error'] = str(e)

        return metrics

    async def _find_process_pid(self, pattern: str) -> Optional[int]:
        """Находит PID процесса по паттерну"""
        try:
            proc = await asyncio.create_subprocess_exec(
                'pgrep', '-f', pattern,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if stdout:
                # Берем первый PID (может быть несколько)
                pids = stdout.decode().strip().split('\n')
                # Исключаем наш собственный процесс
                current_pid = os.getpid()
                for pid_str in pids:
                    try:
                        pid = int(pid_str)
                        if pid != current_pid:
                            return pid
                    except ValueError:
                        continue
            return None
        except Exception as e:
            logger.debug(f"Failed to find process by pattern '{pattern}': {e}")
            return None

    async def _collect_network_metrics(self) -> dict:
        """Сетевые метрики: соединения, CLOSE_WAIT"""
        metrics = {}

        try:
            # Количество CLOSE_WAIT соединений
            proc = await asyncio.create_subprocess_exec(
                'ss', '-tan', 'state', 'close-wait',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            # Минус 1 на заголовок
            close_wait_count = max(0, len(stdout.decode().strip().split('\n')) - 1)
            metrics['close_wait_count'] = close_wait_count

            # Общая статистика сокетов
            proc = await asyncio.create_subprocess_exec(
                'ss', '-s',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            output = stdout.decode()

            # Парсим TCP статистику
            tcp_match = re.search(r'TCP:\s+(\d+)', output)
            if tcp_match:
                metrics['tcp_connections_total'] = int(tcp_match.group(1))

            # Established соединения
            estab_match = re.search(r'estab\s+(\d+)', output)
            if estab_match:
                metrics['tcp_established'] = int(estab_match.group(1))

            # TIME-WAIT
            timewait_match = re.search(r'timewait\s+(\d+)', output)
            if timewait_match:
                metrics['tcp_timewait'] = int(timewait_match.group(1))

        except Exception as e:
            logger.warning(f"Failed to collect network metrics: {e}")
            metrics['network_error'] = str(e)

        return metrics

    async def _collect_postgres_metrics(self) -> dict:
        """Метрики PostgreSQL соединений"""
        metrics = {}

        try:
            async with self.async_session() as session:
                # Общее количество соединений
                result = await session.execute(
                    text("SELECT count(*) FROM pg_stat_activity")
                )
                total = result.scalar()
                metrics['pg_connections_total'] = total

                # Активные соединения (не idle)
                result = await session.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE state != 'idle'")
                )
                active = result.scalar()
                metrics['pg_connections_active'] = active

                # Idle соединения
                metrics['pg_connections_idle'] = total - active

                # Соединения в состоянии waiting
                result = await session.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE wait_event IS NOT NULL")
                )
                waiting = result.scalar()
                metrics['pg_connections_waiting'] = waiting

        except Exception as e:
            logger.warning(f"Failed to collect postgres metrics: {e}")
            metrics['pg_error'] = str(e)

        return metrics

    async def _collect_postgres_diagnostics(self) -> dict:
        """
        Расширенная диагностика PostgreSQL для выявления проблем с блокировками.

        Использует существующий async_session (без sudo psql).

        Метрики:
        - pg_blocked_queries: количество заблокированных запросов
        - pg_long_queries: количество запросов > 5 сек
        - pg_idle_in_transaction: соединения в состоянии 'idle in transaction'
        - pg_lock_waits: процессы, ожидающие блокировки
        - pg_oldest_query_seconds: время выполнения самого долгого запроса
        """
        metrics = {}

        try:
            async with self.async_session() as session:
                # 1. Blocked queries - запросы, которые заблокированы другими
                result = await session.execute(text("""
                    SELECT count(*) FROM pg_stat_activity blocked
                    JOIN pg_locks bl ON bl.pid = blocked.pid
                    JOIN pg_locks bl2 ON bl2.locktype = bl.locktype
                        AND bl2.database IS NOT DISTINCT FROM bl.database
                        AND bl2.relation IS NOT DISTINCT FROM bl.relation
                        AND bl2.page IS NOT DISTINCT FROM bl.page
                        AND bl2.tuple IS NOT DISTINCT FROM bl.tuple
                        AND bl2.virtualxid IS NOT DISTINCT FROM bl.virtualxid
                        AND bl2.transactionid IS NOT DISTINCT FROM bl.transactionid
                        AND bl2.classid IS NOT DISTINCT FROM bl.classid
                        AND bl2.objid IS NOT DISTINCT FROM bl.objid
                        AND bl2.objsubid IS NOT DISTINCT FROM bl.objsubid
                        AND bl2.pid != bl.pid
                    JOIN pg_stat_activity blocking ON bl2.pid = blocking.pid
                    WHERE NOT bl.granted
                """))
                metrics['pg_blocked_queries'] = result.scalar() or 0

                # 2. Long queries - запросы > 5 секунд
                result = await session.execute(text("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE state = 'active'
                    AND query_start < now() - interval '5 seconds'
                    AND query NOT LIKE '%pg_stat_activity%'
                """))
                metrics['pg_long_queries'] = result.scalar() or 0

                # 3. Idle in transaction - зависшие транзакции (> 30 сек)
                result = await session.execute(text("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE state = 'idle in transaction'
                    AND xact_start < now() - interval '30 seconds'
                """))
                metrics['pg_idle_in_transaction'] = result.scalar() or 0

                # 4. Lock waits - процессы, ожидающие блокировки
                result = await session.execute(text("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE wait_event_type = 'Lock'
                    AND state = 'active'
                """))
                metrics['pg_lock_waits'] = result.scalar() or 0

                # 5. Oldest query - время самого долгого активного запроса
                result = await session.execute(text("""
                    SELECT COALESCE(
                        EXTRACT(EPOCH FROM (now() - min(query_start)))::integer,
                        0
                    )
                    FROM pg_stat_activity
                    WHERE state = 'active'
                    AND query NOT LIKE '%pg_stat_activity%'
                """))
                metrics['pg_oldest_query_seconds'] = result.scalar() or 0

        except Exception as e:
            logger.warning(f"Failed to collect postgres diagnostics: {e}")
            metrics['pg_diagnostics_error'] = str(e)[:100]

        return metrics

    async def _collect_ffmpeg_metrics(self) -> dict:
        """Количество процессов ffmpeg/ffprobe"""
        metrics = {}

        try:
            # ffmpeg процессы
            proc = await asyncio.create_subprocess_exec(
                'pgrep', '-c', 'ffmpeg',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            metrics['ffmpeg_processes'] = int(stdout.decode().strip() or 0)
        except Exception:
            metrics['ffmpeg_processes'] = 0

        try:
            # ffprobe процессы
            proc = await asyncio.create_subprocess_exec(
                'pgrep', '-c', 'ffprobe',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            metrics['ffprobe_processes'] = int(stdout.decode().strip() or 0)
        except Exception:
            metrics['ffprobe_processes'] = 0

        return metrics

    async def _collect_processing_sessions(self) -> dict:
        """Активные сессии обработки из БД"""
        metrics = {}

        try:
            async with self.async_session() as session:
                # Активные сессии
                result = await session.execute(
                    text("""
                        SELECT count(*) FROM processing_sessions
                        WHERE final_status is NULL
                    """)
                )
                active = result.scalar()
                metrics['active_processing_sessions'] = active or 0

                # Сессии за последний час
                result = await session.execute(
                    text("""
                        SELECT count(*) FROM processing_sessions
                        WHERE started_at > NOW() - INTERVAL '1 hour'
                    """)
                )
                recent = result.scalar()
                metrics['processing_sessions_last_hour'] = recent or 0

        except Exception as e:
            logger.warning(f"Failed to collect processing sessions: {e}")
            metrics['sessions_error'] = str(e)

        return metrics

    async def _collect_lsof_top(self) -> dict:
        """Топ процессов по количеству FD (lsof)"""
        metrics = {}

        try:
            # lsof -nP 2>/dev/null | awk '{print $1}' | sort | uniq -c | sort -nr | head -15
            proc = await asyncio.create_subprocess_shell(
                "lsof -nP 2>/dev/null | awk '{print $1}' | sort | uniq -c | sort -nr | head -15",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)

            if stdout:
                lines = stdout.decode().strip().split('\n')
                lsof_top = []
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            count = int(parts[0])
                            name = parts[1]
                            lsof_top.append({'process': name, 'fd_count': count})
                        except ValueError:
                            continue
                metrics['lsof_top_processes'] = lsof_top

        except Exception as e:
            logger.warning(f"Failed to collect lsof top: {e}")
            metrics['lsof_error'] = str(e)

        return metrics

    async def _collect_detailed_fd_info(self) -> dict:
        """Подробная информация о FD для бота"""
        metrics = {}

        if not self._bot_pid:
            return metrics

        try:
            # Типы FD для процесса бота
            proc = await asyncio.create_subprocess_shell(
                f"ls -la /proc/{self._bot_pid}/fd 2>/dev/null | awk '{{print $NF}}' | "
                f"sed 's/.*://' | sort | uniq -c | sort -nr | head -10",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if stdout:
                lines = stdout.decode().strip().split('\n')
                fd_types = []
                for line in lines:
                    parts = line.strip().split(None, 1)
                    if len(parts) >= 2:
                        try:
                            count = int(parts[0])
                            fd_type = parts[1][:50]  # Ограничиваем длину
                            fd_types.append({'type': fd_type, 'count': count})
                        except ValueError:
                            continue
                if fd_types:
                    metrics['bot_fd_types'] = fd_types

        except Exception as e:
            logger.debug(f"Failed to collect detailed FD info: {e}")

        return metrics

    async def _collect_disk_io_metrics(self) -> dict:
        """Disk I/O метрики: iowait, read/write bytes"""
        metrics = {}

        try:
            # IO Wait из CPU times
            cpu_times = psutil.cpu_times_percent(interval=0.1)
            metrics['iowait_percent'] = round(cpu_times.iowait, 1)

            # Disk I/O counters
            disk_io = psutil.disk_io_counters()
            if disk_io:
                metrics['disk_read_bytes'] = disk_io.read_bytes
                metrics['disk_write_bytes'] = disk_io.write_bytes
                metrics['disk_read_count'] = disk_io.read_count
                metrics['disk_write_count'] = disk_io.write_count

            # Disk usage для корневого раздела
            disk_usage = psutil.disk_usage('/')
            metrics['disk_percent'] = round(disk_usage.percent, 1)
            metrics['disk_free_gb'] = round(disk_usage.free / (1024 ** 3), 1)

        except Exception as e:
            logger.warning(f"Failed to collect disk I/O metrics: {e}")
            metrics['disk_error'] = str(e)

        return metrics

    async def _collect_swap_metrics(self) -> dict:
        """Swap метрики - критично для задержек"""
        metrics = {}

        try:
            swap = psutil.swap_memory()
            metrics['swap_percent'] = round(swap.percent, 1)
            metrics['swap_used_mb'] = round(swap.used / (1024 * 1024), 0)
            metrics['swap_total_mb'] = round(swap.total / (1024 * 1024), 0)

            # sin/sout показывают активность swap (bytes swapped in/out)
            metrics['swap_sin'] = swap.sin  # bytes swapped in since boot
            metrics['swap_sout'] = swap.sout  # bytes swapped out since boot

        except Exception as e:
            logger.warning(f"Failed to collect swap metrics: {e}")
            metrics['swap_error'] = str(e)

        return metrics

    async def _collect_network_throughput(self) -> dict:
        """Network throughput: rx/tx bytes и скорость KB/s"""
        metrics = {}

        try:
            net_io = psutil.net_io_counters()
            current_time = time.time()

            # Абсолютные значения
            metrics['net_bytes_sent'] = net_io.bytes_sent
            metrics['net_bytes_recv'] = net_io.bytes_recv
            metrics['net_packets_sent'] = net_io.packets_sent
            metrics['net_packets_recv'] = net_io.packets_recv
            metrics['net_errin'] = net_io.errin
            metrics['net_errout'] = net_io.errout
            metrics['net_dropin'] = net_io.dropin
            metrics['net_dropout'] = net_io.dropout

            # Расчёт delta (KB/s) - ключевая метрика для диагностики!
            if (self._prev_net_bytes_recv is not None and
                    self._prev_net_time is not None):
                time_delta = current_time - self._prev_net_time
                if time_delta > 0:
                    # Разница в байтах / время = байт/сек, делим на 1024 = KB/s
                    recv_delta = net_io.bytes_recv - self._prev_net_bytes_recv
                    sent_delta = net_io.bytes_sent - self._prev_net_bytes_sent

                    metrics['net_rx_kb_s'] = round(recv_delta / time_delta / 1024, 1)
                    metrics['net_tx_kb_s'] = round(sent_delta / time_delta / 1024, 1)

            # Сохраняем для следующего расчёта
            self._prev_net_bytes_recv = net_io.bytes_recv
            self._prev_net_bytes_sent = net_io.bytes_sent
            self._prev_net_time = current_time

        except Exception as e:
            logger.warning(f"Failed to collect network throughput: {e}")
            metrics['net_throughput_error'] = str(e)

        return metrics

    async def _collect_fd_limits(self) -> dict:
        """FD limits и процент использования для ключевых процессов"""
        metrics = {}

        # Собираем для бота
        if self._bot_pid:
            limit_info = await self._get_process_fd_limit(self._bot_pid)
            if limit_info:
                metrics['bot_fd_limit'] = limit_info['limit']
                metrics['bot_fd_used_percent'] = limit_info['used_percent']

        # Собираем для local server
        if self._local_server_pid:
            limit_info = await self._get_process_fd_limit(self._local_server_pid)
            if limit_info:
                metrics['local_server_fd_limit'] = limit_info['limit']
                metrics['local_server_fd_used_percent'] = limit_info['used_percent']

        # Системный лимит
        try:
            with open('/proc/sys/fs/file-nr', 'r') as f:
                parts = f.read().strip().split()
                if len(parts) >= 3:
                    allocated = int(parts[0])
                    max_fds = int(parts[2])
                    metrics['system_fd_allocated'] = allocated
                    metrics['system_fd_max'] = max_fds
                    metrics['system_fd_used_percent'] = round(allocated / max_fds * 100, 1) if max_fds > 0 else 0
        except Exception as e:
            logger.debug(f"Failed to get system FD limits: {e}")

        return metrics

    async def _get_process_fd_limit(self, pid: int) -> Optional[dict]:
        """Получает лимит FD для процесса и процент использования"""
        try:
            proc = psutil.Process(pid)
            current_fds = proc.num_fds()

            # Читаем лимит из /proc/<pid>/limits
            limits_path = f'/proc/{pid}/limits'
            soft_limit = None

            with open(limits_path, 'r') as f:
                for line in f:
                    if 'Max open files' in line:
                        parts = line.split()
                        # Формат: "Max open files   1024   1048576   files"
                        # Soft limit обычно на позиции 3 (индекс после "files")
                        for i, part in enumerate(parts):
                            if part == 'files' and i >= 2:
                                try:
                                    soft_limit = int(parts[i - 2])  # Soft limit
                                    break
                                except (ValueError, IndexError):
                                    pass
                        break

            if soft_limit and soft_limit > 0:
                used_percent = round(current_fds / soft_limit * 100, 1)
                return {
                    'limit': soft_limit,
                    'current': current_fds,
                    'used_percent': used_percent
                }

        except Exception as e:
            logger.debug(f"Failed to get FD limit for PID {pid}: {e}")

        return None

    async def _collect_bot_internal_metrics(self) -> dict:
        """
        Собирает внутренние метрики бота через HTTP endpoint /metrics.

        Метрики включают:
        - event_loop_lag_ms: задержка event loop (критично!)
        - gc_*: статистика сборщика мусора
        - thread_count: количество потоков
        - asyncio_tasks_*: количество asyncio tasks
        """
        metrics = {}

        try:
            timeout = aiohttp.ClientTimeout(total=2.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(BOT_METRICS_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        # Event loop lag - ключевая метрика!
                        if 'event_loop_lag_ms' in data:
                            metrics['bot_event_loop_lag_ms'] = round(data['event_loop_lag_ms'], 2)
                        if 'event_loop_lag_max_ms' in data:
                            metrics['bot_event_loop_lag_max_ms'] = round(data['event_loop_lag_max_ms'], 2)
                        if 'event_loop_lag_avg_ms' in data:
                            metrics['bot_event_loop_lag_avg_ms'] = round(data['event_loop_lag_avg_ms'], 2)

                        # GC stats
                        if 'gc_count_gen0' in data:
                            metrics['bot_gc_gen0'] = data['gc_count_gen0']
                            metrics['bot_gc_gen1'] = data['gc_count_gen1']
                            metrics['bot_gc_gen2'] = data['gc_count_gen2']
                        if 'gc_objects_tracked' in data:
                            metrics['bot_gc_objects'] = data['gc_objects_tracked']

                        # Threading
                        if 'thread_count' in data:
                            metrics['bot_thread_count'] = data['thread_count']

                        # Asyncio tasks
                        if 'asyncio_tasks_count' in data:
                            metrics['bot_asyncio_tasks'] = data['asyncio_tasks_count']
                        if 'asyncio_tasks_pending' in data:
                            metrics['bot_asyncio_pending'] = data['asyncio_tasks_pending']

                        # Uptime
                        if 'uptime_seconds' in data:
                            metrics['bot_uptime_seconds'] = round(data['uptime_seconds'], 0)

                        # Telegram API latency (через Local Bot API Server)
                        if 'telegram_api_latency_ms' in data:
                            metrics['telegram_api_latency_ms'] = data['telegram_api_latency_ms']
                        if 'telegram_api_latency_avg_ms' in data:
                            metrics['telegram_api_latency_avg_ms'] = data['telegram_api_latency_avg_ms']
                        if 'telegram_api_latency_max_ms' in data:
                            metrics['telegram_api_latency_max_ms'] = data['telegram_api_latency_max_ms']
                        if data.get('telegram_api_error'):
                            metrics['telegram_api_error'] = data['telegram_api_error']

                        logger.debug(f"Collected bot internal metrics: {len(metrics)} fields")
                    else:
                        metrics['bot_internal_error'] = f'HTTP {resp.status}'

        except asyncio.TimeoutError:
            metrics['bot_internal_error'] = 'timeout'
            logger.debug("Timeout fetching bot internal metrics")
        except aiohttp.ClientError as e:
            metrics['bot_internal_error'] = str(e)[:50]
            logger.debug(f"Error fetching bot internal metrics: {e}")
        except Exception as e:
            metrics['bot_internal_error'] = str(e)[:50]
            logger.debug(f"Unexpected error fetching bot internal metrics: {e}")

        return metrics

    async def _collect_direct_api_metrics(self) -> dict:
        """
        Проверяет прямой доступ к Telegram API (обходя Local Bot API Server).

        Метрики:
        - direct_api_connectivity_ms: ping к api.telegram.org (без токена)
        - direct_api_getme_ms: getMe через прямой API
        - direct_api_error: ошибка (если есть)

        Это помогает понять, проблема в:
        - Local Bot API Server (если direct быстрый, а через local медленный)
        - Telegram infrastructure (если оба медленные)
        - Нашем коде (если оба быстрые, но бот тормозит)
        """
        metrics = {}
        bot_token = self.config.tg_bot.token

        try:
            timeout = aiohttp.ClientTimeout(total=10.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1. Простой ping к api.telegram.org (без токена)
                try:
                    start = time.time()
                    async with session.get(f"{TELEGRAM_API_URL}/") as resp:
                        # Telegram возвращает 404 для корня, но соединение работает
                        _ = resp.status
                    connectivity_ms = round((time.time() - start) * 1000, 1)
                    metrics['direct_api_connectivity_ms'] = connectivity_ms
                except Exception as e:
                    metrics['direct_api_connectivity_error'] = str(e)[:50]

                # 2. getMe через прямой API (с токеном)
                try:
                    start = time.time()
                    async with session.get(f"{TELEGRAM_API_URL}/bot{bot_token}/getMe") as resp:
                        if resp.status == 200:
                            getme_ms = round((time.time() - start) * 1000, 1)
                            metrics['direct_api_getme_ms'] = getme_ms

                            # Логируем если высокая латентность
                            if getme_ms > 1000:
                                logger.warning(f"High direct Telegram API latency: {getme_ms}ms")
                        else:
                            metrics['direct_api_getme_error'] = f'HTTP {resp.status}'
                except Exception as e:
                    metrics['direct_api_getme_error'] = str(e)[:50]

        except Exception as e:
            metrics['direct_api_error'] = str(e)[:50]
            logger.debug(f"Error checking direct Telegram API: {e}")

        return metrics
