"""
Инструменты для администрирования Stripe подписок
Этот модуль предоставляет утилиты для:
1. Проверки состояния подписок
2. Синхронизации данных между базой данных и Stripe
3. Исправления несоответствий
"""
import asyncio
import logging
import stripe
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta

from services.init_bot import config
from models.orm import get_user, db_add_subscription, async_session
from sqlalchemy import select
from models.model import User
from utils.i18n import create_translator_hub


stripe.api_key = config.stripe.secret_key
logger = logging.getLogger(__name__)


async def update_stripe_customer_metadata(customer_id: str, telegram_id: str) -> bool:
    """
    Обновляет метаданные клиента Stripe, добавляя telegram_id

    Args:
        customer_id: ID клиента в Stripe
        telegram_id: ID пользователя в Telegram

    Returns:
        True, если обновление успешно, False в противном случае
    """
    try:
        logger.info(f"Updating metadata for customer {customer_id} with telegram_id {telegram_id}")

        # Получаем текущие данные клиента
        customer = await stripe.Customer.retrieve_async(customer_id)

        # Проверяем, есть ли уже telegram_id в метаданных
        current_metadata = getattr(customer, 'metadata', {}) or {}
        if current_metadata.get('telegram_id') == telegram_id:
            logger.info(f"Customer {customer_id} already has telegram_id {telegram_id} in metadata")
            return True

        # Обновляем метаданные клиента, сохраняя существующие
        updated_metadata = dict(current_metadata)
        updated_metadata['telegram_id'] = telegram_id

        # Обновляем клиента
        await stripe.Customer.modify_async(
            customer_id,
            metadata=updated_metadata
        )

        logger.info(f"Successfully updated metadata for customer {customer_id}")
        return True
    except Exception as e:
        logger.error(f"Error updating customer metadata: {e}")
        return False

def _get_subscription_period(subscription_type: str) -> int:
    """
    Возвращает период подписки в днях на основе типа подписки
    """
    return {
        'weekly': 7,
        'monthly': 30,
        'semiannual': 180,
        'annual': 365
    }.get(subscription_type, 30)


def get_subscription_type_by_price_id(price_id: str) -> str:
    """
    Определяет тип подписки на основе price_id из Stripe

    Args:
        price_id: ID цены в Stripe

    Returns:
        Строка с типом подписки ('weekly', 'monthly', 'annual')
    """
    logger.info(f"Determining subscription type for price_id: {price_id}")
    logger.info(
        f"Available price IDs: weekly={config.stripe.weekly_price_id}, monthly={config.stripe.monthly_price_id}, semiannual={getattr(config.stripe, 'semiannual_price_id', None)}, annual={config.stripe.annual_price_id}")

    if price_id in config.stripe.weekly_price_id:
        logger.info(f"Matched to weekly subscription")
        return 'weekly'
    elif price_id in config.stripe.monthly_price_id:
        logger.info(f"Matched to monthly subscription")
        return 'monthly'
    elif price_id in config.stripe.semiannual_price_id:
        logger.info(f"Matched to semiannual subscription")
        return 'semiannual'
    elif price_id == config.stripe.annual_price_id:
        logger.info(f"Matched to annual subscription")
        return 'annual'
    else:
        # Если не удалось определить тип по точному соответствию ID, попробуем получить информацию о цене
        try:
            price = stripe.Price.retrieve(price_id)
            logger.info(f"Retrieved price details: nickname={getattr(price, 'nickname', 'N/A')}, "
                        f"interval={price.recurring.interval if price.recurring else 'N/A'}, "
                        f"interval_count={price.recurring.interval_count if price.recurring else 'N/A'}")

            if price.recurring:
                interval = price.recurring.interval
                interval_count = getattr(price.recurring, 'interval_count', 1)
                logger.info(f"Stripe recurring details: interval={interval}, interval_count={interval_count}")
                if interval == 'week':
                    logger.info(f"Determined as weekly subscription based on interval")
                    return 'weekly'
                elif interval == 'month':
                    if interval_count == 6:
                        logger.info(f"Determined as semiannual subscription based on month x6")
                        return 'semiannual'
                    logger.info(f"Determined as monthly subscription based on interval")
                    return 'monthly'
                elif interval == 'year':
                    logger.info(f"Determined as annual subscription based on interval")
                    return 'annual'
        except Exception as e:
            logger.error(f"Error retrieving price details: {e}")

        logger.warning(f"Unknown price_id: {price_id}, defaulting to weekly")
        return 'weekly'

async def validate_and_sync_subscription(telegram_id: str) -> bool:
    """
    Проверяет и синхронизирует подписку пользователя с Stripe

    Используется для проверки подписки, если возникли проблемы

    Args:
        telegram_id: ID пользователя в Telegram

    Returns:
        True, если подписка активна и синхронизирована, False в противном случае
    """
    try:
        user = await get_user(telegram_id)
        if not user or not user.get('subscription_id'):
            logger.info(f"User {telegram_id} has no subscription to validate")
            return False

        subscription_id = user['subscription_id']

        # Проверяем, что подписка существует в Stripe
        try:
            subscription = await stripe.Subscription.retrieve_async(subscription_id)

            # Обновляем метаданные клиента, если есть customer_id
            customer_id = subscription.customer
            if customer_id:
                await update_stripe_customer_metadata(customer_id, telegram_id)
        except stripe.error.InvalidRequestError:
            logger.error(f"Subscription {subscription_id} does not exist in Stripe")
            # Отменяем подписку в нашей системе
            hub = create_translator_hub()
            i18n = hub.get_translator_by_locale(locale=user['user_language'])
            from models.orm import cancel_subscription
            await cancel_subscription(telegram_id, False, i18n, force_cancel=True)
            return False

        # Проверяем статус подписки
        if subscription.status not in ['active', 'trialing']:
            logger.warning(f"Subscription {subscription_id} has status {subscription.status}")
            return False

        # Обновляем подписку в нашей системе
        price_id = None
        if subscription.items.data:
            price_id = subscription.items.data[0].price.id

        subscription_type = get_subscription_type_by_price_id(price_id) if price_id else user['subscription_type']

        # Calculate start and end dates for the validated subscription
        start_date_dt = datetime.utcnow()
        subscription_period_days = _get_subscription_period(subscription_type)
        end_date_dt = start_date_dt + timedelta(days=subscription_period_days)

        # Обновляем подписку в базе данных
        await db_add_subscription(
            telegram_id=int(telegram_id),
            subscription_id=subscription_id,
            subscription_type_str=subscription_type,
            start_date_dt=start_date_dt,
            end_date_dt=end_date_dt,
            is_autopay_active=True
        )

        logger.info(f"Successfully validated and synced subscription for user {telegram_id}")
        return True
    except Exception as e:
        logger.error(f"Error validating subscription for user {telegram_id}: {e}")
        return False

