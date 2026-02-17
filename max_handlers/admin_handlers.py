import asyncio
import io
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

import aiohttp
import aiofiles

from maxapi import Router, F
from maxapi.context import MemoryContext
from maxapi.types import MessageCreated, MessageCallback, CallbackButton
from maxapi.types.message import Message
from maxapi.types.input_media import InputMediaBuffer
from maxapi.types.attachments.file import File as MaxFileAttachment
from maxapi.types.attachments.attachment import ButtonsPayload
from maxapi.enums.parse_mode import ParseMode
from fluentogram import TranslatorRunner

from config_data.config import get_config
from max_keyboards.admin_keyboards import (
    admin_menu, confirm_spam_keyboard, spam_menu, statistic_source_menu,
    cancel_subscription_keyboard, sub_type_menu, time_period_menu, data_export_menu,
    statistic_source_menu_paginated, logs_time_menu, confirm_give_subscription_keyboard,
    _kb,
)
from models.orm import (
    get_payments_sources, get_sources_with_subscription, get_users, is_admin,
    get_statistics, get_sources, give_subscription, get_user_id_range,
    update_user_blocked_status, engine, get_users_to_exclude_from_broadcast, get_user,
)
from services.init_max_bot import max_bot
from services.services import sources_to_str, split_long_message, sources_to_str_paginated
from max_states.states import AdminSpamSession, AdminGiveSubscription
from services.telegram_alerts import send_alert

logger = logging.getLogger(__name__)
spam_logger = logging.getLogger('spam')


# ---------------------------------------------------------------------------
# DB pool status check
# ---------------------------------------------------------------------------

async def check_real_pool_status():
    try:
        from sqlalchemy import text
        import time

        pool = engine.pool
        pool_size = getattr(pool, '_pool_size', 20)
        max_overflow = getattr(pool, '_max_overflow', 30)

        available = 0
        active = 0

        if hasattr(pool, '_pool') and pool._pool:
            try:
                available = pool._pool.qsize()
            except Exception:
                pass

        try:
            checked_in = pool.checkedin()
            checked_out = pool.checkedout()
            if checked_in >= 0:
                available = max(available, checked_in)
            if checked_out >= 0:
                active = checked_out
        except Exception:
            pass

        test_start = time.time()
        test_passed = False
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
                test_passed = True
        except Exception:
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
            'test_duration': test_duration,
        }
    except Exception as e:
        return {'error': str(e)}


# ---------------------------------------------------------------------------
# Spam logger setup
# ---------------------------------------------------------------------------

_spam_log_queue = None
_spam_queue_listener = None


def setup_spam_logger(campaign_id=None):
    global _spam_log_queue, _spam_queue_listener

    os.makedirs('logs', exist_ok=True)

    if _spam_queue_listener is not None:
        _spam_queue_listener.stop()
        _spam_queue_listener = None

    spam_logger.handlers = []
    spam_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    import queue
    from logging.handlers import QueueHandler, QueueListener

    _spam_log_queue = queue.Queue(-1)

    if campaign_id:
        log_filename = f'logs/spam_campaign_{campaign_id}.log'
        file_handler = logging.FileHandler(log_filename, mode='w')
    else:
        current_date = datetime.now().strftime('%Y-%m-%d')
        log_filename = f'logs/spam_{current_date}.log'
        file_handler = logging.FileHandler(log_filename, mode='a')

    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    _spam_queue_listener = QueueListener(
        _spam_log_queue, file_handler, console_handler, respect_handler_level=True
    )
    _spam_queue_listener.start()

    queue_handler = QueueHandler(_spam_log_queue)
    spam_logger.addHandler(queue_handler)

    if campaign_id:
        spam_logger.info(f"Started new spam campaign log: {campaign_id}")
    else:
        spam_logger.info("Appending to daily spam log")

    return spam_logger


def stop_spam_logger():
    global _spam_queue_listener
    if _spam_queue_listener is not None:
        _spam_queue_listener.stop()
        _spam_queue_listener = None


router = Router()


# ---------------------------------------------------------------------------
# /pool command
# ---------------------------------------------------------------------------

