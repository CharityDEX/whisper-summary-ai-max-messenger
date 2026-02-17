import asyncio
import io
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
import aiofiles

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, BufferedInputFile, LinkPreviewOptions, InlineKeyboardMarkup, InlineKeyboardButton
from fluentogram import TranslatorRunner

from config_data.config import get_config
from keyboards.admin_keyboards import admin_menu, confirm_spam_keyboard, spam_menu, statistic_source_menu, \
    cancel_subscription_keyboard, sub_type_menu, time_period_menu, data_export_menu, statistic_source_menu_paginated, \
    logs_time_menu, confirm_give_subscription_keyboard
from models.orm import get_payments_sources, get_sources_with_subscription, get_users, is_admin, get_statistics, get_sources, give_subscription, get_user_id_range, update_user_blocked_status, engine, get_users_to_exclude_from_broadcast, get_user
from services.init_bot import bot
from services.services import sources_to_str, split_long_message, sources_to_str_paginated
from states.states import AdminSpamSession, AdminGiveSubscription
from services.telegram_alerts import send_alert

logger = logging.getLogger(__name__)
# Create a separate logger for spam activity
spam_logger = logging.getLogger('spam')

async def check_real_pool_status():
    """Проверка состояния реального connection pool бота"""
    try:
        from sqlalchemy import text
        import time
        
        pool = engine.pool
        
        # Получаем настройки
        pool_size = getattr(pool, '_pool_size', 20)
        max_overflow = getattr(pool, '_max_overflow', 30)
        
        # Получаем состояние
        available = 0
        active = 0
        
        if hasattr(pool, '_pool') and pool._pool:
            try:
                available = pool._pool.qsize()
            except:
                pass
        
        try:
            checked_in = pool.checkedin()
            checked_out = pool.checkedout() 
            if checked_in >= 0:
                available = max(available, checked_in)
            if checked_out >= 0:
                active = checked_out
        except:
            pass
        
        # Тест подключения
        test_start = time.time()
        test_passed = False
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text("SELECT 1"))
                test_passed = True
        except:
            pass
        test_duration = time.time() - test_start
        
        max_possible = pool_size + max_overflow
        utilization = (active / max_possible * 100) if max_possible > 0 else 0
        
        if not test_passed:
            status = "🔴 CRITICAL - Connection Failed"
        elif utilization > 95:
            status = "🔴 CRITICAL"
        elif utilization > 80:
            status = "🟡 WARNING"
        elif utilization > 50:
            status = "🟢 NORMAL"
        else:
            status = "🔵 LOW"
        
        return {
            'status': status,
            'pool_size': pool_size,
            'max_overflow': max_overflow,
            'available': available,
            'active': active,
            'max_possible': max_possible,
            'utilization': utilization,
            'test_passed': test_passed,
            'test_duration': test_duration
        }
    except Exception as e:
        return {'error': str(e)}

# Queue и listener для async логирования (глобальные для переиспользования)
_spam_log_queue = None
_spam_queue_listener = None