async def check_all_stripe_subscriptions() -> Dict[str, Any]:
    """
    Проверяет все подписки Stripe в базе данных
    
    Возвращает отчет о состоянии подписок:
    - count_total: Общее количество подписок
    - count_active: Количество активных подписок
    - count_inactive: Количество неактивных подписок
    - count_mismatch: Количество несоответствий (активна в БД, но не в Stripe)
    - count_sync_needed: Количество подписок, требующих синхронизации
    - count_missing_in_db: Количество подписок, которые есть в Stripe, но отсутствуют в БД
    - issues: Список проблемных подписок
    """
    async with async_session() as session:
        # Находим всех пользователей с подписками Stripe
        result = await session.execute(
            select(User).filter(User.subscription_id.isnot(None))
        )
        users = result.scalars().all()
        
        # Создаем словарь для быстрого поиска подписок в БД
        db_subscriptions = {}
        for user in users:
            if user.subscription_id and user.subscription_id.startswith('sub_'):
                # Используем нормализованный ID (без пробелов и в нижнем регистре)
                normalized_id = user.subscription_id.strip().lower()
                db_subscriptions[normalized_id] = user.telegram_id
                # Также добавляем оригинальный ID для надежности
                db_subscriptions[user.subscription_id] = user.telegram_id
        
        logger.info(f"Found {len(db_subscriptions)} Stripe subscriptions in the database")
        
        total_count = len([id for id in db_subscriptions.keys() if id.startswith('sub_')])
        active_count = 0
        inactive_count = 0
        mismatch_count = 0
        sync_needed_count = 0
        missing_in_db_count = 0
        issues = []
        
        # Часть 1: Проверяем подписки из БД в Stripe
        for user in users:
            # Проверяем только подписки, начинающиеся с 'sub_' (Stripe)
            if not user.subscription_id or not user.subscription_id.startswith('sub_'):
                continue
                
            # Проверяем статус подписки в Stripe
            try:
                subscription = await stripe.Subscription.retrieve_async(user.subscription_id)
                
                # Проверяем соответствие статусов
                db_active = user.subscription == 'True'
                stripe_active = subscription.status in ['active', 'trialing']
                
                if db_active and stripe_active:
                    active_count += 1
                    
                    # Проверяем, нужно ли обновить информацию о подписке
                    # Корректно получаем элементы подписки
                    try:
                        # Пробуем получить items как атрибут (для новых версий Stripe API)
                        if hasattr(subscription, 'items') and hasattr(subscription.items, 'data'):
                            items = subscription.items.data
                        # Для случая, когда items - это метод (для старых версий)
                        elif callable(getattr(subscription, 'items', None)):
                            items_response = subscription.items()
                            items = getattr(items_response, 'data', [])
                        else:
                            items = []
                            
                        if items and len(items) > 0:
                            price_id = items[0].price.id
                            stripe_type = get_subscription_type_by_price_id(price_id)
                            
                            if user.subscription_type != stripe_type:
                                sync_needed_count += 1
                                issues.append({
                                    'telegram_id': user.telegram_id,
                                    'issue': 'subscription_type_mismatch',
                                    'db_type': user.subscription_type,
                                    'stripe_type': stripe_type
                                })
                    except Exception as e:
                        # Если произошла ошибка при получении item, логируем её
                        logger.error(f"Error getting subscription items for {user.subscription_id}: {e}")
                        # Не считаем это ошибкой всей подписки
                        
                elif not db_active and not stripe_active:
                    inactive_count += 1
                else:
                    mismatch_count += 1
                    issues.append({
                        'telegram_id': user.telegram_id,
                        'issue': 'status_mismatch',
                        'db_active': db_active,
                        'stripe_active': stripe_active,
                        'stripe_status': subscription.status
                    })
                    
            except stripe.error.InvalidRequestError:
                # Подписка не найдена в Stripe
                mismatch_count += 1
                issues.append({
                    'telegram_id': user.telegram_id,
                    'issue': 'subscription_not_found_in_stripe',
                    'subscription_id': user.subscription_id
                })
            except Exception as e:
                # Другие ошибки
                mismatch_count += 1
                issues.append({
                    'telegram_id': user.telegram_id,
                    'issue': 'error',
                    'error': str(e)
                })
        
        # Часть 2: Проверяем подписки из Stripe в БД
        logger.info("Searching for subscriptions in Stripe that are missing in DB...")
        try:
            # Получаем все активные подписки из Stripe
            all_stripe_subscriptions = []
            has_more = True
            starting_after = None
            
            # Постраничное получение всех подписок
            while has_more:
                params = {
                    'limit': 100,  # Максимальное количество подписок на запрос
                    'status': 'all'  # Получаем все подписки (active, past_due, unpaid, canceled, incomplete, incomplete_expired, trialing)
                }
                
                if starting_after:
                    params['starting_after'] = starting_after
                    
                page = await stripe.Subscription.list_async(**params)
                all_stripe_subscriptions.extend(page.data)
                
                has_more = page.has_more
                if has_more and page.data:
                    starting_after = page.data[-1].id
                else:
                    has_more = False
            
            logger.info(f"Found {len(all_stripe_subscriptions)} subscriptions in Stripe")
            
            # Проверяем каждую подписку из Stripe
            for subscription in all_stripe_subscriptions:
                subscription_id = subscription.id
                # Нормализуем ID для сравнения
                normalized_id = subscription_id.strip().lower()
                
                # Проверяем наличие подписки в БД (как по нормализованному, так и по оригинальному ID)
                if normalized_id not in db_subscriptions and subscription_id not in db_subscriptions:
                    # Только если подписка активна в Stripe, считаем её отсутствующей в БД
                    if subscription.status in ['active', 'trialing']:
                        missing_in_db_count += 1
                        
                        # Получаем данные клиента, если они доступны
                        customer_info = None
                        try:
                            if subscription.customer:
                                customer = await stripe.Customer.retrieve_async(subscription.customer)
                                customer_info = {
                                    'id': customer.id,
                                    'email': customer.email,
                                    'name': customer.name,
                                    'metadata': customer.metadata
                                }
                        except Exception as e:
                            logger.error(f"Error retrieving customer info: {e}")
                        
                        # Пытаемся найти telegram_id в метаданных клиента
                        telegram_id = None
                        if customer_info and customer_info.get('metadata'):
                            telegram_id = customer_info['metadata'].get('telegram_id')
                        
                        # Дополнительно проверяем, есть ли такой пользователь в БД по другой подписке
                        # Это может помочь идентифицировать пользователя, даже если метаданные не содержат telegram_id
                        user_with_subscription = None
                        if telegram_id:
                            try:
                                user_result = await session.execute(
                                    select(User).filter(User.telegram_id == telegram_id)
                                )
                                user_with_subscription = user_result.scalar_one_or_none()
                                if user_with_subscription:
                                    logger.info(f"Found user {telegram_id} in DB for subscription {subscription_id}")
                            except Exception as e:
                                logger.error(f"Error checking user by telegram_id: {e}")
                        
                        issues.append({
                            'issue': 'subscription_missing_in_db',
                            'subscription_id': subscription_id,
                            'stripe_status': subscription.status,
                            'telegram_id': telegram_id,
                            'customer': customer_info,
                            'user_exists_in_db': bool(user_with_subscription)
                        })
        
        except Exception as e:
            logger.error(f"Error checking Stripe subscriptions: {e}", exc_info=True)
            issues.append({
                'issue': 'error_checking_stripe_subscriptions',
                'error': str(e)
            })
        
        return {
            'count_total': total_count,
            'count_active': active_count,
            'count_inactive': inactive_count,
            'count_mismatch': mismatch_count,
            'count_sync_needed': sync_needed_count,
            'count_missing_in_db': missing_in_db_count,
            'issues': issues
        }

