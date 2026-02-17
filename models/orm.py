import traceback
from datetime import datetime, timedelta
from itertools import groupby
import pytz

from fluentogram import TranslatorRunner, TranslatorHub
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.functions import count
import sqlalchemy
from sqlalchemy import RowMapping, select, update

from models.model import Base, Payment, Referral, User, FileDownload, DownloadStatus, Audio, ProcessingSession, LLMRequest, AnonymousChatMessage, NotificationStatusEnum, RecoveryStatusEnum, Transcription, Summary, UserAction
from services.bot_provider import get_bot
from services.scheduler import scheduler
from services.telegram_alerts import send_alert
from utils.i18n import create_translator_hub

import os

from services.init_bot import config
db_name = config.db.database
user_name = config.db.user
password = config.db.password
db_host = os.environ.get('DB_HOST', 'localhost')

# Создаем асинхронный движок
engine = create_async_engine(
    f'postgresql+asyncpg://{user_name}:{password}@{db_host}:5432/{db_name}',
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_timeout=10,
    pool_use_lifo=True,
    pool_recycle=3600,
    pool_pre_ping=True
)

# Создаем фабрику асинхронных сессий
async_session = sessionmaker(
    engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

import logging
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)

# Асинхронная инициализация таблиц
async def init_models():
    async with engine.begin() as conn:
        # Create PostgreSQL enum types before creating tables
        # (needed because ProcessingSession columns use create_type=False)
        await conn.execute(sqlalchemy.text("""
            DO $$ BEGIN
                CREATE TYPE notification_status_enum AS ENUM ('pending', 'sent', 'not_required', 'failed');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        await conn.execute(sqlalchemy.text("""
            DO $$ BEGIN
                CREATE TYPE recovery_status_enum AS ENUM ('pending_decision', 'retry_scheduled', 'retried', 'notification_only', 'ignored', 'manual_review_required');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        await conn.run_sync(Base.metadata.create_all)

async def monitor_connection_pool():
    """Мониторинг состояния connection pool для AsyncEngine"""
    import asyncio
    while True:
        try:
            # Для AsyncEngine доступ к пулу только через sync_engine
            pool = engine.sync_engine.pool

            # Метрики пула (sqlalchemy.pool.QueuePool API)
            size = getattr(pool, 'size', lambda: 0)()
            checked_in = getattr(pool, 'checkedin', lambda: 0)()
            checked_out = getattr(pool, 'checkedout', lambda: 0)()
            overflow = getattr(pool, 'overflow', lambda: 0)()

            total_connections = (size or 0) + (overflow or 0)
            utilization_percent = (checked_out / total_connections * 100) if total_connections > 0 else 0

            logging.info(
                "DB Pool Status: size=%s, checked_in=%s, checked_out=%s, overflow=%s, total=%s, utilization=%.1f%%",
                size, checked_in, checked_out, overflow, total_connections, utilization_percent
            )

            if utilization_percent > 95:
                logging.error("Critical DB pool utilization: %.1f%%", utilization_percent)
            elif utilization_percent > 80:
                logging.warning("High DB pool utilization: %.1f%%", utilization_percent)

            await asyncio.sleep(30)  # каждые 30 сек на время расследования
        except Exception as e:
            logging.error("Pool monitoring error: %s", e)
            await asyncio.sleep(30)

# === АСИНХРОННОЕ ЛОГИРОВАНИЕ ===
import asyncio
from asyncio import Queue
from dataclasses import dataclass
from typing import Dict, Any

# Очередь для фоновых задач логирования
_background_logging_queue: Queue = None

@dataclass
class BackgroundLogTask:
    task_type: str
    data: Dict[str, Any]

async def init_background_logging():
    """Инициализация системы фонового логирования"""
    global _background_logging_queue
    _background_logging_queue = Queue(maxsize=10000)  # Буфер на 10k задач
    
    # Запускаем 3 воркера для обработки очереди
    for i in range(3):
        asyncio.create_task(_background_logging_worker(f"worker-{i}"))

async def _background_logging_worker(worker_name: str):
    """Воркер для обработки фоновых задач логирования"""
    global _background_logging_queue
    
    while True:
        try:
            # Ждем задачу из очереди
            task: BackgroundLogTask = await _background_logging_queue.get()
            
            # Обрабатываем в зависимости от типа
            if task.task_type == 'anonymous_chat':
                await _process_anonymous_chat_task(task.data)
            elif task.task_type == 'llm_request_update':
                await _process_llm_update_task(task.data)
            elif task.task_type == 'user_action':
                await _process_user_action_task(task.data)
            # Можно добавить другие типы задач
            
            _background_logging_queue.task_done()
            
        except Exception as e:
            logging.error(f"Background logging worker {worker_name} error: {e}")
            await asyncio.sleep(1)

async def _process_anonymous_chat_task(data: Dict[str, Any]):
    """Обработка задачи анонимного чата"""
    try:
        await log_anonymous_chat_message(
            chat_session=data['chat_session'],
            message_from=data['message_from'],
            text=data['text'],
            message_order=data['message_order']
        )
    except Exception as e:
        logging.error(f"Failed to process anonymous chat task: {e}")

async def _process_llm_update_task(data: Dict[str, Any]):
    """Обработка обновления LLM запроса"""
    try:
        await update_llm_request(**data)
    except Exception as e:
        logging.error(f"Failed to process LLM update task: {e}")

async def _process_user_action_task(data: Dict[str, Any]):
    """Обработка задачи логирования действия пользователя"""
    try:
        await log_user_action(
            user_id=data['user_id'],
            action_type=data['action_type'],
            action_category=data['action_category'],
            metadata=data.get('metadata', {}),
            session_id=data.get('session_id'),
            payment_id=data.get('payment_id'),
            referral_id=data.get('referral_id')
        )
    except Exception as e:
        logging.error(f"Failed to process user action task: {e}")

async def queue_background_log(task_type: str, data: Dict[str, Any]) -> bool:
    """
    Добавляет задачу логирования в фоновую очередь
    
    Returns:
        True если задача добавлена, False если очередь переполнена
    """
    global _background_logging_queue
    
    if _background_logging_queue is None:
        logging.error("Background logging not initialized!")
        return False
    
    try:
        task = BackgroundLogTask(task_type=task_type, data=data)
        _background_logging_queue.put_nowait(task)
        return True
    except asyncio.QueueFull:
        logging.warning(f"Background logging queue is full, dropping {task_type} task")
        return False

# Неблокирующие версии функций для критического пути
async def log_anonymous_chat_message_async(
    chat_session: str,
    message_from: str,
    text: str,
    message_order: int
) -> bool:
    """Неблокирующая версия логирования анонимного чата"""
    return await queue_background_log('anonymous_chat', {
        'chat_session': chat_session,
        'message_from': message_from,
        'text': text,
        'message_order': message_order
    })

async def update_llm_request_async(
    request_id: int,
    **kwargs
) -> bool:
    """Неблокирующая версия обновления LLM запроса"""
    data = {'request_id': request_id, **kwargs}
    return await queue_background_log('llm_request_update', data)

async def log_user_action_async(
    user_id: int,
    action_type: str,
    action_category: str,
    metadata: dict = None,
    session_id: str = None,
    payment_id: int = None,
    referral_id: int = None
) -> bool:
    """
    Неблокирующая версия логирования действия пользователя.
    Добавляет задачу в фоновую очередь для обработки.

    Args:
        user_id: ID пользователя
        action_type: Тип действия (например, 'conversion_payment_link_created')
        action_category: Категория действия (conversion, subscription, payment, и т.д.)
        metadata: Дополнительные данные в формате dict
        session_id: ID сессии обработки (опционально)
        payment_id: ID платежа (опционально)
        referral_id: ID реферала (опционально)

    Returns:
        True если задача добавлена в очередь, False если очередь переполнена
    """
    return await queue_background_log('user_action', {
        'user_id': user_id,
        'action_type': action_type,
        'action_category': action_category,
        'metadata': metadata or {},
        'session_id': session_id,
        'payment_id': payment_id,
        'referral_id': referral_id
    })

# Получение пользователя
async def get_user(telegram_id: str | int = None, user_id: int = None) -> dict | None:
    """
    Возвращает словарь с данными о пользователе либо None если пользователь не найден
    :param telegram_id:
    :return:
    """
    async with async_session() as session:
        filters = []
        if telegram_id:
            telegram_id = str(telegram_id)
            filters.append(User.telegram_id == telegram_id)
        if user_id:
            user_id = int(user_id)
            filters.append(User.id == user_id)
        result = await session.execute(
            select(User).filter(*filters)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            user_data: dict = _prepare_user_dict(user)
            return user_data
        return user


def _prepare_user_dict(user: User):
    return {
        'id': user.id,
        'username': user.username,
        'telegram_id': user.telegram_id,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'audio_uses': user.audio_uses,
        'subscription': user.subscription,
        'end_date': user.end_date,
        'start_date': user.start_date,
        'subscription_id': user.subscription_id,
        'subscription_type': user.subscription_type,
        'subscription_autopay': user.subscription_autopay,
        'source': user.source,
        'created_at': user.created_at,
        'user_language': user.user_language,
        'llm_model': user.llm_model,
        'specify_audio_language': user.specify_audio_language,
        'download_video': user.download_video,
        'transcription_format': user.transcription_format,
        'is_bot_blocked': user.is_bot_blocked,
    }


async def _create_new_user(message=None,
                           telegram_user=None,
                           active_sub: bool = False,
                           max_user=None) -> dict:
    async with async_session() as session:
        if max_user is not None:
            telegram_id = str(max_user.user_id)
            source = ''
        elif telegram_user is not None:
            telegram_id = str(telegram_user.id)
            source = ''
        else:
            telegram_id = str(message.from_user.id)

        try:
            source = message.text.removeprefix('/start ')
        except Exception as e:
            source = ''
        existing_user = await session.execute(
            select(User).filter_by(telegram_id=telegram_id)
        )
        user = existing_user.scalar_one_or_none()

        # Create timezone-aware datetime objects
        current_time_aware = datetime.now(pytz.utc)
        end_time_aware = current_time_aware + timedelta(days=3)

        # Convert to naive datetime objects for PostgreSQL TIMESTAMP WITHOUT TIME ZONE
        current_time = current_time_aware.replace(tzinfo=None)
        end_time = end_time_aware.replace(tzinfo=None)

        # Resolve user profile fields from whichever source is available
        if max_user is not None:
            _username = max_user.username
            _first_name = max_user.first_name
            _last_name = max_user.last_name
        elif telegram_user is not None:
            _username = telegram_user.username
            _first_name = telegram_user.first_name
            _last_name = telegram_user.last_name
        else:
            _username = message.from_user.username
            _first_name = message.from_user.first_name
            _last_name = message.from_user.last_name

        if user:
            # Обновляем существующего пользователя
            user.audio_uses = 0
            user.gpt_uses = 0
            user.created_at = current_time
            user.username = _username
            user.first_name = _first_name
            user.last_name = _last_name
            user.source = source
        else:
            # Создаем нового пользователя
            user = User(
                telegram_id=telegram_id,
                username=_username,
                first_name=_first_name,
                last_name=_last_name,
                subscription='False' if not active_sub else 'True',
                start_date=current_time if not active_sub else None,
                end_date=end_time if not active_sub else None,
                source=source,
                created_at=current_time
            )
            session.add(user)

        if source.startswith('ref_'):

            referrer: User = await session.execute(
                select(User).filter_by(telegram_id=source.split('_')[1])
            )
            referrer = referrer.scalar_one_or_none()

            if referrer:
                referral = Referral(
                    referrer_id=referrer.id,
                    referred_id=user.id
                )
                session.add(referral)

        await session.commit()
        user_data = _prepare_user_dict(user)

    return user_data

async def create_new_user(message=None, telegram_user=None,
                          active_sub: bool = False, max_user=None) -> dict:
    user_data: dict = await _create_new_user(message=message, telegram_user=telegram_user,
                                              active_sub=active_sub, max_user=max_user)
    # await new_user_notification(user_data)
    return user_data


async def add_voice_use(telegram_id: int | str):
    telegram_id = str(telegram_id)

    async with async_session() as session:
        user = await session.execute(
            select(User).filter_by(telegram_id=telegram_id)
        )
        user = user.scalar_one_or_none()
        if user:
            user.audio_uses += 1
            await session.commit()


async def add_gpt_use(telegram_id: int | str):
    telegram_id = str(telegram_id)

    async with async_session() as session:
        user = await session.execute(
            select(User).filter_by(telegram_id=telegram_id)
        )
        user = user.scalar_one_or_none()
        if user:
            user.gpt_uses += 1
            await session.commit()


def is_admin(telegram_id: int | str) -> bool:
    telegram_id = int(telegram_id)
    admin_ids = config.tg_bot.admin_ids
    if telegram_id in admin_ids:
        return True
    return False

async def get_all_users() -> list[dict]:
    async with async_session() as session:
        # Выбираем не сам ORM-класс User, а его таблицу — User.__table__
        stmt = select(User.__table__)
        result = await session.execute(stmt)
        # mappings() вернёт RowMapping, где ключами — реальные имена колонок
        rows: list[RowMapping] = result.mappings().all()

        # Каждая row уже ведёт себя как dict: ключ = имя столбца, значение = значение
        users: list[dict] = [dict(row) for row in rows]

        return users

async def get_statistics() -> dict:
    """
    Get system statistics using efficient database aggregation.
    This is much faster than the previous implementation that loaded all users into memory.
    """
    from sqlalchemy import func, case
    
    async with async_session() as session:
        # Single query to get all statistics at once using SQL aggregation
        result = await session.execute(
            select(
                func.count(User.id).label('users_count'),
                func.sum(User.audio_uses).label('total_audio_uses'),
                func.sum(User.gpt_uses).label('total_gpt_uses'),
                func.sum(case((User.subscription == 'True', 1), else_=0)).label('active_subs'),
                func.sum(case(((User.subscription == 'True') & (User.subscription_type == 'monthly'), 1), else_=0)).label('monthly_subs'),
                func.sum(case(((User.subscription == 'True') & (User.subscription_type == 'weekly'), 1), else_=0)).label('weekly_subs'),
                func.sum(case(((User.subscription == 'True') & (User.subscription_type.in_(['annual', 'yearly'])), 1), else_=0)).label('annual_subs'),
                func.sum(case(((User.subscription == 'True') & ((User.subscription_type.is_(None)) | (User.subscription_type.in_(['custom', 'manual']))), 1), else_=0)).label('manual_subs'),
                func.sum(case((User.audio_uses > 0, 1), else_=0)).label('users_with_action'),
                func.sum(case((User.is_bot_blocked == False, 1), else_=0)).label('unblocked_users_count')
            )
        )
        
        stats = result.first()
        # Count active (unclosed) processing sessions: final_status IS NULL
        active_sessions_result = await session.execute(
            select(func.count(ProcessingSession.id)).where(ProcessingSession.final_status.is_(None))
        )
        active_sessions = active_sessions_result.scalar() or 0
        
        return {
            'users_count': stats.users_count or 0,
            'voice_uses': (stats.total_audio_uses or 0) + 96950,  # Adding the hardcoded base value
            'gpt_uses': stats.total_gpt_uses or 0,
            'active_subs': stats.active_subs or 0,
            'monthly_subs': stats.monthly_subs or 0,
            'weekly_subs': stats.weekly_subs or 0,
            'annual_subs': stats.annual_subs or 0,
            'manual_subs': stats.manual_subs or 0,
            'users_with_action': stats.users_with_action or 0,
            'unblocked_users_count': stats.unblocked_users_count or 0,
            'active_sessions': active_sessions
        }


# def db_end_subscription(telegram_id: str):
#     with Session() as session:
#         user = session.query(User).filter(User.telegram_id == telegram_id).first()
#         user.subscription = "False"
#         session.commit()
#     return True


async def db_add_subscription(
    telegram_id: int, 
    subscription_id: str,
    subscription_type_str: str, 
    start_date_dt: datetime, 
    end_date_dt: datetime,
    is_autopay_active: bool = True # Default to True for CP managed subs
):
    """
    Adds or updates a user's subscription details in the database.
    This function will update the user's main subscription status and dates.
    Uses the CloudPayments subscription ID.

    Args:
        telegram_id: The user's Telegram ID.
        subscription_id: The subscription ID
        subscription_type_str: Internal string for subscription type (e.g., 'weekly', 'monthly').
        start_date_dt: The start date of the current subscription period (timezone-aware or naive, ensure consistency).
        end_date_dt: The end date (next billing date) of the current subscription period (timezone-aware or naive).
        is_autopay_active: Whether autopay is active for this subscription.
    """
    async with async_session() as session:
        user = await session.execute(
            select(User).filter_by(telegram_id=str(telegram_id))
        )
        user = user.scalar_one_or_none()

        if not user:
            # This should ideally not happen if user exists from other interactions.
            # If it can, you might need to create a minimal user record or log an error.
            logging.error(f"User with telegram_id {telegram_id} not found when trying to add/update subscription.")
            return

        # Ensure dates are naive if your DB column is TIMESTAMP WITHOUT TIME ZONE
        # If your DB is TIMESTAMPTZ, you'd ensure they are timezone-aware (e.g., UTC)
        if start_date_dt.tzinfo is not None:
            start_date_dt = start_date_dt.replace(tzinfo=None)
        if end_date_dt.tzinfo is not None:
            end_date_dt = end_date_dt.replace(tzinfo=None)

        user.subscription = 'True'  # Or True if your column is BOOLEAN
        user.subscription_id = subscription_id # This is the CloudPayments or Stripe subscription ID
        user.subscription_type = subscription_type_str
        user.start_date = start_date_dt
        user.end_date = end_date_dt
        user.subscription_autopay = is_autopay_active
        await session.commit()

        job_id = f"cancel_sub_{telegram_id}_{user.end_date.timestamp()}"
        try:
            scheduler.remove_job(job_id)
            print(f"Removed existing cancellation job for user {telegram_id}")
        except:
            pass
        translator_hub: TranslatorHub = create_translator_hub()
        i18n = translator_hub.get_translator_by_locale(locale=user.user_language)
        # Планируем новую задачу отмены подписки
        scheduler.add_job(
            cancel_subscription,
            'date',
            run_date=user.end_date,
            args=[str(telegram_id), False, i18n],
            id=job_id
        )

        logging.info(f"Subscription for user {telegram_id} updated/added: CP_ID={subscription_id}, Type={subscription_type_str}, End_Date={end_date_dt}")


from apscheduler.schedulers.asyncio import AsyncIOScheduler

async def check_subscriptions(scheduler: AsyncIOScheduler):
    logging.info('Starting check_subscriptions.')
    async with async_session() as session:
        # Use naive UTC for DB comparisons (TIMESTAMP WITHOUT TIME ZONE)
        current_time_naive = datetime.utcnow()
        start_of_day = current_time_naive.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        # Получаем только тех, у кого срок истек или истекает сегодня (сейчас/позже)
        expired_result = await session.execute(
            select(User).filter(
                User.subscription.in_(['trial', 'True']),
                User.end_date.isnot(None),
                User.end_date <= current_time_naive
            )
        )
        expiring_today_result = await session.execute(
            select(User).filter(
                User.subscription.in_(['trial', 'True']),
                User.end_date.isnot(None),
                User.end_date > current_time_naive,
                User.end_date < end_of_day
            )
        )

        expired_users = expired_result.scalars().all()
        expiring_today_users = expiring_today_result.scalars().all()

        # Один hub и кэш переводчиков по языку
        translator_hub: TranslatorHub = create_translator_hub()
        i18n_cache: dict[str, TranslatorRunner] = {}

        def get_i18n(lang: str | None) -> TranslatorRunner:
            lang_code = lang or getattr(config.tg_bot, 'default_lang', 'ru')
            if lang_code not in i18n_cache:
                i18n_cache[lang_code] = translator_hub.get_translator_by_locale(locale=lang_code)
            return i18n_cache[lang_code]

        # Обрабатываем просроченные подписки конкурентно, ограничивая параллелизм
        import asyncio as _asyncio
        semaphore = _asyncio.Semaphore(20)

        async def _process_expired(user: User):
            async with semaphore:
                if user.end_date is None:
                    return
                # Compare naive UTC
                user_end_date_naive = user.end_date if user.end_date.tzinfo is None else user.end_date.replace(tzinfo=None)
                if user_end_date_naive <= current_time_naive:
                    i18n = get_i18n(user.user_language)
                    try:
                        await cancel_subscription(user.telegram_id, user.subscription == 'trial', i18n=i18n)
                    except Exception as e:
                        logging.error(f"Failed to cancel subscription for {user.telegram_id}: {e}")

        if expired_users:
            await _asyncio.gather(*[_process_expired(u) for u in expired_users])

        # Планируем отмену для тех, у кого подписка заканчивается сегодня
        for user in expiring_today_users:
            if user.end_date is None:
                continue
            # Keep run_date naive (UTC) to match DB stored values and previous behavior
            run_dt = user.end_date if user.end_date.tzinfo is None else user.end_date.replace(tzinfo=None)
            try:
                i18n = get_i18n(user.user_language)
                scheduler.add_job(
                    cancel_subscription,
                    'date',
                    run_date=run_dt,
                    args=[user.telegram_id, user.subscription == 'trial', i18n],
                    id=f"cancel_sub_{user.telegram_id}_{run_dt.timestamp()}"
                )
            except Exception as e:
                logging.error(f"Failed to schedule cancellation for {user.telegram_id}: {e}")

async def cancel_subscription(telegram_id: str, was_trial: bool, i18n: TranslatorRunner, force_cancel: bool = False):
    # Local import to avoid circular dependency
    from keyboards.user_keyboards import subscription_menu
    
    async with async_session() as session:
        user = await session.execute(
            select(User).filter(User.telegram_id == telegram_id)
        )
        user = user.scalar_one_or_none()
        current_time = datetime.now(pytz.utc)
        
        # Убедимся, что end_date имеет timezone информацию
        if user.end_date:
            if user.end_date.tzinfo is None:
                user_end_date = pytz.utc.localize(user.end_date)
            else:
                user_end_date = user.end_date
                
            # Теперь сравниваем datetime объекты с одинаковой timezone информацией
            if user_end_date > current_time:
                print(f"Subscription for user {telegram_id} was renewed, cancellation aborted")
                return
        
        if user and user.subscription in ['trial', 'True']:
            user.subscription = 'False' if user.subscription != 'PastDue' else 'PastDue'
            user.subscription_id = None if user.subscription != 'PastDue' else user.subscription_id
            user.start_date = None
            user.end_date = None
            await session.commit()
            
    if was_trial:
        try:
            await get_bot().send_message(
                chat_id=telegram_id,
                text=i18n.trial_ended(),
                parse_mode='HTML',
                reply_markup=subscription_menu(i18n)
            )
        except Exception as e:
            print(f"Failed to send trial end notification to user {telegram_id}: {e}")
    else:
        try:
            await get_bot().send_message(
                chat_id=telegram_id,
                text=i18n.subscription_ended(),
                parse_mode='HTML',
                reply_markup=subscription_menu(i18n)
            )
        except Exception as e:
            print(f"Failed to send subscription end notification to user {telegram_id}: {e}")

async def update_subscription_details(telegram_id: str, 
                                      subscription_id: str = None, 
                                      subscription_type_str: str = None, 
                                      start_date_dt: datetime = None, 
                                      end_date_dt: datetime = None, 
                                      is_autopay_active: bool = None, 
                                      subscription_status: str = None):
    """
    Updates the subscription details for a user in the database.
    Args:
        telegram_id: The user's Telegram ID. - Required
        subscription_id: The subscription ID - Optional
        subscription_type_str: Internal string for subscription type (e.g., 'weekly', 'monthly'). - Optional
        start_date_dt: The start date of the current subscription period (timezone-aware or naive, ensure consistency). - Optional
        end_date_dt: The end date (next billing date) of the current subscription period (timezone-aware or naive). - Optional
        is_autopay_active: Whether autopay is active for this subscription. - Optional
        subscription_status: The status of the subscription ('trial', 'True', 'False'). - Optional
    """
    async with async_session() as session:
        user = await session.execute(
            select(User).filter(User.telegram_id == str(telegram_id))
        )
        user = user.scalar_one_or_none()
        if not user:
            logging.error(f"User with telegram_id {telegram_id} not found when trying to update subscription details.")
            return
        
        if subscription_id:
            user.subscription_id = subscription_id
        if subscription_type_str:
            user.subscription_type = subscription_type_str
        if start_date_dt:
            user.start_date = start_date_dt
        if end_date_dt:
            user.end_date = end_date_dt
        if is_autopay_active:
            user.subscription_autopay = is_autopay_active
        if subscription_status:
            user.subscription = subscription_status
        await session.commit()
        return True

        

async def get_sources():
    """
    Возвращает список источников и количество пользователей из каждого источника
    :return:
    """

    async with async_session() as session:
        result = await session.execute(
            select(User.source, count(User.source).label('count')).filter(User.source != '').group_by(User.source)
        )
        sorted_sources = sorted(result.fetchall(), key=lambda x: x[1], reverse=True)
        return sorted_sources


async def get_users():
    async with async_session() as session:
        users = await session.execute(
            select(User)
        )
        users = users.scalars().all()
        users_list = []
        for user in users:
            users_list.append(_prepare_user_dict(user))

        return users_list

async def get_sources_with_subscription(subscription_type: str = None):
    """
    Возвращает список источников пользователей с активной подпиской
    :return:
    """
    async with async_session() as session:
        if subscription_type is None:
            source_filter = User.source != '', User.subscription == 'True'
        else:
            source_filter = User.source != '', User.subscription == 'True', User.subscription_type == subscription_type

        result = await session.execute(
            select(User.source, count(User.source).label('count')).filter(*source_filter).group_by(User.source)
        )
        sorted_sources = sorted(result.fetchall(), key=lambda x: x[1], reverse=True)
        return sorted_sources


from sqlalchemy import func, or_

async def get_payments_sources(unique: bool = False, subscription_type: str = None, period_days: int = None):
    # Импортируем все необходимые функции SQLAlchemy в начале функции
    from sqlalchemy import func, distinct, or_, and_, select as sqlalchemy_select
    
    async with async_session() as session:
        if unique:
            # Для уникальных платежей используем подзапрос, который сначала выбирает первый платеж
            # для каждого пользователя (по времени создания), и только потом применяет фильтры
            
            # Подзапрос для получения ID первого платежа каждого пользователя
            first_payments_subquery = sqlalchemy_select(
                Payment.id.label('first_payment_id')
            ).select_from(
                Payment
            ).where(
                Payment.source != ''
            ).order_by(
                Payment.user_id,
                Payment.created_at
            ).distinct(
                Payment.user_id
            ).subquery()
            
            # Основной запрос с фильтрами по типу подписки и периоду
            query = select(
                Payment.source,
                func.count(Payment.source).label('count')
            ).join(
                first_payments_subquery, 
                Payment.id == first_payments_subquery.c.first_payment_id
            )
            
            # Добавляем фильтр по типу подписки, если указан
            if subscription_type == 'weekly':
                query = query.filter(or_(Payment.amount == 149, Payment.amount == 55, Payment.amount == 249))
            elif subscription_type == 'monthly':
                query = query.filter(or_(Payment.amount == 190, Payment.amount == 349, Payment.amount == 549))
                
            # Добавляем фильтр по времени, если указан
            if period_days:
                start_date = datetime.now(pytz.utc) - timedelta(days=period_days)
                # Convert to naive datetime for PostgreSQL TIMESTAMP WITHOUT TIME ZONE
                naive_start_date = start_date.replace(tzinfo=None)
                query = query.filter(Payment.created_at >= naive_start_date)
                
            # Группировка и выполнение запроса
            result = await session.execute(query.group_by(Payment.source))
        else:
            # Для обычных платежей оставляем существующую логику
            # Build the base query
            query = select(
                Payment.source,
                func.count(Payment.source).label('count')
            ).filter(Payment.source != '')

            # Add subscription type filter if specified
            if subscription_type == 'weekly':
                query = query.filter(or_(Payment.amount == 149, Payment.amount == 55, Payment.amount == 249))
            elif subscription_type == 'monthly':
                query = query.filter(or_(Payment.amount == 190, Payment.amount == 349, Payment.amount == 549))
                
            # Add time period filter if specified
            if period_days:
                start_date = datetime.now(pytz.utc) - timedelta(days=period_days)
                # Convert to naive datetime for PostgreSQL TIMESTAMP WITHOUT TIME ZONE
                naive_start_date = start_date.replace(tzinfo=None)
                query = query.filter(Payment.created_at >= naive_start_date)

            # Complete the query
            result = await session.execute(query.group_by(Payment.source))

        # Sort the results by count in descending order
        sorted_sources = sorted(result.fetchall(), key=lambda x: x[1], reverse=True)
        return sorted_sources
    
async def db_add_payment(telegram_id: int, amount: float, status: str, token: str = None, transaction_id: str = None):
    async with async_session() as session:
        user_result = await session.execute(
            select(User).filter(User.telegram_id == str(telegram_id))
        )
        user = user_result.scalar_one_or_none()
        if not user:
            logging.error(f"User with telegram_id {telegram_id} not found when trying to add payment.")
            return False # Indicate failure
        
        # Optional: Check if a payment with this transaction_id already exists to ensure idempotency
        if transaction_id:
            existing_payment_result = await session.execute(
                select(Payment).filter_by(transaction_id=transaction_id)
            )
            existing_payment = existing_payment_result.scalar_one_or_none()
            if existing_payment:
                logging.warning(f"Payment with transaction_id {transaction_id} already exists. Skipping duplicate insertion.")
                # You might want to update the existing payment's status or other details if necessary
                # For now, just return True assuming it's already handled.
                return True

        new_payment = Payment(
            user_id=user.id,
            source=user.source,
            amount=amount,
            status=status,
            token=token,
            transaction_id=transaction_id # Save the transaction ID
        )
        session.add(new_payment)
        await session.commit()
        logging.info(f"Payment added for user {telegram_id} (User ID: {user.id}), Transaction ID: {transaction_id}, Status: {status}")
    return True

async def cancel_autopay(telegram_id: int):
    async with async_session() as session:
        user = await session.execute(
            select(User).filter(User.telegram_id == str(telegram_id))
        )
        user = user.scalar_one_or_none()
        user.subscription_autopay = False
        await session.commit()


async def get_payments(telegram_id: int, only_successful: bool = False):
    
    async with async_session() as session:
        user = await session.execute(
            select(User).filter(User.telegram_id == str(telegram_id))
        )
        user = user.scalar_one_or_none()
        if only_successful:
            payments = await session.execute(
                select(Payment).filter(Payment.user_id == user.id).filter(Payment.status.in_(["completed", "success"])))
        else:
            payments = await session.execute(
                select(Payment).filter(Payment.user_id == user.id)
            )
        payments = payments.scalars().all()
        return payments
    
async def renew_subscription_db(telegram_id: int, subscription_id: str):
    async with async_session() as session:
        user = await session.execute(
            select(User).filter(User.telegram_id == str(telegram_id))
        )
        user = user.scalar_one_or_none()
        user.subscription_id = subscription_id
        user.subscription_autopay = True
        await session.commit()

async def give_subscription(i18n: TranslatorRunner, telegram_id: int = None, username: str = None, days: int = 30):
    async with async_session() as session:
        if telegram_id:
            user = await session.execute(
                select(User).filter(User.telegram_id == str(telegram_id))
            )
            user = user.scalar_one_or_none()
            if not user:
                return {'result': False, 'message': i18n.cannot_find_user_by_id_admin()}
        else:
            user = await session.execute(
                select(User).filter(User.username == username)
            )
            user = user.scalar_one_or_none()
            if not user:
                return {'result': False, 'message': i18n.cannot_find_user_by_username_admin()}
        user.subscription = 'True'
        user.subscription_type = 'manual'  # Устанавливаем тип для ручных подписок
        user.start_date = datetime.utcnow()
        user.end_date = datetime.utcnow() + timedelta(days=days)
        await session.commit()
        return {'result': True, 'message': i18n.subscription_success_admin(), 'user_id': user.telegram_id}
        

async def change_user_setting(telegram_id: int, setting_name: str, setting_value: str | bool):
    async with async_session() as session:
        user = await session.execute(
            select(User).filter(User.telegram_id == str(telegram_id))
        )
        user = user.scalar_one_or_none()
        setattr(user, setting_name, setting_value)
        await session.commit()

async def get_user_id_range():
    """
    Returns the minimum and maximum user ID values from the database
    :return: Dictionary with 'min_id' and 'max_id' keys
    """
    async with async_session() as session:
        min_id_result = await session.execute(
            select(User.id).order_by(User.id.asc()).limit(1)
        )
        max_id_result = await session.execute(
            select(User.id).order_by(User.id.desc()).limit(1)
        )

        min_id = min_id_result.scalar_one_or_none()
        max_id = max_id_result.scalar_one_or_none()

        return {
            'min_id': min_id,
            'max_id': max_id
        }

async def add_download_record(
    user_id: int,
    source_type: str,
    identifier: str,
    destination_type: str,
    specific_source: str | None = None,
    initial_file_size: int | None = None,
    session_id: str | None = None,
    attempt_number: int = 1,
    download_method: str | None = None
) -> int | None:
    """Adds a new record to the file_downloads table with PENDING status.

    Args:
        user_id: The internal ID of the user (from the users table).
        source_type: 'url' or 'telegram'.
        identifier: The URL or Telegram file_id.
        destination_type: 'disk' or 'buffer'.
        specific_source: Specific source for URLs (e.g., 'youtube'), optional.
        initial_file_size: File size in bytes, if known beforehand.
        session_id: UUID of the processing session, optional.
        attempt_number: Attempt number within the session (1, 2, 3...).
        download_method: Method used for download ('rapidapi', 'cobalt', 'direct', etc.).

    Returns:
        The ID of the newly created record, or None if user_id is invalid.
    """
    async with async_session() as session:
        # Verify user_id exists (optional but good practice)
        user_exists = await session.get(User, user_id)
        if not user_exists:
            print(f"Error: Cannot add download record for non-existent user_id: {user_id}")
            return None

        new_record = FileDownload(
            user_id=user_id,
            source_type=source_type,
            identifier=identifier,
            destination_type=destination_type,
            specific_source=specific_source,
            status=DownloadStatus.PENDING,
            file_size_bytes=initial_file_size,
            session_id=session_id,
            attempt_number=attempt_number,
            download_method=download_method,
            # created_at is set by default
        )
        session.add(new_record)
        await session.commit()
        await session.refresh(new_record) # To get the auto-generated id
        logging.debug(f"Added download record ID: {new_record.id}")
        return new_record.id


async def update_download_record(
    record_id: int,
    status: DownloadStatus,
    final_file_size: int | None = None,
    duration_seconds: float | None = None,
    temp_file_path: str | None = None,
    error_message: str | None = None
):
    """Updates an existing file_downloads record with final status and details.

    Args:
        record_id: The ID of the record to update.
        status: The final status (DOWNLOADED or ERROR).
        final_file_size: Final file size in bytes.
        duration_seconds: Duration of the download/operation in seconds.
        temp_file_path: Path to the temp file if destination was 'disk'.
        error_message: Error details if status is ERROR.
    """
    async with async_session() as session:
        record = await session.get(FileDownload, record_id)
        if not record:
            print(f"Error: Cannot update non-existent download record_id: {record_id}")
            return

        record.status = status
        if final_file_size is not None:
            record.file_size_bytes = final_file_size
        if duration_seconds is not None:
            record.duration_seconds = duration_seconds
        if temp_file_path is not None:
            record.temp_file_path = temp_file_path
        if error_message is not None:
            record.error_message = error_message

        await session.commit()
        logging.debug(f"Updated download record ID: {record_id} to status: {status.value}")

async def update_user_blocked_status(telegram_id: int | str, is_blocked: bool):
    """
    Обновляет статус блокировки бота пользователем
    """
    telegram_id = str(telegram_id)
    async with async_session() as session:
        users = await session.execute(
            select(User).filter_by(telegram_id=telegram_id)
        )
        users = users.scalars().all()
        if users:
            for user in users:
                user.is_bot_blocked = is_blocked
            await session.commit()
            logging.info(f"Updated {len(users)} users with telegram_id {telegram_id} blocked status to {is_blocked}")
            return True
        logging.warning(f"User {telegram_id} not found when updating blocked status")
        return False


# === REFERRAL SYSTEM FUNCTIONS ===

async def generate_referral_code(telegram_id: int | str) -> str:
    """Генерирует реферальный код для пользователя"""
    return f"ref_{telegram_id}"


async def get_referral_code(telegram_id: int | str) -> str:
    """Получает реферальный код для пользователя"""
    return await generate_referral_code(telegram_id)

async def get_referral_stats(telegram_id: int | str) -> dict:
    """Получает статистику реферальной программы для пользователя"""
    telegram_id = str(telegram_id)
    async with async_session() as session:
        # Находим пользователя
        user = await session.execute(
            select(User).filter_by(telegram_id=telegram_id)
        )
        user = user.scalar_one_or_none()
        
        if not user:
            return {
                'friends_invited': 0,
                'total_weeks_earned': 0,
                'subscription_active_until': 0
            }
        
        # Получаем количество приглашенных друзей
        from models.model import Referral
        friends_count = await session.execute(
            select(count()).select_from(Referral).filter_by(referrer_id=user.id)
        )
        friends_invited = friends_count.scalar() or 0
        
        return {
            'friends_invited': friends_invited,
            'total_weeks_earned': user.referral_registered or 0,
            'subscription_active_until': user.end_date or '-'
        }
    
async def add_free_days_to_subscription_db(telegram_id: int | str, days: int) -> bool:
    """Добавляет свободные дни к подписке"""
    telegram_id = str(telegram_id)
    print('add_free_days_to_subscription_db', telegram_id, days)
    async with async_session() as session:
        user = await session.execute(
            select(User).filter_by(telegram_id=telegram_id)
        )
        user = user.scalar_one_or_none()
        end_date = user.end_date
        if end_date:
            new_end_date = end_date + timedelta(days=days)
            user.end_date = new_end_date

        else:
            user.end_date = datetime.utcnow() + timedelta(days=days)
            user.subscription_type = 'reward'
        user.subscription = 'True'
        await session.commit()
    return True
    
async def confirm_referral_process(referrer_telegram_id: int | str, referral_telegram_id: int | str, success: bool):
    """Подтверждает процесс реферальной программы
    telegram_id: id пользователя, который купил подписку (приглашенный)
    
    """
    referrer_telegram_id = str(referrer_telegram_id)
    referral_telegram_id = str(referral_telegram_id)

    try:
        async with async_session() as session:
            referrer = await session.execute(
                select(User).filter_by(telegram_id=referrer_telegram_id)
            )
            referrer: User = referrer.scalar_one_or_none()
            if success:
                referrer.referral_given += 1
                referrer.referral_registered += 1
            else:
                referrer.referral_registered += 1

            referral: User = await session.execute(
                select(User).filter_by(telegram_id=referral_telegram_id)
            )
            referral: User = referral.scalar_one_or_none()

            await update_referral_payment_status(referral_telegram_id=int(referral_telegram_id), reward_given=True if success else False, made_payment=True, subscription_type=referral.subscription_type)
            await session.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка при confirm_referral_process: {e}")
        logging.debug(f"Трассировка: {traceback.format_exc()}")
        return False


async def update_referral_payment_status(referral_telegram_id: int | None = None, referral_user: dict | None = None,
                                         reward_given: bool | None = None,
                                         made_payment: bool | None = None,
                                         subscription_type: str | None = None):
    """
    Обновляет статус оплаты реферального кода
    referral_telegram_id: id пользователя, купившего подписку
    user: словарь с данными пользователя, купившего подписку
    """
    async with async_session() as session:
        if referral_user:
            user_id = referral_user.get('id')
        elif referral_telegram_id:
            user_id = (await get_user(referral_telegram_id)).get('id')
        else:
            raise ValueError("Either user or referral_telegram_id must be provided")
        
        referral = await session.execute(
            select(Referral).filter_by(referred_id=user_id)
        )
        referral: User = referral.scalar_one_or_none()
        
        if referral:
            if reward_given is not None:
                referral.reward_given = reward_given
                referral.reward_date = datetime.utcnow()
            if made_payment is not None:
                referral.made_payment = made_payment
                referral.payment_date = datetime.utcnow()
                
            if subscription_type is not None:
                referral.subscription_type = subscription_type
            await session.commit()
            return True
        return False

# === AUDIO LOGGING FUNCTIONS ===

async def update_audio_log(audio_log_id: int, 
                          length: float | None = None,
                          file_size_bytes: int | None = None,
                          processing_duration: float | None = None,
                          success: bool | None = None,
                          error_message: str | None = None) -> bool:
    """
    Обновляет существующую запись аудио лога.
    
    Args:
        audio_log_id: ID записи для обновления
        length: Длительность аудио в секундах
        file_size_bytes: Размер файла в байтах
        processing_duration: Время обработки в секундах
        success: Успешно ли обработано
        error_message: Сообщение об ошибке
        
    Returns:
        True если обновление прошло успешно, False при ошибке
    """
    try:
        async with async_session() as session:
            audio_log = await session.get(Audio, audio_log_id)
            if not audio_log:
                logging.error(f"Cannot update non-existent audio log ID: {audio_log_id}")
                return False
            
            # Обновляем только переданные поля
            if length is not None:
                audio_log.length = length
            if file_size_bytes is not None:
                audio_log.file_size_bytes = file_size_bytes
            if processing_duration is not None:
                audio_log.processing_duration = processing_duration
            if success is not None:
                audio_log.success = success
            if error_message is not None:
                audio_log.error_message = error_message
            
            await session.commit()
            logging.debug(f"Updated audio log ID: {audio_log_id}")
            return True
            
    except Exception as e:
        logging.error(f"Error updating audio log {audio_log_id}: {e}")
        return False

# === PROCESSING SESSION FUNCTIONS ===

async def create_processing_session(user_id: int, 
                                   original_identifier: str, 
                                   source_type: str,
                                   specific_source: str | None = None,
                                   waiting_message_id: int | None = None,
                                    user_original_message_id: int | None = None) -> str | None:
    """
    Создает новую сессию обработки файла.
    
    Args:
        user_id: ID пользователя
        original_identifier: Исходный URL или file_id
        source_type: 'url' или 'telegram'
        specific_source: 'youtube', 'instagram', etc.
        
    Returns:
        session_id (UUID) или None при ошибке
    """
    import uuid
    
    try:
        session_id = str(uuid.uuid4())
        
        async with async_session() as session:
            # Проверяем существование пользователя
            user_exists = await session.get(User, user_id)
            if not user_exists:
                logging.error(f"Cannot create processing session for non-existent user_id: {user_id}")
                return None
            
            new_session = ProcessingSession(
                session_id=session_id,
                user_id=user_id,
                original_identifier=original_identifier,
                source_type=source_type,
                specific_source=specific_source,
                waiting_message_id=waiting_message_id,
                user_original_message_id=user_original_message_id
            )
            
            session.add(new_session)
            await session.commit()
            
            logging.debug(f"Created processing session: {session_id} for user {user_id}")
            return session_id
            
    except Exception as e:
        logging.error(f"Error creating processing session: {e}")
        return None


async def update_processing_session(session_id: str,
                                   completed_at: datetime | None = None,
                                   total_duration: float | None = None,
                                   final_status: str | None = None,
                                   error_stage: str | None = None,
                                   error_message: str | None = None,
                                   original_file_size: int | None = None,
                                   total_download_attempts: int | None = None,
                                   waiting_message_id: int | None = None,
                                   notification_status: str | None = None,
                                   recovery_status: str | None = None,
                                   transcription_id: int | None = None) -> bool:
    """
    Обновляет сессию обработки файла.
    
    Args:
        session_id: UUID сессии
        completed_at: Время завершения
        total_duration: Общая длительность в секундах
        final_status: 'success', 'failed'
        error_stage: 'download', 'audio_extraction', 'transcription', 'summary'
        error_message: Сообщение об ошибке
        original_file_size: Размер исходного файла
        total_download_attempts: Количество попыток загрузки
        
    Returns:
        True если обновление прошло успешно
    """
    try:
        async with async_session() as session:
            processing_session = await session.execute(
                select(ProcessingSession).filter_by(session_id=session_id)
            )
            processing_session = processing_session.scalar_one_or_none()
            
            if not processing_session:
                logging.error(f"Cannot update non-existent processing session: {session_id}")
                return False
            
            # Обновляем только переданные поля
            if completed_at is not None:
                processing_session.completed_at = completed_at
            if total_duration is not None:
                processing_session.total_duration = total_duration
            if final_status is not None:
                processing_session.final_status = final_status
            if error_stage is not None:
                processing_session.error_stage = error_stage
            if error_message is not None:
                processing_session.error_message = error_message
            if original_file_size is not None:
                processing_session.original_file_size = original_file_size
            if total_download_attempts is not None:
                processing_session.total_download_attempts = total_download_attempts
            if waiting_message_id is not None:
                processing_session.waiting_message_id = waiting_message_id
            if notification_status is not None:
                processing_session.notification_status = notification_status
            if recovery_status is not None:
                processing_session.recovery_status = recovery_status
            if transcription_id is not None:
                processing_session.transcription_id = transcription_id
            await session.commit()
            logging.debug(f"Updated processing session: {session_id}")
            return True
            
    except Exception as e:
        logging.error(f"Error updating processing session {session_id}: {e}")
        return False

#
# async def get_processing_session_stats(user_id: int | None = None, days: int | None = None) -> dict:
#     """
#     Получает статистику по сессиям обработки (основные KPI).
#
#     Args:
#         user_id: ID пользователя (если None, то статистика по всем)
#         days: Количество дней назад (если None, то за все время)
#
#     Returns:
#         Словарь со статистикой
#     """
#     try:
#         from sqlalchemy import func, case
#
#         async with async_session() as session:
#             query = select(
#                 func.count(ProcessingSession.id).label('total_sessions'),
#                 func.count(ProcessingSession.id).filter(ProcessingSession.final_status == 'success').label('successful_sessions'),
#                 func.count(ProcessingSession.id).filter(ProcessingSession.final_status.in_(['download_failed', 'processing_failed'])).label('failed_sessions'),
#                 func.avg(ProcessingSession.total_duration).label('avg_total_duration'),
#                 func.avg(ProcessingSession.total_download_attempts).label('avg_download_attempts'),
#                 func.sum(ProcessingSession.original_file_size).label('total_bytes_processed'),
#
#                 # Распределение ошибок по стадиям
#                 func.count(ProcessingSession.id).filter(ProcessingSession.error_stage == 'download').label('download_errors'),
#                 func.count(ProcessingSession.id).filter(ProcessingSession.error_stage == 'audio_extraction').label('audio_extraction_errors'),
#                 func.count(ProcessingSession.id).filter(ProcessingSession.error_stage == 'transcription').label('transcription_errors'),
#                 func.count(ProcessingSession.id).filter(ProcessingSession.error_stage == 'summary').label('summary_errors'),
#
#                 # Распределение по источникам
#                 func.count(ProcessingSession.id).filter(ProcessingSession.source_type == 'url').label('url_sessions'),
#                 func.count(ProcessingSession.id).filter(ProcessingSession.source_type == 'telegram').label('telegram_sessions')
#             )
#
#             # Фильтрация по пользователю
#             if user_id is not None:
#                 query = query.filter(ProcessingSession.user_id == user_id)
#
#             # Фильтрация по времени
#             if days is not None:
#                 from datetime import timedelta
#                 start_date = datetime.now() - timedelta(days=days)
#                 query = query.filter(ProcessingSession.started_at >= start_date)
#
#             result = await session.execute(query)
#             stats = result.first()
#
#             total_sessions = stats.total_sessions or 0
#             successful_sessions = stats.successful_sessions or 0
#
#             return {
#                 # Основные KPI
#                 'total_sessions': total_sessions,
#                 'successful_sessions': successful_sessions,
#                 'failed_sessions': stats.failed_sessions or 0,
#                 'overall_success_rate': (successful_sessions / total_sessions * 100) if total_sessions > 0 else 0,
#
#                 # Временные метрики
#                 'avg_total_duration_seconds': float(stats.avg_total_duration or 0),
#                 'avg_download_attempts': float(stats.avg_download_attempts or 0),
#
#                 # Объемы данных
#                 'total_bytes_processed': stats.total_bytes_processed or 0,
#
#                 # Анализ ошибок
#                 'error_breakdown': {
#                     'download': stats.download_errors or 0,
#                     'audio_extraction': stats.audio_extraction_errors or 0,
#                     'transcription': stats.transcription_errors or 0,
#                     'summary': stats.summary_errors or 0
#                 },
#
#                 # Распределение по источникам
#                 'source_breakdown': {
#                     'url': stats.url_sessions or 0,
#                     'telegram': stats.telegram_sessions or 0
#                 }
#             }
#
#     except Exception as e:
#         logging.error(f"Error getting processing session stats: {e}")
#         return {
#             'total_sessions': 0,
#             'successful_sessions': 0,
#             'failed_sessions': 0,
#             'overall_success_rate': 0,
#             'avg_total_duration_seconds': 0,
#             'avg_download_attempts': 0,
#             'total_bytes_processed': 0,
#             'error_breakdown': {'download': 0, 'audio_extraction': 0, 'transcription': 0, 'summary': 0},
#             'source_breakdown': {'url': 0, 'telegram': 0}
#         }


async def increment_download_attempts(session_id: str) -> bool:
    """
    Увеличивает счетчик попыток загрузки для сессии.
    
    Args:
        session_id: UUID сессии
        
    Returns:
        True если успешно обновлено
    """
    try:
        async with async_session() as session:
            processing_session = await session.execute(
                select(ProcessingSession).filter_by(session_id=session_id)
            )
            processing_session = processing_session.scalar_one_or_none()
            
            if processing_session:
                processing_session.total_download_attempts = (processing_session.total_download_attempts or 0) + 1
                await session.commit()
                return True
            return False
    except Exception as e:
        logging.error(f"Error incrementing download attempts for session {session_id}: {e}")
        return False


# Обновляем создание аудио лога для работы с сессиями
async def create_audio_log_with_session(session_id: str, user_id: int) -> int | None:
    """
    Создает запись аудио лога связанную с сессией.
    
    Args:
        session_id: UUID сессии обработки
        user_id: ID пользователя
        
    Returns:
        ID созданной записи или None при ошибке
    """
    try:
        async with async_session() as session:
            # Проверяем существование пользователя и сессии
            user_exists = await session.get(User, user_id)
            if not user_exists:
                logging.error(f"Cannot create audio log for non-existent user_id: {user_id}")
                return None
            
            new_audio_log = Audio(
                user_id=user_id,
                session_id=session_id
            )
            
            session.add(new_audio_log)
            await session.commit()
            await session.refresh(new_audio_log)
            
            logging.debug(f"Created audio log ID: {new_audio_log.id} for session {session_id}")
            return new_audio_log.id
            
    except Exception as e:
        logging.error(f"Error creating audio log for session {session_id}: {e}")
        return None


# === LLM REQUEST ФУНКЦИИ ===

async def create_llm_request(
    user_id: int,
    session_id: str | None,
    request_type: str,
    model_provider: str,
    model_name: str,
    prompt_length: int | None = None,
    context_length: int | None = None,
    success: bool | None = None,
    started_at: datetime | None = None
) -> int | None:
    """
    Создает новую запись LLM запроса.
    
    Args:
        user_id: ID пользователя
        session_id: UUID сессии (может быть None для чата)
        request_type: Тип запроса ('summary', 'chat', 'title_generation')
        model_provider: Провайдер модели ('openai', 'anthropic', 'groq')
        model_name: Название модели ('gpt-4', 'claude-3-sonnet')
        prompt_length: Длина промпта в символах
        context_length: Длина всего контекста в символах
        success: Успешность выполнения
    Returns:
        ID созданной записи или None при ошибке
    """
    try:
        async with async_session() as session:
            if success is not None:
                new_request = LLMRequest(
                user_id=user_id,
                session_id=session_id,
                request_type=request_type,
                model_provider=model_provider,
                model_name=model_name,
                prompt_length=prompt_length,
                context_length=context_length,
                success=success,
                completed_at=datetime.utcnow()
            )
            else:
                new_request = LLMRequest(
                    user_id=user_id,
                    session_id=session_id,
                    request_type=request_type,
                    model_provider=model_provider,
                    model_name=model_name,
                    prompt_length=prompt_length,
                    context_length=context_length
                )
            
            session.add(new_request)
            await session.commit()
            await session.refresh(new_request)
            return new_request.id
    except Exception as e:
        logging.error(f"Error creating LLM request: {e}")
        return None


async def update_llm_request(
    request_id: int,
    response_length: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    processing_duration: float | None = None,
    success: bool | None = None,
    error_message: str | None = None,
    estimated_cost_usd: float | None = None,
    model_provider: str | None = None,
    model_name: str | None = None,
) -> bool:
    """
    Обновляет LLM запрос результатами выполнения.
    
    Args:
        request_id: ID запроса для обновления
        response_length: Длина ответа в символах
        prompt_tokens: Количество токенов в промпте
        completion_tokens: Количество токенов в ответе
        total_tokens: Общее количество токенов
        processing_duration: Длительность обработки в секундах
        success: Успешность выполнения
        error_message: Сообщение об ошибке
        estimated_cost_usd: Оценочная стоимость в USD
        
    Returns:
        True если обновление успешно
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(LLMRequest).filter_by(id=request_id)
            )
            llm_request = result.scalar_one_or_none()
            
            if not llm_request:
                logging.error(f"LLM request with id {request_id} not found")
                return False
            
            # Обновляем поля, если они переданы
            if response_length is not None:
                llm_request.response_length = response_length
            if prompt_tokens is not None:
                llm_request.prompt_tokens = prompt_tokens
            if completion_tokens is not None:
                llm_request.completion_tokens = completion_tokens
            if total_tokens is not None:
                llm_request.total_tokens = total_tokens
            if processing_duration is not None:
                llm_request.processing_duration = processing_duration
            if success is not None:
                llm_request.success = success
                llm_request.completed_at = datetime.utcnow()
            if error_message is not None:
                llm_request.error_message = error_message
            if estimated_cost_usd is not None:
                llm_request.estimated_cost_usd = estimated_cost_usd
            if model_provider is not None:
                llm_request.model_provider = model_provider
            if model_name is not None:
                llm_request.model_name = model_name
            
            await session.commit()
            return True
    except Exception as e:
        logging.error(f"Error updating LLM request {request_id}: {e}")
        return False

#
# async def get_llm_request_stats(
#     user_id: int | None = None,
#     session_id: str | None = None,
#     request_type: str | None = None,
#     model_provider: str | None = None
# ) -> dict:
#     """
#     Получает статистику по LLM запросам.
#
#     Args:
#         user_id: Фильтр по пользователю
#         session_id: Фильтр по сессии
#         request_type: Фильтр по типу запроса
#         model_provider: Фильтр по провайдеру
#
#     Returns:
#         Словарь со статистикой
#     """
#     try:
#         async with async_session() as session:
#             # Базовый запрос
#             query = select(LLMRequest)
#
#             # Добавляем фильтры
#             if user_id:
#                 query = query.filter_by(user_id=user_id)
#             if session_id:
#                 query = query.filter_by(session_id=session_id)
#             if request_type:
#                 query = query.filter_by(request_type=request_type)
#             if model_provider:
#                 query = query.filter_by(model_provider=model_provider)
#
#             result = await session.execute(query)
#             requests = result.scalars().all()
#
#             if not requests:
#                 return {
#                     'total_requests': 0,
#                     'successful_requests': 0,
#                     'failed_requests': 0,
#                     'success_rate': 0.0,
#                     'avg_processing_duration': 0.0,
#                     'total_tokens': 0,
#                     'total_cost_usd': 0.0,
#                     'request_type_breakdown': {},
#                     'model_breakdown': {}
#                 }
#
#             # Вычисляем метрики
#             total_requests = len(requests)
#             successful_requests = sum(1 for r in requests if r.success)
#             failed_requests = total_requests - successful_requests
#             success_rate = (successful_requests / total_requests) * 100 if total_requests > 0 else 0
#
#             processing_durations = [r.processing_duration for r in requests if r.processing_duration]
#             avg_processing_duration = sum(processing_durations) / len(processing_durations) if processing_durations else 0
#
#             total_tokens = sum(r.total_tokens or 0 for r in requests)
#             total_cost_usd = sum(r.estimated_cost_usd or 0 for r in requests)
#
#             # Разбивка по типам запросов
#             request_type_breakdown = {}
#             for r in requests:
#                 rt = r.request_type
#                 if rt not in request_type_breakdown:
#                     request_type_breakdown[rt] = {'count': 0, 'success': 0}
#                 request_type_breakdown[rt]['count'] += 1
#                 if r.success:
#                     request_type_breakdown[rt]['success'] += 1
#
#             # Разбивка по моделям
#             model_breakdown = {}
#             for r in requests:
#                 model_key = f"{r.model_provider}/{r.model_name}"
#                 if model_key not in model_breakdown:
#                     model_breakdown[model_key] = {
#                         'count': 0,
#                         'success': 0,
#                         'total_tokens': 0,
#                         'total_cost': 0.0
#                     }
#                 model_breakdown[model_key]['count'] += 1
#                 if r.success:
#                     model_breakdown[model_key]['success'] += 1
#                 model_breakdown[model_key]['total_tokens'] += r.total_tokens or 0
#                 model_breakdown[model_key]['total_cost'] += r.estimated_cost_usd or 0
#
#             return {
#                 'total_requests': total_requests,
#                 'successful_requests': successful_requests,
#                 'failed_requests': failed_requests,
#                 'success_rate': round(success_rate, 2),
#                 'avg_processing_duration': round(avg_processing_duration, 2),
#                 'total_tokens': total_tokens,
#                 'total_cost_usd': round(total_cost_usd, 4),
#                 'request_type_breakdown': request_type_breakdown,
#                 'model_breakdown': model_breakdown
#             }
#     except Exception as e:
#         logging.error(f"Error getting LLM request stats: {e}")
#         return {}
#

# === АНОНИМНЫЕ ЧАТЫ ФУНКЦИИ ===

async def log_anonymous_chat_message(
    chat_session: str,
    message_from: str,
    text: str,
    message_order: int
) -> bool:
    """
    Логирует сообщение в анонимный чат.

    Args:
        chat_session: UUID анонимной сессии чата
        message_from: 'user' или 'assistant'
        text: Текст сообщения
        message_order: Порядковый номер сообщения в диалоге

    Returns:
        True если сообщение успешно сохранено
    """
    try:
        async with async_session() as session:
            new_message = AnonymousChatMessage(
                chat_session=chat_session,
                message_from=message_from,
                text=text,
                message_order=message_order,
                message_length=len(text)
            )
            session.add(new_message)
            await session.commit()
            return True
    except Exception as e:
        logging.error(f"Error logging anonymous chat message: {e}")
        return False


async def log_user_action(
    user_id: int,
    action_type: str,
    action_category: str,
    metadata: dict = None,
    session_id: str = None,
    payment_id: int = None,
    referral_id: int = None
) -> bool:
    """
    Логирует действие пользователя в базу данных.

    Используется для отслеживания:
    - Конверсионной воронки (от просмотра до покупки)
    - Истории подписок (активация, отмена, апгрейд)
    - Взаимодействия с ботом
    - Обработки контента
    - Реферальной программы
    - Административных действий

    Args:
        user_id: ID пользователя
        action_type: Тип действия (например, 'conversion_payment_link_created')
        action_category: Категория действия:
            - 'conversion': воронка конверсии
            - 'subscription': управление подписками
            - 'payment': платежные события
            - 'bot_interaction': взаимодействие с ботом
            - 'content_processing': обработка аудио/видео
            - 'referral': реферальная программа
            - 'admin': административные действия
            - 'settings': изменение настроек
        metadata: Дополнительные данные в формате dict
        session_id: ID сессии обработки (опционально)
        payment_id: ID платежа (опционально)
        referral_id: ID реферала (опционально)

    Returns:
        True если действие успешно сохранено

    Example:
        await log_user_action(
            user_id=123,
            action_type='conversion_payment_link_created',
            action_category='conversion',
            metadata={
                'payment_method': 'stripe',
                'subscription_type': 'monthly',
                'amount': 9.99
            }
        )
    """
    try:
        async with async_session() as session:
            new_action = UserAction(
                user_id=user_id,
                action_type=action_type,
                action_category=action_category,
                meta=metadata or {},
                session_id=session_id,
                payment_id=payment_id,
                referral_id=referral_id
            )
            session.add(new_action)
            await session.commit()
            return True
    except Exception as e:
        logging.error(f"Error logging user action: {e}. user_id={user_id}, action_type={action_type}, action_category={action_category}")
        return False


async def is_first_subscription_cancellation(user_id: int) -> bool:
    """
    Проверяет, была ли уже ручная отмена подписки у пользователя.

    Используется для определения, нужно ли показывать опрос об отмене.
    Проверяет наличие записей с action_type='subscription_cancelled_by_user'.

    Args:
        user_id: ID пользователя в базе данных

    Returns:
        True если это первая отмена (записей нет), False если уже была отмена
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(UserAction)
                .where(
                    UserAction.user_id == user_id,
                    UserAction.action_type == 'subscription_cancelled_by_user'
                )
                .limit(1)
            )
            existing_cancellation = result.scalar_one_or_none()
            return existing_cancellation is None
    except Exception as e:
        logging.error(f"Error checking first subscription cancellation: {e}. user_id={user_id}")
        return True  # В случае ошибки показываем опрос


async def get_processing_session_by_id(session_id: str) -> dict | None:
    """
    Получает данные сессии обработки по session_id.
    
    Args:
        session_id: UUID сессии обработки
        
    Returns:
        Словарь с данными сессии или None если не найдена
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(ProcessingSession)
                .where(ProcessingSession.session_id == session_id)
            )
            processing_session = result.scalar_one_or_none()
            
            if not processing_session:
                logging.warning(f"Processing session not found: {session_id}")
                return None
                
            return {
                'session_id': processing_session.session_id,
                'user_id': processing_session.user_id,
                'original_identifier': processing_session.original_identifier,
                'source_type': processing_session.source_type,
                'specific_source': processing_session.specific_source,
                'started_at': processing_session.started_at,
                'completed_at': processing_session.completed_at,
                'final_status': processing_session.final_status,
                'error_stage': processing_session.error_stage,
                'error_message': processing_session.error_message,
                'original_file_size': processing_session.original_file_size,
                'total_download_attempts': processing_session.total_download_attempts,
                'total_duration': processing_session.total_duration
            }
            
    except Exception as e:
        logging.error(f"Error getting processing session {session_id}: {e}")
        return None


async def get_transcript_by_chat_session(chat_session: str) -> str | None:
    """
    Получает транскрипт (первое сообщение) из анонимного чата по chat_session.
    
    Args:
        chat_session: UUID анонимной сессии чата
        
    Returns:
        Текст транскрипта или None если не найден
    """
    try:
        async with async_session() as session:
            # Ищем первое сообщение пользователя (message_order=1, message_from='user')
            result = await session.execute(
                select(AnonymousChatMessage.text)
                .filter_by(chat_session=chat_session, message_from='user', message_order=1)
            )
            transcript = result.scalar_one_or_none()
            return transcript
    except Exception as e:
        logging.error(f"Error getting transcript by chat_session {chat_session}: {e}")
        return None

async def get_transcription_data(session_id: str) -> dict | None:
    """
    Получает данные транскрипции по session_id через processing_session.transcription_id.
    Return:
        dict with raw_transcript, timecoded_transcript, transcription_id
    """
    try:
        async with async_session() as session:
            # Находим processing_session
            ps_result = await session.execute(
                select(ProcessingSession).filter_by(session_id=session_id)
            )
            processing_session = ps_result.scalar_one_or_none()

            if not processing_session or not processing_session.transcription_id:
                logging.warning(f"No transcription found for session {session_id}")
                return None

            # Получаем транскрипцию по transcription_id
            t_result = await session.execute(
                select(Transcription).filter_by(id=processing_session.transcription_id)
            )
            transcription = t_result.scalar_one_or_none()

            if not transcription:
                logging.error(f"Transcription {processing_session.transcription_id} not found for session {session_id}")
                return None

            return {
                'raw_transcript': transcription.transcript_raw,
                'timecoded_transcript': transcription.transcript_timecoded,
                'transcription_id': transcription.id
            }
    except Exception as e:
        logging.error(f"Error getting transcription data for session {session_id}: {e}")
        return None

async def count_user_chat_requests_by_session(user_id: int, session_id: str) -> int:
    """
    Подсчитывает количество chat LLM запросов пользователя для конкретной сессии.
    
    Args:
        user_id: ID пользователя
        session_id: UUID сессии обработки
        
    Returns:
        Количество chat запросов
    """
    try:
        async with async_session() as session:
            result = await session.execute(
                select(func.count(LLMRequest.id))
                .filter(
                    LLMRequest.user_id == user_id,
                    LLMRequest.session_id == session_id,
                    LLMRequest.request_type == 'chat'
                )
            )
            count = result.scalar() or 0
            return count
    except Exception as e:
        logging.error(f"Error counting user chat requests for session {session_id}: {e}")
        return 0

#
# async def get_anonymous_chat_dialog(chat_session: str) -> list[dict]:
#     """
#     Получает полный диалог по анонимной сессии.
#
#     Args:
#         chat_session: UUID анонимной сессии
#
#     Returns:
#         Список сообщений в хронологическом порядке
#     """
#     try:
#         async with async_session() as session:
#             result = await session.execute(
#                 select(AnonymousChatMessage)
#                 .filter_by(chat_session=chat_session)
#                 .order_by(AnonymousChatMessage.message_order)
#             )
#             messages = result.scalars().all()
#
#             return [
#                 {
#                     'message_from': msg.message_from,
#                     'text': msg.text,
#                     'message_order': msg.message_order,
#                     'created_at': msg.created_at,
#                     'message_length': msg.message_length
#                 }
#                 for msg in messages
#             ]
#     except Exception as e:
#         logging.error(f"Error getting anonymous chat dialog {chat_session}: {e}")
#         return []

#
# async def get_anonymous_chat_stats() -> dict:
#     """
#     Получает статистику по анонимным чатам для маркетингового анализа.
#
#     Returns:
#         Словарь со статистикой
#     """
#     try:
#         async with async_session() as session:
#             # Общая статистика
#             total_messages_result = await session.execute(
#                 select(func.count(AnonymousChatMessage.id))
#             )
#             total_messages = total_messages_result.scalar()
#
#             # Количество уникальных диалогов
#             unique_chats_result = await session.execute(
#                 select(func.count(func.distinct(AnonymousChatMessage.chat_session)))
#             )
#             unique_chats = unique_chats_result.scalar()
#
#             # Средняя длина сообщений
#             avg_length_result = await session.execute(
#                 select(func.avg(AnonymousChatMessage.message_length))
#             )
#             avg_message_length = avg_length_result.scalar() or 0
#
#             # Распределение по ролям
#             role_stats_result = await session.execute(
#                 select(
#                     AnonymousChatMessage.message_from,
#                     func.count(AnonymousChatMessage.id).label('count'),
#                     func.avg(AnonymousChatMessage.message_length).label('avg_length')
#                 )
#                 .group_by(AnonymousChatMessage.message_from)
#             )
#             role_stats = {row.message_from: {'count': row.count, 'avg_length': round(row.avg_length or 0, 2)}
#                          for row in role_stats_result}
#
#             # Статистика по длине диалогов
#             dialog_length_result = await session.execute(
#                 select(
#                     AnonymousChatMessage.chat_session,
#                     func.count(AnonymousChatMessage.id).label('messages_count')
#                 )
#                 .group_by(AnonymousChatMessage.chat_session)
#             )
#             dialog_lengths = [row.messages_count for row in dialog_length_result]
#             avg_dialog_length = sum(dialog_lengths) / len(dialog_lengths) if dialog_lengths else 0
#
#             return {
#                 'total_messages': total_messages,
#                 'unique_chat_sessions': unique_chats,
#                 'avg_message_length': round(avg_message_length, 2),
#                 'avg_dialog_length': round(avg_dialog_length, 2),
#                 'role_breakdown': role_stats,
#                 'max_dialog_length': max(dialog_lengths) if dialog_lengths else 0,
#                 'min_dialog_length': min(dialog_lengths) if dialog_lengths else 0
#             }
#     except Exception as e:
#         logging.error(f"Error getting anonymous chat stats: {e}")
#         return {}

#
# async def get_popular_user_questions(limit: int = 100) -> list[dict]:
#     """
#     Получает популярные вопросы пользователей для маркетингового анализа.
#
#     Args:
#         limit: Максимальное количество вопросов
#
#     Returns:
#         Список популярных вопросов с частотой
#     """
#     try:
#         async with async_session() as session:
#             result = await session.execute(
#                 select(
#                     AnonymousChatMessage.text,
#                     AnonymousChatMessage.message_length,
#                     func.count(AnonymousChatMessage.text).label('frequency')
#                 )
#                 .filter_by(message_from='user')
#                 .group_by(AnonymousChatMessage.text, AnonymousChatMessage.message_length)
#                 .order_by(func.count(AnonymousChatMessage.text).desc())
#                 .limit(limit)
#             )
#
#             return [
#                 {
#                     'question': row.text,
#                     'frequency': row.frequency,
#                     'length': row.message_length
#                 }
#                 for row in result
#             ]
#     except Exception as e:
#         logging.error(f"Error getting popular user questions: {e}")
#         return []
#
#
# async def get_assistant_response_patterns(limit: int = 50) -> list[dict]:
#     """
#     Анализирует паттерны ответов ассистента для улучшения качества.
#
#     Args:
#         limit: Максимальное количество паттернов
#
#     Returns:
#         Список паттернов ответов с частотой
#     """
#     try:
#         async with async_session() as session:
#             # Получаем самые частые начала ответов (первые 50 символов)
#             result = await session.execute(
#                 select(
#                     func.substring(AnonymousChatMessage.text, 1, 50).label('response_start'),
#                     func.count().label('frequency'),
#                     func.avg(AnonymousChatMessage.message_length).label('avg_length')
#                 )
#                 .filter_by(message_from='assistant')
#                 .group_by(func.substring(AnonymousChatMessage.text, 1, 50))
#                 .order_by(func.count().desc())
#                 .limit(limit)
#             )
#
#             return [
#                 {
#                     'response_pattern': row.response_start,
#                     'frequency': row.frequency,
#                     'avg_length': round(row.avg_length or 0, 2)
#                 }
#                 for row in result
#             ]
#     except Exception as e:
#         logging.error(f"Error getting assistant response patterns: {e}")
#         return []

async def get_sessions(final_status: str | None = 'blank', 
                        notification_status: str | None = 'blank', 
                        recovery_status: str | None = 'blank') -> list[dict]:
    """
    Получает список незакрытых сессий. Они могут быть не закрыты из перезагрузки или потому что активны.
    Делаем поиск по final_status. Если final_status не None, то сессия закрыта.
    Return:
        Список сессий в виде словарей.
    """
    async with async_session() as session:
        try:
            options = []
            if final_status != 'blank':
                options.append(ProcessingSession.final_status == final_status)
            if notification_status != 'blank':
                if notification_status == 'pending':
                    status = NotificationStatusEnum.pending.value
                elif notification_status == 'sent':
                    status = NotificationStatusEnum.sent.value
                elif notification_status == 'not_required':
                    status = NotificationStatusEnum.not_required.value
                options.append(ProcessingSession.notification_status == status)
            if recovery_status != 'blank':
                if recovery_status == 'pending_decision':
                    status = RecoveryStatusEnum.pending_decision.value
                elif recovery_status == 'retry_scheduled':
                    status = RecoveryStatusEnum.retry_scheduled.value
                elif recovery_status == 'retried':
                    status = RecoveryStatusEnum.retried.value
                options.append(ProcessingSession.recovery_status == status)
            stmt = select(ProcessingSession.__table__).where(*options)
            result = await session.execute(stmt)
            rows = result.mappings().all()
            return [dict(row) for row in rows]
        except Exception as e:
            logging.error(f"Error getting sessions: {e}")
            return []



async def mark_sessions_interrupted_on_shutdown() -> int:
    """
    Обновляет все незавершенные сессии (final_status IS NULL),
    отмечая их как прерванные из-за выключения системы.

    Устанавливает:
    - final_status = 'interrupted'
    - notification_status = 'pending'
    - recovery_status = 'ignored'
    - error_stage = 'system_shutdown'

    Returns:
        Количество обновленных строк.
    """
    try:
        async with async_session() as session:
            stmt = (
                update(ProcessingSession)
                .where(ProcessingSession.final_status.is_(None))
                .values(
                    final_status='interrupted',
                    notification_status=NotificationStatusEnum.pending.value,
                    recovery_status=RecoveryStatusEnum.ignored.value,
                    error_stage='system_shutdown'
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)
    except Exception as e:
        logging.error(f"Error marking sessions on shutdown: {e}")
        return 0

async def _send_interruption_notifications_stub(sessions: list[dict]) -> None:
    """
    Болванка для рассылки оповещений по прерванным сессиям.
    Пока просто логируем количество.
    """
    if len(sessions) == 0:
        await send_alert(text="No sessions to notify", topic="SYSTEM", level="INFO")
        return
    
    
    try:
        logging.info(
            "[NOTIFY_STUB] Pending interruption notifications: %s sessions",
            len(sessions)
        )
        success_count = 0
        skipped_count = 0
        
        for session in sessions:
            if session['waiting_message_id']:
                try:
                    user: dict = await get_user(user_id=session['user_id'])
                    telegram_id = user['telegram_id']
                except Exception as e:
                    logging.error(f"Error getting telegram_id for user {session['user_id']}: {e}")
                    continue
                
                if telegram_id:
                    try:
                        logging.info(f"Sending interruption notification to user {session['user_id']}, telegram_id: {telegram_id}")
                        translator_hub: TranslatorHub = create_translator_hub()
                        i18n = translator_hub.get_translator_by_locale(locale=user['user_language'])

                        try:
                            await get_bot().delete_message(chat_id=telegram_id, message_id=session['waiting_message_id'])
                        except Exception as e:
                            logging.error(f"Error editing message for user {session['user_id']}, telegram_id: {telegram_id}: {e}")
                        
                        await get_bot().send_message(chat_id=telegram_id, text=i18n.restart_notification(), reply_to_message_id=session['user_original_message_id'])

                        await update_processing_session(session_id=session['session_id'], notification_status=NotificationStatusEnum.sent.value)
                        success_count += 1
                    except Exception as e:
                        logging.error(f"Error sending message for user {session['user_id']}, telegram_id: {telegram_id}: {e}")
                        await update_processing_session(session_id=session['session_id'], notification_status=NotificationStatusEnum.failed.value)
                        continue
            else:
                logging.error(f"No waiting message id for user {session['user_id']}")
                await update_processing_session(session_id=session['session_id'], notification_status=NotificationStatusEnum.not_required.value)
                skipped_count += 1

        logging.info(
            "[NOTIFY_STUB] Successfully sent interruption notifications: %s sessions, skipped: %s sessions",
            success_count,
            skipped_count
        )
        await send_alert(text=f"Successfully sent interruption notifications:\n{success_count} sessions out of {len(sessions)},\nskipped: {skipped_count} sessions", topic="SYSTEM", level="INFO")
    except Exception as e:
        await send_alert(text=f"Error sending interruption notifications: {e}", topic="SYSTEM", level="ERROR")

async def mark_sessions_interrupted_on_startup() -> None:
    """
    Помечает все незавершенные сессии как прерванные из-за перезапуска.
    """
    try:
        async with async_session() as session:
            stmt = update(ProcessingSession).where(ProcessingSession.final_status.is_(None)).values(final_status='interrupted', notification_status=NotificationStatusEnum.pending.value, recovery_status=RecoveryStatusEnum.ignored.value, error_stage='system_recovery')
            await session.execute(stmt)
            await session.commit()
    except Exception as e:
        logging.error(f"Error marking sessions on startup: {e}")

async def startup_handle_interrupted_sessions() -> list[dict]:
    """
    При старте бота помечает незавершенные сессии как прерванные из-за перезапуска
    и собирает список сессий, требующих оповещения пользователя.

    Шаги:
    1) final_status IS NULL -> обновляем на:
       - final_status = 'interrupted'
       - notification_status = 'pending'
       - recovery_status = 'ignored'
       - error_stage = 'system_recovery'
    2) Собираем все сессии с final_status = 'interrupted' и notification_status = 'pending'
       и передаем их в заглушку рассылки.

    Returns:
        Список сессий (dict), для которых требуется оповещение.
    """
    try:

        logging.info("Marking sessions as interrupted on startup")
        await mark_sessions_interrupted_on_startup()
        
        logging.info("Getting sessions to notify")
        sessions_to_notify = await get_sessions(final_status='interrupted', notification_status='pending')

        logging.info("Sending interruption notifications")
        await _send_interruption_notifications_stub(sessions_to_notify)
        
        return sessions_to_notify
    except Exception as e:
        logging.error(f"Error processing sessions on startup: {e}")
        return []


async def find_cached_transcription(
    source_type: str,
    original_identifier: str,
    file_hash: str = None
) -> dict | None:
    """
    Ищет закэшированную транскрипцию.
    Сначала по source_key, затем по file_hash (если есть).

    Args:
        source_type: Тип источника ('url' или 'telegram')
        original_identifier: Оригинальный идентификатор (URL или file_id)
        file_hash: SHA256 хэш файла (опционально)

    Returns:
        Словарь с данными транскрипции или None
    """
    from services.cache_normalization import normalize_source_key

    try:
        async with async_session() as session:
            # Генерируем source_key
            source_key = normalize_source_key(source_type, original_identifier)

            # Ищем по source_key
            result = await session.execute(
                select(Transcription).filter(
                    Transcription.source_key == source_key
                )
            )
            transcription = result.scalar_one_or_none()

            # Если не нашли по source_key и есть file_hash, ищем по нему
            if not transcription and file_hash:
                result = await session.execute(
                    select(Transcription).filter(
                        Transcription.file_hash == file_hash
                    )
                )
                transcription = result.scalar_one_or_none()

            if transcription:
                # Обновляем статистику использования
                await session.execute(
                    update(Transcription)
                    .where(Transcription.id == transcription.id)
                    .values(
                        reuse_count=Transcription.reuse_count + 1,
                        last_reused_at=datetime.utcnow()
                    )
                )
                await session.commit()

                logging.info(f"Cache HIT for transcription: source_key={source_key}, transcription_id={transcription.id}")

                return {
                    'id': transcription.id,
                    'transcript_raw': transcription.transcript_raw,
                    'transcript_timecoded': transcription.transcript_timecoded,
                    'transcription_provider': transcription.transcription_provider,
                    'transcription_model': transcription.transcription_model,
                    'language_detected': transcription.language_detected,
                    'audio_duration': transcription.audio_duration,
                    'file_size_bytes': transcription.file_size_bytes,
                    'created_at': transcription.created_at,
                    'created_by_session_id': transcription.created_by_session_id
                }

            logging.info(f"Cache MISS for transcription: source_key={source_key}")
            return None

    except Exception as e:
        logging.error(f"Error finding cached transcription: {e}")
        return None


async def find_cached_transcription_by_file_path(file_path: str) -> dict | None:
    """
    Ищет закэшированную транскрипцию по пути к файлу.
    Не блокирует event loop: хэш файла читается асинхронно чанками.

    Args:
        file_path: Путь к локальному файлу

    Returns:
        Словарь с данными транскрипции или None
    """
    try:
        # Генерируем SHA256 хэш файла асинхронно (минимум ресурсов, без блокировки)
        from services.cache_normalization import generate_file_hash_async
        file_hash = await generate_file_hash_async(file_path=file_path)
        if not file_hash:
            return None

        async with async_session() as session:
            result = await session.execute(
                select(Transcription).filter(
                    Transcription.file_hash == file_hash
                )
            )
            transcription = result.scalar_one_or_none()

            if transcription:
                # Обновляем статистику использования
                await session.execute(
                    update(Transcription)
                    .where(Transcription.id == transcription.id)
                    .values(
                        reuse_count=Transcription.reuse_count + 1,
                        last_reused_at=datetime.utcnow()
                    )
                )
                await session.commit()

                logging.info(f"Cache HIT for transcription by file_hash: transcription_id={transcription.id}")

                return {
                    'id': transcription.id,
                    'transcript_raw': transcription.transcript_raw,
                    'transcript_timecoded': transcription.transcript_timecoded,
                    'transcription_provider': transcription.transcription_provider,
                    'transcription_model': transcription.transcription_model,
                    'language_detected': transcription.language_detected,
                    'audio_duration': transcription.audio_duration,
                    'file_size_bytes': transcription.file_size_bytes,
                    'created_at': transcription.created_at,
                    'created_by_session_id': transcription.created_by_session_id
                }

            logging.info("Cache MISS for transcription by file_hash")
            return None

    except Exception as e:
        logging.error(f"Error finding cached transcription by file path: {e}")
        return None


async def find_cached_summary(
    transcription_id: int,
    language_code: str,
    llm_model: str,
    system_prompt: str
) -> dict | None:
    """
    Ищет закэшированное саммари для заданных параметров

    Args:
        transcription_id: ID транскрипции
        language_code: Код языка ('ru', 'en', etc.)
        llm_model: Название LLM модели
        system_prompt: Системный промпт

    Returns:
        Словарь с данными саммари или None
    """
    from services.cache_normalization import generate_prompt_hash

    try:
        prompt_hash = generate_prompt_hash(system_prompt)

        async with async_session() as session:
            result = await session.execute(
                select(Summary).filter(
                    Summary.transcription_id == transcription_id,
                    Summary.language_code == language_code,
                    Summary.llm_model == llm_model,
                    Summary.system_prompt_hash == prompt_hash
                )
            )
            summary = result.scalar_one_or_none()

            if summary:
                # Обновляем статистику использования
                await session.execute(
                    update(Summary)
                    .where(Summary.id == summary.id)
                    .values(
                        reuse_count=Summary.reuse_count + 1,
                        last_reused_at=datetime.utcnow()
                    )
                )
                await session.commit()

                logging.info(f"Cache HIT for summary: transcription_id={transcription_id}, summary_id={summary.id}")

                return {
                    'id': summary.id,
                    'summary_text': summary.summary_text,
                    'generated_title': summary.generated_title,
                    'llm_request_id': summary.llm_request_id
                }

            logging.info(f"Cache MISS for summary: transcription_id={transcription_id}, language={language_code}, model={llm_model}")
            return None

    except Exception as e:
        logging.error(f"Error finding cached summary: {e}")
        return None


async def save_transcription_cache(
    source_type: str,
    original_identifier: str,
    transcript_raw: str,
    transcript_timecoded: str,
    transcription_provider: str,
    session_id: str,
    file_hash: str = None,
    specific_source: str = None,
    transcription_model: str = None,
    language_detected: str = None,
    file_size_bytes: int = None,
    audio_duration: float = None
) -> int:
    """
    Сохраняет транскрипцию в кэш

    Args:
        source_type: Тип источника
        original_identifier: Оригинальный идентификатор
        transcript_raw: Транскрипция без таймкодов
        transcript_timecoded: Транскрипция с таймкодами
        transcription_provider: Провайдер транскрипции
        session_id: ID сессии обработки
        file_hash: SHA256 хэш файла (опционально)
        specific_source: Специфический источник (youtube, instagram, etc.)
        transcription_model: Модель транскрипции
        language_detected: Обнаруженный язык
        file_size_bytes: Размер файла
        audio_duration: Длительность аудио

    Returns:
        ID созданной записи транскрипции
    """
    from services.cache_normalization import normalize_source_key

    try:
        source_key = normalize_source_key(source_type, original_identifier)

        async with async_session() as session:
            # Try find existing by source_key
            result = await session.execute(
                select(Transcription).filter(Transcription.source_key == source_key)
            )
            existing: Transcription | None = result.scalar_one_or_none()

            if existing:
                # Overwrite fields and keep the same id. Reset provider/model/text per new result
                existing.source_type = source_type
                existing.original_identifier = original_identifier
                existing.specific_source = specific_source
                existing.file_hash = file_hash
                existing.file_size_bytes = file_size_bytes
                existing.audio_duration = audio_duration
                existing.transcript_raw = transcript_raw
                existing.transcript_timecoded = transcript_timecoded
                existing.transcription_provider = transcription_provider
                existing.transcription_model = transcription_model
                existing.language_detected = language_detected
                existing.created_by_session_id = session_id
                existing.created_at = datetime.utcnow()
                existing.reuse_count = 0
                existing.last_reused_at = None

                await session.commit()
                logging.info(f"Updated transcription cache by source_key: transcription_id={existing.id}, source_key={source_key}")
                return existing.id
            else:
                transcription = Transcription(
                    source_type=source_type,
                    original_identifier=original_identifier,
                    source_key=source_key,
                    specific_source=specific_source,
                    file_hash=file_hash,
                    file_size_bytes=file_size_bytes,
                    audio_duration=audio_duration,
                    transcript_raw=transcript_raw,
                    transcript_timecoded=transcript_timecoded,
                    transcription_provider=transcription_provider,
                    transcription_model=transcription_model,
                    language_detected=language_detected,
                    created_by_session_id=session_id
                )
                session.add(transcription)
                await session.commit()
                await session.refresh(transcription)

                logging.info(f"Saved transcription to cache: transcription_id={transcription.id}, source_key={source_key}")
                return transcription.id

    except Exception as e:
        logging.error(f"Error saving transcription cache: {e}")
        return None


async def save_summary_cache(
    transcription_id: int,
    language_code: str,
    llm_provider: str,
    llm_model: str,
    system_prompt: str,
    summary_text: str,
    session_id: str,
    generated_title: str = None,
    llm_request_id: int = None
) -> int:
    """
    Сохраняет саммари в кэш

    Args:
        transcription_id: ID транскрипции
        language_code: Код языка
        llm_provider: Провайдер LLM
        llm_model: Модель LLM
        system_prompt: Системный промпт
        summary_text: Текст саммари
        session_id: ID сессии обработки
        generated_title: Сгенерированный заголовок (опционально)
        llm_request_id: ID LLM запроса (опционально)

    Returns:
        ID созданной записи саммари
    """
    from services.cache_normalization import generate_prompt_hash

    try:
        prompt_hash = generate_prompt_hash(system_prompt)

        async with async_session() as session:
            # Try find existing summary for this combination
            result = await session.execute(
                select(Summary).filter(
                    Summary.transcription_id == transcription_id,
                    Summary.language_code == language_code,
                    Summary.llm_model == llm_model,
                    Summary.system_prompt_hash == prompt_hash
                )
            )
            existing: Summary | None = result.scalar_one_or_none()

            if existing:
                existing.llm_provider = llm_provider
                existing.summary_text = summary_text
                existing.generated_title = generated_title
                existing.llm_request_id = llm_request_id
                existing.created_by_session_id = session_id
                existing.created_at = datetime.utcnow()
                existing.reuse_count = 0
                existing.last_reused_at = None
                await session.commit()
                logging.info(f"Updated summary cache: summary_id={existing.id}, transcription_id={transcription_id}")
                return existing.id
            else:
                summary = Summary(
                    transcription_id=transcription_id,
                    language_code=language_code,
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                    system_prompt_hash=prompt_hash,
                    summary_text=summary_text,
                    generated_title=generated_title,
                    llm_request_id=llm_request_id,
                    created_by_session_id=session_id
                )
                session.add(summary)
                await session.commit()
                await session.refresh(summary)

                logging.info(f"Saved summary to cache: summary_id={summary.id}, transcription_id={transcription_id}")
                return summary.id

    except Exception as e:
        logging.error(f"Error saving summary cache: {e}")
        return None



# ==================== PAYMENT REMINDERS ====================
# Функции для системы напоминаний о незавершенных платежах

async def get_users_for_first_reminder(
    batch_size: int = 100,
    reminder_hours: int = 2,
    search_window_hours: int = 1,
    reference_time: datetime | None = None
) -> list[dict]:
    """
    Находит пользователей для первого напоминания о незавершенной оплате.

    Логика: Ищет пользователей с ЛЮБЫМ действием категории 'conversion' (не только платежную ссылку),
    которые не завершили оплату и еще не получали первое напоминание.

    Оптимизированный запрос использует:
    - Partial index idx_user_actions_reminders
    - Subquery для проверки отправки напоминания
    - Проверку отсутствия успешных транзакций в таблице payments
    - LIMIT для контроля нагрузки
    - Динамический расчет временного окна

    Важно: Напоминание отправляется только тем, кто НИКОГДА не оплачивал (нет completed платежей).

    Args:
        batch_size: Максимальное количество пользователей для обработки за раз
        reminder_hours: Через сколько часов после первого действия отправлять напоминание (по умолчанию 2)
        search_window_hours: Ширина временного окна для поиска в часах (по умолчанию 1)
        reference_time: Точка отсчета времени. Если None, используется datetime.utcnow()

    Returns:
        Список словарей с данными пользователей:
        - user_id: ID пользователя в БД
        - telegram_id: Telegram ID
        - user_language: Язык пользователя
        - first_created_at: Время первого действия конверсии
    """
    try:
        from sqlalchemy import func, exists, and_, text
        from datetime import datetime, timedelta

        async with async_session() as session:
            # Динамическое временное окно: от reminder_hours до (reminder_hours + search_window_hours) назад
            # Например, для reminder_hours=2 и search_window_hours=1: от 3 до 2 часов назад
            now = reference_time or datetime.utcnow()
            time_window_start = now - timedelta(hours=reminder_hours + search_window_hours)
            time_window_end = now - timedelta(hours=reminder_hours)

            if reference_time:
                logging.debug(f"get_users_for_first_reminder: using reference_time={reference_time}")

            # Подзапрос для нахождения первого действия конверсии для каждого пользователя
            # в заданном временном окне (ЛЮБОЙ тип, но категория 'conversion')
            first_action_subq = (
                select(
                    UserAction.user_id,
                    func.min(UserAction.created_at).label('first_created_at')
                )
                .where(
                    UserAction.action_category == 'conversion',  # Любое действие конверсии!
                    UserAction.created_at >= time_window_start,
                    UserAction.created_at <= time_window_end
                )
                .group_by(UserAction.user_id)
                .subquery()
            )

            # Основной запрос
            query = (
                select(
                    User.id.label('user_id'),
                    User.telegram_id,
                    User.user_language,
                    first_action_subq.c.first_created_at
                )
                .select_from(User)
                .join(first_action_subq, User.id == first_action_subq.c.user_id)
                .where(
                    # У пользователя нет активной подписки
                    User.subscription != 'True',
                    # Первое напоминание еще не отправляли (успешно или с ошибкой)
                    ~exists(
                        select(1)
                        .where(
                            UserAction.user_id == User.id,
                            UserAction.action_type.in_([
                                'conversion_reminder_first_sent',
                                'conversion_reminder_first_failed'
                            ])
                        )
                    ),
                    # У пользователя НЕТ успешных транзакций (никогда не оплачивал)
                    ~exists(
                        select(1)
                        .where(
                            Payment.user_id == User.id,
                            Payment.status.in_(["completed", "success"])
                        )
                    )
                )
                .limit(batch_size)
            )

            result = await session.execute(query)
            users = result.mappings().all()

            logging.info(
                f"Found {len(users)} users for first payment reminder "
                f"(window: {time_window_start} to {time_window_end})"
            )

            return [dict(row) for row in users]

    except Exception as e:
        logging.error(f"Error getting users for first reminder: {e}", exc_info=True)
        return []


async def get_users_for_second_reminder(
    batch_size: int = 100,
    reminder_hours: int = 24,
    search_window_hours: int = 1,
    reference_time: datetime | None = None
) -> list[dict]:
    """
    Находит пользователей для второго напоминания о незавершенной оплате.

    Сложная логика: Отсчитывает reminder_hours от ПОСЛЕДНЕГО релевантного действия пользователя:
    1. Первое напоминание (conversion_reminder_first_sent)
    2. Любое действие категории 'conversion'
    3. Обработанная сессия (processing_sessions с final_status='success')

    Если пользователь после первого напоминания снова зашел в меню подписки или обработал аудио,
    второе напоминание откладывается.

    Оптимизированный запрос использует:
    - CTE (Common Table Expression) для нахождения последнего действия
    - Subquery для проверки отправки напоминаний
    - Проверку отсутствия успешных транзакций в таблице payments
    - LIMIT для контроля нагрузки
    - Динамический расчет временного окна

    Важно: Напоминание отправляется только тем, кто НИКОГДА не оплачивал (нет completed платежей).

    Args:
        batch_size: Максимальное количество пользователей для обработки за раз
        reminder_hours: Через сколько часов после последнего действия отправлять напоминание (по умолчанию 24)
        search_window_hours: Ширина временного окна для поиска в часах (по умолчанию 1)
        reference_time: Точка отсчета времени. Если None, используется datetime.utcnow()

    Returns:
        Список словарей с данными пользователей:
        - user_id: ID пользователя в БД
        - telegram_id: Telegram ID
        - user_language: Язык пользователя
        - first_created_at: Время первого действия конверсии
        - last_activity_at: Время последнего релевантного действия
    """
    try:
        from sqlalchemy import func, exists, and_, text, case
        from datetime import datetime, timedelta

        async with async_session() as session:
            # Динамическое временное окно
            now = reference_time or datetime.utcnow()
            time_window_start = now - timedelta(hours=reminder_hours + search_window_hours + 5)
            time_window_end = now - timedelta(hours=reminder_hours)

            if reference_time:
                logging.debug(f"get_users_for_second_reminder: using reference_time={reference_time}")

            # Подзапрос 1: Последнее действие конверсии
            last_conversion_subq = (
                select(
                    UserAction.user_id,
                    func.max(UserAction.created_at).label('last_conversion_at')
                )
                .where(UserAction.action_category == 'conversion')
                .group_by(UserAction.user_id)
                .subquery()
            )

            # Подзапрос 2: Последняя обработанная сессия
            last_session_subq = (
                select(
                    ProcessingSession.user_id,
                    func.max(ProcessingSession.completed_at).label('last_session_at')
                )
                .where(ProcessingSession.final_status == 'success')
                .group_by(ProcessingSession.user_id)
                .subquery()
            )

            # Подзапрос 3: Время первого действия конверсии (для метаданных)
            first_conversion_subq = (
                select(
                    UserAction.user_id,
                    func.min(UserAction.created_at).label('first_created_at')
                )
                .where(UserAction.action_category == 'conversion')
                .group_by(UserAction.user_id)
                .subquery()
            )

            # Подзапрос 4: Объединяем все даты и находим максимальную (последнее действие)
            last_activity_subq = (
                select(
                    User.id.label('user_id'),
                    func.greatest(
                        func.coalesce(last_conversion_subq.c.last_conversion_at, datetime(1970, 1, 1)),
                        func.coalesce(last_session_subq.c.last_session_at, datetime(1970, 1, 1))
                    ).label('last_activity_at')
                )
                .select_from(User)
                .outerjoin(last_conversion_subq, User.id == last_conversion_subq.c.user_id)
                .outerjoin(last_session_subq, User.id == last_session_subq.c.user_id)
                .subquery()
            )

            # Подзапрос 5: Последний выбранный метод оплаты
            # Используем DISTINCT ON для получения последней записи для каждого пользователя
            last_payment_method_subq = (
                select(
                    UserAction.user_id,
                    UserAction.meta['payment_method'].astext.label('last_payment_method')
                )
                .where(UserAction.action_type == 'conversion_payment_method_selected')
                .order_by(UserAction.user_id, UserAction.created_at.desc())
                .distinct(UserAction.user_id)
                .subquery()
            )

            # Основной запрос
            query = (
                select(
                    User.id.label('user_id'),
                    User.telegram_id,
                    User.user_language,
                    first_conversion_subq.c.first_created_at,
                    last_activity_subq.c.last_activity_at,
                    last_payment_method_subq.c.last_payment_method
                )
                .select_from(User)
                .join(first_conversion_subq, User.id == first_conversion_subq.c.user_id)
                .join(last_activity_subq, User.id == last_activity_subq.c.user_id)
                .outerjoin(last_payment_method_subq, User.id == last_payment_method_subq.c.user_id)
                .where(
                    # У пользователя нет активной подписки
                    User.subscription != 'True',
                    # Последнее действие было в нужном временном окне
                    last_activity_subq.c.last_activity_at >= time_window_start,
                    last_activity_subq.c.last_activity_at <= time_window_end,
                    # Первое напоминание уже было отправлено
                    exists(
                        select(1)
                        .where(
                            UserAction.user_id == User.id,
                            UserAction.action_type == 'conversion_reminder_first_sent'
                        )
                    ),
                    # Второе напоминание еще не отправляли (успешно или с ошибкой)
                    ~exists(
                        select(1)
                        .where(
                            UserAction.user_id == User.id,
                            UserAction.action_type.in_([
                                'conversion_reminder_second_sent',
                                'conversion_reminder_second_failed'
                            ])
                        )
                    ),
                    # У пользователя НЕТ успешных транзакций (никогда не оплачивал)
                    ~exists(
                        select(1)
                        .where(
                            Payment.user_id == User.id,
                            Payment.status.in_(["completed", "success"])
                        )
                    )
                )
                .limit(batch_size)
            )

            result = await session.execute(query)
            users = result.mappings().all()

            logging.info(
                f"Found {len(users)} users for second payment reminder "
                f"(window: {time_window_start} to {time_window_end})"
            )

            return [dict(row) for row in users]

    except Exception as e:
        logging.error(f"Error getting users for second reminder: {e}", exc_info=True)
        return []


# Алиасы для обратной совместимости (устаревшие, используйте новые названия)
async def get_users_for_2h_reminder(batch_size: int = 100) -> list[dict]:
    """
    УСТАРЕЛО: Используйте get_users_for_first_reminder() вместо этой функции.
    Алиас для обратной совместимости.
    """
    logging.warning("get_users_for_2h_reminder is deprecated, use get_users_for_first_reminder instead")
    return await get_users_for_first_reminder(batch_size=batch_size, reminder_hours=2, search_window_hours=1)


async def get_users_for_24h_reminder(batch_size: int = 100) -> list[dict]:
    """
    УСТАРЕЛО: Используйте get_users_for_second_reminder() вместо этой функции.
    Алиас для обратной совместимости.
    """
    logging.warning("get_users_for_24h_reminder is deprecated, use get_users_for_second_reminder instead")
    return await get_users_for_second_reminder(batch_size=batch_size, reminder_hours=24, search_window_hours=1)


async def main():
    sessions = await startup_handle_interrupted_sessions()
    print(len(sessions))
    # await _send_interruption_notifications_stub(sessions)

# ==================== ONBOARDING REMINDERS ====================

async def get_users_for_onboarding_day1(
    batch_size: int = 100,
    reference_time: datetime | None = None
) -> list[dict]:
    """
    Находит пользователей для первого онбординг-напоминания (Day 1).
    
    Условия:
    - Пользователь зарегистрировался вчера (в предыдущий календарный день).
    - Ничего не загружал (audio_uses == 0 и gpt_uses == 0).
    - Еще не получал это напоминание.

    Args:
        batch_size: Максимальное количество пользователей для обработки
        reference_time: Точка отсчета времени. Если None, используется datetime.utcnow()
                       Полезно для предсказания будущих напоминаний
    """
    try:
        from sqlalchemy import exists
        from datetime import datetime, timedelta
        
        async with async_session() as session:
            # Окно поиска: весь предыдущий день (вчера)
            now = reference_time or datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_start = today_start - timedelta(days=1)
            # Конец окна - начало сегодняшнего дня (не включительно)
            
            if reference_time:
                logging.debug(f"get_users_for_onboarding_day1: using reference_time={reference_time}")

            query = (
                select(User)
                .where(
                    User.created_at >= yesterday_start,
                    User.created_at < today_start,
                    User.audio_uses == 0,
                    User.gpt_uses == 0,
                    # Фильтр заблокированных пользователей
                    User.is_bot_blocked == False,
                    # У пользователя нет активной подписки
                    User.subscription != 'True',
                    # Еще не отправляли (успешно или с ошибкой)
                    ~exists(
                        select(1)
                        .where(
                            UserAction.user_id == User.id,
                            UserAction.action_type.in_([
                                'onboarding_reminder_day1_sent',
                                'onboarding_reminder_day1_failed'
                            ])
                        )
                    ),
                    # У пользователя НЕТ успешных транзакций (никогда не оплачивал)
                    ~exists(
                        select(1)
                        .where(
                            Payment.user_id == User.id,
                            Payment.status.in_(["completed", "success"])
                        )
                    )
                )
                .limit(batch_size)
            )
            
            result = await session.execute(query)
            users = result.scalars().all()
            return [_prepare_user_dict(u) for u in users]
            
    except Exception as e:
        logging.error(f"Error getting users for onboarding day 1: {e}", exc_info=True)
        return []

async def get_users_for_onboarding_day3(
    batch_size: int = 100,
    reference_time: datetime | None = None
) -> list[dict]:
    """
    Находит пользователей для второго онбординг-напоминания (Day 3).
    
    Условия:
    - Пользователь получил первое напоминание 2 дня назад (позавчера).
    - Все еще ничего не загружал (audio_uses == 0 и gpt_uses == 0).
    - Еще не получал это напоминание (и не было неудачных попыток).
    - Не заблокировал бота.
    - Пользователь зарегистрирован не ранее 12 ноября 2025.

    Args:
        batch_size: Максимальное количество пользователей для обработки
        reference_time: Точка отсчета времени. Если None, используется datetime.utcnow()
    """
    try:
        from sqlalchemy import exists, func
        from datetime import datetime, timedelta
        
        async with async_session() as session:
            # Окно поиска: 2 дня назад
            now = reference_time or datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            target_day_start = today_start - timedelta(days=2)
            target_day_end = today_start - timedelta(days=1)
            
            if reference_time:
                logging.debug(f"get_users_for_onboarding_day3: using reference_time={reference_time}")

            # Отсечка по регистрации: фиксированная дата (12 ноября 2025)
            # Мы не трогаем пользователей, зарегистрировавшихся до этой даты
            registration_cutoff = datetime(2025, 11, 12, 0, 0, 0)

            # Подзапрос: когда было отправлено первое напоминание
            first_reminder_subq = (
                select(
                    UserAction.user_id,
                    func.max(UserAction.created_at).label('sent_at')
                )
                .where(UserAction.action_type == 'onboarding_reminder_day1_sent')
                .group_by(UserAction.user_id)
                .subquery()
            )
            
            query = (
                select(User)
                .join(first_reminder_subq, User.id == first_reminder_subq.c.user_id)
                .where(
                    # Регистрация не старее 12 ноября 2025.
                    User.created_at > registration_cutoff,
                    # Прошло 2 дня (календарных) с первого напоминания
                    first_reminder_subq.c.sent_at >= target_day_start,
                    first_reminder_subq.c.sent_at < target_day_end,
                    # Пользователь все еще не активен
                    User.audio_uses == 0,
                    User.gpt_uses == 0,
                    # Фильтр заблокированных пользователей
                    User.is_bot_blocked == False,
                    # У пользователя нет активной подписки
                    User.subscription != 'True',
                    # Еще не отправляли второе (успешно или с ошибкой)
                    ~exists(
                        select(1)
                        .where(
                            UserAction.user_id == User.id,
                            UserAction.action_type.in_([
                                'onboarding_reminder_day3_sent',
                                'onboarding_reminder_day3_failed'
                            ])
                        )
                    ),
                    # У пользователя НЕТ успешных транзакций (никогда не оплачивал)
                    ~exists(
                        select(1)
                        .where(
                            Payment.user_id == User.id,
                            Payment.status.in_(["completed", "success"])
                        )
                    )
                )
                .limit(batch_size)
            )
            
            result = await session.execute(query)
            users = result.scalars().all()
            return [_prepare_user_dict(u) for u in users]
            
    except Exception as e:
        logging.error(f"Error getting users for onboarding day 3: {e}", exc_info=True)
        return []

async def get_users_for_first_upload_reminder(
    batch_size: int = 100,
    reference_time: datetime | None = None
) -> list[dict]:
    """
    Находит пользователей для напоминания после первой загрузки.
    
    Условия:
    - Пользователь имеет ровно 1 загрузку (audio_uses == 1).
    - Первая загрузка была более 48 часов назад (и менее 96 часов).
    - Еще не получал это напоминание (и не было неудачных попыток).
    - Не заблокировал бота.

    Args:
        batch_size: Максимальное количество пользователей для обработки
        reference_time: Точка отсчета времени. Если None, используется datetime.utcnow()
    """
    try:
        from sqlalchemy import exists, func
        from datetime import datetime, timedelta
        
        async with async_session() as session:
            # Окно поиска: 2 дня назад
            now = reference_time or datetime.utcnow()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            target_day_start = today_start - timedelta(days=2)
            target_day_end = today_start - timedelta(days=1)
            
            if reference_time:
                logging.debug(f"get_users_for_first_upload_reminder: using reference_time={reference_time}")

            # Отсечка по регистрации: фиксированная дата (12 ноября 2025)
            # Мы не трогаем пользователей, зарегистрировавшихся до этой даты
            registration_cutoff = datetime(2025, 11, 12, 0, 0, 0)

            # Подзапрос: когда была первая успешная сессия
            # Используем ProcessingSession так как она точнее отражает "загрузку"
            first_upload_subq = (
                select(
                    ProcessingSession.user_id,
                    func.min(ProcessingSession.completed_at).label('first_upload_at')
                )
                .where(ProcessingSession.final_status == 'success')
                .group_by(ProcessingSession.user_id)
                .subquery()
            )
            
            query = (
                select(User)
                .join(first_upload_subq, User.id == first_upload_subq.c.user_id)
                .where(
                    # Регистрация не старее 2 недель
                    User.created_at > registration_cutoff,
                    # Прошло 48+ часов с первой загрузки
                    first_upload_subq.c.first_upload_at >= target_day_start,
                    first_upload_subq.c.first_upload_at <= target_day_end,
                    # У пользователя ровно 1 использование (или хотя бы 1, но мы хотим таргетировать новичков)
                    # Промпт: "Если пользователь загрузил 1 файл"
                    User.audio_uses == 1,
                    # Фильтр заблокированных пользователей
                    User.is_bot_blocked == False,
                    # У пользователя нет активной подписки
                    User.subscription != 'True',
                    # Еще не отправляли (успешно или с ошибкой)
                    ~exists(
                        select(1)
                        .where(
                            UserAction.user_id == User.id,
                            UserAction.action_type.in_([
                                'onboarding_reminder_first_upload_sent',
                                'onboarding_reminder_first_upload_failed'
                            ])
                        )
                    ),
                    # У пользователя НЕТ успешных транзакций (никогда не оплачивал)
                    ~exists(
                        select(1)
                        .where(
                            Payment.user_id == User.id,
                            Payment.status.in_(["completed", "success"])
                        )
                    )
                )
                .limit(batch_size)
            )
            
            result = await session.execute(query)
            users = result.scalars().all()
            return [_prepare_user_dict(u) for u in users]
            
    except Exception as e:
        logging.error(f"Error getting users for first upload reminder: {e}", exc_info=True)
        return []


async def get_users_to_exclude_from_broadcast(
    time_window_hours: int = 24,
    extended_day3_hours: int = 48
) -> dict:
    """
    Получает пользователей, которых нужно исключить из рекламной рассылки.

    Исключаются пользователи, которые:
    1. Получили любое напоминание в последние N часов (по умолчанию 24)
    2. Должны получить любое напоминание в следующие N часов

    ВАЖНО: Day 3 reminder использует расширенное окно (48ч по умолчанию), так как
    отсчитывается от Day 1 reminder (не от регистрации). Это гарантирует, что мы
    не будем беспокоить пользователей, находящихся в воронке onboarding.

    Args:
        time_window_hours: Стандартное временное окно в часах. По умолчанию 24.
        extended_day3_hours: Расширенное окно для Day 3 reminder в часах.
                            По умолчанию 48 (48ч от Day 1 = Day 3).

    Returns:
        dict с ключами:
        - 'user_ids': set внутренних ID пользователей для исключения
        - 'stats': статистика по типам напоминаний
    """
    try:
        now = datetime.utcnow()
        past_time = now - timedelta(hours=time_window_hours)

        # Стандартное окно для большинства напоминаний
        standard_future = now + timedelta(hours=time_window_hours)

        # Расширенное окно для Day 3 (отсчитывается от Day 1, не от регистрации)
        extended_future = now + timedelta(hours=extended_day3_hours)

        # ========== 1. ПОЛЬЗОВАТЕЛИ, ПОЛУЧИВШИЕ НАПОМИНАНИЯ В ПОСЛЕДНИЕ N ЧАСОВ ==========

        async with async_session() as session:
            # Типы напоминаний для проверки
            reminder_types = [
                'onboarding_reminder_day1_sent',
                'onboarding_reminder_day3_sent',
                'onboarding_reminder_first_upload_sent',
                'conversion_reminder_first_sent',
                'conversion_reminder_second_sent'
            ]

            # Получаем пользователей с недавними напоминаниями
            recent_reminders_query = (
                select(UserAction.user_id, UserAction.action_type)
                .where(
                    UserAction.action_type.in_(reminder_types),
                    UserAction.created_at >= past_time
                )
            )

            result = await session.execute(recent_reminders_query)
            recent_reminders = result.all()

            # Статистика по недавним напоминаниям
            recent_users = {}
            for user_id, action_type in recent_reminders:
                if user_id not in recent_users:
                    recent_users[user_id] = []
                recent_users[user_id].append(action_type)

        # ========== 2. ПОЛЬЗОВАТЕЛИ, КОТОРЫЕ ДОЛЖНЫ ПОЛУЧИТЬ НАПОМИНАНИЯ В СЛЕДУЮЩИЕ N ЧАСОВ ==========

        upcoming_users = {}

        logging.debug(f"Checking upcoming reminders: standard={standard_future}, extended={extended_future}")

        # --- 2.1. Onboarding Day 1 (стандартное окно +24ч) ---
        day1_upcoming = await get_users_for_onboarding_day1(
            batch_size=10000,  # Получаем всех
            reference_time=standard_future
        )
        for user_dict in day1_upcoming:
            user_id = user_dict['id']
            if user_id not in upcoming_users:
                upcoming_users[user_id] = []
            upcoming_users[user_id].append('upcoming_onboarding_day1')

        # --- 2.2. Onboarding Day 3 (расширенное окно +48ч) ---
        # Day 3 отсчитывается от Day 1 reminder, поэтому используем extended_future
        day3_upcoming = await get_users_for_onboarding_day3(
            batch_size=10000,
            reference_time=extended_future  # +48h вместо +24h
        )
        for user_dict in day3_upcoming:
            user_id = user_dict['id']
            if user_id not in upcoming_users:
                upcoming_users[user_id] = []
            upcoming_users[user_id].append('upcoming_onboarding_day3')

        # --- 2.3. First Upload Reminder (стандартное окно +24ч) ---
        first_upload_upcoming = await get_users_for_first_upload_reminder(
            batch_size=10000,
            reference_time=standard_future
        )
        for user_dict in first_upload_upcoming:
            user_id = user_dict['id']
            if user_id not in upcoming_users:
                upcoming_users[user_id] = []
            upcoming_users[user_id].append('upcoming_first_upload_reminder')

        # --- 2.4. Conversion First Reminder (стандартное окно +24ч) ---
        conversion_first_upcoming = await get_users_for_first_reminder(
            batch_size=10000,
            reminder_hours=2,
            search_window_hours=1,
            reference_time=standard_future
        )
        for user_dict in conversion_first_upcoming:
            user_id = user_dict['user_id']
            if user_id not in upcoming_users:
                upcoming_users[user_id] = []
            upcoming_users[user_id].append('upcoming_conversion_first_reminder')

        # --- 2.5. Conversion Second Reminder (стандартное окно +24ч) ---
        conversion_second_upcoming = await get_users_for_second_reminder(
            batch_size=10000,
            reminder_hours=24,
            search_window_hours=1,
            reference_time=standard_future
        )
        for user_dict in conversion_second_upcoming:
            user_id = user_dict['user_id']
            if user_id not in upcoming_users:
                upcoming_users[user_id] = []
            upcoming_users[user_id].append('upcoming_conversion_second_reminder')

        # ========== 3. ОБЪЕДИНЯЕМ ВСЕ ==========

        all_excluded_user_ids = set(recent_users.keys()) | set(upcoming_users.keys())

        # Статистика
        stats = {
            'total_excluded': len(all_excluded_user_ids),
            'recent_reminders': len(recent_users),
            'upcoming_reminders': len(upcoming_users),
            'breakdown': {
                'recent': {},
                'upcoming': {}
            }
        }

        # Подсчет по типам недавних напоминаний
        for user_id, types in recent_users.items():
            for reminder_type in types:
                if reminder_type not in stats['breakdown']['recent']:
                    stats['breakdown']['recent'][reminder_type] = 0
                stats['breakdown']['recent'][reminder_type] += 1

        # Подсчет по типам предстоящих напоминаний
        for user_id, types in upcoming_users.items():
            for reminder_type in types:
                if reminder_type not in stats['breakdown']['upcoming']:
                    stats['breakdown']['upcoming'][reminder_type] = 0
                stats['breakdown']['upcoming'][reminder_type] += 1

        logging.info(f"Excluded {len(all_excluded_user_ids)} users from broadcast: "
                    f"{len(recent_users)} recent, {len(upcoming_users)} upcoming")

        return {
            'user_ids': all_excluded_user_ids,
            'stats': stats
        }

    except Exception as e:
        logging.error(f"Error getting users to exclude from broadcast: {e}", exc_info=True)
        return {
            'user_ids': set(),
            'stats': {
                'total_excluded': 0,
                'recent_reminders': 0,
                'upcoming_reminders': 0,
                'breakdown': {'recent': {}, 'upcoming': {}}
            }
        }


if __name__ == '__main__':
    import asyncio
    print(asyncio.run(get_payments(telegram_id=6194069336, only_successful=True)))