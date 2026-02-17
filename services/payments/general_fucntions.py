from fluentogram import TranslatorRunner
import stripe
from cloudpayments import CloudPayments
from models.orm import cancel_autopay, config, get_user, get_payments, add_free_days_to_subscription_db, \
    confirm_referral_process
from services.bot_provider import get_bot
from services.payments.services import create_bill_direct, create_subscription, update_cloudpayments_subscription, \
    add_free_days_to_subscription_cloudpayments, logger
from services.payments.stripe_tools import add_free_days_to_subscription_stripe, upgrade_stripe_subscription

from utils.i18n import create_translator_hub
from typing import Dict, List, Any, Optional

async def referral_need_reward(user: dict | None = None,
                               telegram_id: int | None = None) -> bool:
    """
    Checks if a user needs to be rewarded for their referral.
    """
    if user:
        telegram_id = user.get('telegram_id')

    user_payments = await get_payments(telegram_id, only_successful=True)
    if len(user_payments) <= 1:
        return True
    else:
        return False



async def complete_referral_process(user: dict):
    """
    Completes the referral process for a user.
    """

    # Check if it's first payment
    need_reward: bool = await referral_need_reward(user)
    if need_reward:
        # First payment
        referral_code = user.get('source').split('_')[1]
        referral_user = await get_user(referral_code)

        if referral_user:
            result: bool = await add_free_days_to_subscription(referral_user['telegram_id'], 7)
            if result:
                logger.debug(f"Successfully added free days to subscription for user {referral_user['telegram_id']}")

            else:
                logger.error(f"Failed to add free days to subscription for user {referral_user['telegram_id']}")
                raise Exception(f"Failed to add free days to subscription for user {referral_user['telegram_id']}")

            confirm_result = await confirm_referral_process(referrer_telegram_id=referral_user['telegram_id'],
                                                            referral_telegram_id=user['telegram_id'],
                                                            success=True if result else False)

            if confirm_result:
                logger.debug(f"Successfully confirmed referral process for user {referral_user['telegram_id']}")


                try:
                    updated_referral_user = await get_user(referral_code)
                    translator_hub = create_translator_hub()
                    i18n = translator_hub.get_translator_by_locale(locale=updated_referral_user['user_language'])
                    await get_bot().send_message(chat_id=int(updated_referral_user['telegram_id']), text=i18n.referral_reward_received(end_date=updated_referral_user['end_date']))
                except Exception as e:
                    logger.error(f"Failed to send message to user {referral_user['telegram_id']} about referral reward: {e}")
            else:
                logger.error(f"Failed to confirm referral process for user {referral_user['telegram_id']}")
                raise Exception(f"Failed to confirm referral process for user {referral_user['telegram_id']}")



async def add_free_days_to_subscription(telegram_id: int, days: int) -> bool:
    """
    Adds free days to a subscription.
    """
    user: dict = await get_user(telegram_id)
    if not user:
        logger.error(f"User not found for telegram_id: {telegram_id}")
        return False

    current_subscription_id = user.get('subscription_id')
    if not current_subscription_id:
        return await add_free_days_to_subscription_db(telegram_id, days)
    else:
        logger.info(f"Adding free days to subscription for user {telegram_id}")
        # Handle Stripe subscription
        if current_subscription_id.startswith('sub_'):
            logger.info(f"Adding free days to Stripe subscription for user {telegram_id}")
            result: Optional[Dict] | None = await add_free_days_to_subscription_stripe(days, telegram_id)
        # Handle CloudPayments subscription
        else:
            logger.info(f"Adding free days to CloudPayments subscription for user {telegram_id}")
            result: Optional[Dict] | None = await add_free_days_to_subscription_cloudpayments(days, telegram_id)

        if result:
            result: bool = await add_free_days_to_subscription_db(telegram_id, days)
            # result = await confirm_referral_process(telegram_id, result)
            return result
        else:
            logger.error(f"Failed to add free days to subscription for user {telegram_id}")
            return False