async def sync_stripe_subscriptions(fix_issues: bool = False) -> Dict[str, Any]:
    """
    Синхронизирует все подписки Stripe между базой данных и Stripe
    
    Args:
        fix_issues: Если True, попытается исправить несоответствия
        
    Returns:
        Отчет о результатах синхронизации
    """
    report = await check_all_stripe_subscriptions()
    
    if not fix_issues:
        return {**report, 'message': 'Issues found but not fixed (dry run)'}
    
    # Исправляем несоответствия
    fixed_count = 0
    failed_count = 0
    results = []
    
    # Собираем информацию о существующих подписках Stripe в БД
    async with async_session() as session:
        # Поиск всех subscription_id в БД
        result = await session.execute(
            select(User.subscription_id, User.telegram_id).filter(
                User.subscription_id.isnot(None)
            )
        )
        db_subscriptions = {row[0]: row[1] for row in result if row[0] and row[0].startswith('sub_')}
        
        # Создаем обратное отображение telegram_id -> subscription_id
        users_with_subscriptions = {}
        for sub_id, tg_id in db_subscriptions.items():
            if tg_id not in users_with_subscriptions:
                users_with_subscriptions[tg_id] = []
            users_with_subscriptions[tg_id].append(sub_id)
        
        logger.info(f"Found {len(db_subscriptions)} subscriptions in DB")
    
    for issue in report['issues']:
        try:
            if issue['issue'] == 'status_mismatch':
                # Если подписка активна в Stripe, но не в базе данных
                if not issue['db_active'] and issue['stripe_active']:
                    # Получаем данные пользователя
                    telegram_id = issue['telegram_id']
                    user = await get_user(telegram_id)
                    if not user:
                        raise ValueError(f"User not found: {telegram_id}")
                    
                    # Синхронизируем подписку
                    success = await validate_and_sync_subscription(telegram_id)
                    
                    if success:
                        fixed_count += 1
                        results.append({
                            'telegram_id': telegram_id,
                            'issue': issue['issue'],
                            'fixed': True,
                            'action': 'subscription_activated'
                        })
                    else:
                        failed_count += 1
                        results.append({
                            'telegram_id': telegram_id,
                            'issue': issue['issue'],
                            'fixed': False,
                            'error': 'Failed to sync subscription'
                        })
                
                # Если подписка активна в базе данных, но не в Stripe
                elif issue['db_active'] and not issue['stripe_active']:
                    # Отменяем подписку в базе данных
                    telegram_id = issue['telegram_id']
                    user = await get_user(telegram_id)
                    if not user:
                        raise ValueError(f"User not found: {telegram_id}")
                    
                    hub = create_translator_hub()
                    i18n = hub.get_translator_by_locale(locale=user['user_language'])
                    
                    from models.orm import cancel_subscription
                    await cancel_subscription(telegram_id, False, i18n, force_cancel=True)
                    
                    fixed_count += 1
                    results.append({
                        'telegram_id': telegram_id,
                        'issue': issue['issue'],
                        'fixed': True,
                        'action': 'subscription_deactivated'
                    })
            
            elif issue['issue'] == 'subscription_not_found_in_stripe':
                # Отменяем подписку в базе данных
                telegram_id = issue['telegram_id']
                user = await get_user(telegram_id)
                if not user:
                    raise ValueError(f"User not found: {telegram_id}")
                
                hub = create_translator_hub()
                i18n = hub.get_translator_by_locale(locale=user['user_language'])
                
                from models.orm import cancel_subscription
                await cancel_subscription(telegram_id, False, i18n, force_cancel=True)
                
                fixed_count += 1
                results.append({
                    'telegram_id': telegram_id,
                    'issue': issue['issue'],
                    'fixed': True,
                    'action': 'subscription_removed'
                })
            
            elif issue['issue'] == 'subscription_type_mismatch':
                # Обновляем тип подписки в базе данных
                telegram_id = issue['telegram_id']
                user = await get_user(telegram_id)
                if not user:
                    raise ValueError(f"User not found: {telegram_id}")
                
                subscription_type = issue['stripe_type']
                subscription_period = _get_subscription_period(subscription_type)
                
                await db_add_subscription(
                    telegram_id=telegram_id,
                    subscription_id=user['subscription_id'],
                    subscription_type=subscription_type,
                    subscription_period=subscription_period
                )
                
                fixed_count += 1
                results.append({
                    'telegram_id': telegram_id,
                    'issue': issue['issue'],
                    'fixed': True,
                    'action': 'subscription_type_updated',
                    'new_type': subscription_type
                })
            
            elif issue['issue'] == 'subscription_missing_in_db':
                subscription_id = issue['subscription_id']
                
                # Проверяем, действительно ли подписка отсутствует в БД (двойная проверка)
                # Используем как оригинальный, так и нормализованный ID
                normalized_id = subscription_id.strip().lower()
                
                if subscription_id in db_subscriptions or normalized_id in db_subscriptions:
                    logger.info(f"Subscription {subscription_id} already exists in DB, skipping")
                    # Подписка уже есть в БД, пропускаем
                    results.append({
                        'issue': issue['issue'],
                        'fixed': True,
                        'action': 'already_exists_in_db',
                        'subscription_id': subscription_id
                    })
                    fixed_count += 1
                    continue
                
                # Проверяем, есть ли Telegram ID в метаданных клиента
                telegram_id = issue.get('telegram_id')
                if not telegram_id:
                    logger.warning(f"Cannot add subscription to DB: No telegram_id in metadata for subscription {subscription_id}")
                    failed_count += 1
                    results.append({
                        'issue': issue['issue'],
                        'fixed': False,
                        'error': 'No telegram_id in customer metadata',
                        'subscription_id': subscription_id
                    })
                    continue
                
                # Проверяем, существует ли пользователь в БД
                user = await get_user(telegram_id)
                if not user:
                    logger.warning(f"User not found in DB: {telegram_id}")
                    failed_count += 1
                    results.append({
                        'telegram_id': telegram_id,
                        'issue': issue['issue'],
                        'fixed': False,
                        'error': 'User not found in database',
                        'subscription_id': subscription_id
                    })
                    continue
                
                # Получаем информацию о подписке из Stripe для определения типа
                try:
                    subscription = await stripe.Subscription.retrieve_async(subscription_id)
                    
                    # Добавляем информацию о плане подписки
                    # Безопасное получение items
                    try:
                        # subscription.items is actually a method in Stripe API, not an attribute
                        if hasattr(subscription, 'items') and callable(subscription.items):
                            items_response = subscription.items()
                            items_data = getattr(items_response, 'data', [])
                            logger.info(f"Got items via subscription.items() method: {len(items_data)} items")
                            
                            # If we got items, we're done
                            if items_data:
                                pass  # items_data is already set
                            # If no items from the method, try alternative approaches for active subscriptions
                            elif subscription.status in ['active', 'trialing']:
                                # For active subscriptions, try to get items via direct API call
                                try:
                                    items_list = await stripe.SubscriptionItem.list_async(subscription=subscription.id)
                                    if items_list and hasattr(items_list, 'data'):
                                        items_data = items_list.data
                                        logger.info(f"Got items via SubscriptionItem.list: {len(items_data)} items")
                                except Exception as e:
                                    logger.warning(f"Failed to get subscription items via SubscriptionItem.list: {e}")
                                
                        # Fallback: try if items is somehow an attribute with data
                        elif hasattr(subscription, 'items') and hasattr(subscription.items, 'data'):
                            items_data = subscription.items.data
                            logger.info(f"Got items via subscription.items.data: {len(items_data)} items")
                        # Fallback: try if items is a dict
                        elif hasattr(subscription, 'items') and isinstance(subscription.items, dict):
                            items_data = subscription.items.get('data', [])
                            logger.info(f"Got items via subscription.items dict: {len(items_data)} items")
                        else:
                            items_data = []
                            logger.warning(f"No items found using standard methods")
                    except Exception as e:
                        logger.warning(f"Error getting subscription items: {e}")
                        items_data = []
                    
                    # If no items found in subscription (happens with canceled subscriptions),
                    # try to get price information from the latest invoice
                    if not items_data and subscription.latest_invoice:
                        try:
                            logger.info("Trying to get subscription items from latest invoice")
                            
                            # The invoice should have line_items with the subscription details
                            if hasattr(subscription.latest_invoice, 'lines'):
                                invoice_lines = subscription.latest_invoice.lines
                                
                                if hasattr(invoice_lines, 'data'):
                                    line_items = invoice_lines.data
                                    
                                    for line_item in line_items:
                                        if hasattr(line_item, 'price') and line_item.price:
                                            # Try to get the real subscription item ID from the line item
                                            real_subscription_item_id = None
                                            if (hasattr(line_item, 'subscription_item') and 
                                                line_item.subscription_item):
                                                real_subscription_item_id = line_item.subscription_item
                                            
                                            # If we don't have a real subscription item ID, create a mock one
                                            # but mark it clearly so the upgrade service knows it's not usable
                                            item_id = real_subscription_item_id if real_subscription_item_id else f"mock_{line_item.id}"
                                            
                                            # Create a mock item structure similar to subscription items
                                            mock_item = type('MockItem', (), {
                                                'id': item_id,
                                                'price': line_item.price,
                                                'is_mock': real_subscription_item_id is None  # Flag to indicate if this is a mock item
                                            })()
                                            items_data.append(mock_item)
                                elif callable(invoice_lines):
                                    line_items = invoice_lines()
                                    if hasattr(line_items, 'data'):
                                        for line_item in line_items.data:
                                            if hasattr(line_item, 'price') and line_item.price:
                                                # Try to get the real subscription item ID
                                                real_subscription_item_id = None
                                                if (hasattr(line_item, 'subscription_item') and 
                                                    line_item.subscription_item):
                                                    real_subscription_item_id = line_item.subscription_item
                                                
                                                item_id = real_subscription_item_id if real_subscription_item_id else f"mock_{line_item.id}"
                                                
                                                mock_item = type('MockItem', (), {
                                                    'id': item_id,
                                                    'price': line_item.price,
                                                    'is_mock': real_subscription_item_id is None
                                                })()
                                                items_data.append(mock_item)
                        except Exception as e:
                            logger.error(f"Error getting items from invoice: {e}")
                    
                    for item in items_data:
                        logger.info(f"Processing item: {item}")
                        result['items'].append({
                            'id': item.id,
                            'price': {
                                'id': item.price.id,
                                'nickname': item.price.nickname,
                                'unit_amount': item.price.unit_amount / 100,  # Конвертируем из центов
                                'currency': item.price.currency,
                                'recurring': {
                                    'interval': item.price.recurring.interval,
                                    'interval_count': item.price.recurring.interval_count
                                }
                            }
                        })
                    
                    # Добавляем информацию о последнем счете
                    if subscription.latest_invoice:
                        result['latest_invoice'] = {
                            'id': subscription.latest_invoice.id,
                            'status': subscription.latest_invoice.status,
                            'amount_due': subscription.latest_invoice.amount_due / 100,  # Конвертируем из центов
                            'amount_paid': subscription.latest_invoice.amount_paid / 100,
                            'created': datetime.fromtimestamp(subscription.latest_invoice.created),
                            'due_date': datetime.fromtimestamp(subscription.latest_invoice.due_date) if subscription.latest_invoice.due_date else None,
                            'payment_intent': None
                        }
                        
                        # Добавляем информацию о платеже
                        if subscription.latest_invoice.payment_intent:
                            result['latest_invoice']['payment_intent'] = {
                                'id': subscription.latest_invoice.payment_intent.id,
                                'status': subscription.latest_invoice.payment_intent.status,
                                'amount': subscription.latest_invoice.payment_intent.amount / 100,
                                'created': datetime.fromtimestamp(subscription.latest_invoice.payment_intent.created),
                                'last_payment_error': subscription.latest_invoice.payment_intent.last_payment_error
                            }
                    
                    # Добавляем подписку в БД
                    await db_add_subscription(
                        telegram_id=telegram_id,
                        subscription_id=subscription_id,
                        subscription_type=subscription_type,
                        subscription_period=subscription_period
                    )
                    
                    # Отправляем уведомление пользователю
                    hub = create_translator_hub()
                    i18n = hub.get_translator_by_locale(locale=user['user_language'])
                    try:
                        await bot.send_message(chat_id=telegram_id, text=i18n.subscription_renewal_success())
                    except Exception as e:
                        logger.error(f"Failed to send subscription notification: {e}")
                    
                    fixed_count += 1
                    results.append({
                        'telegram_id': telegram_id,
                        'issue': issue['issue'],
                        'fixed': True,
                        'action': 'subscription_added_to_db',
                        'subscription_type': subscription_type
                    })
                except Exception as e:
                    logger.error(f"Error adding subscription to DB: {e}")
                    failed_count += 1
                    results.append({
                        'telegram_id': telegram_id,
                        'issue': issue['issue'],
                        'fixed': False,
                        'error': str(e),
                        'subscription_id': subscription_id
                    })
            
            else:
                # Неизвестная проблема
                failed_count += 1
                results.append({
                    'issue': issue['issue'],
                    'fixed': False,
                    'error': 'Unknown issue type'
                })
                
        except Exception as e:
            failed_count += 1
            results.append({
                'issue': issue['issue'],
                'fixed': False,
                'error': str(e)
            })
    
    return {
        **report,
        'fixed_count': fixed_count,
        'failed_count': failed_count,
        'results': results,
        'message': f'Fixed {fixed_count} issues, failed to fix {failed_count} issues'
    }