@router.message_created(F.message.body.text == '/pool')
async def process_pool_command(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.message.sender.user_id):
        await event.message.answer(text="❌ Доступ запрещен")
        return

    status_msg_result = await event.message.answer(text="🔄 Проверяю состояние connection pool...")
    status_msg = status_msg_result.message if status_msg_result else None

    try:
        pool_info = await check_real_pool_status()

        if 'error' in pool_info:
            if status_msg:
                await status_msg.edit(text=f"❌ Ошибка проверки pool:\n{pool_info['error']}")
            return

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
        if pool_info['utilization'] > 95:
            text += "\n🚨 <b>КРИТИЧНО:</b> Pool практически исчерпан!"
        elif pool_info['utilization'] > 80:
            text += "\n⚠️ <b>Предупреждение:</b> Высокая утилизация pool!"

        if status_msg:
            await status_msg.edit(text=text, parse_mode=ParseMode.HTML)

    except Exception as e:
        if status_msg:
            await status_msg.edit(text=f"❌ Ошибка: {e}")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

async def _show_statistics(user_id: int, message, i18n: TranslatorRunner, is_edit: bool = False):
    if not is_admin(user_id):
        return

    usage_data: dict = await get_statistics()
    text = i18n.statistics_menu(
        users_count=usage_data['users_count'],
        users_with_action=usage_data['users_with_action'],
        audios_num=usage_data['voice_uses'],
        gpts_num=usage_data['gpt_uses'],
        active_sessions=usage_data['active_sessions'],
        active_subs=usage_data['active_subs'],
        weekly_subs=usage_data['weekly_subs'],
        monthly_subs=usage_data['monthly_subs'],
        annual_subs=usage_data['annual_subs'],
        manual_subs=usage_data['manual_subs'],
        unblocked_users_count=usage_data['unblocked_users_count'],
    )
    if is_edit:
        await message.edit(text=text, attachments=[admin_menu(i18n)])
    else:
        await message.answer(text=text, attachments=[admin_menu(i18n)])


