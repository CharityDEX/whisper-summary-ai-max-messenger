from pprint import pprint
import logging
import json
from typing import Optional
import os
import datetime

import stripe
from cloudpayments import CloudPayments
from fastapi import FastAPI, Request, Response, status, Depends, HTTPException, Query
from fluentogram import TranslatorHub

from services.payments.stripe_service import handle_stripe_webhook
from utils.i18n import create_translator_hub
from models.orm import db_add_payment, db_add_subscription, get_user, update_subscription_details, \
    confirm_referral_process, log_user_action
from services.bot_provider import get_bot
from services.payments.services import (
    create_subscription,
    get_cloudpayments_subscription_details_by_sub_id,
    determine_subscription_type_from_cp_data,
)
from services.payments.general_fucntions import complete_referral_process, referral_need_reward
from services.payments.stripe_tools import (
    check_all_stripe_subscriptions, 
    sync_stripe_subscriptions, 
    get_stripe_subscription_details,
    manually_sync_subscription
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = FastAPI()
# Попытка загрузки конфигурации с подробным логированием
logger.info("Attempting to load configuration...")
try:
    from services.init_bot import config
    logger.info(f"Configuration loaded successfully: TgBot fields: {dir(config.tg_bot)}")
    logger.info(f"Admin API key from loaded config: {config.tg_bot.admin_api_key}")
except Exception as e:
    logger.error(f"Error loading configuration: {e}", exc_info=True)
    config = None

# Простая аутентификация по API-ключу для админки
async def verify_admin_key(api_key: str = Query(..., alias="api_key")):
    # Добавим отладочную информацию
    logger.info(f"Config loaded from: {config}")
    logger.info(f"TgBot config: {config.tg_bot}")
    logger.info(f"Admin API key from config: {config.tg_bot.admin_api_key}")
    logger.info(f"Provided API key: {api_key}")
    
    # Получаем API-ключ напрямую из переменной окружения как запасной вариант
    env_admin_api_key = os.environ.get('ADMIN_API_KEY')
    logger.info(f"Admin API key from environment: {env_admin_api_key}")
    
    # Используем ключ из конфигурации или из переменной окружения
    admin_api_key = config.tg_bot.admin_api_key or env_admin_api_key
    
    if not admin_api_key or api_key != admin_api_key:
        logger.error(f"Invalid API key provided: {api_key}. Expected: {admin_api_key}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    return api_key

def get_subscription_type(description: str, translator_hub: TranslatorHub) -> tuple[str, int, bool]:
    """
    Determine subscription type and period based on description using translations.
    True if payment was made by subscription -> no need to create subscription
    False if this is the first payment -> need to create subscription
    """
    for locale in ['ru', 'en']:
        i18n = translator_hub.get_translator_by_locale(locale=locale)
        if i18n.subscription_monthly() in description:
            return 'monthly', 31, False
        elif i18n.subscription_monthly_payment() in description.lower():
            return 'monthly', 31, True
        elif i18n.subscription_semiannual() in description:
            return 'semiannual', 180, False
        elif i18n.subscription_semiannual_payment() in description.lower():
            return 'semiannual', 180, True
        elif i18n.subscription_annual() in description:
            return 'annual', 365, False
        elif i18n.subscription_annual_payment() in description.lower():
            return 'annual', 365, True
        elif i18n.subscription_weekly() in description:
            return 'weekly', 7, False
        elif i18n.subscription_weekly_payment() in description.lower():
            return 'weekly', 7, True
    return 'weekly', 7, True

@app.post("/payment_notification")
async def payment_notification(request: Request):
    form_data = await request.form()
    data = dict(form_data)
    logger.info(f"Received CloudPayments webhook: {data}") # Log incoming data

    telegram_id_str: str = data.get('AccountId')
    webhook_subscription_id: Optional[str] = data.get('SubscriptionId') # Can be None or empty
    transaction_id: str = data.get('TransactionId')
    amount_str: str = data.get('Amount')
    token: Optional[str] = data.get('Token') # Can be None
    webhook_status: str = data.get('Status') # e.g., Completed, Declined
    operation_type: str = data.get('OperationType') # e.g., Payment, Refund, etc.
    webhook_description: str = data.get('Description', '')
    # CloudPayments sends custom data under the 'Data' key, as a JSON string.
    json_data_str: Optional[str] = data.get('Data') 

    if not telegram_id_str or not transaction_id or not webhook_status:
        logger.error(f"Missing critical data in CloudPayments webhook: AccountId, TransactionId, or Status. Data: {data}")
        return {"code": 1} # Indicate an error to CP, though they might not use it

    try:
        telegram_id_int = int(telegram_id_str)
        amount_float = float(amount_str)
    except ValueError as e:
        logger.error(f"Could not parse telegram_id or amount from webhook. Data: {data}. Error: {e}")
        return {"code": 1}

    # 1. Record the payment attempt (regardless of whether it's initial or recurrent for now)
    # Status here is from the webhook (e.g., 'Completed', 'Declined')
    await db_add_payment(telegram_id=telegram_id_int, amount=amount_float, status=webhook_status.lower(), token=token, transaction_id=transaction_id)


    if webhook_status != 'Completed' or operation_type != 'Payment':
        logger.info(f"Webhook is not a completed payment. Status: {webhook_status}, OperationType: {operation_type}. User: {telegram_id_str}. No subscription update needed.")
        # Potentially handle 'Declined' or other statuses here (e.g., notify user, update DB state)
        if webhook_status == 'Declined':
            pass
        return {"code": 0} # Acknowledge webhook

    # --- At this point, we have a 'Completed' 'Payment' webhook --- 
    translator_hub: TranslatorHub = create_translator_hub()
    user: dict | None = await get_user(telegram_id_int)
    if not user:
        logger.error(f"User {telegram_id_int} not found in DB after receiving completed payment webhook {transaction_id}."
                     f" This should not happen if users are created upon bot interaction.")
        return {"code": 1} 

    i18n = translator_hub.get_translator_by_locale(locale=user.get('user_language', 'ru'))
    cp_public_id = config.payment.public_id
    cp_api_secret = config.payment.api_secret
    final_subscription_id_cp: Optional[str] = webhook_subscription_id

    # Determine if referral reward should be applied: only for users with referral source
    referral_reward: bool = False
    try:
        if user.get('source', '').startswith('ref_'):
            referral_reward = await referral_need_reward(user=user)
    except Exception as e:
        logger.error(f"Error determining referral reward for user {telegram_id_int}: {e}", exc_info=True)

    # Determine if this is an initial payment needing a new subscription in CloudPayments,
    # or a recurrent payment for an existing CloudPayments subscription.
    is_initial_payment_flow = not webhook_subscription_id 

    if is_initial_payment_flow:
        logger.info(f"Webhook for user {telegram_id_str}, transaction {transaction_id} appears to be an INITIAL payment (no SubscriptionId in webhook)."
                    f" Attempting to create a new CP subscription.")
        
        intended_sub_type_from_json: Optional[str] = None
        if json_data_str: #getting subscription type from JsonData
            try:
                parsed_json_data = json.loads(json_data_str)
                if isinstance(parsed_json_data, dict):
                    intended_sub_type_from_json = parsed_json_data.get("intended_sub_type")
                    if intended_sub_type_from_json:
                        logger.info(f"Found 'intended_sub_type': '{intended_sub_type_from_json}' in JsonData for user {telegram_id_str}")
                else:
                    logger.warning(f"Parsed JsonData for user {telegram_id_str} is not a dictionary: {parsed_json_data}")    
            except json.JSONDecodeError as e_json:
                logger.error(f"Failed to parse JsonData string '{json_data_str}' for user {telegram_id_str}. Error: {e_json}")
        
        if not intended_sub_type_from_json:
            # Fallback to old logic if JsonData or intended_sub_type is missing (less reliable)
            logger.warning(f"'intended_sub_type' not found in JsonData for initial payment. User: {telegram_id_str}, Tx: {transaction_id}. Falling back to parsing description (this is unreliable).")
            temp_subscription_type, _, _ = get_subscription_type(webhook_description, translator_hub) 
            if not temp_subscription_type:
                logger.error(f"Could not determine subscription type for initial payment (even from description fallback). User: {telegram_id_str}, Desc: {webhook_description}")
                try:
                    await get_bot().send_message(chat_id=telegram_id_int, text=i18n.subscription_failure_general())
                except Exception as e_notify:
                    logger.error(f'Failed to send general subscription failure message to user: {telegram_id_str}. Error: {e_notify}')
                return {"code": 0} # Acknowledge webhook, but can't proceed
            final_intended_sub_type = temp_subscription_type
        else:
            final_intended_sub_type = intended_sub_type_from_json

        created_cp_sub_id = create_subscription(
            account_id=telegram_id_str, 
            token=token, 
            subscription_type=final_intended_sub_type,
            is_referral_reward=referral_reward
        )
        if not created_cp_sub_id:
            logger.error(f"Failed to create CloudPayments subscription for initial payment. User: {telegram_id_str}, Transaction: {transaction_id}")
            # Notify user of failure
            try:
                await get_bot().send_message(chat_id=telegram_id_int, text=i18n.subscription_failure_creation())
            except Exception as e_notify:
                logger.error(f'Failed to send subscription creation failure message to user: {telegram_id_str}. Error: {e_notify}')
            return {"code": 0} # Acknowledge webhook
        final_subscription_id_cp = created_cp_sub_id
        logger.info(f"Successfully created new CloudPayments subscription {final_subscription_id_cp} for user {telegram_id_str}.")
    else:
        logger.info(f"Webhook for user {telegram_id_str}, transaction {transaction_id} is for an EXISTING CP subscription: {final_subscription_id_cp}")

    # --- Fetch authoritative details from CloudPayments for the subscription_id_cp --- 
    if not final_subscription_id_cp:
        logger.error(f"No CloudPayments subscription ID available (either from webhook or creation) for user {telegram_id_str}, transaction {transaction_id}. Cannot update DB subscription.")
        return {"code": 0} # Acknowledge webhook

    cp_sub_details = await get_cloudpayments_subscription_details_by_sub_id(
        account_id=telegram_id_str, 
        subscription_id_to_find=final_subscription_id_cp,
        cp_public_id=cp_public_id,
        cp_api_secret=cp_api_secret
    )

    if not cp_sub_details:
        logger.error(f"Failed to fetch details for CP subscription {final_subscription_id_cp} for user {telegram_id_str}. Transaction: {transaction_id}."
                     f" DB subscription may not be updated correctly.")
        # Notify user that there was an issue activating/renewing?
        # This is a critical failure point if the sub exists in CP but we can't get details.
        return {"code": 0} # Acknowledge webhook

    # Determine subscription type and dates from CP data
    authoritative_sub_type = determine_subscription_type_from_cp_data(cp_sub_details)
    next_transaction_date_iso_str = cp_sub_details.get('NextTransactionDateIso')
    cp_status_str = cp_sub_details.get('Status') # e.g. Active, Cancelled

    # For initial payment, LastTransactionDateIso might be null from CP sub details.
    # In this case, the start_date is the DateTime of the current webhook.
    # For recurrent payments, LastTransactionDateIso from CP sub details should be used.
    if is_initial_payment_flow:
        current_webhook_datetime_str = data.get('DateTime') # e.g., '2025-06-02 10:04:01'
        if not current_webhook_datetime_str:
            logger.error(f"Missing DateTime in webhook for initial payment, cannot set start_date. User: {telegram_id_str}, CP Sub: {final_subscription_id_cp}")
            return {"code": 0}
        source_for_start_date_str = current_webhook_datetime_str
        logger.info(f"Initial payment flow: Using webhook DateTime '{source_for_start_date_str}' as start_date for DB.")
    else: # Recurrent payment flow
        source_for_start_date_str = cp_sub_details.get('LastTransactionDateIso')
        logger.info(f"Recurrent payment flow: Using CP LastTransactionDateIso '{source_for_start_date_str}' as start_date for DB.")

    if not authoritative_sub_type or not next_transaction_date_iso_str or not source_for_start_date_str:
        logger.error(f"Missing critical data for DB update. User: {telegram_id_str}, CP Sub: {final_subscription_id_cp}. "
                     f"Type: {authoritative_sub_type}, NextDate: {next_transaction_date_iso_str}, SourceForStartDate: {source_for_start_date_str}. Cannot update DB.")
        return {"code": 0}
    try:
        # CloudPayments ISO dates are like "2021-11-02T21:00:00"
        # Webhook DateTime is like "2025-06-02 10:04:01" (space separator)
        end_date_dt = datetime.datetime.fromisoformat(next_transaction_date_iso_str)
        
        # Adjust parsing for start_date based on its source
        if 'T' in source_for_start_date_str: # ISO format from CP (e.g., "2021-11-02T21:00:00")
            start_date_dt = datetime.datetime.fromisoformat(source_for_start_date_str)
        else: # Likely from webhook DateTime (e.g., "2025-06-02 10:04:01")
            start_date_dt = datetime.datetime.strptime(source_for_start_date_str, '%Y-%m-%d %H:%M:%S')

        if referral_reward:
            start_date_dt = start_date_dt + datetime.timedelta(days=7)

    except ValueError as e_date:
        logger.error(f"Could not parse dates. User: {telegram_id_str}, CP Sub: {final_subscription_id_cp}. "
                     f"NextDate_str: '{next_transaction_date_iso_str}', StartDate_str: '{source_for_start_date_str}'. Error: {e_date}")
        return {"code": 0}

    # Update local database with authoritative information
    await db_add_subscription(
        telegram_id=telegram_id_int,
        subscription_id=final_subscription_id_cp,
        subscription_type_str=authoritative_sub_type,
        start_date_dt=start_date_dt, # This is the date of the current successful payment
        end_date_dt=end_date_dt,     # This is the next billing date from CP
        is_autopay_active=(cp_status_str == 'Active') # Autopay is true if CP sub is Active
    )

    # Check referral data
    if user.get('source', '').startswith('ref_'):
        logger.info(f"Found referral data for user {telegram_id_str}, CP Sub ID: {final_subscription_id_cp}, New End Date: {end_date_dt}, source: {user['source']}")
        referrer: str = user['source'].replace('ref_', '')
        try:
            await complete_referral_process(user=user)
        except Exception as e:
            logger.error(f"Error completing referral process: {e}", exc_info=True)
            confirm_result = await confirm_referral_process(referrer_telegram_id=referrer,
                                                            referral_telegram_id=user['telegram_id'],
                                                            success=False)


    logger.info(f"Successfully processed COMPLETED payment and updated/created subscription for user {telegram_id_str}, CP Sub ID: {final_subscription_id_cp}, New End Date: {end_date_dt}")
    try:
        await get_bot().send_message(chat_id=telegram_id_int, text=i18n.subscription_success())
    except Exception as e_notify:
        logger.error(f'Failed to send subscription success message to user: {telegram_id_str}. Error: {e_notify}')

    return {"code": 0}

@app.post("/stripe_webhook")
async def stripe_webhook(request: Request):
    """
    Обработчик webhook-событий от Stripe
    
    Stripe отправляет webhook-события по различным событиям:
    - checkout.session.completed: Первичная подписка через Checkout
    - invoice.paid: Успешная оплата повторяющихся платежей
    - customer.subscription.updated: Обновление подписки
    - customer.subscription.deleted: Удаление подписки
    - invoice.payment_failed: Неудачный платеж
    
    Stripe ожидает ответ со статусом 200 для подтверждения получения webhook,
    даже если обработка события завершилась с ошибкой.
    """
    # Получаем полезную нагрузку и заголовок подписи
    try:
        payload = await request.body()
        sig_header = request.headers.get('stripe-signature')
        
        if not sig_header:
            logger.error("Missing Stripe signature header")
            # Возвращаем 400 Bad Request вместо 200, так как без подписи webhook не может быть проверен
            return Response(
                status_code=status.HTTP_400_BAD_REQUEST,
                content='{"status": "error", "message": "Missing signature header"}'
            )
        
        # Логируем получение webhook'а (без конфиденциальных данных)
        logger.info(f"Received Stripe webhook with signature: {sig_header[:10]}...")
        
        # Передаем обработку в сервис Stripe
        success = await handle_stripe_webhook(payload, sig_header)
        
        if success:
            logger.info("Successfully processed Stripe webhook")
            return {"status": "success"}
        else:
            # Обработка завершилась с ошибкой, но мы все равно возвращаем 200 OK
            # чтобы Stripe не пытался повторно отправить webhook
            logger.warning("Failed to process Stripe webhook, but returning success to prevent retries")
            return {"status": "error", "message": "Processing error, but acknowledged"}
    except Exception as e:
        # Критическая ошибка при обработке
        logger.error(f"Error handling Stripe webhook: {e}", exc_info=True)
        # Возвращаем 200 OK, чтобы Stripe не пытался повторно отправить webhook
        return {"status": "error", "message": "Error encountered, but acknowledged"}

# Административные API для управления подписками Stripe

@app.get("/admin/stripe/subscriptions/check", dependencies=[Depends(verify_admin_key)])
async def admin_check_subscriptions():
    """
    Проверяет все подписки Stripe и возвращает отчет о состоянии
    
    Эта функция идентифицирует несоответствия между базой данных и Stripe
    """
    try:
        report = await check_all_stripe_subscriptions()
        return report
    except Exception as e:
        logger.error(f"Error checking Stripe subscriptions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error checking subscriptions: {str(e)}"
        )

@app.post("/admin/stripe/subscriptions/sync", dependencies=[Depends(verify_admin_key)])
async def admin_sync_subscriptions(fix_issues: bool = Query(False, description="Если True, попытается исправить несоответствия")):
    """
    Синхронизирует все подписки Stripe между базой данных и Stripe
    
    Args:
        fix_issues: Если True, попытается исправить несоответствия
    """
    try:
        report = await sync_stripe_subscriptions(fix_issues=fix_issues)
        return report
    except Exception as e:
        logger.error(f"Error syncing Stripe subscriptions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error syncing subscriptions: {str(e)}"
        )

@app.get("/admin/stripe/subscription/{subscription_id}", dependencies=[Depends(verify_admin_key)])
async def admin_get_subscription_details(subscription_id: str):
    """
    Получает подробную информацию о подписке из Stripe
    
    Args:
        subscription_id: ID подписки в Stripe
    """
    try:
        details = await get_stripe_subscription_details(subscription_id)
        if not details:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Subscription not found: {subscription_id}"
            )
        return details
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting subscription details: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting subscription details: {str(e)}"
        )