async def get_stripe_subscription_details(subscription_id: str) -> Optional[Dict[str, Any]]:
    """
    Получает подробную информацию о подписке из Stripe
    
    Args:
        subscription_id: ID подписки в Stripe
        
    Returns:
        Словарь с информацией о подписке или None, если подписка не найдена
    """
    try:
        subscription = await stripe.Subscription.retrieve_async(
            subscription_id, 
            expand=['customer', 'latest_invoice', 'latest_invoice.payment_intent']
        )
        
        # Преобразуем данные в более удобный формат
        result = {
            'id': subscription.id,
            'status': subscription.status,
            'current_period_start': datetime.fromtimestamp(subscription.current_period_start),
            'current_period_end': datetime.fromtimestamp(subscription.current_period_end),
            'created': datetime.fromtimestamp(subscription.created),
            'canceled_at': datetime.fromtimestamp(subscription.canceled_at) if subscription.canceled_at else None,
            'cancel_at_period_end': subscription.cancel_at_period_end,
            'customer': {
                'id': subscription.customer.id,
                'email': subscription.customer.email,
                'name': subscription.customer.name
            } if subscription.customer else None,
            'items': []
        }
        
        # Добавляем информацию о плане подписки
        # Безопасное получение items
        try:
            # subscription.items is actually a method in Stripe API, not an attribute
            if hasattr(subscription, 'items') and callable(subscription.items):
                items_response = subscription.items()
                items_data = getattr(items_response, 'data', [])
                logger.info(f"Got items via subscription.items() method: {len(items_data)} items")
                
                # If we got items, we're done
                if items_data:
                    pass  # items_data is already set
                # If no items from the method, try alternative approaches for active subscriptions
                elif subscription.status in ['active', 'trialing']:
                    # For active subscriptions, try to get items via direct API call
                    try:
                        items_list = await stripe.SubscriptionItem.list_async(subscription=subscription.id)
                        if items_list and hasattr(items_list, 'data'):
                            items_data = items_list.data
                            logger.info(f"Got items via SubscriptionItem.list: {len(items_data)} items")
                    except Exception as e:
                        logger.warning(f"Failed to get subscription items via SubscriptionItem.list: {e}")
                        
            # Fallback: try if items is somehow an attribute with data
            elif hasattr(subscription, 'items') and hasattr(subscription.items, 'data'):
                items_data = subscription.items.data
                logger.info(f"Got items via subscription.items.data: {len(items_data)} items")
            # Fallback: try if items is a dict
            elif hasattr(subscription, 'items') and isinstance(subscription.items, dict):
                items_data = subscription.items.get('data', [])
                logger.info(f"Got items via subscription.items dict: {len(items_data)} items")
            else:
                items_data = []
                logger.warning(f"No items found using standard methods")
        except Exception as e:
            logger.warning(f"Error getting subscription items: {e}")
            items_data = []
        
        # DEBUG: Log what we found
        logger.info(f"Final items_data: {items_data}")
        
        # If no items found in subscription (happens with canceled subscriptions),
        # try to get price information from the latest invoice
        if not items_data and subscription.latest_invoice:
            try:
                logger.info("Trying to get subscription items from latest invoice")
                
                # The invoice should have line_items with the subscription details
                if hasattr(subscription.latest_invoice, 'lines'):
                    invoice_lines = subscription.latest_invoice.lines
                    
                    if hasattr(invoice_lines, 'data'):
                        line_items = invoice_lines.data
                        
                        for line_item in line_items:
                            if hasattr(line_item, 'price') and line_item.price:
                                # Try to get the real subscription item ID from the line item
                                real_subscription_item_id = None
                                if (hasattr(line_item, 'subscription_item') and 
                                    line_item.subscription_item):
                                    real_subscription_item_id = line_item.subscription_item
                                
                                # If we don't have a real subscription item ID, create a mock one
                                # but mark it clearly so the upgrade service knows it's not usable
                                item_id = real_subscription_item_id if real_subscription_item_id else f"mock_{line_item.id}"
                                
                                # Create a mock item structure similar to subscription items
                                mock_item = type('MockItem', (), {
                                    'id': item_id,
                                    'price': line_item.price,
                                    'is_mock': real_subscription_item_id is None  # Flag to indicate if this is a mock item
                                })()
                                items_data.append(mock_item)
                    elif callable(invoice_lines):
                        line_items = invoice_lines()
                        if hasattr(line_items, 'data'):
                            for line_item in line_items.data:
                                if hasattr(line_item, 'price') and line_item.price:
                                    # Try to get the real subscription item ID
                                    real_subscription_item_id = None
                                    if (hasattr(line_item, 'subscription_item') and 
                                        line_item.subscription_item):
                                        real_subscription_item_id = line_item.subscription_item
                                    
                                    item_id = real_subscription_item_id if real_subscription_item_id else f"mock_{line_item.id}"
                                    
                                    mock_item = type('MockItem', (), {
                                        'id': item_id,
                                        'price': line_item.price,
                                        'is_mock': real_subscription_item_id is None
                                    })()
                                    items_data.append(mock_item)
            except Exception as e:
                logger.error(f"Error getting items from invoice: {e}")
        
        for item in items_data:
            logger.info(f"Processing item: {item}")
            result['items'].append({
                'id': item.id,
                'price': {
                    'id': item.price.id,
                    'nickname': item.price.nickname,
                    'unit_amount': item.price.unit_amount / 100,  # Конвертируем из центов
                    'currency': item.price.currency,
                    'recurring': {
                        'interval': item.price.recurring.interval,
                        'interval_count': item.price.recurring.interval_count
                    }
                }
            })
        
        logger.info(f"Final result items count: {len(result['items'])}")
        # Добавляем информацию о последнем счете
        if subscription.latest_invoice:
            result['latest_invoice'] = {
                'id': subscription.latest_invoice.id,
                'status': subscription.latest_invoice.status,
                'amount_due': subscription.latest_invoice.amount_due / 100,  # Конвертируем из центов
                'amount_paid': subscription.latest_invoice.amount_paid / 100,
                'created': datetime.fromtimestamp(subscription.latest_invoice.created),
                'due_date': datetime.fromtimestamp(subscription.latest_invoice.due_date) if subscription.latest_invoice.due_date else None,
                'payment_intent': None
            }
            
            # Добавляем информацию о платеже
            if subscription.latest_invoice.payment_intent:
                result['latest_invoice']['payment_intent'] = {
                    'id': subscription.latest_invoice.payment_intent.id,
                    'status': subscription.latest_invoice.payment_intent.status,
                    'amount': subscription.latest_invoice.payment_intent.amount / 100,
                    'created': datetime.fromtimestamp(subscription.latest_invoice.payment_intent.created),
                    'last_payment_error': subscription.latest_invoice.payment_intent.last_payment_error
                }
        
        return result
    except stripe.error.InvalidRequestError as e:
        logger.error(f"Subscription not found: {subscription_id}")
        return None
    except Exception as e:
        logger.error(f"Error retrieving subscription details: {e}")
        return None