@router.message_callback(F.callback.payload == 'statistics_menu')
async def process_statistics_callback(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await context.clear()
    await _show_statistics(
        user_id=event.callback.user.user_id,
        message=event.message,
        i18n=i18n,
        is_edit=True,
    )


@router.message_created(F.message.body.text == '/statistics')
async def process_statistics_command(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    await context.clear()
    await _show_statistics(
        user_id=event.message.sender.user_id,
        message=event.message,
        i18n=i18n,
        is_edit=False,
    )


# ---------------------------------------------------------------------------
# Source statistics
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'source_statistic')
async def process_source_statistic(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    source_data = await get_sources()
    await display_sources_paginated(
        event=event,
        source_data=source_data,
        data_type='sources',
        page=1,
        i18n=i18n,
        subscription=False,
    )


@router.message_callback(F.callback.payload.startswith('statistic_data_period|'))
async def process_choose_subscription_type_for_statistic(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    data_type = event.callback.payload.split('|')[1]
    if not is_admin(event.callback.user.user_id):
        return

    await event.message.edit(
        text=i18n.sources_of_what_type(),
        attachments=[sub_type_menu(data_type=data_type, i18n=i18n)],
    )


@router.message_callback(F.callback.payload.startswith('statistic_data|subscriptions'))
async def process_source_with_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return
    subscription_type = event.callback.payload.split('|')[-1]
    source_data = await get_sources_with_subscription(
        subscription_type=subscription_type if subscription_type != 'all' else None
    )

    await display_sources_paginated(
        event=event,
        source_data=source_data,
        data_type='subscriptions',
        page=1,
        i18n=i18n,
        subscription=True,
        subscription_type=subscription_type,
    )


@router.message_callback(F.callback.payload.startswith('statistic_data|payments'))
async def process_payment_source_statistics(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    subscription_type = event.callback.payload.split('|')[-1]
    await event.message.edit(
        text=i18n.select_time_period(),
        attachments=[time_period_menu(i18n=i18n, data_type='payments', subscription_type=subscription_type)],
    )


@router.message_callback(F.callback.payload.startswith('statistic_data_time|payments'))
async def process_payment_source_with_time_filter(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    parts = event.callback.payload.split('|')
    subscription_type = parts[2]
    period_days = int(parts[3])

    source_data = await get_payments_sources(
        unique=False,
        subscription_type=subscription_type if subscription_type != 'all' else None,
        period_days=period_days if period_days > 0 else None,
    )

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
        event=event,
        source_data=source_data,
        data_type='payments',
        page=1,
        i18n=i18n,
        subscription=True,
        period_text=period_text,
        subscription_text=subscription_text,
        subscription_type=subscription_type,
        period_days=period_days,
    )


@router.message_callback(F.callback.payload.startswith('statistic_data_time|unique_payments'))
async def process_unique_payment_source_with_time_filter(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    parts = event.callback.payload.split('|')
    subscription_type = parts[2]
    period_days = int(parts[3])

    source_data = await get_payments_sources(
        unique=True,
        subscription_type=subscription_type if subscription_type != 'all' else None,
        period_days=period_days if period_days > 0 else None,
    )

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
        event=event,
        source_data=source_data,
        data_type='unique_payments',
        page=1,
        i18n=i18n,
        subscription=True,
        period_text=period_text,
        subscription_text=subscription_text,
        subscription_type=subscription_type,
        period_days=period_days,
    )


@router.message_callback(F.callback.payload.startswith('statistic_data|unique_payments'))
async def process_unique_payment_source_statistics(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    subscription_type = event.callback.payload.split('|')[-1]
    await event.message.edit(
        text=i18n.select_time_period_unique_payments(),
        attachments=[time_period_menu(i18n=i18n, data_type='unique_payments', subscription_type=subscription_type)],
    )


# ---------------------------------------------------------------------------
# Spam
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'spam_menu')
async def process_spam_menu(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    id_range = await get_user_id_range()
    await event.message.edit(
        text=i18n.enter_start_id(min_id=id_range['min_id'], max_id=id_range['max_id']),
        attachments=[spam_menu(i18n, skip=True)],
    )
    await context.set_state(AdminSpamSession.waiting_start_id)


@router.message_created(AdminSpamSession.waiting_start_id)
async def process_start_id(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.message.sender.user_id):
        return

    text = event.message.body.text if event.message.body else ''
    try:
        start_id, end_id = text.split('-')
    except ValueError:
        await event.message.answer(text=i18n.invalid_start_id_splitting())
        return

    start_id = int(start_id) if start_id else None
    end_id = int(end_id) if end_id else None
    await context.update_data(start_id=start_id, end_id=end_id)

    await event.message.answer(
        text=i18n.choose_users_for_spam(),
        attachments=[spam_menu(i18n)],
    )
    await context.set_state(AdminSpamSession.waiting_spam_message)


@router.message_callback(F.callback.payload == 'skip_start_id')
async def process_skip_start_id(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    await event.message.edit(
        text=i18n.choose_users_for_spam(),
        attachments=[spam_menu(i18n)],
    )
    await context.set_state(AdminSpamSession.waiting_spam_message)


@router.message_callback(F.callback.payload.startswith('spam_'))
async def process_spam_selection(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    spam_type = event.callback.payload
    state_data = await context.get_data()
    start_id = state_data.get('start_id', None)
    end_id = state_data.get('end_id', None)

    users = await get_users()

    if start_id and end_id:
        users = [user for user in users if start_id <= int(user['id']) <= end_id]
    elif start_id:
        users = [user for user in users if start_id <= int(user['id'])]
    elif end_id:
        users = [user for user in users if end_id >= int(user['id'])]

    users = [user for user in users if not user.get('is_bot_blocked', False)]

    if spam_type == 'spam_subscribed':
        users = [user for user in users if user['subscription'] == 'True']
    elif spam_type == 'spam_unsubscribed':
        users = [user for user in users if user['subscription'] != 'True']

    exclusion_data = await get_users_to_exclude_from_broadcast()
    excluded_user_ids = exclusion_data['user_ids']
    exclusion_stats = exclusion_data['stats']

    users_before_exclusion = len(users)
    users = [user for user in users if int(user['id']) not in excluded_user_ids]
    users_excluded_count = users_before_exclusion - len(users)

    await context.update_data(
        spam_type=spam_type,
        target_users=users,
        exclusion_stats={
            'excluded_count': users_excluded_count,
            'recent_reminders': exclusion_stats['recent_reminders'],
            'upcoming_reminders': exclusion_stats['upcoming_reminders'],
            'breakdown': exclusion_stats['breakdown'],
        },
    )

    detail_text = ""
    if users_excluded_count > 0:
        detail_text = f"\n\n🔔 <b>Исключено из рассылки:</b> {users_excluded_count} чел."
        detail_text += f"\n├ Получили напоминания (24ч): {exclusion_stats['recent_reminders']}"
        detail_text += f"\n└ Получат напоминания (24ч): {exclusion_stats['upcoming_reminders']}"

    try:
        await event.message.edit(
            text=i18n.spam_menu(users_num=len(users)) + detail_text,
            attachments=[spam_menu(i18n, show_exclude_button=True)],
            parse_mode=ParseMode.HTML,
        )
        await context.set_state(AdminSpamSession.waiting_spam_message)
    except Exception:
        try:
            await event.answer()
        except Exception:
            pass


@router.message_created(AdminSpamSession.waiting_spam_message)
async def process_spam_message(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.message.sender.user_id):
        return

    state_data = await context.get_data()
    target_users = state_data.get('target_users', [])
    exclusion_stats = state_data.get('exclusion_stats', {})

    # Store the message text for broadcasting (Max doesn't have copy_message)
    spam_text = event.message.body.text if event.message.body else ''
    await context.update_data(spam_message_text=spam_text)

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

    await event.message.answer(
        text=confirmation_text,
        attachments=[confirm_spam_keyboard(i18n)],
        parse_mode=ParseMode.HTML,
    )


@router.message_callback(F.callback.payload == 'confirm_spam')
async def process_confirm_spam(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    state_data = await context.get_data()
    target_users = state_data.get('target_users', [])
    spam_text = state_data.get('spam_message_text', '')
    exclusion_stats = state_data.get('exclusion_stats', {})

    campaign_id = f"{event.callback.user.user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    setup_spam_logger(campaign_id)

    await event.message.edit(text=i18n.spam_start())

    batch_size = 15
    user_batches = [target_users[i:i + batch_size] for i in range(0, len(target_users), batch_size)]

    total_sent = 0
    spam_logger.info(f"Starting spam campaign {campaign_id}")
    spam_logger.info(f"Target users count: {len(target_users)}")
    spam_logger.info(f"Admin ID: {event.callback.user.user_id}")
    spam_logger.info(f"Message type: text")

    if exclusion_stats:
        spam_logger.info(f"Exclusion stats: {exclusion_stats.get('excluded_count', 0)} users excluded")

    try:
        alert_text = (
            f"<b>Starting spam campaign</b> {campaign_id}.\n"
            f"<b>Target users count:</b> {len(target_users)}.\n"
            f"<b>Admin ID:</b> {event.callback.user.user_id}"
        )
        if exclusion_stats and exclusion_stats.get('excluded_count', 0) > 0:
            alert_text += f"\n<b>Excluded (reminders):</b> {exclusion_stats['excluded_count']}"
        await send_alert(text=alert_text, topic="SPAM", level="INFO", fingerprint=f"spam_campaign_{campaign_id}")
    except Exception as e:
        spam_logger.error(f"Failed to send alert: {e}")

    for batch_index, batch in enumerate(user_batches):
        spam_logger.info(f"Processing batch {batch_index + 1}/{len(user_batches)}")
        coros = [spam_gather(spam_text, int(user['telegram_id']), user) for user in batch]
        results = await asyncio.gather(*coros)
        successful_sends = sum(1 for result in results if result)
        total_sent += successful_sends

        spam_logger.info(f"Batch {batch_index + 1} completed: {successful_sends}/{len(batch)} successful")
        await asyncio.sleep(1)

    spam_logger.info(f"Spam campaign completed. Total sent: {total_sent}/{len(target_users)}")

    result_text = i18n.spam_success(total_sent=total_sent)

    if exclusion_stats and exclusion_stats.get('excluded_count', 0) > 0:
        result_text += f"\n\n📊 <b>Статистика исключений:</b>"
        result_text += f"\n├ Всего исключено: {exclusion_stats['excluded_count']}"
        result_text += f"\n├ Получили напоминания (24ч): {exclusion_stats['recent_reminders']}"
        result_text += f"\n└ Получат напоминания (24ч): {exclusion_stats['upcoming_reminders']}"

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
        alert_text = (
            f"<b>Spam campaign</b> {campaign_id} completed.\n"
            f"<b>Total sent:</b> {total_sent}/{len(target_users)}.\n"
            f"<b>Admin ID:</b> {event.callback.user.user_id}"
        )
        await send_alert(text=alert_text, topic="SPAM", level="INFO", fingerprint=f"spam_campaign_{campaign_id}")
    except Exception as e:
        spam_logger.error(f"Failed to send alert: {e}")

    await event.message.answer(text=result_text, parse_mode=ParseMode.HTML)
    await context.clear()


@router.message_callback(F.callback.payload == 'continue_to_message')
async def process_continue_to_message(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    state_data = await context.get_data()
    target_users = state_data.get('target_users', [])
    exclusion_stats = state_data.get('exclusion_stats', {})

    msg_text = f"👥 Выбрано пользователей для рассылки: {len(target_users)}\n"
    if exclusion_stats:
        if exclusion_stats.get('excluded_count', 0) > 0:
            msg_text += f"\n🔔 Исключено (напоминания): {exclusion_stats['excluded_count']}"
        if exclusion_stats.get('manual_excluded', 0) > 0:
            msg_text += f"\n📂 Исключено (файл): {exclusion_stats['manual_excluded']}"

    msg_text += "\n\n📝 Отправьте сообщение для рассылки:"

    await event.message.edit(text=msg_text)
    await context.set_state(AdminSpamSession.waiting_spam_message)


@router.message_callback(F.callback.payload == 'exclude_ids')
async def process_exclude_ids(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    await event.message.edit(
        text="📂 Отправьте текстовый файл с ID пользователей для исключения из рассылки.\n\n"
             "⚠️ ВАЖНО: Используйте внутренние ID из базы данных (поле 'id'), НЕ telegram_id!\n\n"
             "Каждый ID должен быть на отдельной строке.\n"
             "Пример:\n"
             "132739\n"
             "63963\n"
             "109230",
        attachments=[_kb([CallbackButton(text="❌ Отмена", payload='statistics_menu')])],
    )
    await context.set_state(AdminSpamSession.waiting_exclude_file)


@router.message_created(AdminSpamSession.waiting_exclude_file)
async def process_exclude_file(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.message.sender.user_id):
        return

    cancel_kb = _kb([CallbackButton(text="❌ Отмена", payload='statistics_menu')])

    # Check for file attachment
    attachments = event.message.body.attachments if event.message.body else []
    attachments = attachments or []
    file_att = next((a for a in attachments if isinstance(a, MaxFileAttachment)), None)

    if not file_att:
        await event.message.answer(
            text="❌ Пожалуйста, отправьте текстовый файл.",
            attachments=[cancel_kb],
        )
        return

    file_size = getattr(file_att.payload, 'size', 0) or 0
    if file_size > 1024 * 1024:
        await event.message.answer(
            text="❌ Файл слишком большой. Максимальный размер: 1MB",
            attachments=[cancel_kb],
        )
        return

    try:
        # Download file via URL
        file_url = getattr(file_att.payload, 'url', None)
        if not file_url:
            await event.message.answer(text="❌ Не удалось получить URL файла.", attachments=[cancel_kb])
            return

        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                file_content_bytes = await resp.read()

        content = file_content_bytes.decode('utf-8')

        exclude_ids = []
        lines = content.strip().split('\n')

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if line:
                try:
                    user_id = int(line)
                    exclude_ids.append(user_id)
                except ValueError:
                    await event.message.answer(
                        text=f"❌ Ошибка в строке {line_num}: '{line}' не является валидным ID.\nID должны быть числами.",
                        attachments=[cancel_kb],
                    )
                    return

        if not exclude_ids:
            await event.message.answer(
                text="❌ В файле не найдено ни одного валидного ID.",
                attachments=[cancel_kb],
            )
            return

        await context.update_data(exclude_ids=exclude_ids)

        state_data = await context.get_data()
        target_users = state_data.get('target_users', [])
        exclusion_stats = state_data.get('exclusion_stats', {})

        original_count = len(target_users)
        filtered_users = [user for user in target_users if int(user['id']) not in exclude_ids]
        excluded_count = original_count - len(filtered_users)

        if exclusion_stats:
            exclusion_stats['manual_excluded'] = excluded_count
        else:
            exclusion_stats = {
                'excluded_count': 0,
                'manual_excluded': excluded_count,
                'recent_reminders': 0,
                'upcoming_reminders': 0,
                'breakdown': {'recent': {}, 'upcoming': {}},
            }

        await context.update_data(target_users=filtered_users, exclusion_stats=exclusion_stats)

        stats_msg = "✅ Файл обработан успешно!\n\n"
        stats_msg += "📊 Статистика:\n"
        stats_msg += f"• Исходное количество пользователей: {original_count}\n"
        stats_msg += f"• Исключено из файла: {excluded_count}\n"

        if exclusion_stats.get('excluded_count', 0) > 0:
            stats_msg += f"• Исключено (напоминания): {exclusion_stats['excluded_count']}\n"

        stats_msg += f"• <b>Итоговое количество для рассылки: {len(filtered_users)}</b>\n\n"
        stats_msg += "Выберите дальнейшее действие:"

        await event.message.answer(
            text=stats_msg,
            attachments=[spam_menu(i18n, show_exclude_button=True)],
            parse_mode=ParseMode.HTML,
        )
        await context.set_state(AdminSpamSession.waiting_spam_message)

    except Exception as e:
        logger.error(f"Error processing exclude file: {e}")
        await event.message.answer(
            text=f"❌ Ошибка при обработке файла: {str(e)}",
            attachments=[cancel_kb],
        )


async def spam_gather(spam_text: str, telegram_id: int, user_info=None):
    """Send a broadcast message to a single user.

    Max doesn't have copy_message, so we re-send the text directly.
    TODO: Support multimedia broadcast messages (Phase 5).
    """
    user_id_str = f"{telegram_id}"
    if user_info:
        user_id_str = f"{telegram_id} (ID: {user_info.get('id', 'unknown')}, Username: {user_info.get('username', 'unknown')})"

    try:
        spam_logger.debug(f"Attempting to send message to user {user_id_str}")
        result = await max_bot.send_message(chat_id=telegram_id, text=spam_text)
        if result:
            spam_logger.debug(f"Successfully sent message to user {user_id_str}")
            return telegram_id, True
        return False
    except Exception as e:
        error_str = str(e)
        spam_logger.error(f"Failed to send message to user {user_id_str}: {error_str}")

        if "blocked" in error_str.lower() or "forbidden" in error_str.lower():
            spam_logger.warning(f"User {user_id_str} has blocked the bot. Updating database.")
            try:
                await update_user_blocked_status(telegram_id, True)
            except Exception as db_e:
                spam_logger.error(f"Failed to update user {user_id_str} blocked status: {db_e}")
            return False
        elif "deactivated" in error_str.lower():
            spam_logger.warning(f"User {user_id_str} account is deactivated.")
            try:
                await update_user_blocked_status(telegram_id, True)
            except Exception as db_e:
                spam_logger.error(f"Failed to update user {user_id_str} blocked status: {db_e}")
            return False
        else:
            spam_logger.error(f"Unknown error for user {user_id_str}: {error_str}")
            # Retry once
            try:
                spam_logger.warning(f"Retrying message send to user {user_id_str}")
                result = await max_bot.send_message(chat_id=telegram_id, text=spam_text)
                if result:
                    spam_logger.debug(f"Successfully sent message to user {user_id_str} on retry")
                    return telegram_id, True
                return False
            except Exception as retry_e:
                spam_logger.error(f"Failed to send message to user {user_id_str} on retry: {retry_e}")
                return False


# ---------------------------------------------------------------------------
# Give subscription
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'give_subscription')
async def process_give_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return
    await event.message.edit(
        text=i18n.give_subscription_desc(),
        attachments=[cancel_subscription_keyboard(i18n)],
    )
    await context.set_state(AdminGiveSubscription.waiting_for_user_data)


@router.message_created(AdminGiveSubscription.waiting_for_user_data)
async def process_give_subscription_user_data(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.message.sender.user_id):
        return

    text = event.message.body.text if event.message.body else ''
    try:
        user_id = int(text)
        await context.update_data(user_id=user_id, username=None)
    except (ValueError, TypeError):
        username = text.removeprefix("@")
        await context.update_data(username=username, user_id=None)

    await event.message.answer(
        text=i18n.give_subscription_length(),
        attachments=[cancel_subscription_keyboard(i18n)],
    )
    await context.set_state(AdminGiveSubscription.waiting_for_subscription_length)


@router.message_created(AdminGiveSubscription.waiting_for_subscription_length)
async def process_give_subscription_length(event: MessageCreated, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.message.sender.user_id):
        return

    text = event.message.body.text if event.message.body else ''
    try:
        length = int(text)
    except (ValueError, TypeError):
        await event.message.answer(text=i18n.give_subscription_length_error())
        return

    data = await context.get_data()
    await context.update_data(days=length)

    user_data = None
    if data.get('user_id'):
        user_data = await get_user(telegram_id=data.get('user_id'))
    elif data.get('username'):
        from models.orm import async_session, User
        from sqlalchemy import select
        async with async_session() as session:
            result = await session.execute(
                select(User).filter(User.username == data.get('username'))
            )
            user = result.scalar_one_or_none()
            if user:
                user_data = await get_user(telegram_id=user.telegram_id)

    if user_data and user_data.get('subscription') == 'True':
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

        await event.message.answer(
            text=warning_text,
            attachments=[confirm_give_subscription_keyboard(i18n)],
            parse_mode=ParseMode.HTML,
        )
        await context.set_state(AdminGiveSubscription.waiting_for_confirmation)
    else:
        result = await give_subscription(
            telegram_id=data.get('user_id'),
            username=data.get('username'),
            days=length,
            i18n=i18n,
        )
        await event.message.answer(text=result['message'])
        if result['result']:
            await max_bot.send_message(chat_id=result['user_id'], text=i18n.subscription_success())
            await context.clear()


@router.message_callback(F.callback.payload == 'confirm_give_subscription')
async def process_confirm_give_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    data = await context.get_data()
    result = await give_subscription(
        telegram_id=data.get('user_id'),
        username=data.get('username'),
        days=data.get('days'),
        i18n=i18n,
    )
    await event.message.edit(text=result['message'])
    if result['result']:
        await max_bot.send_message(chat_id=result['user_id'], text=i18n.subscription_success())
        await context.clear()


@router.message_callback(F.callback.payload == 'cancel_give_subscription')
async def process_cancel_give_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    await event.message.edit(text="❌ Выдача подписки отменена.")
    await context.clear()


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'data_export_menu')
async def process_data_export_menu(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    await event.message.edit(
        text=i18n.data_export_menu(),
        attachments=[data_export_menu(i18n)],
    )


@router.message_callback(F.callback.payload == 'export_telegram_ids')
async def process_export_telegram_ids(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    try:
        await event.message.edit(text=i18n.export_preparing())
        users = await get_users()

        telegram_ids = [user['telegram_id'] for user in users]
        file_content = '\n'.join(str(tid) for tid in telegram_ids)
        file_bytes = file_content.encode('utf-8')

        await event.message.answer(
            text=i18n.export_telegram_ids_success(),
            attachments=[InputMediaBuffer(buffer=file_bytes, filename='whisper_telegram_ids.txt')],
        )

        await event.message.edit(
            text=i18n.data_export_menu(),
            attachments=[data_export_menu(i18n)],
        )

    except Exception as e:
        logger.error(f"Error exporting telegram IDs: {e}")
        await event.message.edit(
            text=i18n.export_telegram_ids_error(error=str(e)),
            attachments=[data_export_menu(i18n)],
        )


@router.message_callback(F.callback.payload == 'export_sources_excel')
async def process_export_sources_excel(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    try:
        await event.message.edit(text=i18n.export_preparing())

        sources_data = await get_sources()

        import pandas as pd

        data_for_df = []
        for idx, (source, count) in enumerate(sources_data, 1):
            data_for_df.append({
                '№': idx,
                'Источник': source,
                'Количество заходов': count,
            })

        df = pd.DataFrame(data_for_df)
        excel_buffer = io.BytesIO()

        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Статистика источников', index=False)

            workbook = writer.book
            worksheet = writer.sheets['Статистика источников']
            worksheet.column_dimensions['A'].width = 5
            worksheet.column_dimensions['B'].width = 50
            worksheet.column_dimensions['C'].width = 20

            from openpyxl.styles import Font, Alignment

            worksheet.insert_rows(1, 3)
            title_cell = worksheet['A1']
            title_cell.value = f"Статистика источников заходов - {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            title_cell.font = Font(bold=True, size=14)

            total_cell = worksheet['A2']
            total_cell.value = f"Всего источников: {len(sources_data)}"
            total_cell.font = Font(bold=True)

            total_visits = sum(count for _, count in sources_data)
            total_visits_cell = worksheet['A3']
            total_visits_cell.value = f"Общее количество заходов: {total_visits}"
            total_visits_cell.font = Font(bold=True)

            for col in range(1, 4):
                cell = worksheet.cell(row=4, column=col)
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal='center')

        excel_buffer.seek(0)

        current_time = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'sources_statistics_{current_time}.xlsx'

        await event.message.answer(
            text=i18n.export_sources_excel_success(),
            attachments=[InputMediaBuffer(buffer=excel_buffer.getvalue(), filename=filename)],
        )

        await event.message.edit(
            text=i18n.data_export_menu(),
            attachments=[data_export_menu(i18n)],
        )

    except Exception as e:
        logger.error(f"Error exporting sources to Excel: {e}")
        await event.message.edit(
            text=i18n.export_sources_excel_error(error=str(e)),
            attachments=[data_export_menu(i18n)],
        )


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'logs_menu')
async def process_logs_menu(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    await event.message.edit(
        text=i18n.logs_menu(),
        attachments=[logs_time_menu(i18n)],
    )


@router.message_callback(F.callback.payload.startswith('logs_download|'))
async def process_logs_download(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    period = event.callback.payload.split('|')[1]
    await event.message.edit(text=i18n.logs_sending())

    config = get_config()
    service_name = config.tg_bot.service_name

    try:
        cmd = f"journalctl -u {service_name} --since '{period} ago' --no-pager"
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if stdout:
            current_time = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{service_name.replace('.service', '')}_logs_{period.replace(' ', '_')}_{current_time}.txt"

            await event.message.answer(
                text=f"Logs for last {period}",
                attachments=[InputMediaBuffer(buffer=stdout, filename=filename)],
            )

            await event.message.answer(
                text=i18n.logs_menu(),
                attachments=[logs_time_menu(i18n)],
            )
        else:
            if stderr:
                logger.error(f"Journalctl stderr: {stderr.decode()}")

            await event.message.edit(
                text=i18n.logs_empty(),
                attachments=[logs_time_menu(i18n)],
            )

    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        await event.message.edit(
            text=i18n.logs_error(error=str(e)),
            attachments=[logs_time_menu(i18n)],
        )


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

async def display_sources_paginated(event: MessageCallback, source_data: list, data_type: str,
                                     page: int, i18n: TranslatorRunner, subscription: bool = False,
                                     period_text: str = None, subscription_text: str = None, **kwargs):
    per_page = 50

    list_text, total_pages, has_previous, has_next = sources_to_str_paginated(
        sources=source_data,
        page=page,
        per_page=per_page,
        i18n=i18n,
        subscription=subscription,
    )

    if data_type == 'sources':
        full_text = i18n.source_statistic_paginated(
            list=list_text, current_page=page, total_pages=total_pages,
        )
    elif data_type == 'subscriptions':
        full_text = i18n.source_with_subscription_paginated(
            list=list_text, current_page=page, total_pages=total_pages,
        )
    elif data_type == 'payments':
        full_text = i18n.payments_sources_statistic_with_period_paginated(
            list=list_text, period=period_text or "", subscription_type=subscription_text or "",
            current_page=page, total_pages=total_pages,
        )
    elif data_type == 'unique_payments':
        full_text = i18n.unique_payments_sources_statistic_with_period_paginated(
            list=list_text, period=period_text or "", subscription_type=subscription_text or "",
            current_page=page, total_pages=total_pages,
        )
    else:
        full_text = list_text

    keyboard_kwargs = {k: v for k, v in kwargs.items() if k not in ['period_text', 'subscription_text']}
    reply_markup = statistic_source_menu_paginated(
        i18n=i18n,
        data_type=data_type,
        page=page,
        total_pages=total_pages,
        has_previous=has_previous,
        has_next=has_next,
        **keyboard_kwargs,
    )

    await event.message.edit(text=full_text, attachments=[reply_markup])


@router.message_callback(F.callback.payload.startswith('source_page|'))
async def process_source_page_navigation(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    if not is_admin(event.callback.user.user_id):
        return

    try:
        parts = event.callback.payload.split('|')
        data_type = parts[1]
        page = int(parts[2])

        param_reverse_mapping = {'st': 'subscription_type', 'pd': 'period_days'}
        params = {}
        for part in parts[3:]:
            if ':' in part:
                short_key, value = part.split(':', 1)
                key = param_reverse_mapping.get(short_key, short_key)
                params[key] = value

        if data_type == 'sources':
            source_data = await get_sources()
            await display_sources_paginated(
                event=event, source_data=source_data, data_type=data_type,
                page=page, i18n=i18n, subscription=False,
            )

        elif data_type == 'subscriptions':
            subscription_type = params.get('subscription_type', 'all')
            source_data = await get_sources_with_subscription(
                subscription_type=subscription_type if subscription_type != 'all' else None,
            )
            await display_sources_paginated(
                event=event, source_data=source_data, data_type=data_type,
                page=page, i18n=i18n, subscription=True, subscription_type=subscription_type,
            )

        elif data_type == 'payments':
            subscription_type = params.get('subscription_type', 'all')
            period_days = int(params.get('period_days', 0))

            source_data = await get_payments_sources(
                unique=False,
                subscription_type=subscription_type if subscription_type != 'all' else None,
                period_days=period_days if period_days > 0 else None,
            )

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
                event=event, source_data=source_data, data_type=data_type,
                page=page, i18n=i18n, subscription=True,
                period_text=period_text, subscription_text=subscription_text,
                subscription_type=subscription_type, period_days=period_days,
            )

        elif data_type == 'unique_payments':
            subscription_type = params.get('subscription_type', 'all')
            period_days = int(params.get('period_days', 0))

            source_data = await get_payments_sources(
                unique=True,
                subscription_type=subscription_type if subscription_type != 'all' else None,
                period_days=period_days if period_days > 0 else None,
            )

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
                event=event, source_data=source_data, data_type=data_type,
                page=page, i18n=i18n, subscription=True,
                period_text=period_text, subscription_text=subscription_text,
                subscription_type=subscription_type, period_days=period_days,
            )

    except (ValueError, IndexError) as e:
        logger.error(f"Error processing page navigation: {e}")
        await event.answer()


@router.message_callback(F.callback.payload == 'page_info')
async def process_page_info_click(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await event.answer()
