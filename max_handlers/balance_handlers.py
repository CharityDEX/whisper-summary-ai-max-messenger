import logging
from datetime import datetime

from maxapi import Router, F
from maxapi.context import MemoryContext
from maxapi.types import MessageCreated, MessageCallback
from maxapi.enums.parse_mode import ParseMode
from fluentogram import TranslatorRunner

from max_keyboards.user_keyboards import (
    inline_subscription_menu, bill_keyboard, payment_methods_keyboard,
    sure_cancel_keyboard, subscription_forward, subscription_type_keyboard,
    sure_upgrade_keyboard,
)
from models.orm import get_user, renew_subscription_db, update_subscription_details, log_user_action_async
from services.init_max_bot import config
from services.payments.services import create_bill_direct
from services.payments.general_fucntions import cancel_subscription_payments, renew_subscription, upgrade_subscription
from services.payments.stripe_service import create_stripe_subscription

router = Router()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subscription menu
# ---------------------------------------------------------------------------

async def _show_subscription_menu(user_id: int, message, user: dict, i18n: TranslatorRunner, is_edit: bool = False):
    """Shared logic for showing the subscription menu."""
    autopay_dict = {True: i18n.enable(), False: i18n.disable()}
    user = await get_user(telegram_id=user_id)

    await log_user_action_async(
        user_id=user['id'],
        action_type='conversion_subscription_menu_opened',
        action_category='conversion',
        metadata={
            'current_subscription_status': user['subscription'],
            'has_active_subscription': user['subscription'] == 'True',
            'subscription_type': user.get('subscription_type'),
            'days_remaining': (user['end_date'] - datetime.now()).days if user.get('end_date') else None,
            'trigger_source': 'callback' if is_edit else 'direct_call',
        },
    )

    keyboard = None

    if user['subscription'] == 'True':
        if user['subscription_autopay'] is None:
            status = i18n.status_active()
            text = i18n.subscription_data_old(
                status=status,
                end_date=user['end_date'].strftime("%Y-%m-%d"),
            )
        else:
            status = i18n.status_active()
            text = i18n.subscription_data(
                status=status,
                end_date=user['end_date'].strftime("%Y-%m-%d"),
                auto_pay=autopay_dict[user['subscription_autopay']],
            )
        keyboard = await inline_subscription_menu(i18n, user=user)
    elif user['subscription'] == 'PastDue':
        text = i18n.subscription_past_due_menu()
        keyboard = await inline_subscription_menu(i18n, user=user)
    else:
        text = i18n.subscription_forward()
        keyboard = subscription_forward(i18n)

    attachments = [keyboard] if keyboard else []
    if is_edit:
        await message.edit(text=text, attachments=attachments)
    else:
        await message.answer(text=text, attachments=attachments)