async def manually_sync_subscription(telegram_id: str) -> Dict[str, Any]:
    """
    Вручную синхронизирует подписку пользователя с Stripe
    
    Args:
        telegram_id: ID пользователя в Telegram
        
    Returns:
        Отчет о результатах синхронизации
    """
    try:
        # Получаем данные пользователя
        user = await get_user(telegram_id)
        if not user:
            return {'success': False, 'error': f"User not found: {telegram_id}"}
        
        # Проверяем, есть ли у пользователя подписка
        if not user.get('subscription_id'):
            return {'success': False, 'error': f"User has no subscription"}
        
        subscription_id = user['subscription_id']
        
        # Если подписка не в Stripe, нет смысла синхронизировать
        if not subscription_id.startswith('sub_'):
            return {
                'success': False, 
                'error': f"Not a Stripe subscription: {subscription_id}"
            }
        
        # Пытаемся синхронизировать
        success = await validate_and_sync_subscription(telegram_id)
        
        if success:
            # Получаем обновленные данные пользователя
            updated_user = await get_user(telegram_id)
            
            return {
                'success': True,
                'message': 'Subscription successfully synchronized',
                'user': {
                    'telegram_id': updated_user['telegram_id'],
                    'subscription': updated_user['subscription'],
                    'subscription_id': updated_user['subscription_id'],
                    'subscription_type': updated_user['subscription_type'],
                    'start_date': updated_user['start_date'],
                    'end_date': updated_user['end_date']
                }
            }
        else:
            return {
                'success': False,
                'error': 'Failed to synchronize subscription'
            }
    except Exception as e:
        logger.error(f"Error manually syncing subscription: {e}")
        return {
            'success': False,
            'error': str(e)
        }


