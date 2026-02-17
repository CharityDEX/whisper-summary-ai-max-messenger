"""
Audio queue manager for Max messenger bot.

Mirrors services/audio_queue_service.py but uses maxapi types:
- maxapi Message instead of aiogram Message
- MemoryContext instead of FSMContext
- max_keyboards instead of keyboards
- max_handlers instead of handlers
"""

import asyncio
import logging
import time
from typing import Dict, Optional

from fluentogram import TranslatorRunner

from max_keyboards.user_keyboards import inline_cancel_queue
from services.init_max_bot import max_bot

logger = logging.getLogger(__name__)


class MaxAudioQueueManager:
    """Audio queue manager for Max messenger — same logic as AudioQueueManager."""

    def __init__(self):
        self.user_queues: Dict[int, asyncio.Queue] = {}
        self.user_workers: Dict[int, asyncio.Task] = {}
        self.is_processing: Dict[int, bool] = {}
        self.user_locks: Dict[int, asyncio.Lock] = {}
        self.message_counters: Dict[int, int] = {}
        self.collection_active: Dict[int, bool] = {}
        self.collection_buffers: Dict[int, list] = {}
        self.collection_tasks: Dict[int, asyncio.Task] = {}

    async def add_to_queue(
        self,
        user_id: int,
        message,
        context,
        i18n: TranslatorRunner,
        language_code: str | None = None,
        media_data: dict | None = None,
    ) -> tuple[bool, Optional[object]]:
        """
        Adds a message to the user's audio queue.

        Returns:
            tuple[bool, Optional[Message]]: (True if queued, queue_message or None)
        """
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()

        if user_id not in self.message_counters:
            self.message_counters[user_id] = 0

        async with self.user_locks[user_id]:
            self.message_counters[user_id] += 1
            message_order = self.message_counters[user_id]

            if user_id not in self.user_queues:
                self.user_queues[user_id] = asyncio.Queue()

            is_currently_processing = self.is_processing.get(user_id, False)
            queue_not_empty = not self.user_queues[user_id].empty()

            if is_currently_processing or queue_not_empty:
                collection_needed = (
                    is_currently_processing
                    and not self.collection_active.get(user_id, False)
                )

                if collection_needed:
                    await self._start_collection_window(
                        user_id, message, context, i18n, language_code, media_data
                    )
                    return True, None
                elif self.collection_active.get(user_id, False):
                    await self._add_to_collection_buffer(
                        user_id, message, context, i18n, language_code, media_data
                    )
                    return True, None
                else:
                    queue_item = {
                        'message': message,
                        'context': context,
                        'i18n': i18n,
                        'language_code': language_code,
                        'media_data': media_data,
                        'queue_message': None,
                        'order': message_order,
                        'timestamp': time.time(),
                    }
                    await self.user_queues[user_id].put(queue_item)

                    queue_size = self.user_queues[user_id].qsize()
                    if is_currently_processing:
                        queue_size += 1

                    if queue_size == 2:
                        queue_text = i18n.audio_added_to_queue_first(position=queue_size)
                    else:
                        queue_text = i18n.audio_added_to_queue(position=queue_size)

                    queue_message = await self._reply_with_queue_keyboard(
                        message, queue_text, i18n
                    )
                    queue_item['queue_message'] = queue_message

                    logger.info(
                        f"Added audio to queue for user {user_id}, "
                        f"queue size: {queue_size}, order: {message_order}"
                    )
                    return True, queue_message

            # Not queued — process directly
            self.is_processing[user_id] = True
            logger.debug(f"User {user_id} started processing (not queued), order: {message_order}")
            return False, None

    async def finish_processing(self, user_id: int):
        """Notify manager that current file processing is done."""
        if user_id in self.user_locks:
            async with self.user_locks[user_id]:
                self.is_processing[user_id] = False
                logger.info(f"Finished processing for user {user_id}")

                if user_id in self.user_queues and not self.user_queues[user_id].empty():
                    if user_id not in self.user_workers or self.user_workers[user_id].done():
                        logger.debug(f"Starting queue worker for user {user_id}")
                        self.user_workers[user_id] = asyncio.create_task(
                            self._process_queue_worker(user_id)
                        )
        else:
            self.is_processing[user_id] = False
            logger.info(f"Finished processing for user {user_id}")

            if user_id in self.user_queues and not self.user_queues[user_id].empty():
                if user_id not in self.user_workers or self.user_workers[user_id].done():
                    logger.debug(f"Starting queue worker for user {user_id}")
                    self.user_workers[user_id] = asyncio.create_task(
                        self._process_queue_worker(user_id)
                    )

    async def _process_queue_worker(self, user_id: int):
        """Worker that processes queued audio for a user."""
        try:
            logger.debug(f"Queue worker started for user {user_id}")

            if user_id in self.user_queues:
                queue = self.user_queues[user_id]

                while not queue.empty():
                    try:
                        queue_item = await queue.get()
                        try:
                            if queue_item['i18n'] and user_id in self.user_queues and not self.user_queues[user_id].empty():
                                await self.update_queue_count_in_messages(user_id, queue_item['i18n'])
                        except Exception as e:
                            logger.error(f"Error updating queue count for user {user_id}: {e}")

                        message = queue_item['message']
                        context = queue_item['context']
                        i18n = queue_item['i18n']
                        language_code = queue_item.get('language_code')
                        queue_message = queue_item.get('queue_message')
                        media_data = queue_item.get('media_data')

                        self.is_processing[user_id] = True

                        from max_handlers.user_handlers import _process_audio_internal

                        await _process_audio_internal(
                            message=message,
                            context=context,
                            i18n=i18n,
                            language_code=language_code,
                            queue_message=queue_message,
                            media_data=media_data,
                        )

                        self.is_processing[user_id] = False
                        queue.task_done()

                        logger.info(f"Processed queued audio for user {user_id}")

                    except Exception as e:
                        logger.error(f"Error processing queued audio for user {user_id}: {e}")
                        self.is_processing[user_id] = False

                        try:
                            await message.answer(text=i18n.something_went_wrong())
                        except Exception:
                            pass

        except Exception as e:
            logger.error(f"Error in queue worker for user {user_id}: {e}")
        finally:
            self.is_processing[user_id] = False

    def get_queue_size(self, user_id: int) -> int:
        if user_id in self.user_queues:
            return self.user_queues[user_id].qsize()
        return 0

    def is_user_processing(self, user_id: int) -> bool:
        return self.is_processing.get(user_id, False)

    async def clear_queue(self, user_id: int) -> bool:
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()

        async with self.user_locks[user_id]:
            if user_id in self.user_queues:
                queue = self.user_queues[user_id]
                was_not_empty = not queue.empty()

                while not queue.empty():
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                del self.user_queues[user_id]

                if user_id in self.user_workers and not self.user_workers[user_id].done():
                    self.user_workers[user_id].cancel()
                    del self.user_workers[user_id]

                self.is_processing[user_id] = False
                self.message_counters[user_id] = 0

                if user_id in self.collection_tasks and not self.collection_tasks[user_id].done():
                    self.collection_tasks[user_id].cancel()
                    del self.collection_tasks[user_id]

                self.collection_active[user_id] = False
                if user_id in self.collection_buffers:
                    del self.collection_buffers[user_id]

                logger.info(f"Cleared queue for user {user_id}, was not empty: {was_not_empty}")
                return was_not_empty

            return False

    async def remove_from_queue(self, user_id: int, message_id) -> bool:
        """Remove a specific item from the user's queue by message_id (body.mid)."""
        if user_id not in self.user_locks:
            self.user_locks[user_id] = asyncio.Lock()

        async with self.user_locks[user_id]:
            if user_id not in self.user_queues:
                return False

            queue = self.user_queues[user_id]
            if queue.empty():
                return False

            temp_items = []
            found = False

            while not queue.empty():
                try:
                    item = queue.get_nowait()
                    msg = item.get('message')
                    item_mid = msg.body.mid if msg and hasattr(msg, 'body') and msg.body else None
                    if item_mid and str(item_mid) == str(message_id):
                        found = True
                        logger.info(f"Removed message {message_id} from queue for user {user_id}")
                        queue.task_done()
                    else:
                        temp_items.append(item)
                except asyncio.QueueEmpty:
                    break

            for item in temp_items:
                queue.put_nowait(item)

            return found

    def get_queue_items(self, user_id: int) -> list:
        if user_id not in self.user_queues:
            return []

        queue = self.user_queues[user_id]
        if queue.empty():
            return []

        temp_items = []
        while not queue.empty():
            try:
                item = queue.get_nowait()
                temp_items.append(item)
            except asyncio.QueueEmpty:
                break

        for item in temp_items:
            queue.put_nowait(item)

        return temp_items

    async def update_queue_count_in_messages(self, user_id: int, i18n: TranslatorRunner):
        if user_id not in self.user_queues:
            return False

        queue = self.user_queues[user_id]
        if queue.empty():
            return False

        queue_items = self.get_queue_items(user_id)

        for index, item in enumerate(queue_items):
            qm = item.get('queue_message')
            if qm:
                try:
                    msg = item.get('message')
                    msg_mid = msg.body.mid if msg and hasattr(msg, 'body') and msg.body else None
                    await qm.edit(
                        text=i18n.audio_added_to_queue(position=index + 2),
                        attachments=[inline_cancel_queue(i18n=i18n, message_id=msg_mid)],
                    )
                except Exception as e:
                    if 'message is not modified' not in str(e):
                        logger.warning(f"Failed to edit queue message: {e}")

        return True

    # -----------------------------------------------------------------------
    # Collection window (batching rapid messages)
    # -----------------------------------------------------------------------

    async def _start_collection_window(
        self, user_id, message, context, i18n, language_code, media_data
    ):
        self.collection_active[user_id] = True
        self.collection_buffers[user_id] = []

        msg_mid = message.body.mid if hasattr(message, 'body') and message.body else None
        collection_item = {
            'message': message,
            'context': context,
            'i18n': i18n,
            'language_code': language_code,
            'media_data': media_data,
            'message_id': msg_mid,
            'timestamp': time.time(),
        }
        self.collection_buffers[user_id].append(collection_item)

        logger.info(f"Started collection window for user {user_id}")

        self.collection_tasks[user_id] = asyncio.create_task(
            self._collection_window_worker(user_id)
        )

    async def _collection_window_worker(self, user_id: int):
        initial_delay = 0.1
        max_total_delay = 0.2
        start_time = time.time()

        try:
            while time.time() - start_time < max_total_delay:
                await asyncio.sleep(initial_delay)

                if user_id in self.collection_buffers:
                    buffer = self.collection_buffers[user_id]
                    if buffer:
                        latest_timestamp = max(item['timestamp'] for item in buffer)
                        if time.time() - latest_timestamp < 0.05:
                            continue
                break

            await self._process_collected_messages(user_id)

        except Exception as e:
            logger.error(f"Error in collection window worker for user {user_id}: {e}")
            await self._process_collected_messages(user_id)
        finally:
            self.collection_active[user_id] = False
            if user_id in self.collection_buffers:
                del self.collection_buffers[user_id]
            if user_id in self.collection_tasks:
                del self.collection_tasks[user_id]

    async def _process_collected_messages(self, user_id: int):
        if user_id not in self.collection_buffers:
            return

        buffer = self.collection_buffers[user_id]
        if not buffer:
            return

        buffer.sort(key=lambda x: x.get('message_id') or '')

        logger.info(f"Processing {len(buffer)} collected messages for user {user_id}")

        async with self.user_locks[user_id]:
            if user_id not in self.user_queues:
                self.user_queues[user_id] = asyncio.Queue()

            queue = self.user_queues[user_id]

            for i, item in enumerate(buffer):
                self.message_counters[user_id] += 1
                message_order = self.message_counters[user_id]

                queue_item = {
                    'message': item['message'],
                    'context': item['context'],
                    'i18n': item['i18n'],
                    'language_code': item['language_code'],
                    'media_data': item['media_data'],
                    'queue_message': None,
                    'order': message_order,
                    'timestamp': item['timestamp'],
                }

                await queue.put(queue_item)

                queue_size = queue.qsize()
                if self.is_processing.get(user_id, False):
                    queue_position = queue_size
                else:
                    queue_position = queue_size

                if queue_position == 1:
                    queue_text = item['i18n'].audio_added_to_queue_first(position=queue_position + 1)
                else:
                    queue_text = item['i18n'].audio_added_to_queue(position=queue_position + 1)

                try:
                    queue_message = await self._reply_with_queue_keyboard(
                        item['message'], queue_text, item['i18n']
                    )
                    queue_item['queue_message'] = queue_message
                except Exception as e:
                    logger.error(f"Failed to send queue message for user {user_id}: {e}")

                logger.info(
                    f"Added collected message {item['message_id']} to queue for user {user_id}, "
                    f"position: {queue_position + 1}"
                )

            # Safety: if processing already finished before collection window completed,
            # start a queue worker to drain the queue
            if not self.is_processing.get(user_id, False) and not queue.empty():
                if user_id not in self.user_workers or self.user_workers[user_id].done():
                    logger.info(f"Starting queue worker from collection window for user {user_id}")
                    self.user_workers[user_id] = asyncio.create_task(
                        self._process_queue_worker(user_id)
                    )

    async def _add_to_collection_buffer(
        self, user_id, message, context, i18n, language_code, media_data
    ):
        if user_id not in self.collection_buffers:
            self.collection_buffers[user_id] = []

        msg_mid = message.body.mid if hasattr(message, 'body') and message.body else None
        collection_item = {
            'message': message,
            'context': context,
            'i18n': i18n,
            'language_code': language_code,
            'media_data': media_data,
            'message_id': msg_mid,
            'timestamp': time.time(),
        }

        self.collection_buffers[user_id].append(collection_item)
        logger.debug(f"Added message {msg_mid} to collection buffer for user {user_id}")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _reply_with_queue_keyboard(self, message, text, i18n):
        """Reply with queue position text and cancel button. Returns the inner Message."""
        msg_mid = message.body.mid if hasattr(message, 'body') and message.body else None
        result = await message.reply(
            text=text,
            attachments=[inline_cancel_queue(i18n=i18n, message_id=msg_mid)],
        )
        if result:
            result.message.bot = max_bot
            return result.message
        return None


# Global singleton
max_audio_queue_manager = MaxAudioQueueManager()