@router.message_callback(F.callback.payload == 'subscription_menu')
@router.message_callback(F.callback.payload == 'subscription_offer')
async def process_subscription(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    await _show_subscription_menu(
        user_id=event.callback.user.user_id,
        message=event.message,
        user=user,
        i18n=i18n,
        is_edit=True,
    )


@router.message_callback(F.callback.payload == 'buy_subscription')
async def process_buy_subscription(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    await _show_subscription_menu(
        user_id=event.callback.user.user_id,
        message=event.message,
        user=user,
        i18n=i18n,
        is_edit=True,
    )


# ---------------------------------------------------------------------------
# Payment methods
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'payment_methods')
async def process_payment_methods(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await event.message.edit(
        text=i18n.payment_method_menu(),
        attachments=[payment_methods_keyboard(i18n)],
    )


@router.message_callback(F.callback.payload.startswith('payment_method|'))
async def process_payment_method(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    payment_method = event.callback.payload.split('|')[1]

    await log_user_action_async(
        user_id=user['id'],
        action_type='conversion_payment_method_selected',
        action_category='conversion',
        metadata={
            'payment_method': payment_method,
            'available_types': ['weekly', 'monthly', 'semiannual'],
            'trigger_source': 'payment_method_callback',
            'has_active_subscription': user.get('subscription') == 'True',
        },
    )

    await event.message.edit(
        text=i18n.choose_subscription_type(),
        attachments=[subscription_type_keyboard(i18n, payment_method)],
    )


# ---------------------------------------------------------------------------
# Cancel subscription
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'cancel_subscription')
async def process_cancel_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    await event.message.edit(
        text=i18n.confirm_subscription_cancellation(),
        attachments=[sure_cancel_keyboard(i18n)],
    )


@router.message_callback(F.callback.payload == 'sure_cancel')
async def process_sure_cancel(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    try:
        await cancel_subscription_payments(event.callback.user.user_id)
    except Exception:
        await event.message.edit(text=i18n.something_went_wrong())
        return

    if user['subscription'] == 'PastDue':
        await update_subscription_details(
            telegram_id=user['telegram_id'],
            subscription_status='False',
            subscription_id=None,
            start_date_dt=None,
            end_date_dt=None,
            is_autopay_active=None,
        )
    await event.message.edit(text=i18n.subscription_canceled())

    from services.max_survey_service import send_cancellation_survey
    await send_cancellation_survey(event, user=user, i18n=i18n)

    await log_user_action_async(
        user_id=user['id'],
        action_type='subscription_cancelled_by_user',
        action_category='subscription',
        metadata={
            'subscription_type': user.get('subscription_type'),
            'subscription_id': user.get('subscription_id'),
            'reason': 'user_initiated',
            'previous_status': user.get('subscription'),
            'survey_shown': True,
        },
    )


# ---------------------------------------------------------------------------
# Create bill / payment
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('create_bill|'))
async def process_create_bill(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    payment_type, subscription_type = event.callback.payload.split('|')[-2:]

    order_labels = {
        'monthly': i18n.monthly_subscription_lable(),
        'semiannual': i18n.semiannual_subscription_lable(),
        'weekly': i18n.weekly_subscription_lable(),
    }
    pricing_labels = {
        'stripe': {
            'monthly': i18n.montly_pricing_stripe(price=config.stripe.price.monthly),
            'semiannual': i18n.semiannual_pricing_stripe(price=config.stripe.price.semiannual),
            'weekly': i18n.weekly_pricing_stripe(price=config.stripe.price.weekly),
        },
        'cloudpayments': {
            'monthly': i18n.montly_prising_cloudpayments(price=config.cloudpayments.price.monthly),
            'semiannual': i18n.semiannual_pricing_cloudpayments(price=config.cloudpayments.price.semiannual),
            'weekly': i18n.weekly_pricing_cloudpayments(price=config.cloudpayments.price.weekly),
        },
    }
    text = i18n.make_payment_description(
        order_option=order_labels[subscription_type],
        prising=pricing_labels[payment_type][subscription_type],
    )

    if payment_type == 'stripe':
        amount_map = {
            'weekly': config.stripe.price.weekly,
            'monthly': config.stripe.price.monthly,
            'semiannual': config.stripe.price.semiannual,
        }
        amount = amount_map.get(subscription_type, 0)
        currency = 'USD'
    else:
        amount_map = {
            'weekly': config.cloudpayments.price.weekly,
            'monthly': config.cloudpayments.price.monthly,
            'semiannual': config.cloudpayments.price.semiannual,
        }
        amount = amount_map.get(subscription_type, 0)
        currency = 'RUB'

    if payment_type == 'stripe':
        checkout_data = await create_stripe_subscription(
            account_id=str(user['telegram_id']),
            subscription_type=subscription_type,
        )
        if checkout_data:
            await log_user_action_async(
                user_id=user['id'],
                action_type='conversion_payment_link_created',
                action_category='conversion',
                metadata={
                    'payment_method': 'stripe',
                    'subscription_type': subscription_type,
                    'amount': amount,
                    'currency': currency,
                    'checkout_session_id': checkout_data.get('session_id'),
                    'trigger_source': 'create_bill_callback',
                    'has_active_subscription': user.get('subscription') == 'True',
                    'link_created_successfully': True,
                },
            )
            await event.message.edit(
                text=text,
                attachments=[bill_keyboard(i18n, checkout_data['url'], oferta_confirm=False, payment_method='stripe')],
            )
        else:
            await event.message.edit(text=i18n.payment_error())
    else:
        # CloudPayments
        bill_url = await create_bill_direct(
            subscription_type=subscription_type,
            account_id=user['telegram_id'],
            i18n=i18n,
        )
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_payment_link_created',
            action_category='conversion',
            metadata={
                'payment_method': 'cloudpayments',
                'subscription_type': subscription_type,
                'amount': amount,
                'currency': currency,
                'trigger_source': 'create_bill_callback',
                'has_active_subscription': user.get('subscription') == 'True',
                'oferta_confirmed': False,
                'link_created_successfully': True,
            },
        )
        # Store bill_url in context so the oferta toggle handler can rebuild the keyboard
        await context.update_data(
            bill_url=bill_url,
            bill_payment_method='cloudpayments',
            bill_text=text,
        )
        text += '\n' + i18n.read_offerta()
        await event.message.edit(
            text=text,
            attachments=[bill_keyboard(i18n, bill_url, oferta_confirm=False, payment_method='cloudpayments')],
        )


# ---------------------------------------------------------------------------
# Oferta toggle (CloudPayments only)
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('oferta_status|'))
async def process_oferta_status(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    oferta_confirm_str = event.callback.payload.split('|')[-1]
    # Toggle: if was True, now False and vice versa
    oferta_confirm = oferta_confirm_str != 'True'

    if oferta_confirm:
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_oferta_confirmed',
            action_category='conversion',
            metadata={
                'payment_method': 'cloudpayments',
                'oferta_confirmed': True,
                'trigger_source': 'oferta_checkbox',
                'has_active_subscription': user.get('subscription') == 'True',
            },
        )

    # Retrieve the bill URL and text from context (stored when bill was created)
    data = await context.get_data()
    bill_url = data.get('bill_url', '')
    payment_method = data.get('bill_payment_method', 'cloudpayments')
    bill_text = data.get('bill_text', '')

    display_text = bill_text + '\n' + i18n.read_offerta() if bill_text else ''

    await event.message.edit(
        text=display_text,
        attachments=[bill_keyboard(i18n, bill_url, oferta_confirm=oferta_confirm, payment_method=payment_method)],
    )


@router.message_callback(F.callback.payload.startswith('url|'))
async def process_url(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    """User clicked pay button without confirming oferta."""
    await event.answer()
    # In aiogram, callback.answer(text=...) shows a popup. Max doesn't support popup notifications
    # on callback answer, so we send a brief message instead.
    await event.message.answer(text=i18n.confirm_oferta())


# ---------------------------------------------------------------------------
# Renew subscription
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload == 'renew_subscription')
async def process_renew_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner):
    try:
        subscription_id: str = await renew_subscription(event.callback.user.user_id)
        if subscription_id:
            await renew_subscription_db(event.callback.user.user_id, subscription_id)
            await event.message.edit(text=i18n.payment_renewed())
        else:
            await event.message.edit(
                text=i18n.subscription_forward(),
                attachments=[subscription_forward(i18n)],
            )
    except Exception as e:
        logger.error(f'Error renewing subscription: {e}')
        await event.message.edit(text=i18n.payment_renewed_error())