async def get_stripe_customer_metadata(customer_id: str) -> Dict[str, Any]:
    """
    Получает метаданные клиента Stripe по его customer_id

    Args:
        customer_id: ID клиента в Stripe (начинается с 'cus_')

    Returns:
        Словарь с информацией о клиенте, включая метаданные, или словарь с ошибкой
    """
    try:
        logger.info(f"Retrieving customer data for customer_id: {customer_id}")

        # Получаем данные клиента из Stripe
        customer = await stripe.Customer.retrieve_async(customer_id)

        # Возвращаем основную информацию и метаданные
        result = {
            'id': customer.id,
            'email': customer.email,
            'name': customer.name,
            'metadata': customer.metadata,
            'has_telegram_id': 'telegram_id' in customer.metadata,
            'telegram_id': customer.metadata.get('telegram_id') if hasattr(customer, 'metadata') else None
        }

        # Дополнительная проверка для метаданных, которая упростит анализ
        if hasattr(customer, 'metadata') and customer.metadata:
            result['all_metadata_keys'] = list(customer.metadata.keys())
        else:
            result['all_metadata_keys'] = []

        return result

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Customer not found: {customer_id} - {e}")
        return {
            'success': False,
            'error': f"Customer not found: {customer_id}",
            'details': str(e)
        }
    except Exception as e:
        logger.error(f"Error retrieving customer metadata: {e}", exc_info=True)
        return {
            'success': False,
            'error': f"Error retrieving customer data",
            'details': str(e)
        }