@app.post("/admin/stripe/user/{telegram_id}/sync", dependencies=[Depends(verify_admin_key)])
async def admin_sync_user_subscription(telegram_id: str):
    """
    Вручную синхронизирует подписку пользователя с Stripe
    
    Args:
        telegram_id: ID пользователя в Telegram
    """
    try:
        result = await manually_sync_subscription(telegram_id)
        if not result.get('success'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result.get('error', 'Unknown error')
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing user subscription: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error syncing user subscription: {str(e)}"
        )

# Dummy handlers for different subscription statuses
async def handle_subscription_active(subscription_data: dict, user: dict, telegram_id: int):
    """Handle Active subscription status - subscription is active after creation or successful payment"""
    logger.info(f"Handling ACTIVE subscription for user {telegram_id}, subscription {subscription_data.get('Id')}")
    # TODO: Implement logic for active subscription
    # - Update user's subscription status in database
    # - Send confirmation message to user
    # - Update subscription end date based on NextTransactionDate
    pass

async def handle_subscription_past_due(subscription_data: dict, user: dict, telegram_id: int):
    """Handle PastDue subscription status - after one or two consecutive failed payment attempts"""
    logger.info(f"Handling PAST_DUE subscription for user {telegram_id}, subscription {subscription_data.get('Id')}")
    # TODO: Implement logic for past due subscription
    # - Send warning message to user about failed payment
    # - Update subscription status in database
    # - Possibly disable premium features with grace period

    await update_subscription_details(telegram_id=str(telegram_id), subscription_status='PastDue')
    logger.info(f"Updated subscription status to PastDue for user {telegram_id} for subscription {subscription_data.get('Id')}")

    pass