def setup_spam_logger(campaign_id=None):
    """
    Configure a logger for spam campaigns with async file writing.

    Uses QueueHandler + QueueListener to avoid blocking the event loop
    during file I/O operations.

    Args:
        campaign_id: Optional unique ID for this spam campaign. If provided,
                    creates a separate log file for this campaign.
    
    Returns:
        Configured logger instance
    """
    global _spam_log_queue, _spam_queue_listener

    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)

    # Stop previous listener if exists
    if _spam_queue_listener is not None:
        _spam_queue_listener.stop()
        _spam_queue_listener = None

    # Reset handlers to avoid duplicates
    spam_logger.handlers = []
    
    # Set log level
    spam_logger.setLevel(logging.INFO)
    
    # Define a formatter for the logs
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Create queue for async logging
    import queue
    from logging.handlers import QueueHandler, QueueListener

    _spam_log_queue = queue.Queue(-1)  # No limit on size

    # Create actual file handler (will be used by listener in separate thread)
    if campaign_id:
        log_filename = f'logs/spam_campaign_{campaign_id}.log'
        file_handler = logging.FileHandler(log_filename, mode='w')
    else:
        current_date = datetime.now().strftime('%Y-%m-%d')
        log_filename = f'logs/spam_{current_date}.log'
        file_handler = logging.FileHandler(log_filename, mode='a')

    file_handler.setFormatter(formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Create QueueListener that processes logs in background thread
    _spam_queue_listener = QueueListener(
        _spam_log_queue,
        file_handler,
        console_handler,
        respect_handler_level=True
    )
    _spam_queue_listener.start()

    # Add QueueHandler to logger (non-blocking)
    queue_handler = QueueHandler(_spam_log_queue)
    spam_logger.addHandler(queue_handler)

    if campaign_id:
        spam_logger.info(f"Started new spam campaign log: {campaign_id}")
    else:
        spam_logger.info("Appending to daily spam log")

    return spam_logger


def stop_spam_logger():
    """Stop the background logging thread gracefully."""
    global _spam_queue_listener
    if _spam_queue_listener is not None:
        _spam_queue_listener.stop()
        _spam_queue_listener = None

router = Router()

@router.message(F.text == '/pool')
async def process_pool_command(message: Message, state: FSMContext, i18n: TranslatorRunner):
    """Команда для проверки состояния connection pool"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещен")
        return
    
    status_msg = await message.answer("🔄 Проверяю состояние connection pool...")
    
    try:
        pool_info = await check_real_pool_status()
        
        if 'error' in pool_info:
            await status_msg.edit_text(f"❌ Ошибка проверки pool:\n{pool_info['error']}")
            return
        
        # Форматируем вывод
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        text = f"""
🗄 <b>Database Pool Status</b>
🕒 {timestamp}

📊 <b>Статус:</b> {pool_info['status']}
🔧 <b>Настройки:</b>
├ Pool size: {pool_info['pool_size']}
├ Max overflow: {pool_info['max_overflow']}
└ Max possible: {pool_info['max_possible']}

📈 <b>Текущее состояние:</b>
├ Доступно: {pool_info['available']}
├ Используется: {pool_info['active']}
└ Утилизация: {pool_info['utilization']:.1f}%

🔗 <b>Тест подключения:</b>
├ Статус: {'✅ PASS' if pool_info['test_passed'] else '❌ FAIL'}
└ Время: {pool_info['test_duration']:.3f}s
"""
        
        if pool_info['utilization'] > 80:
            text += "\n⚠️ <b>Предупреждение:</b> Высокая утилизация pool!"
        elif pool_info['utilization'] > 95:
            text += "\n🚨 <b>КРИТИЧНО:</b> Pool практически исчерпан!"
        
        await status_msg.edit_text(text, parse_mode='HTML')
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")

@router.callback_query(F.data == 'statistics_menu')
@router.message(F.text == '/statistics')
async def process_statistics_command(message: Message, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(message.from_user.id):
        return

    usage_data: dict = await get_statistics()
    await state.clear()
    text = i18n.statistics_menu(users_count=usage_data['users_count'],
                                users_with_action=usage_data['users_with_action'],
                                audios_num=usage_data['voice_uses'],
                                gpts_num=usage_data['gpt_uses'],
                                active_sessions=usage_data['active_sessions'],
                                active_subs=usage_data['active_subs'],
                                weekly_subs=usage_data['weekly_subs'],
                                monthly_subs=usage_data['monthly_subs'],
                                annual_subs=usage_data['annual_subs'],
                                manual_subs=usage_data['manual_subs'],
                                unblocked_users_count=usage_data['unblocked_users_count'])
    if type(message) is CallbackQuery:
        await message.message.edit_text(text=text,
                                        reply_markup=admin_menu(i18n))
    else:
        await message.answer(text=text,
                             reply_markup=admin_menu(i18n))

#Источники заходов
@router.callback_query(F.data == 'source_statistic')
async def process_source_statistic(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return

    source_data = await get_sources()
    
    # Используем пагинацию для отображения
    await display_sources_paginated(
        callback=callback,
        source_data=source_data,
        data_type='sources',
        page=1,
        i18n=i18n,
        subscription=False
    )



@router.callback_query(F.data.startswith('statistic_data_period|'))
async def process_choose_subscription_type_for_statistic(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    data_type = callback.data.split('|')[1]
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(text=i18n.sources_of_what_type(), reply_markup=sub_type_menu(data_type=data_type,
                                                                                                  i18n=i18n))

#Источники подписок (всех, не только активных)
@router.callback_query(F.data.startswith('statistic_data|subscriptions'))
async def process_source_with_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    subscription_type = callback.data.split('|')[-1]
    source_data = await get_sources_with_subscription(subscription_type=subscription_type if subscription_type != 'all' else None)
    
    # Используем пагинацию для отображения
    await display_sources_paginated(
        callback=callback,
        source_data=source_data,
        data_type='subscriptions',
        page=1,
        i18n=i18n,
        subscription=True,
        subscription_type=subscription_type
    )

#Источники Оплат
@router.callback_query(F.data.startswith('statistic_data|payments'))
async def process_payment_source_statistics(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    subscription_type = callback.data.split('|')[-1]
    
    # Instead of directly showing results, show time period selection menu
    await callback.message.edit_text(
        text=i18n.select_time_period(),
        reply_markup=time_period_menu(i18n=i18n, data_type='payments', subscription_type=subscription_type)
    )

@router.callback_query(F.data.startswith('statistic_data_time|payments'))
async def process_payment_source_with_time_filter(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    # Extract parameters
    parts = callback.data.split('|')
    subscription_type = parts[2]
    period_days = int(parts[3])
    
    # Get data with both subscription type and time period filters
    source_data = await get_payments_sources(
        unique=False, 
        subscription_type=subscription_type if subscription_type != 'all' else None,
        period_days=period_days if period_days > 0 else None
    )
    
    # Format period text for display
    period_text = ""
    if period_days == 7:
        period_text = i18n.last_7_days()
    elif period_days == 30:
        period_text = i18n.last_30_days()
    else:
        period_text = i18n.all_time_period()
    
    # Format subscription type text
    subscription_text = ""
    if subscription_type == 'weekly':
        subscription_text = i18n.weekly_subscriptions()
    elif subscription_type == 'monthly':
        subscription_text = i18n.monthly_subscriptions()
    else:
        subscription_text = i18n.all_subscriptions()
    
    # Используем пагинацию для отображения
    await display_sources_paginated(
        callback=callback,
        source_data=source_data,
        data_type='payments',
        page=1,
        i18n=i18n,
        subscription=True,
        period_text=period_text,
        subscription_text=subscription_text,
        subscription_type=subscription_type,
        period_days=period_days
    )

@router.callback_query(F.data.startswith('statistic_data_time|unique_payments'))
async def process_unique_payment_source_with_time_filter(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    # Extract parameters
    parts = callback.data.split('|')
    subscription_type = parts[2]
    period_days = int(parts[3])
    
    # Get data with both subscription type and time period filters
    # The key difference is unique=True
    source_data = await get_payments_sources(
        unique=True, 
        subscription_type=subscription_type if subscription_type != 'all' else None,
        period_days=period_days if period_days > 0 else None
    )
    
    # Format period text for display
    period_text = ""
    if period_days == 7:
        period_text = i18n.last_7_days()
    elif period_days == 30:
        period_text = i18n.last_30_days()
    else:
        period_text = i18n.all_time_period()
    
    # Format subscription type text
    subscription_text = ""
    if subscription_type == 'weekly':
        subscription_text = i18n.weekly_subscriptions()
    elif subscription_type == 'monthly':
        subscription_text = i18n.monthly_subscriptions()
    else:
        subscription_text = i18n.all_subscriptions()
    
    # Используем пагинацию для отображения
    await display_sources_paginated(
        callback=callback,
        source_data=source_data,
        data_type='unique_payments',
        page=1,
        i18n=i18n,
        subscription=True,
        period_text=period_text,
        subscription_text=subscription_text,
        subscription_type=subscription_type,
        period_days=period_days
    )

@router.callback_query(F.data == 'spam_menu')
async def process_spam_menu(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
        
    # Get user ID range to show the admin
    id_range = await get_user_id_range()
    
    await callback.message.edit_text(
        text=i18n.enter_start_id(min_id=id_range['min_id'], max_id=id_range['max_id']),
        reply_markup=spam_menu(i18n, skip=True)
    )
    await state.set_state(AdminSpamSession.waiting_start_id)

@router.message(StateFilter(AdminSpamSession.waiting_start_id))
async def process_start_id(message: Message, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(message.from_user.id):
        return

    try:
        start_id, end_id = message.text.split('-')
    except ValueError:
        await message.answer(text=i18n.invalid_start_id_splitting())
        return


    start_id = int(start_id) if start_id else None
    end_id = int(end_id) if end_id else None
    await state.update_data(start_id=start_id, end_id=end_id)
    
    await message.answer(
        text=i18n.choose_users_for_spam(),
        reply_markup=spam_menu(i18n)
    )
    await state.set_state(AdminSpamSession.waiting_spam_message)

@router.callback_query(F.data == 'skip_start_id')
async def process_skip_start_id(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        text=i18n.choose_users_for_spam(),
        reply_markup=spam_menu(i18n)
    )
    await state.set_state(AdminSpamSession.waiting_spam_message)

@router.callback_query(F.data.startswith('spam_'))
async def process_spam_selection(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    spam_type = callback.data
    state_data = await state.get_data()
    start_id: int | None = state_data.get('start_id', None)
    end_id: int | None = state_data.get('end_id', None)
    
    users: list[dict] = await get_users()
    
    # Filter users based on start_id

    if start_id and end_id:
        users = [user for user in users if start_id <= int(user['id']) <= end_id]
    elif start_id:
        users = [user for user in users if start_id <= int(user['id'])]
    elif end_id:
        users = [user for user in users if end_id >= int(user['id'])]
    
    # Фильтруем пользователей, которые заблокировали бота
    users = [user for user in users if not user.get('is_bot_blocked', False)]
    
    if spam_type == 'spam_subscribed':
        users = [user for user in users if user['subscription'] == 'True']
    elif spam_type == 'spam_unsubscribed':
        users = [user for user in users if user['subscription'] != 'True']
    # spam_all uses all filtered users
    
    # Получаем пользователей для исключения из рассылки (получали/получат напоминания)
    exclusion_data = await get_users_to_exclude_from_broadcast()
    excluded_user_ids = exclusion_data['user_ids']
    exclusion_stats = exclusion_data['stats']

    # Подсчитываем, сколько пользователей будет исключено
    users_before_exclusion = len(users)
    users = [user for user in users if int(user['id']) not in excluded_user_ids]
    users_excluded_count = users_before_exclusion - len(users)

    # Сохраняем статистику в состоянии для отображения при подтверждении
    await state.update_data(
        spam_type=spam_type,
        target_users=users,
        exclusion_stats={
            'excluded_count': users_excluded_count,
            'recent_reminders': exclusion_stats['recent_reminders'],
            'upcoming_reminders': exclusion_stats['upcoming_reminders'],
            'breakdown': exclusion_stats['breakdown']
        }
    )

    # Формируем детальное сообщение о фильтрации
    detail_text = ""
    if users_excluded_count > 0:
        detail_text = f"\n\n🔔 <b>Исключено из рассылки:</b> {users_excluded_count} чел."
        detail_text += f"\n├ Получили напоминания (24ч): {exclusion_stats['recent_reminders']}"
        detail_text += f"\n└ Получат напоминания (24ч): {exclusion_stats['upcoming_reminders']}"

    try:
        await callback.message.edit_text(
            text=i18n.spam_menu(users_num=len(users)) + detail_text,
            reply_markup=spam_menu(i18n, show_exclude_button=True),
            parse_mode='HTML'
        )
        await state.set_state(AdminSpamSession.waiting_spam_message)
    except TelegramBadRequest as e:
        try:
            await callback.answer()
        except:
            pass

@router.message(AdminSpamSession.waiting_spam_message)
async def process_spam_message(message: Message, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(message.from_user.id):
        return

    state_data = await state.get_data()
    target_users = state_data.get('target_users', [])
    exclusion_stats = state_data.get('exclusion_stats', {})

    await state.update_data(spam_message=message)

    # Формируем сообщение с подтверждением
    confirmation_text = i18n.spam_confirmation()
    confirmation_text += f"\n\n📊 <b>Итого для рассылки:</b> {len(target_users)} чел."

    if exclusion_stats:
        total_excluded = exclusion_stats.get('excluded_count', 0) + exclusion_stats.get('manual_excluded', 0)
        if total_excluded > 0:
            confirmation_text += f"\n\n🚫 <b>Исключено:</b> {total_excluded} чел."
            if exclusion_stats.get('excluded_count', 0) > 0:
                confirmation_text += f"\n  • Напоминания: {exclusion_stats['excluded_count']}"
            if exclusion_stats.get('manual_excluded', 0) > 0:
                confirmation_text += f"\n  • Файл: {exclusion_stats['manual_excluded']}"

    await message.answer(
        text=confirmation_text,
        reply_markup=confirm_spam_keyboard(i18n),
        parse_mode='HTML'
    )
    
@router.callback_query(F.data == 'confirm_spam')
async def process_confirm_spam(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    state_data = await state.get_data()
    target_users = state_data.get('target_users', [])
    message_to_spam = state_data.get('spam_message')
    exclusion_stats = state_data.get('exclusion_stats', {})

    # Generate a unique campaign ID based on timestamp
    campaign_id = f"{callback.from_user.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Setup logger for this campaign
    setup_spam_logger(campaign_id)
    
    await callback.message.edit_text(text=i18n.spam_start())

    # Create batches of 20 users
    batch_size = 15
    user_batches = [target_users[i:i + batch_size] for i in range(0, len(target_users), batch_size)]
    
    total_sent = 0
    spam_logger.info(f"Starting spam campaign {campaign_id}")
    spam_logger.info(f"Target users count: {len(target_users)}")
    spam_logger.info(f"Admin ID: {callback.from_user.id}")
    spam_logger.info(f"Message type: {message_to_spam.content_type}")

    # Log exclusion statistics
    if exclusion_stats:
        spam_logger.info(f"Exclusion stats: {exclusion_stats['excluded_count']} users excluded")
        spam_logger.info(f"  - Recent reminders (24h): {exclusion_stats['recent_reminders']}")
        spam_logger.info(f"  - Upcoming reminders (24h): {exclusion_stats['upcoming_reminders']}")
        if exclusion_stats.get('breakdown'):
            spam_logger.info(f"  - Breakdown: {exclusion_stats['breakdown']}")

    try:
        alert_text = f"<b>Starting spam campaign</b> {campaign_id}.\n<b>Target users count:</b> {len(target_users)}.\n<b>Admin ID:</b> {callback.from_user.username}"
        if exclusion_stats and exclusion_stats.get('excluded_count', 0) > 0:
            alert_text += f"\n<b>Excluded (reminders):</b> {exclusion_stats['excluded_count']}"

        await send_alert(text=alert_text,
                    topic="SPAM", level="INFO", 
                    fingerprint=f"spam_campaign_{campaign_id}")
    except Exception as e:
        spam_logger.error(f"Failed to send alert: {e}")


    
    for batch_index, batch in enumerate(user_batches):
        spam_logger.info(f"Processing batch {batch_index+1}/{len(user_batches)}")
        coros = [spam_gather(message_to_spam, int(user['telegram_id']), i18n, user) for user in batch]
        results = await asyncio.gather(*coros)
        successful_sends = sum(1 for result in results if result)
        total_sent += successful_sends
        
        spam_logger.info(f"Batch {batch_index+1} completed: {successful_sends}/{len(batch)} successful")
        
        # Wait for 1 second before the next batch
        await asyncio.sleep(1)

    spam_logger.info(f"Spam campaign completed. Total sent: {total_sent}/{len(target_users)}")

    # Формируем детальное сообщение о результатах
    result_text = i18n.spam_success(total_sent=total_sent)

    if exclusion_stats and exclusion_stats.get('excluded_count', 0) > 0:
        result_text += f"\n\n📊 <b>Статистика исключений:</b>"
        result_text += f"\n├ Всего исключено: {exclusion_stats['excluded_count']}"
        result_text += f"\n├ Получили напоминания (24ч): {exclusion_stats['recent_reminders']}"
        result_text += f"\n└ Получат напоминания (24ч): {exclusion_stats['upcoming_reminders']}"

        # Детальная разбивка
        if exclusion_stats.get('breakdown'):
            breakdown = exclusion_stats['breakdown']
            if breakdown.get('recent'):
                result_text += "\n\n<b>Недавние напоминания:</b>"
                for reminder_type, count in breakdown['recent'].items():
                    reminder_name = reminder_type.replace('_', ' ').title()
                    result_text += f"\n  • {reminder_name}: {count}"

            if breakdown.get('upcoming'):
                result_text += "\n\n<b>Предстоящие напоминания:</b>"
                for reminder_type, count in breakdown['upcoming'].items():
                    reminder_name = reminder_type.replace('upcoming_', '').replace('_', ' ').title()
                    result_text += f"\n  • {reminder_name}: {count}"

    try:
        alert_text = f"<b>Spam campaign</b> {campaign_id} completed.\n<b>Total sent:</b> {total_sent}/{len(target_users)}.\n<b>Admin ID:</b> {callback.from_user.username}"
        if exclusion_stats and exclusion_stats.get('excluded_count', 0) > 0:
            alert_text += f"\n<b>Excluded (reminders):</b> {exclusion_stats['excluded_count']}"

        await send_alert(text=alert_text,
                    topic="SPAM", level="INFO", 
                    fingerprint=f"spam_campaign_{campaign_id}")
    except Exception as e:
        spam_logger.error(f"Failed to send alert: {e}")
    
    await callback.message.answer(text=result_text, parse_mode='HTML')
    await state.clear()


@router.callback_query(F.data == 'continue_to_message')
async def process_continue_to_message(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return

    state_data = await state.get_data()
    target_users = state_data.get('target_users', [])
    exclusion_stats = state_data.get('exclusion_stats', {})

    msg_text = f"👥 Выбрано пользователей для рассылки: {len(target_users)}\n"

    # Добавляем статистику исключений если есть
    if exclusion_stats:
        if exclusion_stats.get('excluded_count', 0) > 0:
            msg_text += f"\n🔔 Исключено (напоминания): {exclusion_stats['excluded_count']}"
        if exclusion_stats.get('manual_excluded', 0) > 0:
            msg_text += f"\n📂 Исключено (файл): {exclusion_stats['manual_excluded']}"

    msg_text += "\n\n📝 Отправьте сообщение для рассылки:"

    await callback.message.edit_text(text=msg_text)
    await state.set_state(AdminSpamSession.waiting_spam_message)


@router.callback_query(F.data == 'exclude_ids')
async def process_exclude_ids(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        text="📂 Отправьте текстовый файл с ID пользователей для исключения из рассылки.\n\n"
             "⚠️ ВАЖНО: Используйте внутренние ID из базы данных (поле 'id'), НЕ telegram_id!\n\n"
             "Каждый ID должен быть на отдельной строке.\n"
             "Пример:\n"
             "132739\n"
             "63963\n"
             "109230",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data='statistics_menu'
                )]
            ]
        )
    )
    await state.set_state(AdminSpamSession.waiting_exclude_file)


@router.message(StateFilter(AdminSpamSession.waiting_exclude_file))
async def process_exclude_file(message: Message, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(message.from_user.id):
        return

    # Проверяем, что это документ
    if not message.document:
        await message.answer(
            "❌ Пожалуйста, отправьте текстовый файл.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data='statistics_menu'
                    )]
                ]
            )
        )
        return

    # Проверяем размер файла (максимум 1MB)
    if message.document.file_size > 1024 * 1024:
        await message.answer(
            "❌ Файл слишком большой. Максимальный размер: 1MB",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data='statistics_menu'
                    )]
                ]
            )
        )
        return

    try:
        # Скачиваем файл
        file = await bot.get_file(message.document.file_id)

        # Для локального Bot API сервера нужно использовать file.file_path напрямую

        # Проверяем, является ли file_path абсолютным путем (локальный Bot API)
        if os.path.isabs(file.file_path):
            # Читаем файл напрямую с диска
            async with aiofiles.open(file.file_path, 'rb') as f:
                file_content_bytes = await f.read()
        else:
            # Стандартное скачивание через Bot API
            file_content = await bot.download_file(file.file_path)
            file_content_bytes = file_content.read()

        # Читаем содержимое файла
        content = file_content_bytes.decode('utf-8')

        # Парсим ID из файла
        exclude_ids = []
        lines = content.strip().split('\n')

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if line:  # Пропускаем пустые строки
                try:
                    # Проверяем, что это число (ID)
                    user_id = int(line)
                    exclude_ids.append(user_id)
                except ValueError:
                    await message.answer(
                        f"❌ Ошибка в строке {line_num}: '{line}' не является валидным ID.\n"
                        "ID должны быть числами.",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="❌ Отмена",
                                    callback_data='statistics_menu'
                                )]
                            ]
                        )
                    )
                    return

        if not exclude_ids:
            await message.answer(
                "❌ В файле не найдено ни одного валидного ID.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data='statistics_menu'
                        )]
                    ]
                )
            )
            return

        # Сохраняем список исключений в состоянии
        await state.update_data(exclude_ids=exclude_ids)

        # Получаем текущие данные состояния
        state_data = await state.get_data()
        target_users = state_data.get('target_users', [])
        exclusion_stats = state_data.get('exclusion_stats', {})

        # Фильтруем пользователей, исключая указанные ID
        original_count = len(target_users)
        filtered_users = []

        for user in target_users:
            user_internal_id = int(user['id'])  # Используем внутренний ID базы данных
            if user_internal_id not in exclude_ids:
                filtered_users.append(user)

        # Обновляем список пользователей, сохраняя статистику исключений
        excluded_count = original_count - len(filtered_users)

        # Обновляем статистику: добавляем к уже существующим исключениям
        if exclusion_stats:
            # Обновляем счетчик manual exclusions
            exclusion_stats['manual_excluded'] = excluded_count
            total_excluded = exclusion_stats.get('excluded_count', 0) + excluded_count
        else:
            exclusion_stats = {
                'excluded_count': 0,
                'manual_excluded': excluded_count,
                'recent_reminders': 0,
                'upcoming_reminders': 0,
                'breakdown': {'recent': {}, 'upcoming': {}}
            }
            total_excluded = excluded_count

        await state.update_data(target_users=filtered_users, exclusion_stats=exclusion_stats)

        # Формируем детальное сообщение
        stats_msg = f"✅ Файл обработан успешно!\n\n"
        stats_msg += f"📊 Статистика:\n"
        stats_msg += f"• Исходное количество пользователей: {original_count}\n"
        stats_msg += f"• Исключено из файла: {excluded_count}\n"

        if exclusion_stats.get('excluded_count', 0) > 0:
            stats_msg += f"• Исключено (напоминания): {exclusion_stats['excluded_count']}\n"
            stats_msg += f"  ├ Получили (24ч): {exclusion_stats['recent_reminders']}\n"
            stats_msg += f"  └ Получат (24ч): {exclusion_stats['upcoming_reminders']}\n"

        stats_msg += f"• <b>Итоговое количество для рассылки: {len(filtered_users)}</b>\n\n"
        stats_msg += f"Выберите дальнейшее действие:"

        await message.answer(
            stats_msg,
            reply_markup=spam_menu(i18n, show_exclude_button=True),
            parse_mode='HTML'
        )

        # Возвращаемся к состоянию ожидания сообщения для рассылки
        await state.set_state(AdminSpamSession.waiting_spam_message)

    except Exception as e:
        logger.error(f"Error processing exclude file: {e}")
        await message.answer(
            f"❌ Ошибка при обработке файла: {str(e)}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data='statistics_menu'
                    )]
                ]
            )
        )


async def spam_gather(message: Message, telegram_id: int, i18n: TranslatorRunner, user_info=None):
    user_id_str = f"{telegram_id}"
    if user_info:
        # Include additional user info in logs if available
        user_id_str = f"{telegram_id} (ID: {user_info.get('id', 'unknown')}, Username: {user_info.get('username', 'unknown')})"
    
    try:
        spam_logger.debug(f"Attempting to send message to user {user_id_str}")
        msg = await bot.copy_message(
            chat_id=telegram_id,
            from_chat_id=message.from_user.id,
            message_id=message.message_id,
        )
        spam_logger.debug(f"Successfully sent message to user {user_id_str}, message_id: {msg.message_id}")
        return telegram_id, msg.message_id
    except TelegramBadRequest as e:
        error_str = str(e)
        spam_logger.error(f"Failed to send message to user {user_id_str}: {error_str}")

        spam_logger.error(f"TelegramBadRequest: {e}")
        if "Forbidden: bot was blocked by the user" in error_str:
            spam_logger.debug(f"User {user_id_str} has blocked the bot. Updating database.")
            try:   
                await update_user_blocked_status(telegram_id, True)
            except Exception as e:
                spam_logger.error(f"Failed to update user {user_id_str} blocked status: {e}")
            return False
        # Обработка деактивированных аккаунтов
        elif "Forbidden: user is deactivated" in error_str:
            spam_logger.debug(f"User {user_id_str} account is deactivated.")
            try:
                await update_user_blocked_status(telegram_id, True)
            except Exception as e:
                spam_logger.error(f"Failed to update user {user_id_str} blocked status: {e}")
            # Примечание: мы не отмечаем это как блокировку бота, так как 
            # пользователь может восстановить аккаунт позже
            return False
        else:
            spam_logger.warning(f"Unknown bad request error for user {user_id_str}: {error_str}")
            return False
    except Exception as e:
        error_str = str(e)
        spam_logger.error(f"Failed to send message to user {user_id_str}: {error_str}")
        
        # Проверяем ошибки, связанные с блокировкой бота
        if "Forbidden: bot was blocked by the user" in error_str:
            spam_logger.warning(f"User {user_id_str} has blocked the bot. Updating database.")
            try:
                await update_user_blocked_status(telegram_id, True)
            except Exception as e:
                spam_logger.error(f"Failed to update user {user_id_str} blocked status: {e}")
            return False
        # Обработка деактивированных аккаунтов
        elif "Forbidden: user is deactivated" in error_str:
            spam_logger.warning(f"User {user_id_str} account is deactivated.")
            try:
                await update_user_blocked_status(telegram_id, True)
            except Exception as e:
                spam_logger.error(f"Failed to update user {user_id_str} blocked status: {e}")
            # Примечание: мы не отмечаем это как блокировку бота, так как 
            # пользователь может восстановить аккаунт позже
            return False
        else:
            spam_logger.error(f"Unknown error for user {user_id_str}: {error_str}")
            try:
                spam_logger.warning(f"Retrying message send to user {user_id_str}")
                msg = await bot.copy_message(
                    chat_id=telegram_id,
                    from_chat_id=message.from_user.id,
                    message_id=message.message_id
                )
                spam_logger.debug(f"Successfully sent message to user {user_id_str} on retry, message_id: {msg.message_id}")
                return telegram_id, msg.message_id
            except Exception as e:
                error_str = str(e)
                spam_logger.error(f"Failed to send message to user {user_id_str} on retry: {error_str}")
                return False


########################################################################################################################

@router.callback_query(F.data == 'give_subscription')
async def process_give_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.edit_text(text=i18n.give_subscription_desc(),
                                     reply_markup=cancel_subscription_keyboard(i18n))
    await state.set_state(AdminGiveSubscription.waiting_for_user_data)


@router.message(StateFilter(AdminGiveSubscription.waiting_for_user_data))
async def process_give_subscription_user_data(message: Message, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text)
        await state.update_data(user_id=user_id,
                                username=None)
    except:
        username = message.text.removeprefix("@")
        await state.update_data(username=username,
                                user_id=None)

    await message.answer(text=i18n.give_subscription_length(),
                         reply_markup=cancel_subscription_keyboard(i18n))
    await state.set_state(AdminGiveSubscription.waiting_for_subscription_length)

@router.message(StateFilter(AdminGiveSubscription.waiting_for_subscription_length))
async def process_give_subscription_length(message: Message, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(message.from_user.id):
        return
    try:
        length = int(message.text)
    except:
        await message.answer(text=i18n.give_subscription_length_error())
        return
    data = await state.get_data()
    await state.update_data(days=length)

    # Получаем информацию о пользователе для проверки существующей подписки
    user_data = None
    if data.get('user_id'):
        user_data = await get_user(telegram_id=data.get('user_id'))
    elif data.get('username'):
        # Нужно найти пользователя по username через отдельный запрос
        from models.orm import async_session, User
        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(
                select(User).filter(User.username == data.get('username'))
            )
            user = result.scalar_one_or_none()
            if user:
                user_data = await get_user(telegram_id=user.telegram_id)

    # Проверяем есть ли активная подписка
    if user_data and user_data.get('subscription') == 'True':
        # Формируем сообщение с информацией о текущей подписке
        start_date = user_data.get('start_date')
        end_date = user_data.get('end_date')
        sub_type = user_data.get('subscription_type') or 'не указан'
        sub_id = user_data.get('subscription_id') or 'нет'
        autopay = '✅ Да' if user_data.get('subscription_autopay') else '❌ Нет'

        start_date_str = start_date.strftime('%Y-%m-%d %H:%M:%S') if start_date else 'не указана'
        end_date_str = end_date.strftime('%Y-%m-%d %H:%M:%S') if end_date else 'не указана'

        warning_text = (
            f"⚠️ <b>Внимание!</b> У пользователя уже есть активная подписка.\n\n"
            f"<b>Текущая подписка:</b>\n"
            f"├ Тип: <code>{sub_type}</code>\n"
            f"├ ID подписки: <code>{sub_id}</code>\n"
            f"├ Дата начала: <code>{start_date_str}</code>\n"
            f"├ Дата окончания: <code>{end_date_str}</code>\n"
            f"└ Автопродление: {autopay}\n\n"
            f"<b>Вы хотите выдать:</b> {length} дней\n\n"
            f"⚠️ Если подтвердите, текущая подписка будет перезаписана на 'manual' тип."
        )

        await message.answer(
            text=warning_text,
            reply_markup=confirm_give_subscription_keyboard(i18n),
            parse_mode='HTML'
        )
        await state.set_state(AdminGiveSubscription.waiting_for_confirmation)
    else:
        # Подписки нет, выдаём сразу
        result = await give_subscription(
            telegram_id=data.get('user_id'),
            username=data.get('username'),
            days=length,
            i18n=i18n
        )
        await message.answer(text=result['message'])
        if result['result']:
            await bot.send_message(chat_id=result['user_id'], text=i18n.subscription_success())
            await state.clear()


@router.callback_query(F.data == 'confirm_give_subscription')
async def process_confirm_give_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    """Подтверждение перезаписи существующей подписки"""
    if not is_admin(callback.from_user.id):
        return

    data = await state.get_data()
    result = await give_subscription(
        telegram_id=data.get('user_id'),
        username=data.get('username'),
        days=data.get('days'),
        i18n=i18n
    )
    await callback.message.edit_text(text=result['message'])
    if result['result']:
        await bot.send_message(chat_id=result['user_id'], text=i18n.subscription_success())
        await state.clear()


@router.callback_query(F.data == 'cancel_give_subscription')
async def process_cancel_give_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    """Отмена выдачи подписки"""
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(text="❌ Выдача подписки отменена.")
    await state.clear()

@router.callback_query(F.data.startswith('statistic_data|unique_payments'))
async def process_unique_payment_source_statistics(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    subscription_type = callback.data.split('|')[-1]
    
    # Show time period selection menu
    await callback.message.edit_text(
        text=i18n.select_time_period_unique_payments(),
        reply_markup=time_period_menu(i18n=i18n, data_type='unique_payments', subscription_type=subscription_type)
    )

async def send_long_message(callback: CallbackQuery, text: str, reply_markup=None, link_preview_disabled: bool = True):
    """
    Отправляет длинное сообщение, разбивая его на части если необходимо.
    
    Args:
        callback: Callback query для отправки сообщения
        text: Текст для отправки
        reply_markup: Клавиатура (добавляется только к последнему сообщению)
        link_preview_disabled: Отключить предпросмотр ссылок
    """
    message_parts = split_long_message(text)
    
    # Первое сообщение заменяет текущее
    link_preview_options = LinkPreviewOptions(is_disabled=True) if link_preview_disabled else None
    
    if len(message_parts) == 1:
        # Если сообщение одно, просто отправляем его с клавиатурой
        await callback.message.edit_text(
            text=message_parts[0],
            reply_markup=reply_markup,
            link_preview_options=link_preview_options
        )
    else:
        # Если сообщений несколько
        # Первое сообщение редактируем
        await callback.message.edit_text(
            text=message_parts[0],
            link_preview_options=link_preview_options
        )
        
        # Средние сообщения отправляем как новые
        for i in range(1, len(message_parts) - 1):
            await callback.message.answer(
                text=message_parts[i],
                link_preview_options=link_preview_options
            )
        
        # Последнее сообщение отправляем с клавиатурой
        await callback.message.answer(
            text=message_parts[-1],
            reply_markup=reply_markup,
            link_preview_options=link_preview_options
        )


async def display_sources_paginated(callback: CallbackQuery, source_data: list, data_type: str, page: int, 
                                  i18n: TranslatorRunner, subscription: bool = False, 
                                  period_text: str = None, subscription_text: str = None, **kwargs):
    """
    Отображает источники с пагинацией.
    
    Args:
        callback: Callback query
        source_data: Данные источников
        data_type: Тип данных ('sources', 'subscriptions', 'payments', 'unique_payments')
        page: Номер страницы
        i18n: Переводчик
        subscription: Флаг подписки
        period_text: Текст периода для статистики оплат
        subscription_text: Текст типа подписки для статистики оплат
        **kwargs: Дополнительные параметры для callback данных
    """
    per_page = 50
    
    # Получаем данные для страницы
    list_text, total_pages, has_previous, has_next = sources_to_str_paginated(
        sources=source_data,
        page=page,
        per_page=per_page,
        i18n=i18n,
        subscription=subscription
    )
    
    # Формируем текст сообщения в зависимости от типа данных
    if data_type == 'sources':
        full_text = i18n.source_statistic_paginated(
            list=list_text,
            current_page=page,
            total_pages=total_pages
        )
    elif data_type == 'subscriptions':
        full_text = i18n.source_with_subscription_paginated(
            list=list_text,
            current_page=page,
            total_pages=total_pages
        )
    elif data_type == 'payments':
        full_text = i18n.payments_sources_statistic_with_period_paginated(
            list=list_text,
            period=period_text or "",
            subscription_type=subscription_text or "",
            current_page=page,
            total_pages=total_pages
        )
    elif data_type == 'unique_payments':
        full_text = i18n.unique_payments_sources_statistic_with_period_paginated(
            list=list_text,
            period=period_text or "",
            subscription_type=subscription_text or "",
            current_page=page,
            total_pages=total_pages
        )
    else:
        full_text = list_text
    
    # Создаем клавиатуру с пагинацией
    # Фильтруем kwargs для избежания превышения лимита callback_data
    keyboard_kwargs = {k: v for k, v in kwargs.items() if k not in ['period_text', 'subscription_text']}
    reply_markup = statistic_source_menu_paginated(
        i18n=i18n,
        data_type=data_type,
        page=page,
        total_pages=total_pages,
        has_previous=has_previous,
        has_next=has_next,
        **keyboard_kwargs
    )
    
    # Отправляем сообщение
    await callback.message.edit_text(
        text=full_text,
        reply_markup=reply_markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

########################################################################################################################
# Data Export Handlers
########################################################################################################################

@router.callback_query(F.data == 'data_export_menu')
async def process_data_export_menu(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    await callback.message.edit_text(
        text=i18n.data_export_menu(),
        reply_markup=data_export_menu(i18n)
    )

@router.callback_query(F.data == 'export_telegram_ids')
async def process_export_telegram_ids(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    try:
        await callback.message.edit_text(text=i18n.export_preparing())
        # Получаем всех пользователей
        users = await get_users()
        
        # Создаем файл с telegram_id
        telegram_ids = [user['telegram_id'] for user in users]
        file_content = '\n'.join(telegram_ids)
        
        # Создаем файл в памяти
        file_bytes = file_content.encode('utf-8')
        file_buffer = BufferedInputFile(file_bytes, filename='whisper_telegram_ids.txt')
        
        # Отправляем файл
        await callback.message.answer_document(
            document=file_buffer,
            caption=i18n.export_telegram_ids_success()
        )
        
        # Возвращаемся в меню статистики
        await callback.message.edit_text(
            text=i18n.data_export_menu(),
            reply_markup=data_export_menu(i18n)
        )
        
    except Exception as e:
        logger.error(f"Error exporting telegram IDs: {e}")
        await callback.message.edit_text(
            text=i18n.export_telegram_ids_error(error=str(e)),
            reply_markup=data_export_menu(i18n)
        )

@router.callback_query(F.data == 'export_sources_excel')
async def process_export_sources_excel(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return
    
    try:
        await callback.message.edit_text(text=i18n.export_preparing())
        
        # Получаем данные источников
        sources_data = await get_sources()
        
        # Создаем DataFrame с данными
        import pandas as pd
        from datetime import datetime
        
        # Конвертируем данные в список словарей для DataFrame
        data_for_df = []
        for i, (source, count) in enumerate(sources_data, 1):
            data_for_df.append({
                '№': i,
                'Источник': source,
                'Количество заходов': count
            })
        
        # Создаем DataFrame
        df = pd.DataFrame(data_for_df)
        
        # Создаем файл Excel в памяти
        excel_buffer = io.BytesIO()
        
        # Записываем данные в Excel с форматированием
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Статистика источников', index=False)
            
            # Получаем рабочую книгу и лист для форматирования
            workbook = writer.book
            worksheet = writer.sheets['Статистика источников']
            
            # Устанавливаем ширину колонок
            worksheet.column_dimensions['A'].width = 5   # №
            worksheet.column_dimensions['B'].width = 50  # Источник
            worksheet.column_dimensions['C'].width = 20  # Количество заходов
            
            # Добавляем заголовок с информацией о выгрузке
            from openpyxl.styles import Font, Alignment
            
            # Вставляем строки сверху для заголовка
            worksheet.insert_rows(1, 3)
            
            # Заголовок
            title_cell = worksheet['A1']
            title_cell.value = f"Статистика источников заходов - {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            title_cell.font = Font(bold=True, size=14)
            
            # Общее количество источников
            total_cell = worksheet['A2']
            total_cell.value = f"Всего источников: {len(sources_data)}"
            total_cell.font = Font(bold=True)
            
            # Общее количество заходов
            total_visits = sum(count for _, count in sources_data)
            total_visits_cell = worksheet['A3']
            total_visits_cell.value = f"Общее количество заходов: {total_visits}"
            total_visits_cell.font = Font(bold=True)
            
            # Форматируем заголовки таблицы (теперь в строке 4)
            for col in range(1, 4):  # A, B, C
                cell = worksheet.cell(row=4, column=col)
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal='center')
        
        excel_buffer.seek(0)
        
        # Создаем BufferedInputFile для отправки
        current_time = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'sources_statistics_{current_time}.xlsx'
        file_buffer = BufferedInputFile(excel_buffer.getvalue(), filename=filename)
        
        # Отправляем файл
        await callback.message.answer_document(
            document=file_buffer,
            caption=i18n.export_sources_excel_success()
        )
        
        # Возвращаемся в меню экспорта
        await callback.message.edit_text(
            text=i18n.data_export_menu(),
            reply_markup=data_export_menu(i18n)
        )
        
    except Exception as e:
        logger.error(f"Error exporting sources to Excel: {e}")
        await callback.message.edit_text(
            text=i18n.export_sources_excel_error(error=str(e)),
            reply_markup=data_export_menu(i18n)
        )

########################################################################################################################
# Logs Handlers
########################################################################################################################

@router.callback_query(F.data == 'logs_menu')
async def process_logs_menu(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return

    await callback.message.edit_text(
        text=i18n.logs_menu(),
        reply_markup=logs_time_menu(i18n)
    )

@router.callback_query(F.data.startswith('logs_download|'))
async def process_logs_download(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    if not is_admin(callback.from_user.id):
        return

    period = callback.data.split('|')[1]

    await callback.message.edit_text(text=i18n.logs_sending())

    config = get_config()
    service_name = config.tg_bot.service_name

    try:
        # Construct command
        # We use --since to get logs for the specified period
        cmd = f"journalctl -u {service_name} --since '{period} ago' --no-pager"

        # Execute command
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if stdout:
            # Create file in memory
            current_time = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{service_name.replace('.service', '')}_logs_{period.replace(' ', '_')}_{current_time}.txt"
            file_buffer = BufferedInputFile(stdout, filename=filename)

            await callback.message.answer_document(
                document=file_buffer,
                caption=f"Logs for last {period}"
            )

            # Return to logs menu
            await callback.message.answer(
                text=i18n.logs_menu(),
                reply_markup=logs_time_menu(i18n)
            )
        else:
             # If stderr is present, it might be an error or just no logs
             if stderr:
                 logger.error(f"Journalctl stderr: {stderr.decode()}")

             await callback.message.edit_text(
                text=i18n.logs_empty(),
                reply_markup=logs_time_menu(i18n)
            )

    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        await callback.message.edit_text(
            text=i18n.logs_error(error=str(e)),
            reply_markup=logs_time_menu(i18n)
        )

########################################################################################################################
# Pagination Handlers
########################################################################################################################

@router.callback_query(F.data.startswith('source_page|'))
async def process_source_page_navigation(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    """
    Обработчик навигации по страницам статистики источников.
    
    Формат callback_data: source_page|{data_type}|{page}|param1:value1|param2:value2...
    """
    if not is_admin(callback.from_user.id):
        return
    
    try:
        # Парсим callback данные
        parts = callback.data.split('|')
        data_type = parts[1]  # sources, subscriptions, payments, unique_payments
        page = int(parts[2])
        
        # Маппинг сокращенных имен параметров обратно на полные
        param_reverse_mapping = {
            'st': 'subscription_type',
            'pd': 'period_days'
        }
        
        # Парсим дополнительные параметры
        params = {}
        for part in parts[3:]:
            if ':' in part:
                short_key, value = part.split(':', 1)
                # Преобразуем сокращенное имя обратно в полное
                key = param_reverse_mapping.get(short_key, short_key)
                params[key] = value
        
        # Получаем данные в зависимости от типа
        if data_type == 'sources':
            source_data = await get_sources()
            await display_sources_paginated(
                callback=callback,
                source_data=source_data,
                data_type=data_type,
                page=page,
                i18n=i18n,
                subscription=False
            )
            
        elif data_type == 'subscriptions':
            subscription_type = params.get('subscription_type', 'all')
            source_data = await get_sources_with_subscription(
                subscription_type=subscription_type if subscription_type != 'all' else None
            )
            await display_sources_paginated(
                callback=callback,
                source_data=source_data,
                data_type=data_type,
                page=page,
                i18n=i18n,
                subscription=True,
                subscription_type=subscription_type
            )
            
        elif data_type == 'payments':
            subscription_type = params.get('subscription_type', 'all')
            period_days = int(params.get('period_days', 0))
            
            source_data = await get_payments_sources(
                unique=False,
                subscription_type=subscription_type if subscription_type != 'all' else None,
                period_days=period_days if period_days > 0 else None
            )
            
            # Format period and subscription texts
            period_text = ""
            if period_days == 7:
                period_text = i18n.last_7_days()
            elif period_days == 30:
                period_text = i18n.last_30_days()
            else:
                period_text = i18n.all_time_period()
            
            subscription_text = ""
            if subscription_type == 'weekly':
                subscription_text = i18n.weekly_subscriptions()
            elif subscription_type == 'monthly':
                subscription_text = i18n.monthly_subscriptions()
            else:
                subscription_text = i18n.all_subscriptions()
            
            await display_sources_paginated(
                callback=callback,
                source_data=source_data,
                data_type=data_type,
                page=page,
                i18n=i18n,
                subscription=True,
                period_text=period_text,
                subscription_text=subscription_text,
                subscription_type=subscription_type,
                period_days=period_days
            )
            
        elif data_type == 'unique_payments':
            subscription_type = params.get('subscription_type', 'all')
            period_days = int(params.get('period_days', 0))
            
            source_data = await get_payments_sources(
                unique=True,
                subscription_type=subscription_type if subscription_type != 'all' else None,
                period_days=period_days if period_days > 0 else None
            )
            
            # Format period and subscription texts
            period_text = ""
            if period_days == 7:
                period_text = i18n.last_7_days()
            elif period_days == 30:
                period_text = i18n.last_30_days()
            else:
                period_text = i18n.all_time_period()
            
            subscription_text = ""
            if subscription_type == 'weekly':
                subscription_text = i18n.weekly_subscriptions()
            elif subscription_type == 'monthly':
                subscription_text = i18n.monthly_subscriptions()
            else:
                subscription_text = i18n.all_subscriptions()
            
            await display_sources_paginated(
                callback=callback,
                source_data=source_data,
                data_type=data_type,
                page=page,
                i18n=i18n,
                subscription=True,
                period_text=period_text,
                subscription_text=subscription_text,
                subscription_type=subscription_type,
                period_days=period_days
            )
            
    except (ValueError, IndexError) as e:
        logger.error(f"Error processing page navigation: {e}")
        await callback.answer(text="Произошла ошибка при навигации по страницам")


@router.callback_query(F.data == 'page_info')
async def process_page_info_click(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    """
    Обработчик нажатия на неактивную кнопку с информацией о странице.
    Просто отвечаем пустым ответом, чтобы убрать индикатор загрузки.
    """
    await callback.answer()