async def upgrade_subscription(telegram_id: int, subscription_type: str = None):
    """
    Upgrades a subscription to a new subscription type.
    Handles both CloudPayments and Stripe subscriptions.
    Args:
        telegram_id: The user's Telegram ID.
        subscription_type: The new subscription type to upgrade to.
    Returns:
        True if the subscription was upgraded successfully, False otherwise.
    """
    user: dict = await get_user(telegram_id)

    if not user:
        logger.error(f"User not found for telegram_id: {telegram_id}")
        return False

    current_subscription_id = user.get('subscription_id')
    if not current_subscription_id:
        logger.error(f"No subscription_id found for user {telegram_id}")
        return False

    # Handle Stripe subscription
    if current_subscription_id.startswith('sub_'):
        logger.info(f"Upgrading Stripe subscription for user {telegram_id}")
        return await upgrade_stripe_subscription(telegram_id, subscription_type)

    # Handle CloudPayments subscription
    else:
        logger.info(f"Upgrading CloudPayments subscription for user {telegram_id}")

        # Skip if it's already the target subscription type
        current_subscription_type = user.get('subscription_type')
        target_subscription_type = subscription_type or 'monthly'

        if current_subscription_type == target_subscription_type:
            logger.info(f"User {telegram_id} already has {target_subscription_type} subscription")
            return True

        # Create translator for descriptions
        translator_hub = create_translator_hub()
        i18n = translator_hub.get_translator_by_locale(locale='ru')

        # Update the CloudPayments subscription
        updated_subscription = await update_cloudpayments_subscription(
            subscription_id=current_subscription_id,
            new_subscription_type=target_subscription_type,
            i18n=i18n
        )

        if updated_subscription:
            logger.info(
                f"Successfully upgraded CloudPayments subscription for user {telegram_id} to {target_subscription_type}")
            return True
        else:
            logger.error(f"Failed to upgrade CloudPayments subscription for user {telegram_id}")
            return False


async def renew_subscription(telegram_id: int) -> str:
    user: dict = await get_user(telegram_id)

    # Handle Stripe subscription
    if user['subscription_id'] and user['subscription_id'].startswith('sub_'):
        success = await reactivate_stripe_subscription(user['subscription_id'])
        if success:
            return user['subscription_id']
        return None
    else:
        payment_data = await get_payments(telegram_id)
        for payment in payment_data[::-1]:
            if payment.token:
                token = payment.token
                break
        sub_id = create_subscription(account_id=user['telegram_id'],
                                     token=token,
                                     subscription_type=user['subscription_type'],
                                     start_date=user['end_date'])
        print(sub_id)
        return sub_id

async def cancel_subscription_payments(telegram_id: int):
    user: dict = await get_user(telegram_id)

    if user['subscription_id'].startswith('sub_'):  # Stripe subscription
        try:
            await stripe.Subscription.modify_async(id=user['subscription_id'],
                                                   cancel_at_period_end=True)
        except Exception as e:
            logger.error(f"Error canceling Stripe subscription: {e}")
    else:  # CloudPayments subscription
        client = CloudPayments(config.payment.public_id, config.payment.api_secret)
        response = client.cancel_subscription(subscription_id=user['subscription_id'])

    await cancel_autopay(telegram_id)
    return True

async def create_payment_url(user_data: dict, subscription_type: str, payment_method: str, i18n: TranslatorRunner) -> str:
    if payment_method == 'cloudpayments':
        bill_url = await create_bill_direct(account_id=user_data['telegram_id'],
                                            subscription_type=subscription_type,
                                            i18n=i18n)
    elif payment_method == 'stripe':
        from services.payments.stripe_service import create_stripe_subscription
        if 'discounted' in subscription_type:

            subscription_type = 'monthly'
            checkout_data = await create_stripe_subscription(account_id=user_data['telegram_id'],
                                                             subscription_type=subscription_type,
                                                             discount_type='notification_discount')
        else:
            checkout_data = await create_stripe_subscription(account_id=user_data['telegram_id'],
                                                        subscription_type=subscription_type)
        if checkout_data:
            bill_url = checkout_data.get('url')
        else:
            logger.error(f"Failed to create stripe subscription for user {user_data['telegram_id']} with subscription type {subscription_type}")
            raise Exception(f"Failed to create stripe subscription for user {user_data['telegram_id']} with subscription type {subscription_type}")
    else:
        logger.error(f"Invalid payment method: {payment_method}")
        raise ValueError(f"Invalid payment method: {payment_method}")
    return bill_url