async def handle_subscription_cancelled(subscription_data: dict, user: dict, telegram_id: int):
    """Handle Cancelled subscription status - cancelled by request"""
    logger.info(f"Handling CANCELLED subscription for user {telegram_id}, subscription {subscription_data.get('Id')}")

    # Логируем отмену подписки в user_actions
    await log_user_action(
        user_id=user['id'],
        action_type='subscription_cancelled_by_user',
        action_category='subscription',
        metadata={
            'subscription_type': subscription_data.get('Description'),
            'subscription_id': subscription_data.get('Id'),
            'payment_provider': 'cloudpayments',
            'reason': 'cancelled_by_request',
            'successful_transactions': subscription_data.get('SuccessfulTransactionsNumber'),
            'failed_transactions': subscription_data.get('FailedTransactionsNumber'),
        }
    )

    # TODO: Implement logic for cancelled subscription
    # - Update subscription status in database
    # - Send cancellation confirmation to user
    # - Disable premium features
    # - Set subscription end date to current date or grace period

async def handle_subscription_rejected(subscription_data: dict, user: dict, telegram_id: int):
    """Handle Rejected subscription status - after three consecutive failed payment attempts"""
    logger.info(f"Handling REJECTED subscription for user {telegram_id}, subscription {subscription_data.get('Id')}")

    # Логируем отмену подписки из-за неудачных платежей
    await log_user_action(
        user_id=user['id'],
        action_type='subscription_cancelled_payment_failure',
        action_category='subscription',
        metadata={
            'subscription_type': subscription_data.get('Description'),
            'subscription_id': subscription_data.get('Id'),
            'payment_provider': 'cloudpayments',
            'reason': 'payment_failure',
            'successful_transactions': subscription_data.get('SuccessfulTransactionsNumber'),
            'failed_transactions': subscription_data.get('FailedTransactionsNumber'),
        }
    )

    # TODO: Implement logic for rejected subscription
    # - Update subscription status in database
    # - Send notification to user about subscription termination
    # - Disable premium features
    # - Suggest manual payment or subscription renewal

