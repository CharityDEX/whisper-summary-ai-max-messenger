import asyncio
import logging
import time
from typing import Dict, Optional

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from fluentogram import TranslatorRunner

from keyboards.user_keyboards import inline_cancel_queue

logger = logging.getLogger(__name__)


class AudioQueueManager:
    """Менеджер очередей аудио обработки для пользователей"""
    
    def __init__(self):
        # Словарь очередей для каждого пользователя: user_id -> asyncio.Queue
        self.user_queues: Dict[int, asyncio.Queue] = {}
        # Словарь воркеров для каждого пользователя: user_id -> asyncio.Task
        self.user_workers: Dict[int, asyncio.Task] = {}
        # Флаги активной обработки: user_id -> bool
        self.is_processing: Dict[int, bool] = {}
        # Блокировки для синхронизации доступа к очередям: user_id -> asyncio.Lock
        self.user_locks: Dict[int, asyncio.Lock] = {}
        # Счетчик для упорядочивания сообщений: user_id -> int
        self.message_counters: Dict[int, int] = {}
        # Состояние сбора сообщений для батчевой обработки
        self.collection_active: Dict[int, bool] = {}
        self.collection_buffers: Dict[int, list] = {}
        self.collection_tasks: Dict[int, asyncio.Task] = {}
    
    async def add_to_queue(self, user_id: int, message: Message, state: FSMContext, i18n: TranslatorRunner, language_code: str | None = None) -> tuple[bool, Optional[Message]]:
        """
        Добавляет сообщение в очередь пользователя с правильной синхронизацией.
        
        Returns:
            tuple[bool, Optional[Message]]: (True если добавлено в очередь, queue_message или None)
        """
        # Создаем блокировку для пользователя, если её нет
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()
        
        # Инициализируем счетчик сообщений для пользователя
        if user_id not in self.message_counters:
            self.message_counters[user_id] = 0
        
        # Используем блокировку для синхронизации доступа
        async with self.user_locks[user_id]:
            # Увеличиваем счетчик сообщений для правильного упорядочивания
            self.message_counters[user_id] += 1
            message_order = self.message_counters[user_id]
            
            # Создаем очередь для пользователя, если её нет
            if user_id not in self.user_queues:
                self.user_queues[user_id] = asyncio.Queue()
            
            # Проверяем, обрабатывается ли что-то в данный момент
            is_currently_processing = self.is_processing.get(user_id, False)
            queue_not_empty = not self.user_queues[user_id].empty()
            
            if is_currently_processing or queue_not_empty:
                # Проверяем, нужно ли запустить окно сбора для батчевой обработки
                collection_needed = (
                    is_currently_processing and  # Кто-то обрабатывается (значит, это 2+ сообщение)
                    not self.collection_active.get(user_id, False)  # Сбор еще не активен
                )
                
                if collection_needed:
                    # Запускаем окно сбора сообщений
                    await self._start_collection_window(user_id, message, state, i18n, language_code)
                    return True, None  # Возвращаем True, но без queue_message (он будет отправлен позже)
                elif self.collection_active.get(user_id, False):
                    # Добавляем в буфер сбора
                    await self._add_to_collection_buffer(user_id, message, state, i18n, language_code)
                    return True, None  # Возвращаем True, но без queue_message (он будет отправлен позже)
                else:
                    # Обычное добавление в очередь (для случаев без батчевой обработки)
                    queue_item = {
                        'message': message,
                        'state': state,
                        'i18n': i18n,
                        'language_code': language_code,
                        'queue_message': None,
                        'order': message_order,
                        'timestamp': time.time()
                    }
                    await self.user_queues[user_id].put(queue_item)
                    
                    # Получаем размер очереди для уведомления (добавляем 1, так как текущий файл тоже в очереди)
                    queue_size = self.user_queues[user_id].qsize()
                    if is_currently_processing:
                        queue_size += 1  # Добавляем 1, если что-то уже обрабатывается
                    
                    if queue_size == 2:
                        queue_text = i18n.audio_added_to_queue_first(position=queue_size)
                    else:
                        queue_text = i18n.audio_added_to_queue(position=queue_size)
                    
                    queue_message = await message.reply(
                        text=queue_text,
                        reply_markup=inline_cancel_queue(i18n=i18n, message_id=message.message_id)
                    )
                    
                    queue_item['queue_message'] = queue_message
                    
                    logger.info(f"Added audio to queue for user {user_id}, queue size: {queue_size}, order: {message_order}")
                    return True, queue_message
            
            # Если пользователь не обрабатывает аудио и очередь пуста, помечаем как обрабатывающего
            # и возвращаем False для прямой обработки
            self.is_processing[user_id] = True
            logger.debug(f"User {user_id} started processing (not queued), order: {message_order}")
            return False, None
    
    async def finish_processing(self, user_id: int):
        """Уведомляет менеджер о завершении обработки текущего файла"""
        # Используем блокировку для синхронизации
        if user_id in self.user_locks:
            async with self.user_locks[user_id]:
                self.is_processing[user_id] = False
                logger.info(f"Finished processing for user {user_id}")
                
                # Запускаем воркер для обработки очереди, если есть файлы в очереди
                if user_id in self.user_queues and not self.user_queues[user_id].empty():
                    if user_id not in self.user_workers or self.user_workers[user_id].done():
                        logger.debug(f"Starting queue worker for user {user_id}")
                        self.user_workers[user_id] = asyncio.create_task(
                            self._process_queue_worker(user_id)
                        )
        else:
            # Fallback для случаев, когда блокировка еще не создана
            self.is_processing[user_id] = False
            logger.info(f"Finished processing for user {user_id}")
            
            if user_id in self.user_queues and not self.user_queues[user_id].empty():
                if user_id not in self.user_workers or self.user_workers[user_id].done():
                    logger.debug(f"Starting queue worker for user {user_id}")
                    self.user_workers[user_id] = asyncio.create_task(
                        self._process_queue_worker(user_id)
                    )
    
    async def _process_queue_worker(self, user_id: int):
        """Воркер для обработки очереди пользователя"""
        try:
            logger.debug(f"Queue worker started for user {user_id}")
            
            # Обрабатываем очередь (воркер запускается только после завершения текущей обработки)
            if user_id in self.user_queues:
                queue = self.user_queues[user_id]
                
                while not queue.empty():
                    try:
                        # Получаем следующий элемент из очереди
                        queue_item = await queue.get()
                        try:
                            if queue_item['i18n'] and user_id in self.user_queues and not self.user_queues[user_id].empty():
                                await self.update_queue_count_in_messages(user_id, queue_item['i18n'])
                        except Exception as e:
                            logger.error(f"Error updating queue count in messages for user {user_id}: {e}")

                        message = queue_item['message']
                        state = queue_item['state']
                        i18n = queue_item['i18n']
                        language_code = queue_item.get('language_code')
                        queue_message = queue_item.get('queue_message')
                        # Помечаем как обрабатывающийся
                        self.is_processing[user_id] = True
                        
                        # Импортируем функцию обработки
                        from handlers.user_handlers import _process_audio_internal
                        
                        # Обрабатываем аудио с переданным языком
                        await _process_audio_internal(message, state, i18n, language_code, queue_message)
                        
                        # Помечаем как завершенный
                        self.is_processing[user_id] = False
                        
                        # Отмечаем задачу как выполненную
                        queue.task_done()
                        
                        logger.info(f"Processed queued audio for user {user_id}")
                        
                    except Exception as e:
                        logger.error(f"Error processing queued audio for user {user_id}: {e}")
                        self.is_processing[user_id] = False
                        
                        # Уведомляем пользователя об ошибке
                        try:
                            await message.answer(text=i18n.something_went_wrong())
                        except Exception:
                            pass
        
        except Exception as e:
            logger.error(f"Error in queue worker for user {user_id}: {e}")
        finally:
            # Очищаем флаг обработки
            self.is_processing[user_id] = False
    
    def get_queue_size(self, user_id: int) -> int:
        """Возвращает размер очереди пользователя"""
        if user_id in self.user_queues:
            return self.user_queues[user_id].qsize()
        return 0
    
    def is_user_processing(self, user_id: int) -> bool:
        """Проверяет, обрабатывает ли пользователь аудио в данный момент"""
        return self.is_processing.get(user_id, False)

    async def clear_queue(self, user_id: int) -> bool:
        """
        Очищает очередь пользователя.
        
        Returns:
            bool: True если очередь была непустой, False если была пустой
        """
        # Создаем блокировку для пользователя, если её нет
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()
        
        async with self.user_locks[user_id]:
            if user_id in self.user_queues:
                queue = self.user_queues[user_id]
                was_not_empty = not queue.empty()
                
                # Очищаем очередь
                while not queue.empty():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                
                # Удаляем очередь из словаря
                del self.user_queues[user_id]
                
                # Останавливаем воркер, если он запущен
                if user_id in self.user_workers and not self.user_workers[user_id].done():
                    self.user_workers[user_id].cancel()
                    del self.user_workers[user_id]
                
                # Сбрасываем флаг обработки и счетчик
                self.is_processing[user_id] = False
                self.message_counters[user_id] = 0
                
                # Очищаем состояние сбора, если активно
                if user_id in self.collection_tasks and not self.collection_tasks[user_id].done():
                    self.collection_tasks[user_id].cancel()
                    del self.collection_tasks[user_id]
                
                self.collection_active[user_id] = False
                if user_id in self.collection_buffers:
                    del self.collection_buffers[user_id]
                
                logger.info(f"Cleared queue for user {user_id}, was not empty: {was_not_empty}")
                return was_not_empty
            
            return False

    async def remove_from_queue(self, user_id: int, message_id: int) -> bool:
        """
        Удаляет конкретный объект из очереди пользователя по message_id.
        
        Args:
            user_id: ID пользователя
            message_id: ID сообщения для удаления
            
        Returns:
            bool: True если объект был найден и удален, False если не найден
        """
        # Создаем блокировку для пользователя, если её нет
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()
        
        async with self.user_locks[user_id]:
            if user_id not in self.user_queues:
                logger.debug(f"No queue found for user {user_id}")
                return False
            
            queue = self.user_queues[user_id]
            if queue.empty():
                logger.debug(f"Queue is empty for user {user_id}")
                return False
            
            # Создаем временный список для хранения элементов
            temp_items = []
            found = False
            
            # Извлекаем все элементы из очереди
            while not queue.empty():
                try:
                    item = queue.get_nowait()
                    # Проверяем, является ли это элемент, который нужно удалить
                    if item.get('message') and item['message'].message_id == message_id:
                        found = True
                        logger.info(f"Removed message {message_id} from queue for user {user_id}")
                        # Отмечаем задачу как выполненную для удаляемого элемента
                        queue.task_done()
                    else:
                        # Сохраняем элемент для возврата в очередь
                        temp_items.append(item)
                except asyncio.QueueEmpty:
                    break
            
            # Возвращаем все элементы обратно в очередь (кроме удаленного)
            for item in temp_items:
                queue.put_nowait(item)
            
            return found

    def get_queue_items(self, user_id: int) -> list:
        """
        Возвращает список элементов в очереди пользователя без их удаления.
        
        Args:
            user_id: ID пользователя
            
        Returns:
            list: Список элементов очереди
        """
        if user_id not in self.user_queues:
            return []
        
        queue = self.user_queues[user_id]
        if queue.empty():
            return []
        
        # Создаем временный список для хранения элементов
        temp_items = []
        
        # Извлекаем все элементы из очереди
        while not queue.empty():
            try:
                item = queue.get_nowait()
                temp_items.append(item)
            except asyncio.QueueEmpty:
                break
        
        # Возвращаем все элементы обратно в очередь
        for item in temp_items:
            queue.put_nowait(item)
        
        return temp_items

    async def update_queue_count_in_messages(self, user_id: int, i18n: TranslatorRunner):
        """
        Обновляет номер позиции в очереди в сообщениях пользователя.
        """
        if user_id not in self.user_queues:
            return False

        queue = self.user_queues[user_id]
        if queue.empty():
            return False

        # Получаем все элементы очереди без их удаления
        queue_items = self.get_queue_items(user_id)

        for index, item in enumerate(queue_items):
            if item.get('queue_message'):
                try:
                    await item['queue_message'].edit_text(text=i18n.audio_added_to_queue(position=index + 2),
                                                      reply_markup=inline_cancel_queue(i18n=i18n, message_id=item['message'].message_id))
                except TelegramBadRequest as e:
                    if 'message is not modified' not in str(e):
                        logger.warning(f"Failed to edit queue message: {e}")


        return True
    
    async def _start_collection_window(self, user_id: int, message: Message, state: FSMContext, i18n: TranslatorRunner, language_code: str | None = None):
        """
        Запускает окно сбора сообщений для батчевой обработки.
        Собирает сообщения в течение короткого времени, затем сортирует их по message_id.
        """
        # Помечаем, что сбор активен
        self.collection_active[user_id] = True
        self.collection_buffers[user_id] = []
        
        # Добавляем текущее сообщение в буфер
        collection_item = {
            'message': message,
            'state': state,
            'i18n': i18n,
            'language_code': language_code,
            'message_id': message.message_id,
            'timestamp': time.time()
        }
        self.collection_buffers[user_id].append(collection_item)
        
        logger.info(f"Started collection window for user {user_id}")
        
        # Запускаем задачу сбора с таймаутом
        self.collection_tasks[user_id] = asyncio.create_task(
            self._collection_window_worker(user_id)
        )
    
    async def _collection_window_worker(self, user_id: int):
        """
        Воркер окна сбора сообщений. Ждет определенное время, затем обрабатывает собранные сообщения.
        """
        initial_delay = 0.1  # 100ms начальная задержка
        max_total_delay = 0.2  # максимум 200ms общей задержки
        start_time = time.time()
        
        try:
            while time.time() - start_time < max_total_delay:
                await asyncio.sleep(initial_delay)
                
                # Проверяем, не добавились ли новые сообщения за последние 50ms
                if user_id in self.collection_buffers:
                    buffer = self.collection_buffers[user_id]
                    if buffer:
                        latest_timestamp = max(item['timestamp'] for item in buffer)
                        if time.time() - latest_timestamp < 0.05:  # Последнее сообщение было меньше 50ms назад
                            continue  # Продолжаем ждать
                
                # Если новых сообщений нет, завершаем сбор
                break
            
            # Обрабатываем собранные сообщения
            await self._process_collected_messages(user_id)
            
        except Exception as e:
            logger.error(f"Error in collection window worker for user {user_id}: {e}")
            # В случае ошибки все равно обрабатываем то, что собрали
            await self._process_collected_messages(user_id)
        finally:
            # Очищаем состояние сбора
            self.collection_active[user_id] = False
            if user_id in self.collection_buffers:
                del self.collection_buffers[user_id]
            if user_id in self.collection_tasks:
                del self.collection_tasks[user_id]
    
    async def _process_collected_messages(self, user_id: int):
        """
        Обрабатывает собранные сообщения: сортирует по message_id и добавляет в очередь.
        """
        if user_id not in self.collection_buffers:
            return
        
        buffer = self.collection_buffers[user_id]
        if not buffer:
            return
        
        # Сортируем по message_id для правильного порядка
        buffer.sort(key=lambda x: x['message_id'])
        
        logger.info(f"Processing {len(buffer)} collected messages for user {user_id}")
        
        # Используем блокировку для добавления в очередь
        async with self.user_locks[user_id]:
            # Создаем очередь если её нет
            if user_id not in self.user_queues:
                self.user_queues[user_id] = asyncio.Queue()
            
            queue = self.user_queues[user_id]
            
            # Добавляем все сообщения в правильном порядке
            for i, item in enumerate(buffer):
                self.message_counters[user_id] += 1
                message_order = self.message_counters[user_id]
                
                queue_item = {
                    'message': item['message'],
                    'state': item['state'],
                    'i18n': item['i18n'],
                    'language_code': item['language_code'],
                    'queue_message': None,
                    'order': message_order,
                    'timestamp': item['timestamp']
                }
                
                await queue.put(queue_item)
                
                # Вычисляем позицию в очереди (учитывая обрабатывающееся сообщение)
                queue_size = queue.qsize()
                if self.is_processing.get(user_id, False):
                    queue_position = queue_size
                else:
                    queue_position = queue_size
                
                # Отправляем уведомление о позиции в очереди
                if queue_position == 1:
                    queue_text = item['i18n'].audio_added_to_queue_first(position=queue_position + 1)
                else:
                    queue_text = item['i18n'].audio_added_to_queue(position=queue_position + 1)
                
                try:
                    queue_message = await item['message'].reply(
                        text=queue_text,
                        reply_markup=inline_cancel_queue(i18n=item['i18n'], message_id=item['message'].message_id)
                    )
                    queue_item['queue_message'] = queue_message
                except Exception as e:
                    logger.error(f"Failed to send queue message for user {user_id}: {e}")
                
                logger.info(f"Added collected message {item['message_id']} to queue for user {user_id}, position: {queue_position + 1}")
    
    async def _add_to_collection_buffer(self, user_id: int, message: Message, state: FSMContext, i18n: TranslatorRunner, language_code: str | None = None):
        """
        Добавляет сообщение в буфер сбора.
        """
        if user_id not in self.collection_buffers:
            self.collection_buffers[user_id] = []
        
        collection_item = {
            'message': message,
            'state': state,
            'i18n': i18n,
            'language_code': language_code,
            'message_id': message.message_id,
            'timestamp': time.time()
        }
        
        self.collection_buffers[user_id].append(collection_item)
        logger.debug(f"Added message {message.message_id} to collection buffer for user {user_id}")


# Создаем глобальный экземпляр менеджера очередей
audio_queue_manager = AudioQueueManager()