# ---------------------------------------------------------------------------
# Upgrade subscription
# ---------------------------------------------------------------------------

@router.message_callback(F.callback.payload.startswith('upgrade_subscription|'))
async def process_upgrade_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    subscription_type = event.callback.payload.split('|')[-1]
    await context.update_data(new_subscription_type=subscription_type)

    if subscription_type == 'to_monthly' and user['subscription_type'] == 'weekly':
        await event.message.edit(
            text=i18n.upgrade_subscription_from_weekly_to_monthly(),
            attachments=[sure_upgrade_keyboard(i18n)],
        )
    elif subscription_type == 'to_semiannual' and user['subscription_type'] == 'weekly':
        await event.message.edit(
            text=i18n.upgrade_subscription_from_weekly_to_semiannual(),
            attachments=[sure_upgrade_keyboard(i18n)],
        )
    elif subscription_type == 'to_semiannual' and user['subscription_type'] == 'monthly':
        await event.message.edit(
            text=i18n.upgrade_subscription_from_monthly_to_semiannual(),
            attachments=[sure_upgrade_keyboard(i18n)],
        )


@router.message_callback(F.callback.payload == 'sure_upgrade_subscription')
async def process_sure_upgrade_subscription(event: MessageCallback, context: MemoryContext, i18n: TranslatorRunner, user: dict):
    data = await context.get_data()
    new_subscription_type = data.get('new_subscription_type')
    if new_subscription_type:
        new_subscription_type = new_subscription_type.lstrip('to_')
        await context.update_data(new_subscription_type=None)
    else:
        await event.message.edit(text=i18n.upgrade_subscription_error())
        return

    result = await upgrade_subscription(user['telegram_id'], new_subscription_type)
    logger.info(f'SUBSCRIPTION UPGRADE: SUCCESS. User: {user["telegram_id"]}. New sub_type: {new_subscription_type}')

    if result:
        if new_subscription_type == 'monthly':
            await event.message.edit(text=i18n.upgraded_subscription_from_weekly_to_monthly())
        elif new_subscription_type == 'semiannual':
            await event.message.edit(text=i18n.upgraded_subscription_to_semiannual())
    else:
        await event.message.edit(text=i18n.upgrade_subscription_error())


# ---------------------------------------------------------------------------
# CAPTCHA handlers — intentionally omitted
# ---------------------------------------------------------------------------
# The CAPTCHA trigger in process_create_bill is already commented out in the
# Telegram version. The CAPTCHA handlers use answer_photo/edit_media which
# require a different approach in Max (photo attachments instead of dedicated
# photo methods). They will be ported if/when the CAPTCHA feature is re-enabled.