async def update_all_customers_metadata() -> Dict[str, Any]:
    """
    Массовое обновление метаданных всех клиентов Stripe, 
    добавляя telegram_id из связанных подписок в базе данных
    
    Returns:
        Отчет о результатах обновления
    """
    logger.info("Starting mass update of Stripe customer metadata")
    
    # Результаты операции
    results = {
        'total_users': 0,
        'updated_count': 0,
        'failed_count': 0,
        'skipped_count': 0,
        'errors': []
    }
    
    # Шаг 1: Получаем всех пользователей с подписками Stripe из базы данных
    async with async_session() as session:
        result = await session.execute(
            select(User).filter(User.subscription_id.isnot(None))
        )
        users = result.scalars().all()
        
        # Подсчитываем количество пользователей с подписками Stripe
        stripe_subscription_users = [user for user in users if user.subscription_id and user.subscription_id.startswith('sub_')]
        results['total_users'] = len(stripe_subscription_users)
        
        logger.info(f"Found {results['total_users']} users with Stripe subscriptions")
        
        # Шаг 2: Для каждой подписки, получаем информацию о подписке и клиенте
        for user in stripe_subscription_users:
            telegram_id = user.telegram_id
            subscription_id = user.subscription_id
            
            try:
                # Получаем информацию о подписке из Stripe
                subscription = await stripe.Subscription.retrieve_async(subscription_id)
                customer_id = subscription.customer
                
                if not customer_id:
                    logger.warning(f"No customer_id for subscription {subscription_id}")
                    results['skipped_count'] += 1
                    continue
                
                # Обновляем метаданные клиента
                success = await update_stripe_customer_metadata(customer_id, telegram_id)
                
                if success:
                    results['updated_count'] += 1
                    logger.info(f"Updated metadata for customer {customer_id} with telegram_id {telegram_id}")
                else:
                    results['failed_count'] += 1
                    results['errors'].append({
                        'telegram_id': telegram_id,
                        'subscription_id': subscription_id,
                        'customer_id': customer_id,
                        'error': 'Failed to update metadata'
                    })
            except stripe.error.InvalidRequestError as e:
                # Подписка не найдена в Stripe
                logger.error(f"Subscription not found in Stripe: {subscription_id} - {e}")
                results['failed_count'] += 1
                results['errors'].append({
                    'telegram_id': telegram_id,
                    'subscription_id': subscription_id,
                    'error': f"Subscription not found: {str(e)}"
                })
            except Exception as e:
                # Другие ошибки
                logger.error(f"Error updating metadata for user {telegram_id}: {e}")
                results['failed_count'] += 1
                results['errors'].append({
                    'telegram_id': telegram_id,
                    'subscription_id': subscription_id,
                    'error': str(e)
                })
    
    logger.info(f"Completed metadata update: {results['updated_count']} updated, {results['failed_count']} failed, {results['skipped_count']} skipped")
    return results


