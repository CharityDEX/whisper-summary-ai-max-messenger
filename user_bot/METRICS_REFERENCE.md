# Health Monitor Metrics Reference

Справочник по всем метрикам, собираемым Health Monitor (v1.8.0).

## Содержание

1. [Системные метрики](#системные-метрики)
2. [Метрики процессов](#метрики-процессов)
3. [Disk I/O и Swap](#disk-io-и-swap)
4. [Сетевые метрики](#сетевые-метрики)
5. [PostgreSQL](#postgresql)
6. [PostgreSQL Diagnostics](#postgresql-diagnostics)
7. [Сессии обработки](#сессии-обработки)
8. [Внутренние метрики бота](#внутренние-метрики-бота)
9. [Telegram API Latency](#telegram-api-latency)
10. [Direct API (обход Local Server)](#direct-api-обход-local-server)
11. [Rolling Average](#rolling-average)
12. [Расширенные метрики](#расширенные-метрики)
13. [Диагностика проблем](#диагностика-проблем)

---

## Системные метрики

Базовые метрики системы, собираются через `psutil`.

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `cpu_percent` | `psutil.cpu_percent(interval=0.1)` | Загрузка CPU в % | > 80% - высокая нагрузка |
| `memory_percent` | `psutil.virtual_memory().percent` | Использование RAM в % | > 90% - критично, начнется swap |
| `memory_available_mb` | `psutil.virtual_memory().available` | Доступная память в MB | < 500MB - мало памяти |
| `load_avg_1m` | `os.getloadavg()[0]` | Load Average за 1 мин | > кол-ва CPU ядер = перегрузка |
| `load_avg_5m` | `os.getloadavg()[1]` | Load Average за 5 мин | Показывает тренд |
| `load_avg_15m` | `os.getloadavg()[2]` | Load Average за 15 мин | Показывает тренд |

**Пример диагностики:**
- `cpu_percent > 80%` + `load_avg > cores` = CPU-bound нагрузка
- `memory_percent > 90%` = возможен swap, задержки неизбежны

---

## Метрики процессов

Метрики конкретных процессов: основного бота и Local Bot API Server.

### Бот (Python процесс)

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `bot_pid` | `pgrep -f {pattern}` | PID процесса бота | - |
| `bot_memory_mb` | `Process.memory_info().rss` | Потребление RAM в MB | Рост = утечка памяти |
| `bot_threads` | `Process.num_threads()` | Количество потоков | Резкий рост = проблема |
| `bot_fd_count` | `Process.num_fds()` | Количество file descriptors | Приближение к лимиту = утечка FD |
| `bot_cpu_percent` | `Process.cpu_percent()` | CPU процесса в % | > 100% = многопоточная нагрузка |

### Local Bot API Server

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `local_server_pid` | `pgrep -f {pattern}` | PID local server | - |
| `local_server_memory_mb` | `Process.memory_info().rss` | RAM в MB | - |
| `local_server_fd_count` | `Process.num_fds()` | Количество FD | Утечка FD при загрузке файлов |

**Настройка паттернов (в .env):**
```env
HEALTH_BOT_PROCESS_PATTERN=python.*bot
HEALTH_LOCAL_SERVER_PATTERN=telegram-bot-api
```

---

## Disk I/O и Swap

Критически важные метрики для диагностики задержек.

### Disk I/O

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `iowait_percent` | `psutil.cpu_times_percent().iowait` | % CPU в ожидании I/O | > 10% - диск тормозит систему |
| `disk_read_bytes` | `psutil.disk_io_counters()` | Прочитано байт (с загрузки) | Абсолютное значение |
| `disk_write_bytes` | `psutil.disk_io_counters()` | Записано байт (с загрузки) | Абсолютное значение |
| `disk_percent` | `psutil.disk_usage('/').percent` | Заполненность диска в % | > 90% - критично |
| `disk_free_gb` | `psutil.disk_usage('/').free` | Свободно на диске в GB | < 5GB - внимание |

### Swap

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `swap_percent` | `psutil.swap_memory().percent` | Использование swap в % | **> 0% = проблема!** |
| `swap_used_mb` | `psutil.swap_memory().used` | Используемый swap в MB | Любое значение > 0 - плохо |
| `swap_total_mb` | `psutil.swap_memory().total` | Общий размер swap в MB | - |
| `swap_sin` | `psutil.swap_memory().sin` | Байт swapped in (с загрузки) | Рост = активный swap |
| `swap_sout` | `psutil.swap_memory().sout` | Байт swapped out (с загрузки) | Рост = активный swap |

**Диагностика:**
- `swap_percent > 0%` - система использует swap, задержки гарантированы
- `iowait_percent > 10%` - диск является узким местом
- `swap_sin/sout растут` - активный swapping прямо сейчас

---

## Сетевые метрики

### Throughput (скорость)

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `net_rx_kb_s` | Дельта `bytes_recv` / время | Входящий трафик KB/s | Резкие скачки |
| `net_tx_kb_s` | Дельта `bytes_sent` / время | Исходящий трафик KB/s | Резкие скачки |
| `net_bytes_sent` | `psutil.net_io_counters()` | Отправлено байт (с загрузки) | Абсолютное |
| `net_bytes_recv` | `psutil.net_io_counters()` | Получено байт (с загрузки) | Абсолютное |

### Ошибки и соединения

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `net_errin` | `psutil.net_io_counters()` | Входящие ошибки | > 0 = проблема сети |
| `net_errout` | `psutil.net_io_counters()` | Исходящие ошибки | > 0 = проблема сети |
| `net_dropin` | `psutil.net_io_counters()` | Dropped входящие | > 0 = потеря пакетов |
| `net_dropout` | `psutil.net_io_counters()` | Dropped исходящие | > 0 = потеря пакетов |
| `close_wait_count` | `ss -tan state close-wait` | CLOSE_WAIT соединения | > 50 = утечка соединений |
| `tcp_connections_total` | `ss -s` | Всего TCP соединений | - |
| `tcp_established` | `ss -s` | Установленные соединения | - |
| `tcp_timewait` | `ss -s` | TIME_WAIT соединения | Много = высокий churn |

**Диагностика:**
- `close_wait_count > 50` - код не закрывает соединения (утечка)
- `net_errin/errout > 0` - проблемы с сетью/драйверами
- `net_rx_kb_s` резко упал до 0 - потеря связи

---

## PostgreSQL

Метрики соединений с базой данных.

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `pg_connections_total` | `pg_stat_activity` | Всего соединений | Приближение к max_connections |
| `pg_connections_active` | `pg_stat_activity WHERE state != 'idle'` | Активные соединения | > 10 = высокая нагрузка на БД |
| `pg_connections_idle` | Вычисляется | Idle соединения | Много idle = возможно утечка |
| `pg_connections_waiting` | `pg_stat_activity WHERE wait_event IS NOT NULL` | Ожидающие соединения | > 0 = contention/блокировки |

**Диагностика:**
- `pg_connections_waiting > 0` - запросы ждут блокировок
- `pg_connections_total` близко к `max_connections` - пул исчерпан

---

## PostgreSQL Diagnostics

Расширенная диагностика PostgreSQL для выявления проблем с блокировками и долгими запросами.
Метрики собираются через существующий `async_session` (SQLAlchemy).

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `pg_blocked_queries` | `pg_stat_activity` + `pg_locks` | Количество заблокированных запросов | > 0 - есть блокировки! |
| `pg_long_queries` | `pg_stat_activity WHERE query_start < now() - 5s` | Запросы > 5 секунд | > 0 - долгие запросы |
| `pg_idle_in_transaction` | `pg_stat_activity WHERE state = 'idle in transaction'` | Зависшие транзакции (> 30 сек) | > 0 - незакрытые транзакции |
| `pg_lock_waits` | `pg_stat_activity WHERE wait_event_type = 'Lock'` | Процессы, ожидающие блокировки | > 0 - contention |
| `pg_oldest_query_seconds` | `MIN(query_start)` | Время самого долгого запроса в секундах | > 5 - внимание, > 30 - проблема |

**SQL запросы, которые выполняются:**

```sql
-- Заблокированные запросы (blocked_queries)
SELECT count(*) FROM pg_stat_activity blocked
JOIN pg_locks bl ON bl.pid = blocked.pid
JOIN pg_locks bl2 ON ... -- полный join для поиска блокирующих
WHERE NOT bl.granted;

-- Долгие запросы (> 5 сек)
SELECT count(*) FROM pg_stat_activity
WHERE state = 'active'
AND query_start < now() - interval '5 seconds';

-- Idle in transaction (> 30 сек)
SELECT count(*) FROM pg_stat_activity
WHERE state = 'idle in transaction'
AND xact_start < now() - interval '30 seconds';

-- Ожидающие блокировки
SELECT count(*) FROM pg_stat_activity
WHERE wait_event_type = 'Lock' AND state = 'active';

-- Самый долгий запрос
SELECT EXTRACT(EPOCH FROM (now() - min(query_start)))::integer
FROM pg_stat_activity WHERE state = 'active';
```

**Диагностика:**

| Сценарий | Что происходит | Решение |
|----------|---------------|---------|
| `pg_blocked_queries > 0` | Запросы блокируют друг друга | Проверить транзакции, индексы |
| `pg_long_queries > 0` | Долгие запросы нагружают БД | Оптимизировать SQL, добавить индексы |
| `pg_idle_in_transaction > 0` | Незакрытые транзакции держат блокировки | Найти код, не вызывающий commit/rollback |
| `pg_lock_waits > 0` | Contention - много процессов ждут | Уменьшить конкуренцию, проверить индексы |
| `pg_oldest_query_seconds > 30` | Очень долгий запрос | Убить запрос или оптимизировать |

**Ручная проверка при проблемах:**

```bash
# Посмотреть все активные запросы
sudo -u postgres psql -c "SELECT pid, now() - query_start AS duration, state, query FROM pg_stat_activity WHERE state != 'idle' ORDER BY query_start;"

# Посмотреть блокировки
sudo -u postgres psql -c "SELECT blocked.pid, blocked.query, blocking.pid AS blocking_pid, blocking.query AS blocking_query FROM pg_stat_activity blocked JOIN pg_locks bl ON bl.pid = blocked.pid JOIN pg_locks bl2 ON bl2.locktype = bl.locktype AND bl2.pid != bl.pid JOIN pg_stat_activity blocking ON bl2.pid = blocking.pid WHERE NOT bl.granted;"

# Убить долгий запрос
sudo -u postgres psql -c "SELECT pg_terminate_backend(PID);"
```

---

## Сессии обработки

Метрики из таблицы `processing_sessions`.

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `active_processing_sessions` | `processing_sessions WHERE final_status IS NULL` | Активные сессии | Много = высокая нагрузка |
| `processing_sessions_last_hour` | `processing_sessions WHERE started_at > NOW() - 1h` | Сессии за час | Показывает объем работы |
| `ffmpeg_processes` | `pgrep -c ffmpeg` | Процессы ffmpeg | Много = конверсия файлов |
| `ffprobe_processes` | `pgrep -c ffprobe` | Процессы ffprobe | - |

---

## Внутренние метрики бота

Метрики изнутри Python процесса бота. Собираются через HTTP endpoint `http://127.0.0.1:3000/metrics`.

### Event Loop Lag (критично!)

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `bot_event_loop_lag_ms` | `asyncio.call_soon()` timing | Текущая задержка event loop в ms | > 50ms - проблема |
| `bot_event_loop_lag_max_ms` | История замеров | Максимальная задержка за период | Показывает пики |
| `bot_event_loop_lag_avg_ms` | История замеров | Средняя задержка за период | - |

**Как измеряется:**
1. Планируем callback через `loop.call_soon()`
2. Замеряем реальное время до его выполнения
3. Разница = lag

**Диагностика:**
- `> 50ms` - event loop перегружен
- `> 100ms` - критическая проблема, бот будет тормозить
- `> 1000ms` - бот "завис", возможно blocking I/O в коде

### GC (Garbage Collection)

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `bot_gc_gen0` | `gc.get_count()[0]` | Объекты в поколении 0 | - |
| `bot_gc_gen1` | `gc.get_count()[1]` | Объекты в поколении 1 | - |
| `bot_gc_gen2` | `gc.get_count()[2]` | Объекты в поколении 2 | - |
| `bot_gc_objects` | `len(gc.get_objects())` | Всего отслеживаемых объектов | Постоянный рост = утечка |

### Потоки и Tasks

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `bot_thread_count` | `threading.enumerate()` | Количество потоков | Рост = возможно не завершаются |
| `bot_asyncio_tasks` | `asyncio.all_tasks()` | Всего asyncio tasks | - |
| `bot_asyncio_pending` | Tasks где `not done()` | Pending tasks | Постоянный рост = утечка tasks |

### Uptime

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `bot_uptime_seconds` | `datetime.utcnow() - start_time` | Время работы бота в секундах | - |

---

## Telegram API Latency

Метрики latency Telegram API через Local Bot API Server. Собираются из основного бота (`internal_metrics.py`).

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `telegram_api_latency_ms` | `bot.get_me()` через Local Server | Текущая latency API в ms | > 1000ms - проблема |
| `telegram_api_latency_avg_ms` | История замеров | Средняя latency за период | Тренд |
| `telegram_api_latency_max_ms` | История замеров | Максимальная latency | Пики |
| `telegram_api_error` | Ошибка запроса | Текст ошибки (если есть) | Наличие = проблема |

**Путь запроса:**
```
Bot → Local Bot API Server → Telegram Servers → обратно
```

**Диагностика:**
- `> 500ms` - повышенная latency, возможны задержки
- `> 1000ms` - высокая latency, будет влиять на response time
- `telegram_api_error` - проблемы с соединением

---

## Direct API (обход Local Server)

Метрики прямого доступа к Telegram API, минуя Local Bot API Server. Собираются из `metrics_collector.py`.

### Базовые метрики (getMe)

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `direct_api_connectivity_ms` | HTTPS GET к `api.telegram.org/` | Ping к серверам Telegram | Базовая сетевая связность |
| `direct_api_getme_ms` | HTTPS GET к `api.telegram.org/bot.../getMe` | getMe через прямой API | Сравнить с `telegram_api_latency_ms` |

### Direct API Ping Test (send + delivery)

Тест отправки сообщения через прямой API и получения через Telethon.

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `direct_api_send_ms` | HTTP POST sendMessage | Время ответа API на отправку | Время до HTTP 200 |
| `direct_api_delivery_ms` | Telethon получение | Полное время до получения | Общая задержка доставки |

**Путь запроса (Direct API):**
```
Health Monitor → api.telegram.org → Telegram Servers → Telethon (получение)
```

**Сравнение для диагностики:**

| Сценарий | `telegram_api_latency` | `direct_api_getme` | Вывод |
|----------|----------------------|-------------------|-------|
| Оба быстрые | < 500ms | < 500ms | Всё ОК |
| Local медленный, Direct быстрый | > 1000ms | < 500ms | Проблема в Local Bot API Server |
| Оба медленные | > 1000ms | > 1000ms | Проблема в Telegram или сети |
| Local быстрый, бот медленный | < 500ms | < 500ms | Проблема в коде бота |

**Когда запускается Direct API Ping Test:**
- Автоматически при response_time > 5000ms (в manual check)
- Позволяет понять, проблема в Local Server или в Telegram

---

## Rolling Average

Скользящее среднее времени ответа за последние 30 секунд.

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `response_time_avg_30s` | Среднее по истории | Средний response time за 30 сек | Тренд |
| `response_time_max_30s` | Максимум по истории | Максимальный response time | Пики |
| `response_time_min_30s` | Минимум по истории | Минимальный response time | - |
| `response_time_samples_30s` | Количество замеров | Сколько замеров в окне | - |

**Использование:**
- Показывает не только текущий response time, но и тренд
- Позволяет понять, проблема разовая или систематическая
- При частых проверках (каждые 5 мин) накапливается история

---

## Расширенные метрики

Собираются только когда `extended=True` (по threshold или всегда если `HEALTH_EXTENDED_METRICS_THRESHOLD_MS=0`).

### FD Limits

| Метрика | Источник | Описание | На что обратить внимание |
|---------|----------|----------|-------------------------|
| `bot_fd_limit` | `/proc/{pid}/limits` | Soft limit FD для бота | - |
| `bot_fd_used_percent` | Вычисляется | % использования лимита | > 80% - критично |
| `local_server_fd_limit` | `/proc/{pid}/limits` | Soft limit для local server | - |
| `local_server_fd_used_percent` | Вычисляется | % использования | > 80% - критично |
| `system_fd_allocated` | `/proc/sys/fs/file-nr` | Выделено FD в системе | - |
| `system_fd_max` | `/proc/sys/fs/file-nr` | Максимум FD в системе | - |
| `system_fd_used_percent` | Вычисляется | % системного лимита | - |

### lsof Top

| Метрика | Источник | Описание |
|---------|----------|----------|
| `lsof_top_processes` | `lsof -nP \| awk \| sort \| uniq -c` | Топ-15 процессов по FD |

Формат: `[{"process": "python", "fd_count": 1234}, ...]`

### Bot FD Types

| Метрика | Источник | Описание |
|---------|----------|----------|
| `bot_fd_types` | `/proc/{pid}/fd` + анализ | Типы FD у бота |

Формат: `[{"type": "socket:[...]", "count": 50}, {"type": "pipe:[...]", "count": 20}, ...]`

---

## Диагностика проблем

### Бот отвечает медленно (> 5 сек)

**Что проверить:**
1. `bot_event_loop_lag_ms` - если > 50ms, event loop перегружен
2. `iowait_percent` - если > 10%, диск тормозит
3. `swap_percent` - если > 0%, swap = гарантированные задержки
4. `pg_connections_waiting` - если > 0, БД блокирует
5. `pg_blocked_queries` - если > 0, есть блокировки в БД
6. `pg_long_queries` - если > 0, есть долгие запросы
7. `pg_oldest_query_seconds` - если > 5, проверить что за запрос
8. `cpu_percent` + `load_avg` - CPU перегружен?

### Бот не отвечает (timeout)

**Что проверить:**
1. `bot_event_loop_lag_ms` - если > 1000ms, blocking I/O
2. `bot_asyncio_pending` - много pending tasks = deadlock?
3. `close_wait_count` - много = сетевые проблемы
4. `net_errin/errout` - сетевые ошибки

### Память растет постоянно

**Что проверить:**
1. `bot_memory_mb` - сравнить с предыдущими замерами
2. `bot_gc_objects` - постоянный рост = утечка объектов
3. `bot_fd_count` - рост FD часто сопровождает утечку памяти

### FD исчерпаны

**Что проверить:**
1. `bot_fd_used_percent` - близко к 100%?
2. `bot_fd_types` - какие типы FD доминируют?
3. `lsof_top_processes` - какой процесс "съел" FD?
4. `close_wait_count` - незакрытые соединения?

---

## Конфигурация

Настройки в `.env`:

```env
# Включить сбор метрик
HEALTH_COLLECT_METRICS=true

# Threshold для расширенных метрик (0 = всегда собирать)
HEALTH_EXTENDED_METRICS_THRESHOLD_MS=0

# Паттерны для поиска процессов
HEALTH_BOT_PROCESS_PATTERN=python.*bot
HEALTH_LOCAL_SERVER_PATTERN=telegram-bot-api
```

---

## Примеры анализа

### Пример 1: Медленный ответ из-за swap

```json
{
  "response_time_ms": 8500,
  "cpu_percent": 45.2,
  "memory_percent": 94.1,
  "swap_percent": 12.3,
  "swap_used_mb": 512,
  "iowait_percent": 8.2
}
```

**Диагноз:** Память почти исчерпана (94%), система использует swap (12%), что вызывает I/O wait (8.2%). Решение: добавить RAM или оптимизировать потребление памяти.

### Пример 2: Блокировка event loop

```json
{
  "response_time_ms": 15000,
  "bot_event_loop_lag_ms": 2500,
  "bot_event_loop_lag_max_ms": 5200,
  "cpu_percent": 25.1,
  "memory_percent": 45.0
}
```

**Диагноз:** Event loop заблокирован (lag 2.5 сек), хотя CPU и память в норме. Скорее всего, в коде есть blocking I/O (синхронные вызовы). Нужен profiling с `py-spy`.

### Пример 3: Утечка соединений

```json
{
  "response_time_ms": 3200,
  "close_wait_count": 156,
  "bot_fd_count": 892,
  "bot_fd_used_percent": 87.1
}
```

**Диагноз:** Много CLOSE_WAIT соединений (156), FD близки к лимиту (87%). Код не закрывает соединения. Нужно найти место утечки в коде.

### Пример 4: Блокировки в БД

```json
{
  "response_time_ms": 12000,
  "bot_event_loop_lag_ms": 0.02,
  "pg_blocked_queries": 3,
  "pg_long_queries": 2,
  "pg_idle_in_transaction": 1,
  "pg_lock_waits": 2,
  "pg_oldest_query_seconds": 15,
  "cpu_percent": 15.0,
  "memory_percent": 45.0
}
```

**Диагноз:** Event loop в норме (0.02ms), CPU/память тоже. Но есть 3 заблокированных запроса, 2 долгих запроса и 1 зависшая транзакция. Самый долгий запрос идёт 15 секунд. Проблема в БД - нужно проверить блокировки командой `sudo -u postgres psql -c "SELECT * FROM pg_stat_activity WHERE state != 'idle';"` и найти проблемный код.

---

*Документация для Health Monitor v1.8.0*