async def handle_subscription_expired(subscription_data: dict, user: dict, telegram_id: int):
    """Handle Expired subscription status - completed maximum number of periods"""
    logger.info(f"Handling EXPIRED subscription for user {telegram_id}, subscription {subscription_data.get('Id')}")

    # Логируем истечение подписки
    await log_user_action(
        user_id=user['id'],
        action_type='subscription_expired',
        action_category='subscription',
        metadata={
            'subscription_type': subscription_data.get('Description'),
            'subscription_id': subscription_data.get('Id'),
            'payment_provider': 'cloudpayments',
            'reason': 'expired',
            'successful_transactions': subscription_data.get('SuccessfulTransactionsNumber'),
            'max_periods': subscription_data.get('MaxPeriods'),
        }
    )

    # TODO: Implement logic for expired subscription
    # - Update subscription status in database
    # - Send notification about subscription completion
    # - Disable premium features
    # - Suggest subscription renewal

@app.post("/subscription_notification")
async def subscription_notification(request: Request):
    """
    Handle CloudPayments webhook for subscription status changes.
    
    This webhook is triggered when subscription status changes:
    - Active: After creation and successful payment
    - PastDue: After 1-2 consecutive failed payments
    - Cancelled: Cancelled by request
    - Rejected: After 3 consecutive failed payments
    - Expired: Completed maximum number of periods
    """
    form_data = await request.form()
    data = dict(form_data)
    logger.info(f"Received CloudPayments subscription webhook: {data}")

    # Extract required parameters
    subscription_id: str = data.get('Id')
    telegram_id_str: str = data.get('AccountId')
    description: str = data.get('Description', '')
    email: str = data.get('Email', '')
    amount_str: str = data.get('Amount')
    currency: str = data.get('Currency', '')
    require_confirmation: str = data.get('RequireConfirmation', 'false')
    start_date_str: str = data.get('StartDate', '')
    interval: str = data.get('Interval', '')
    period_str: str = data.get('Period')
    status: str = data.get('Status', '')
    successful_transactions_str: str = data.get('SuccessfulTransactionsNumber', '0')
    failed_transactions_str: str = data.get('FailedTransactionsNumber', '0')
    max_periods_str: str = data.get('MaxPeriods', '')
    last_transaction_date_str: str = data.get('LastTransactionDate', '')
    next_transaction_date_str: str = data.get('NextTransactionDate', '')

    # Validate required parameters
    if not subscription_id or not telegram_id_str or not status:
        logger.error(f"Missing critical data in CloudPayments subscription webhook: Id, AccountId, or Status. Data: {data}")
        return {"code": 0}  # Still acknowledge to prevent retries

    try:
        telegram_id_int = int(telegram_id_str)
        amount_float = float(amount_str) if amount_str else 0.0
        period_int = int(period_str) if period_str else 0
        successful_transactions = int(successful_transactions_str)
        failed_transactions = int(failed_transactions_str)
        max_periods = int(max_periods_str) if max_periods_str else None
    except ValueError as e:
        logger.error(f"Could not parse numeric values from subscription webhook. Data: {data}. Error: {e}")
        return {"code": 0}

    # Get user from database
    user = await get_user(telegram_id_int)
    if not user:
        logger.error(f"User {telegram_id_int} not found in DB for subscription webhook {subscription_id}")
        return {"code": 0}

    # Prepare subscription data for handlers
    subscription_data = {
        'Id': subscription_id,
        'AccountId': telegram_id_str,
        'Description': description,
        'Email': email,
        'Amount': amount_float,
        'Currency': currency,
        'RequireConfirmation': require_confirmation.lower() == 'true',
        'StartDate': start_date_str,
        'Interval': interval,
        'Period': period_int,
        'Status': status,
        'SuccessfulTransactionsNumber': successful_transactions,
        'FailedTransactionsNumber': failed_transactions,
        'MaxPeriods': max_periods,
        'LastTransactionDate': last_transaction_date_str,
        'NextTransactionDate': next_transaction_date_str
    }

    # Route to appropriate handler based on status
    try:
        status_upper = status.upper()
        if status_upper == 'ACTIVE':
            await handle_subscription_active(subscription_data, user, telegram_id_int)
        elif status_upper == 'PASTDUE':
            await handle_subscription_past_due(subscription_data, user, telegram_id_int)
        elif status_upper == 'CANCELLED':
            await handle_subscription_cancelled(subscription_data, user, telegram_id_int)
        elif status_upper == 'REJECTED':
            await handle_subscription_rejected(subscription_data, user, telegram_id_int)
        elif status_upper == 'EXPIRED':
            await handle_subscription_expired(subscription_data, user, telegram_id_int)
        else:
            logger.warning(f"Unknown subscription status received: {status} for user {telegram_id_str}, subscription {subscription_id}")
    
    except Exception as e:
        logger.error(f"Error handling subscription status {status} for user {telegram_id_str}, subscription {subscription_id}: {e}", exc_info=True)
        # Still return success to prevent CloudPayments retries
    
    logger.info(f"Successfully processed subscription webhook for user {telegram_id_str}, subscription {subscription_id}, status {status}")
    return {"code": 0}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
