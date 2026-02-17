"""
Модуль для управления напоминаниями о незавершенных платежах.

Система отправляет напоминания пользователям, которые начали процесс подписки,
но не завершили оплату:
- Первое напоминание: через N часов после первого действия конверсии (по умолчанию 2 часа)
- Второе напоминание: через M часов после последнего действия конверсии или обработанной сессии (по умолчанию 24 часа)

Время напоминаний настраивается через конфиг.
Используются оптимизированные запросы к БД с батчингом для минимизации нагрузки.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fluentogram import TranslatorRunner

from config_data.config import get_config
from keyboards.user_keyboards import initial_payment_notification_keyboard, second_payment_notification_keyboard
from models.orm import (
    get_users_for_first_reminder,
    get_users_for_second_reminder,
    log_user_action, log_user_action_async, init_background_logging
)
from services.bot_provider import get_bot
from services.payments.general_fucntions import create_payment_url
from utils.i18n import create_translator_hub

logger = logging.getLogger(__name__)
config = get_config()


async def send_first_payment_reminder() -> dict:
    """
    Отправляет первые напоминания о незавершенной оплате.

    Время отправки настраивается через config.payment_reminders.first_reminder_hours (по умолчанию 2 часа).
    Функция вызывается периодически через scheduler.
    Находит пользователей с любым действием категории 'conversion', у которых нет платежей.
    Обрабатывает пользователей батчами до тех пор, пока не закончатся подходящие записи.

    Returns:
        dict: Статистика отправки:
            - found: количество найденных пользователей
            - sent: количество успешно отправленных
            - failed: количество неудачных отправок
    """
    stats = {
        'found': 0,
        'sent': 0,
        'failed': 0,
        'started_at': datetime.utcnow().isoformat()
    }

    try:
        reminder_hours = config.payment_reminders.first_reminder_hours
        search_window = config.payment_reminders.search_window_hours

        logger.info(f"Starting first payment reminder check (after {reminder_hours}h)...")

        # Обрабатываем пользователей батчами до тех пор, пока они есть
        while True:
            users = await get_users_for_first_reminder(
                batch_size=100,
                reminder_hours=reminder_hours,
                search_window_hours=search_window
            )

            if not users:
                logger.info(
                    f"No more users found for first payment reminder. "
                    f"Total processed: found={stats['found']}, sent={stats['sent']}, failed={stats['failed']}"
                )
                break

            logger.info(f"Found batch of {len(users)} users for first payment reminder ({reminder_hours}h)")
            stats['found'] += len(users)

            # Создаем translator hub для батча
            translator_hub = create_translator_hub()

            # Отправляем напоминания
            for user_data in users:
                success = await _send_reminder_to_user(
                    user_data=user_data,
                    reminder_type='first',
                    translator_hub=translator_hub
                )

                if success:
                    stats['sent'] += 1
                else:
                    stats['failed'] += 1

                # Rate limiting между отправками
                await asyncio.sleep(0.1)

            # Пауза между батчами
            await asyncio.sleep(1)

        logger.info(
            f"First payment reminder completed: "
            f"found={stats['found']}, sent={stats['sent']}, failed={stats['failed']}"
        )

    except Exception as e:
        logger.error(f"Error in send_first_payment_reminder: {e}", exc_info=True)

    return stats


async def send_second_payment_reminder() -> dict:
    """
    Отправляет вторые напоминания о незавершенной оплате.

    Время отправки настраивается через config.payment_reminders.second_reminder_hours (по умолчанию 24 часа).
    Отсчитывается от ПОСЛЕДНЕГО релевантного действия пользователя:
    - Первое напоминание
    - Любое действие категории 'conversion'
    - Обработанная сессия (processing_sessions)

    Функция вызывается периодически через scheduler.
    Находит пользователей, которым уже отправили первое напоминание, но они до сих пор не оплатили.
    Обрабатывает пользователей батчами до тех пор, пока не закончатся подходящие записи.

    Returns:
        dict: Статистика отправки:
            - found: количество найденных пользователей
            - sent: количество успешно отправленных
            - failed: количество неудачных отправок
    """
    stats = {
        'found': 0,
        'sent': 0,
        'failed': 0,
        'started_at': datetime.utcnow().isoformat()
    }

    try:
        reminder_hours = config.payment_reminders.second_reminder_hours
        search_window = config.payment_reminders.search_window_hours

        logger.info(f"Starting second payment reminder check (after {reminder_hours}h from last activity)...")

        # Обрабатываем пользователей батчами до тех пор, пока они есть
        while True:
            users = await get_users_for_second_reminder(
                batch_size=100,
                reminder_hours=reminder_hours,
                search_window_hours=search_window
            )

            if not users:
                logger.info(
                    f"No more users found for second payment reminder. "
                    f"Total processed: found={stats['found']}, sent={stats['sent']}, failed={stats['failed']}"
                )
                break

            logger.info(f"Found batch of {len(users)} users for second payment reminder ({reminder_hours}h)")
            stats['found'] += len(users)

            # Создаем translator hub для батча
            translator_hub = create_translator_hub()

            # Отправляем напоминания
            for user_data in users:
                success = await _send_reminder_to_user(
                    user_data=user_data,
                    reminder_type='second',
                    translator_hub=translator_hub
                )

                if success:
                    stats['sent'] += 1
                else:
                    stats['failed'] += 1

                # Rate limiting между отправками
                await asyncio.sleep(0.1)

            # Пауза между батчами
            await asyncio.sleep(1)

        logger.info(
            f"Second payment reminder completed: "
            f"found={stats['found']}, sent={stats['sent']}, failed={stats['failed']}"
        )

    except Exception as e:
        logger.error(f"Error in send_second_payment_reminder: {e}", exc_info=True)

    return stats


async def _send_reminder_to_user(
    user_data: dict,
    reminder_type: str,
    translator_hub
) -> bool:
    """
    Отправляет напоминание конкретному пользователю.

    Args:
        user_data: Данные пользователя из БД:
            - user_id: ID пользователя
            - telegram_id: Telegram ID
            - user_language: Язык пользователя ('ru' или 'en')
            - first_created_at: Время первого действия конверсии
            - last_activity_at: (для second) Время последнего релевантного действия
        reminder_type: Тип напоминания ('first' или 'second')
        translator_hub: TranslatorHub для локализации

    Returns:
        bool: True если сообщение успешно отправлено и залогировано
    """
    user_id = user_data['user_id']
    telegram_id = user_data['telegram_id']
    user_language = user_data.get('user_language', 'ru')
    first_created_at = user_data.get('first_created_at')
    last_activity_at = user_data.get('last_activity_at', first_created_at)

    try:

        # Получаем переводчик для языка пользователя
        i18n: TranslatorRunner = translator_hub.get_translator_by_locale(locale=user_language)

        # Получаем текст напоминания
        if reminder_type == 'first':
            message_text = i18n.payment_reminder_2h()
            action_type_sent = 'conversion_reminder_first_sent'
            action_type_failed = 'conversion_reminder_first_failed'
            keyboard = initial_payment_notification_keyboard(i18n=i18n)
        elif reminder_type == 'second':
            message_text = i18n.payment_reminder_24h()
            action_type_sent = 'conversion_reminder_second_sent'
            action_type_failed = 'conversion_reminder_second_failed'
            
            # Определяем платежную систему
            last_payment_method = user_data.get('last_payment_method')
            payment_method = last_payment_method if last_payment_method else 'cloudpayments'
            bill_url = await create_payment_url(user_data=user_data, subscription_type='monthly_discounted_notification', payment_method=payment_method, i18n=i18n)
            keyboard = second_payment_notification_keyboard(i18n=i18n, user_data=user_data, payment_method=payment_method, bill_url=bill_url)
        else:
            logger.error(f"Unknown reminder type: {reminder_type}")
            return False

        # Отправляем сообщение
        try:
            await get_bot().send_message(
                chat_id=int(telegram_id),
                text=message_text,
                parse_mode='HTML',
                reply_markup=keyboard
            )
            logger.info(
                f"Payment reminder {reminder_type} sent to user {telegram_id} "
                f"(first_action: {first_created_at}, last_activity: {last_activity_at})"
            )
        except Exception as e:
            logger.error(
                f"Failed to send reminder {reminder_type} to user {telegram_id}: {e}",
                exc_info=True
            )

            # Логируем неудачную попытку для предотвращения повторной отправки
            try:
                await log_user_action_async(
                    user_id=user_id,
                    action_type=action_type_failed,
                    action_category='conversion',
                    metadata={
                        'reminder_type': reminder_type,
                        'failed_at': datetime.utcnow().isoformat(),
                        'telegram_id': telegram_id,
                        'error': str(e),
                        'first_conversion_action_at': first_created_at.isoformat() if first_created_at else None,
                        'last_activity_at': last_activity_at.isoformat() if last_activity_at else None
                    }
                )
                logger.info(f"Failed reminder logged for user {user_id}: {action_type_failed}")
            except Exception as log_error:
                logger.error(f"Failed to log failed reminder for user {user_id}: {log_error}", exc_info=True)

            return False

        # Логируем успешную отправку напоминания в user_actions
        try:
            await log_user_action_async(
                user_id=user_id,
                action_type=action_type_sent,
                action_category='conversion',
                metadata={
                    'reminder_type': reminder_type,
                    'first_conversion_action_at': first_created_at.isoformat() if first_created_at else None,
                    'last_activity_at': last_activity_at.isoformat() if last_activity_at else None,
                    'reminder_sent_at': datetime.utcnow().isoformat(),
                    'user_language': user_language,
                    'telegram_id': telegram_id
                }
            )
            logger.info(f"User action logged for user {user_id}: {action_type_sent}")
        except Exception as log_error:
            logger.error(f"Failed to log user action for user {user_id}: {log_error}", exc_info=True)
            # Не возвращаем False, так как сообщение уже отправлено

        return True

    except Exception as e:
        logger.error(f"Error sending reminder to user {telegram_id}: {e}", exc_info=True)

        # Логируем неудачную попытку
        try:
            action_type_failed = f'conversion_reminder_{reminder_type}_failed'
            await log_user_action_async(
                user_id=user_id,
                action_type=action_type_failed,
                action_category='conversion',
                metadata={
                    'reminder_type': reminder_type,
                    'failed_at': datetime.utcnow().isoformat(),
                    'telegram_id': telegram_id,
                    'error': str(e)
                }
            )
        except Exception as log_error:
            logger.error(f"Failed to log failed reminder for user {user_id}: {log_error}", exc_info=True)

        return False


async def get_reminder_statistics(days: int = 7) -> dict:
    """
    Получает статистику по отправленным напоминаниям за указанный период.

    Args:
        days: Количество дней для анализа (по умолчанию 7)

    Returns:
        dict: Статистика:
            - reminders_first_sent: количество отправленных первых напоминаний
            - reminders_second_sent: количество отправленных вторых напоминаний
            - unique_users: количество уникальных пользователей, получивших напоминания
            - conversion_rate: конверсия в оплату после напоминаний
    """
    # TODO: Реализовать сбор статистики из user_actions
    # Это можно сделать позже для мониторинга эффективности напоминаний
    pass


# Для тестирования
if __name__ == "__main__":
    import asyncio

    async def test():
        await init_background_logging()
        try:
            print("Testing first reminder...")
            stats_first = await send_first_payment_reminder()
            print(f"First reminder stats: {stats_first}")

            print("\nTesting second reminder...")
            stats_second = await send_second_payment_reminder()
            print(f"Second reminder stats: {stats_second}")
        finally:
            # Закрываем сессию бота
            await get_bot().session.close()

    asyncio.run(test())