async def add_free_days_to_subscription_stripe(days: int, telegram_id: int = None, subscription_id: str = None) -> Optional[Dict] | None:
    """
    Добавляет дни к подписке Stripe
    subscription_id - опционально. Пример использования: если записи ещё нет в базе данных
    """
    user: dict = await get_user(telegram_id)
    subscription_id = subscription_id if subscription_id else user['subscription_id']
    if subscription_id.startswith('sub_'):
        subscription = await stripe.Subscription.retrieve_async(subscription_id)
        print(subscription)
        print('trial_end', subscription.trial_end, type(subscription.trial_end))
        
        current_trial_end: int | None = subscription.trial_end
        
        if current_trial_end:
            current_end_datetime = datetime.fromtimestamp(current_trial_end)
        else:
            sub_end_date = subscription.current_period_end
            sub_end_date = datetime.fromtimestamp(sub_end_date)
            current_end_datetime = sub_end_date
        
        new_trial_end = current_end_datetime + timedelta(days=days)
        
            
        updated_subscription = await stripe.Subscription.modify_async(
            subscription_id,
            proration_behavior="none",
            trial_end=new_trial_end
        )
        logger.debug(f"Updated subscription: {updated_subscription}")
        return updated_subscription.to_dict()
    else:
        logger.error(f"Subscription {subscription_id} is not a Stripe subscription")
        return None



async def upgrade_stripe_subscription(telegram_id: int, subscription_type: str = None) -> bool:
    """
    Upgrades a Stripe subscription to a new subscription type.
    Args:
        telegram_id: The user's Telegram ID.
        subscription_type: The new subscription type to upgrade to.
    Returns:
        True if the subscription was upgraded successfully, False otherwise.
    """
    try:
        user = await get_user(telegram_id)

        if not user:
            logger.error(f"User not found for telegram_id: {telegram_id}")
            return False

        current_subscription_id = user.get('subscription_id')
        if not current_subscription_id:
            logger.error(f"No subscription_id found for user {telegram_id}")
            return False

        # Skip if it's not a Stripe subscription
        if not current_subscription_id.startswith('sub_'):
            logger.info(f"Skipping Stripe upgrade for non-Stripe subscription {current_subscription_id}")
            return False

        # Default to monthly if no subscription_type provided
        target_subscription_type = subscription_type or 'monthly'

        # Update the subscription
        try:
            updated_subscription = await update_stripe_subscription(
                subscription_id=current_subscription_id,
                new_subscription_type=target_subscription_type
            )
        except Exception as e:
            logger.error(f"Error updating Stripe subscription for user {telegram_id}: {e}", exc_info=True)
            return False
        return True
    except Exception as e:
        logger.error(f"Error upgrading Stripe subscription for user {telegram_id}: {e}", exc_info=True)
        return False


async def update_stripe_subscription(
        subscription_id: str,
        new_subscription_type: str = "monthly"
) -> Optional[Dict[str, Any]]:
    """
    Updates a Stripe subscription to a new plan to take effect at the end of the
    current billing period by using Subscription.modify.

    This is a simpler and more direct alternative to using a Subscription Schedule.

    :param subscription_id: Stripe subscription ID to update.
    :param new_subscription_type: Target subscription type ('monthly', 'weekly', 'annual').
    :return: The updated subscription object as a dictionary or None if failed.
    """
    try:
        # 1. Get the target price ID
        target_price_id = None
        if new_subscription_type == 'monthly':
            target_price_id = config.stripe.monthly_price_id[-1] if config.stripe.monthly_price_id else None
        elif new_subscription_type == 'weekly':
            target_price_id = config.stripe.weekly_price_id[-1] if config.stripe.weekly_price_id else None
        elif new_subscription_type == 'semiannual':
            target_price_id = config.stripe.semiannual_price_id[-1] if config.stripe.semiannual_price_id else None
        elif new_subscription_type == 'annual':
            target_price_id = config.stripe.annual_price_id

        if not target_price_id:
            logger.error(f"Price ID not found for subscription type: {new_subscription_type}")
            return None

        # 2. Get current subscription details to find the item ID
        from services.payments.stripe_tools import get_stripe_subscription_details
        subscription_details = await get_stripe_subscription_details(subscription_id)
        if not subscription_details:
            logger.error(f"Could not retrieve details for subscription {subscription_id}")
            return None

        subscription_item_id = subscription_details['items'][0]['id']
        current_period_end = subscription_details['current_period_end']

        # 3. Modify the subscription directly
        updated_subscription = await stripe.Subscription.modify_async(
            subscription_id,
            items=[{
                "id": subscription_item_id,
                "price": target_price_id,
            }],
            # Don't create an immediate invoice for the change
            proration_behavior="none",
            # CORRECTED: Keep the renewal date the same.
            trial_end=current_period_end,
        )

        # Fix datetime handling - check if current_period_end is already a datetime object
        if isinstance(current_period_end, datetime):
            change_date = current_period_end.strftime('%Y-%m-%d')
        else:
            # If it's a timestamp integer, convert it
            change_date = datetime.fromtimestamp(current_period_end).strftime('%Y-%m-%d')

        logger.info(
            f"Successfully updated subscription {subscription_id} to {new_subscription_type}. Change will take effect on {change_date}.")

        return updated_subscription.to_dict()

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error updating subscription {subscription_id}: {e}", exc_info=True)
        return None
    except (KeyError, IndexError) as e:
        logger.error(f"Data structure error from get_stripe_subscription_details for sub {subscription_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Generic error updating subscription {subscription_id}: {e}", exc_info=True)
        return None
if __name__ == "__main__":
    asyncio.run(add_free_days_to_subscription_stripe(700, 449290141))