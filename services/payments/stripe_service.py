import asyncio
import logging
import json
from typing import Optional, Dict, Any, Tuple

import stripe
from datetime import datetime, timedelta
from models.orm import db_add_subscription, db_add_payment, get_user, get_payments, log_user_action
from services.payments.general_fucntions import complete_referral_process, confirm_referral_process, \
    referral_need_reward
from services.bot_provider import get_bot
from services.payments.stripe_tools import get_subscription_type_by_price_id, _get_subscription_period, \
    update_stripe_customer_metadata, add_free_days_to_subscription_stripe
from utils.i18n import create_translator_hub

from services.init_bot import config
stripe.api_key = config.stripe.secret_key
logger = logging.getLogger(__name__)

# Настроим логгер для более подробного логирования Stripe операций
webhook_logger = logging.getLogger('stripe_webhooks')
webhook_logger.setLevel(logging.INFO)
# Если нужен отдельный файл для логов webhook
# handler = logging.FileHandler('stripe_webhooks.log')
# webhook_logger.addHandler(handler)

async def create_stripe_subscription(
    account_id: str, # telegram_id
    subscription_type: str, # monthly, semiannual, annual, weekly
    discount_type: str | None = None,
    email: str = None
) -> dict:
    """
    Creates a Stripe subscription and returns checkout session
    """
    # Define price IDs based on subscription type
    price_id = None
    if subscription_type == 'weekly':
        price_id = config.stripe.weekly_price_id[-1] if config.stripe.weekly_price_id else None
    elif subscription_type == 'monthly':
        price_id = config.stripe.monthly_price_id[-1] if config.stripe.monthly_price_id else None
    elif subscription_type == 'semiannual':
        price_id = config.stripe.semiannual_price_id[-1] if config.stripe.semiannual_price_id else None
    elif subscription_type == 'annual':
        price_id = config.stripe.annual_price_id

    if not price_id:
        logger.error(f"Price ID not found for subscription type: {subscription_type}")
        return None

    try:
        # Добавляем идемпотентный ключ для предотвращения дублирования
        idempotency_key = f"{account_id}_{subscription_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if discount_type == 'notification_discount':
            checkout_session = stripe.checkout.Session.create(
                customer_email=email,
                client_reference_id=account_id,  # telegram_id
                payment_method_types=['card'],
                discounts=[{"coupon": config.stripe.monthly_notification_coupon_id}],
                line_items=[{
                    'price': price_id,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=config.stripe.success_url,
                cancel_url=config.stripe.cancel_url,
                metadata={
                    'telegram_id': account_id,
                    'subscription_type': subscription_type
                },
                # Используем идемпотентный ключ для предотвращения дублирования при повторных запросах
                idempotency_key=idempotency_key
            )
        else:
            checkout_session = stripe.checkout.Session.create(
                customer_email=email,
                client_reference_id=account_id,  # telegram_id
                payment_method_types=['card'],
                line_items=[{
                    'price': price_id,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=config.stripe.success_url,
                cancel_url=config.stripe.cancel_url,
                metadata={
                    'telegram_id': account_id,
                    'subscription_type': subscription_type
                },
                # Используем идемпотентный ключ для предотвращения дублирования при повторных запросах
                idempotency_key=idempotency_key
            )
        logger.info(f"Created Stripe checkout session for user {account_id}: {checkout_session.id}")
        return {
            'session_id': checkout_session.id,
            'url': checkout_session.url
        }
    except stripe.error.CardError as e:
        # Ошибка карты (отклонена, недостаточно средств и т.д.)
        logger.error(f"Card error for user {account_id}: {e.error.message}")
        return None
    except stripe.error.InvalidRequestError as e:
        # Неверные параметры запроса
        logger.error(f"Invalid request error for user {account_id}: {e}")
        return None
    except Exception as e:
        # Сохраняем первоначальное поведение, но добавляем более подробное логирование
        logger.error(f"Error creating Stripe subscription: {e}")
        return None

async def handle_stripe_webhook(payload, sig_header: str) -> bool:
    """
    Handles Stripe webhook events
    
    Этот обработчик теперь поддерживает следующие типы событий:
    - checkout.session.completed: Первичная подписка через Checkout
    - invoice.paid: Первичное событие об оплате (только логирование)
    - invoice.payment_succeeded: Окончательное подтверждение платежа (обновление БД и уведомления)
    - customer.subscription.updated: Обновление подписки
    - customer.subscription.deleted: Удаление подписки
    - invoice.payment_failed: Неудачный платеж
    """
    try:
        # Проверяем подпись webhook для безопасности
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            config.stripe.webhook_secret
        )

        # Логируем полученное событие
        webhook_logger.info(f"Received Stripe webhook: {event.type} - {event.id}")
        
        # Обработка различных типов событий
        if event.type == 'checkout.session.completed':
            # Обработка первичной подписки - сохраняем существующую функциональность
            return await handle_checkout_completed(event)
        
        elif event.type == 'invoice.paid':
            # Проверяем $0 инвойсы только для invoice событий
            if hasattr(event.data.object, 'amount_paid') and event.data.object.amount_paid == 0:
                webhook_logger.info(f"Received $0 invoice {event.id} for subscription {getattr(event.data.object, 'subscription', 'N/A')} - acknowledging without database updates")
                return True
            # Обработка первичного события об оплате, но для рекуррентных платежей мы ждем invoice.payment_succeeded для окончательного подтверждения
            return await handle_invoice_paid(event)
        
        elif event.type == 'customer.subscription.updated':
            # Обработка обновления подписки
            return await handle_subscription_updated(event)
        
        elif event.type == 'customer.subscription.deleted':
            # Обработка удаления подписки
            return await handle_subscription_deleted(event)
        
        elif event.type == 'invoice.payment_failed':
            # Проверяем $0 инвойсы только для invoice событий
            if hasattr(event.data.object, 'amount_paid') and event.data.object.amount_paid == 0:
                webhook_logger.info(f"Received $0 invoice {event.id} for subscription {getattr(event.data.object, 'subscription', 'N/A')} - acknowledging without database updates")
                return True
            # Обработка неудачного платежа
            return await handle_payment_failed(event)
        elif event.type == 'invoice.payment_succeeded':
            # Проверяем $0 инвойсы только для invoice событий
            if hasattr(event.data.object, 'amount_paid') and event.data.object.amount_paid == 0:
                webhook_logger.info(f"Received $0 invoice {event.id} for subscription {getattr(event.data.object, 'subscription', 'N/A')} - acknowledging without database updates")
                return True
            # Обработка успешного подтверждения платежа
            return await handle_invoice_payment_succeeded(event)
        
        else:
            # Для неизвестных типов событий просто логируем и возвращаем успех
            webhook_logger.info(f"Unhandled event type: {event.type}")
            return True

    except ValueError as e:
        # Ошибка подписи webhook
        webhook_logger.error(f"Invalid Stripe webhook signature: {e}")
        return False
    except Exception as e:
        # Другие ошибки
        webhook_logger.error(f"Error handling Stripe webhook: {e}", exc_info=True)
        return False

# Вспомогательные функции для обработки различных типов событий


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

async def handle_checkout_completed(event) -> bool:
    """
    Обрабатывает событие checkout.session.completed
    Это первичная подписка через Checkout
    """
    try:
        session = event.data.object
        telegram_id = session.metadata.get('telegram_id')
        subscription_type = session.metadata.get('subscription_type')
        subscription_id = session.subscription
        
        # Если нет telegram_id или subscription_id, это необычная ситуация
        if not telegram_id or not subscription_id:
            webhook_logger.error(f"Missing telegram_id or subscription_id in checkout session: {session.id}")
            return False

        user = await get_user(telegram_id)
        if not user:
            webhook_logger.error(f"User not found for telegram_id: {telegram_id}")
            return False
            
        hub = create_translator_hub()
        i18n = hub.get_translator_by_locale(locale=user['user_language'])
        
        # Получаем ID клиента из подписки
        try:
            subscription = await stripe.Subscription.retrieve_async(subscription_id)
            customer_id = subscription.customer
            
            # Обновляем метаданные клиента, добавляя telegram_id
            if customer_id:
                await update_stripe_customer_metadata(customer_id, telegram_id)
        except Exception as e:
            webhook_logger.error(f"Error updating customer metadata: {e}")
            # Продолжаем выполнение, даже если обновление метаданных не удалось
        
        # Calculate start and end dates for the subscription
        start_date_dt = datetime.utcnow()
        subscription_period_days: int = _get_subscription_period(subscription_type)
        end_date_dt = start_date_dt + timedelta(days=subscription_period_days)

        # Успешная обработка
        need_reward: bool = False
        if user:
            if user.get('source', '').startswith('ref_'):
                logger.info(
                    f"Found referral data for user {telegram_id}, source: {user['source']}")
                referrer: str = user['source'].replace('ref_', '')
                try:
                    await complete_referral_process(user=user)
                    need_reward: bool = await referral_need_reward(user)
                except Exception as e:
                    logger.error(f"Error completing referral process: {e}", exc_info=True)
                    confirm_result = await confirm_referral_process(referrer_telegram_id=referrer,
                                                                    referral_telegram_id=user['telegram_id'],
                                                                    success=False)

        # Добавляем подписку в базу данных
        if need_reward:
            try:
                await add_free_days_to_subscription_stripe(telegram_id=telegram_id, days=7, subscription_id=subscription_id)
                end_date_dt = end_date_dt + timedelta(days=7)
            except Exception as e:
                logger.error(f"Error adding free days to subscription: {e}", exc_info=True)


        await db_add_subscription(
            telegram_id=telegram_id,
            subscription_id=subscription_id,
            subscription_type_str=subscription_type,
            start_date_dt=start_date_dt,
            end_date_dt=end_date_dt,
            is_autopay_active=True
        )

        # Добавляем запись о платеже
        await db_add_payment(
            telegram_id=int(telegram_id),
            amount=session.amount_total / 100,  # Конвертируем из центов
            status='success',
            token=subscription_id
        )

        # Отправляем сообщение о успешной подписке
        try:
            await get_bot().send_message(
                chat_id=telegram_id,
                text=i18n.subscription_success()
            )
            webhook_logger.info(f"Successfully processed checkout.session.completed for user {telegram_id}")
        except Exception as e:
            webhook_logger.error(f"Failed to send confirmation message to user {telegram_id}: {e}")
            # Не возвращаем False, так как подписка уже создана
        
        return True
    except Exception as e:
        webhook_logger.error(f"Error handling checkout.session.completed: {e}", exc_info=True)
        return False

async def handle_invoice_paid(event) -> bool:
    """
    Обрабатывает событие invoice.paid
    Это первичное событие об оплате, но для рекуррентных платежей мы ждем invoice.payment_succeeded для окончательного подтверждения
    """
    try:
        invoice = event.data.object
        subscription_id = invoice.subscription
        customer_id = invoice.customer
        
        # Check if this is a $0 invoice - if so, just acknowledge and skip processing
        invoice_amount = invoice.amount_paid / 100  # Convert from cents
        if invoice_amount == 0:
            webhook_logger.info(f"Received $0 invoice {invoice.id} for subscription {subscription_id} - acknowledging without database updates")
            return True
        
        if not subscription_id:
            webhook_logger.error(f"Missing subscription_id in invoice: {invoice.id}")
            return False

        # Check if this is the initial subscription payment
        # For new subscriptions, billing_reason is 'subscription_create'
        # For renewals, billing_reason is 'subscription_cycle'
        billing_reason = getattr(invoice, 'billing_reason', None)
        if billing_reason == 'subscription_create':
            webhook_logger.info(f"Skipping renewal notification for initial subscription payment (invoice {invoice.id})")
            # Still process the payment and update subscription, but don't send renewal message
            # The checkout.session.completed handler already sent the activation message
        elif billing_reason == 'subscription_update':
            webhook_logger.info(f"Skipping renewal notification for subscription update payment (invoice {invoice.id})")
            # This is likely a proration from subscription changes
        
        # Check if this is a proration invoice (subscription upgrade/downgrade)
        # If so, we don't want to send renewal messages since the user already got upgrade messages
        is_proration_invoice = False
        if invoice.lines and hasattr(invoice.lines, 'data'):
            for line_item in invoice.lines.data:
                if hasattr(line_item, 'proration') and line_item.proration:
                    is_proration_invoice = True
                    webhook_logger.info(f"Invoice {invoice.id} contains proration - skipping renewal notification")
                    break

        # Encontra o usuário por subscription_id
        user = await find_user_by_subscription_id(subscription_id)
        if not user:
            webhook_logger.error(f"User not found for subscription: {subscription_id}")
            return False
            
        telegram_id = user['telegram_id']
        
        # Обновляем метаданные клиента, добавляя telegram_id, если он есть
        if customer_id:
            await update_stripe_customer_metadata(customer_id, telegram_id)
            
        # Для invoice.paid мы только логируем событие, не выполняем обновления БД
        # Обновления БД и уведомления будут выполнены в invoice.payment_succeeded
        webhook_logger.info(f"Received invoice.paid for user {telegram_id}, waiting for invoice.payment_succeeded for final processing")
            
        return True
    except Exception as e:
        webhook_logger.error(f"Error handling invoice.paid: {e}", exc_info=True)
        return False

async def handle_subscription_updated(event) -> bool:
    """
    Обрабатывает событие customer.subscription.updated
    Например, изменение плана подписки, статуса и т.д.
    """
    try:
        subscription = event.data.object
        subscription_id = subscription.id
        customer_id = subscription.customer
        
        # Находим пользователя по subscription_id
        user = await find_user_by_subscription_id(subscription_id)
        if not user:
            webhook_logger.error(f"User not found for subscription: {subscription_id}")
            return False
            
        telegram_id = user['telegram_id']
        
        # Обновляем метаданные клиента, добавляя telegram_id, если он есть
        if customer_id:
            await update_stripe_customer_metadata(customer_id, telegram_id)
        
        # Обрабатываем только существенные изменения статуса
        if subscription.status in ['active', 'trialing']:
            # Если подписка активна, обновляем ее статус
            
            # Определяем тип подписки
            price_id = None
            # Безопасное получение items
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
            except Exception as e:
                webhook_logger.warning(f"Error getting subscription items, defaulting to existing type: {e}")
                # Продолжаем выполнение, используя существующий тип подписки из БД
                
            subscription_type = get_subscription_type_by_price_id(price_id) if price_id else user['subscription_type']
            
            # Calculate start and end dates for the updated subscription
            start_date_dt = datetime.utcnow()
            subscription_period_days = _get_subscription_period(subscription_type)
            end_date_dt = start_date_dt + timedelta(days=subscription_period_days)

            user = await get_user(telegram_id=telegram_id)
            if end_date_dt < user['end_date']:
                pass
                webhook_logger.info(f"Skipping subscription update for user {telegram_id} - end date is in the past")
            else:
                # Обновляем подписку в базе данных
                await db_add_subscription(
                    telegram_id=telegram_id,
                    subscription_id=subscription_id,
                    subscription_type_str=subscription_type,
                    start_date_dt=start_date_dt,
                    end_date_dt=end_date_dt,
                    is_autopay_active=True
                )
                webhook_logger.info(f"Updated active subscription for user {telegram_id}")
        elif subscription.status in ['canceled', 'unpaid', 'past_due']:
            # Если подписка отменена или просрочена, отмечаем это в базе данных
            # Но не отменяем подписку полностью, так как это произойдет 
            # либо через webhook customer.subscription.deleted,
            # либо по истечении срока подписки через scheduler
            webhook_logger.info(f"Subscription {subscription_id} changed to status: {subscription.status}")
        
        return True
    except Exception as e:
        webhook_logger.error(f"Error handling customer.subscription.updated: {e}", exc_info=True)
        return False

async def handle_subscription_deleted(event) -> bool:
    """
    Обрабатывает событие customer.subscription.deleted
    Когда подписка полностью удалена в Stripe
    """
    try:
        subscription = event.data.object
        subscription_id = subscription.id
        
        # Находим пользователя по subscription_id
        user = await find_user_by_subscription_id(subscription_id)
        if not user:
            webhook_logger.error(f"User not found for deleted subscription: {subscription_id}")
            return False
            
        telegram_id = user['telegram_id']

        # Определяем причину отмены из Stripe
        cancellation_reason = None
        if hasattr(subscription, 'cancellation_details') and subscription.cancellation_details:
            cancellation_reason = getattr(subscription.cancellation_details, 'reason', None)

        # Определяем тип отмены на основе причины
        if cancellation_reason == 'payment_failed':
            action_type = 'subscription_cancelled_payment_failure'
            reason = 'payment_failure'
        elif cancellation_reason == 'cancellation_requested':
            action_type = 'subscription_cancelled_by_user'
            reason = 'user_initiated'
        else:
            action_type = 'subscription_deleted_stripe'
            reason = cancellation_reason or 'unknown'

        # Логируем отмену подписки в user_actions
        await log_user_action(
            user_id=user['id'],
            action_type=action_type,
            action_category='subscription',
            metadata={
                'subscription_type': user.get('subscription_type'),
                'subscription_id': subscription_id,
                'payment_provider': 'stripe',
                'reason': reason,
                'stripe_status': subscription.status,
            }
        )

        # Создаем переводчик для пользователя
        hub = create_translator_hub()
        i18n = hub.get_translator_by_locale(locale=user['user_language'])
        
        # Отменяем подписку в нашей системе
        # Для этого используем существующую функцию cancel_subscription
        # Но импортируем ее здесь, чтобы избежать циклических импортов
        from models.orm import cancel_subscription
        await cancel_subscription(telegram_id, False, i18n, force_cancel=True)
        
        webhook_logger.info(f"Subscription deleted for user {telegram_id}")
        return True
    except Exception as e:
        webhook_logger.error(f"Error handling customer.subscription.deleted: {e}", exc_info=True)
        return False

async def handle_payment_failed(event) -> bool:
    """
    Обрабатывает событие invoice.payment_failed
    Когда платеж по подписке не прошел
    """
    try:
        invoice = event.data.object
        subscription_id = invoice.subscription
        
        if not subscription_id:
            webhook_logger.error(f"Missing subscription_id in failed invoice: {invoice.id}")
            return False

        # Находим пользователя по subscription_id
        user = await find_user_by_subscription_id(subscription_id)
        if not user:
            webhook_logger.error(f"User not found for subscription with failed payment: {subscription_id}")
            return False
            
        telegram_id = user['telegram_id']
        
        # Добавляем запись о неудачном платеже
        await db_add_payment(
            telegram_id=int(telegram_id),
            amount=invoice.amount_due / 100,  # Конвертируем из центов
            status='failed',
            token=invoice.payment_intent
        )
        
        # Отправляем уведомление пользователю о неудачном платеже
        try:
            hub = create_translator_hub()
            i18n = hub.get_translator_by_locale(locale=user['user_language'])
            
            # Определяем причину ошибки платежа
            payment_intent = None
            error_message = "Unknown error"
            
            if invoice.payment_intent:
                try:
                    payment_intent = await stripe.PaymentIntent.retrieve_async(invoice.payment_intent)
                    if payment_intent.last_payment_error:
                        error_message = payment_intent.last_payment_error.message
                except Exception as e:
                    webhook_logger.error(f"Failed to retrieve payment intent: {e}")
            
            # Отправляем сообщение с информацией о неудачном платеже
            await get_bot().send_message(
                chat_id=telegram_id,
                text=i18n.payment_failed(error=error_message)
            )
            webhook_logger.info(f"Notified user {telegram_id} about failed payment")
        except Exception as e:
            webhook_logger.error(f"Failed to send payment failure notification to user {telegram_id}: {e}")
        
        return True
    except Exception as e:
        webhook_logger.error(f"Error handling invoice.payment_failed: {e}", exc_info=True)
        return False

async def handle_invoice_payment_succeeded(event) -> bool:
    """
    Обрабатывает событие invoice.payment_succeeded
    Это основной обработчик для рекуррентных платежей - выполняет обновление БД и отправку уведомлений
    """
    try:
        invoice = event.data.object
        subscription_id = invoice.subscription
        customer_id = invoice.customer

        # Check if this is a $0 invoice - if so, just acknowledge and skip processing
        invoice_amount = invoice.amount_paid / 100  # Convert from cents  
        if invoice_amount == 0:
            webhook_logger.info(f"Received $0 invoice {invoice.id} for subscription {subscription_id} - acknowledging without database updates")
            return True

        if not subscription_id:
            webhook_logger.error(f"Missing subscription_id in invoice (payment_succeeded): {invoice.id}")
            return False

        # Check if this is the initial subscription payment
        # For new subscriptions, billing_reason is 'subscription_create'
        # For renewals, billing_reason is 'subscription_cycle'
        billing_reason = getattr(invoice, 'billing_reason', None)
        if billing_reason == 'subscription_create':
            webhook_logger.info(f"Skipping renewal notification for initial subscription payment (invoice {invoice.id}) - payment_succeeded")
            # Still process the payment and update subscription, but don't send renewal message
            # The checkout.session.completed handler already sent the activation message
        elif billing_reason == 'subscription_update':
            webhook_logger.info(f"Skipping renewal notification for subscription update payment (invoice {invoice.id}) - payment_succeeded")
            # This is likely a proration from subscription changes

        # Check if this is a proration invoice (subscription upgrade/downgrade)
        # If so, we don't want to send renewal messages since the user already got upgrade messages
        is_proration_invoice = False
        if invoice.lines and hasattr(invoice.lines, 'data'):
            for line_item in invoice.lines.data:
                if hasattr(line_item, 'proration') and line_item.proration:
                    is_proration_invoice = True
                    webhook_logger.info(f"Invoice {invoice.id} contains proration - skipping renewal notification (payment_succeeded)")
                    break

        # Находим пользователя по subscription_id
        user = await find_user_by_subscription_id(subscription_id)
        if not user:
            # Попытка найти пользователя по customer_id, если subscription_id не дал результата
            if customer_id:
                webhook_logger.info(f"User not found by subscription_id {subscription_id} for invoice.payment_succeeded. Trying by customer_id {customer_id}")
                user = await find_user_by_customer_id_metadata(customer_id)
                if not user: # Если и по customer_id не нашли
                    webhook_logger.error(f"User not found for customer_id {customer_id} (after trying subscription_id {subscription_id}) for invoice.payment_succeeded.")
                    return False
            else: # Если нет customer_id, то точно не можем найти
                webhook_logger.error(f"User not found for subscription (payment_succeeded): {subscription_id} and no customer_id provided.")
                return False
            
        telegram_id = user['telegram_id']
        
        # Обновляем метаданные клиента, добавляя telegram_id, если он есть
        if customer_id:
            await update_stripe_customer_metadata(customer_id, telegram_id)
            
        # Определяем тип подписки на основе price_id
        if not invoice.lines.data:
            webhook_logger.error(f"No line items in invoice (payment_succeeded): {invoice.id}")
            return False
            
        price_id = invoice.lines.data[0].price.id
        subscription_type = get_subscription_type_by_price_id(price_id)
        
        # Calculate start and end dates for the payment succeeded subscription
        start_date_dt = datetime.utcnow()
        subscription_period_days = _get_subscription_period(subscription_type)
        end_date_dt = start_date_dt + timedelta(days=subscription_period_days)
            
        # Обновляем подписку в базе данных
        await db_add_subscription(
            telegram_id=telegram_id,
            subscription_id=subscription_id,
            subscription_type_str=subscription_type,
            start_date_dt=start_date_dt,
            end_date_dt=end_date_dt,
            is_autopay_active=True
        )
            
        # Записываем платеж
        if invoice.payment_intent:
            await db_add_payment(
                telegram_id=int(telegram_id),
                amount=invoice.amount_paid / 100,  # Конвертируем из центов
                status='success', # Статус 'success' так как платеж успешен
                token=invoice.payment_intent # Используем payment_intent из invoice
            )
            
        # Отправляем уведомление пользователю только для настоящих продлений (не для первичных подписок и не для прорейшн)
        should_send_renewal_message = (
            billing_reason == 'subscription_cycle' and 
            not is_proration_invoice
        )
        
        if should_send_renewal_message:
            try:
                hub = create_translator_hub()
                i18n = hub.get_translator_by_locale(locale=user['user_language'])
                await get_bot().send_message(
                    chat_id=telegram_id,
                    text=i18n.subscription_renewal_success() # Используем то же сообщение, что и для invoice.paid
                )
                webhook_logger.info(f"Successfully processed invoice.payment_succeeded renewal for user {telegram_id}")
            except Exception as e:
                webhook_logger.error(f"Failed to send renewal notification (payment_succeeded) to user {telegram_id}: {e}")
                # Не возвращаем False, так как подписка уже обновлена
        else:
            webhook_logger.info(f"Skipped renewal notification for invoice {invoice.id} (payment_succeeded) - billing_reason: {billing_reason}, proration: {is_proration_invoice}")
            
        return True
    except Exception as e:
        webhook_logger.error(f"Error handling invoice.payment_succeeded: {e}", exc_info=True)
        return False

async def find_user_by_subscription_id(subscription_id: str) -> Optional[Dict[str, Any]]:
    """
    Находит пользователя по ID подписки Stripe
    
    Args:
        subscription_id: ID подписки в Stripe
        
    Returns:
        Словарь с данными пользователя или None, если пользователь не найден
    """
    from sqlalchemy import select
    from models.model import User
    from models.orm import async_session, _prepare_user_dict
    
    async with async_session() as session:
        result = await session.execute(
            select(User).filter(User.subscription_id == subscription_id)
        )
        user = result.scalar_one_or_none()
        if user:
            return _prepare_user_dict(user)
        return None




async def find_user_by_customer_id_metadata(customer_id: str) -> Optional[Dict[str, Any]]:
    """
    Находит пользователя по ID клиента Stripe, используя telegram_id из метаданных.
    
    Args:
        customer_id: ID клиента в Stripe
        
    Returns:
        Словарь с данными пользователя или None, если пользователь не найден или нет telegram_id в метаданных.
    """
    try:
        webhook_logger.info(f"Attempting to find user by customer_id via metadata: {customer_id}")
        customer_data = await get_stripe_customer_metadata(customer_id)
        
        if customer_data and customer_data.get('success', True) and customer_data.get('has_telegram_id'):
            telegram_id = customer_data.get('telegram_id')
            if telegram_id:
                webhook_logger.info(f"Found telegram_id {telegram_id} in metadata for customer_id {customer_id}")
                user = await get_user(telegram_id)
                if user:
                    webhook_logger.info(f"Successfully found user for telegram_id {telegram_id} (from customer_id {customer_id})")
                    return user
                else:
                    webhook_logger.warning(f"User not found in DB for telegram_id {telegram_id} (from customer_id {customer_id})")
            else:
                webhook_logger.warning(f"telegram_id not found in metadata for customer_id {customer_id}")
        else:
            error_details = customer_data.get('error', 'Unknown error') if isinstance(customer_data, dict) else 'Failed to retrieve customer data'
            webhook_logger.warning(f"Could not retrieve valid customer metadata or telegram_id for customer_id {customer_id}. Details: {error_details}")
            
        return None
    except Exception as e:
        webhook_logger.error(f"Error in find_user_by_customer_id_metadata for {customer_id}: {e}", exc_info=True)
        return None 

async def reactivate_stripe_subscription(subscription_id: str) -> bool:
    """
    Reactivates a canceled Stripe subscription
    """
    try:
        await stripe.Subscription.modify_async(id=subscription_id, cancel_at_period_end=False)
        logger.info(f"Successfully reactivated Stripe subscription: {subscription_id}")
        return True
    except Exception as e:
        logger.error(f"Error reactivating Stripe subscription: {e}")
        return False


if __name__ == "__main__":
    result = asyncio.run(create_stripe_subscription(account_id='449290141', subscription_type='monthly', discount_type='notification_discount'))
    print(result['url'])
    # print(asyncio.run(get_stripe_customer_metadata('cus_RNNTBgh7hWNRAW')))