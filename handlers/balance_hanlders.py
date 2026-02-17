import logging
from datetime import datetime
from typing import Union

import io
import asyncio

from aiogram import Router, F, types
from aiogram.filters import StateFilter, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, BufferedInputFile, InlineKeyboardButton, LinkPreviewOptions, InlineKeyboardMarkup
from fluentogram import TranslatorRunner

from keyboards.user_keyboards import inline_subscription_menu, \
    bill_keyboard, payment_methods_keyboard, sure_cancel_keyboard, \
    subscription_forward, subscription_type_keyboard, captcha_keyboard, sure_upgrade_keyboard
from models.orm import get_user, renew_subscription_db, update_subscription_details, log_user_action_async
from services import config
from services.payments.services import create_bill_direct
from services.payments.general_fucntions import cancel_subscription_payments, renew_subscription, upgrade_subscription, \
    referral_need_reward
from services.payments.stripe_service import create_stripe_subscription
from services.payments.captcha_service import create_new_captcha, verify_captcha
from services.survey_service import send_cancellation_survey
from states.states import CaptchaVerification

router = Router()
logger = logging.getLogger(__name__)

@router.callback_query(F.data == 'subscription_offer')
async def process_subscription(event: Union[Message, CallbackQuery], state: FSMContext, user: dict, i18n: TranslatorRunner):
    # Определяем, с каким типом события работаем
    if isinstance(event, CallbackQuery):
        message = event.message
        edit_message = message.edit_text
    else:
        message = event
        edit_message = message.answer

    autopay_dict = {True: i18n.enable(), False: i18n.disable()}
    user = await get_user(telegram_id=user['telegram_id'])

    # Логируем открытие меню подписки
    await log_user_action_async(
        user_id=user['id'],
        action_type='conversion_subscription_menu_opened',
        action_category='conversion',
        metadata={
            'current_subscription_status': user['subscription'],
            'has_active_subscription': user['subscription'] == 'True',
            'subscription_type': user.get('subscription_type'),
            'days_remaining': (user['end_date'] - datetime.now()).days if user.get('end_date') else None,
            'trigger_source': 'callback' if isinstance(event, CallbackQuery) else 'direct_call'
        }
    )

    # Если есть активная подписка
    if user['subscription'] == 'True':
        if user['subscription_autopay'] is None:
            status = i18n.status_active()
            await edit_message(
                text=i18n.subscription_data_old(
                    status=status,
                    end_date=user['end_date'].strftime("%Y-%m-%d")
                ),
                reply_markup=await inline_subscription_menu(i18n, user=user)
            )
        else:
            status = i18n.status_active()
            await edit_message(
                text=i18n.subscription_data(
                    status=status,
                    end_date=user['end_date'].strftime("%Y-%m-%d"),
                    auto_pay=autopay_dict[user['subscription_autopay']]
                ),
                reply_markup=await inline_subscription_menu(i18n, user=user)
            )
    
    elif user['subscription'] == 'PastDue':
        await edit_message(
            text=i18n.subscription_past_due_menu(),
            reply_markup=await inline_subscription_menu(i18n, user=user)
        )
    # Если нет активной подписки - показываем выбор способа оплаты
    else:
        await edit_message(
            text=i18n.subscription_forward(),
            reply_markup=subscription_forward(i18n)
        )

@router.callback_query(F.data == 'payment_methods')
async def process_payment_methods(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):

    await callback.message.edit_text(
        text=i18n.payment_method_menu(),
        reply_markup=payment_methods_keyboard(i18n)
    )

@router.callback_query(F.data == 'buy_subscription')
async def process_buy_subscription(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    await process_subscription(callback, state, user, i18n)


@router.callback_query(F.data.startswith('payment_method|'))
async def process_payment_method(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):

    payment_method = callback.data.split('|')[1]

    # Логируем выбор способа оплаты
    await log_user_action_async(
        user_id=user['id'],
        action_type='conversion_payment_method_selected',
        action_category='conversion',
        metadata={
            'payment_method': payment_method,
            'available_types': ['weekly', 'monthly', 'semiannual'],
            'trigger_source': 'payment_method_callback',
            'has_active_subscription': user.get('subscription') == 'True'
        }
    )

    await callback.message.edit_text(
        text=i18n.choose_subscription_type(),
        reply_markup=subscription_type_keyboard(i18n, payment_method)
    )


@router.callback_query(F.data == 'cancel_subscription')
async def process_cancel_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    await callback.message.edit_text(text=i18n.confirm_subscription_cancellation(),
                                     reply_markup=sure_cancel_keyboard(i18n))


@router.callback_query(F.data == 'sure_cancel')
async def process_sure_cancel(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    try:
        result = await cancel_subscription_payments(callback.from_user.id)
    except:
        await callback.message.edit_text(text=i18n.something_went_wrong())
        return
    # Если подписка активна, то просто отменяем платежи. Если просрочена, то отменяем подписку и платежи.
    if user['subscription'] == 'PastDue':
        await update_subscription_details(telegram_id=user['telegram_id'], subscription_status='False', subscription_id=None, start_date_dt=None, end_date_dt=None, is_autopay_active=None)
    await callback.message.edit_text(text=i18n.subscription_canceled())

    # Отправляем опрос Typeform только при первой ручной отмене (проверка внутри функции)
    survey_shown = False
    try:
        await send_cancellation_survey(callback, user, i18n)
        survey_shown = True
    except Exception as e:
        logger.exception(f"Не удалось отправить форму отмены подписки: {e}")

    # Логируем отмену подписки в user_actions
    await log_user_action_async(
        user_id=user['id'],
        action_type='subscription_cancelled_by_user',
        action_category='subscription',
        metadata={
            'subscription_type': user.get('subscription_type'),
            'subscription_id': user.get('subscription_id'),
            'reason': 'user_initiated',
            'previous_status': user.get('subscription'),
            'survey_shown': survey_shown
        }
    )


@router.callback_query(F.data.startswith('create_bill|'))
async def process_create_bill(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    payment_type, subscription_type = callback.data.split('|')[-2:]

    # If payment type is stripe, show CAPTCHA verification first
    # if payment_type == 'stripe':
        # try:
        #     # Generate CAPTCHA
        #     captcha_text, captcha_image = create_new_captcha()
        #
        #     # Log the CAPTCHA text for debugging
        #     print(f"Generated CAPTCHA for user {callback.from_user.id}: {captcha_text}")
        #
        #     # Send CAPTCHA image
        #     captcha_photo = BufferedInputFile(
        #         captcha_image,
        #         filename="captcha.png"
        #     )
        #
        #     # Set state to waiting for CAPTCHA
        #     await state.set_state(CaptchaVerification.waiting_for_captcha)
        #
        #     # Store payment details and captcha text in state
        #     await state.update_data(
        #         payment_type=payment_type,
        #         subscription_type=subscription_type,
        #         captcha_text=captcha_text,
        #         attempts=0
        #     )
        #
        #     # Send CAPTCHA message
        #     captcha_message = await callback.message.answer_photo(
        #         photo=captcha_photo,
        #         caption=i18n.captcha_verification(),
        #         reply_markup=captcha_keyboard(i18n, payment_type, subscription_type)
        #     )
        #
        #     # Store the captcha message ID in state
        #     await state.update_data(captcha_message_id=captcha_message.message_id)
        #
        #     # Delete the original message with payment options to keep the chat clean
        #     try:
        #         await callback.message.delete()
        #     except Exception as e:
        #         print(f"Could not delete original message: {e}")
        #
        #     return
        # except Exception as e:
        #     print(f"Error generating CAPTCHA: {e}")
        #     # If CAPTCHA generation fails, proceed with normal payment flow
    
    # For other payment methods, proceed as usual
    order_labels = {
            'monthly': i18n.monthly_subscription_lable(),
            'semiannual': i18n.semiannual_subscription_lable(),
            'weekly': i18n.weekly_subscription_lable(),
    }
    pricing_labels = {
        'stripe': {
            'monthly': i18n.montly_pricing_stripe(price=config.stripe.price.monthly),
            'semiannual': i18n.semiannual_pricing_stripe(price=config.stripe.price.semiannual),
            'weekly': i18n.weekly_pricing_stripe(price=config.stripe.price.weekly)
        },
        'cloudpayments': {
            'monthly': i18n.montly_prising_cloudpayments(price=config.cloudpayments.price.monthly),
            'semiannual': i18n.semiannual_pricing_cloudpayments(price=config.cloudpayments.price.semiannual),
            'weekly': i18n.weekly_pricing_cloudpayments(price=config.cloudpayments.price.weekly)
        }
    }
    text = i18n.make_payment_description(order_option=order_labels[subscription_type], prising=pricing_labels[payment_type][subscription_type])

    # Определяем amount и currency в зависимости от payment_type
    if payment_type == 'stripe':
        amount_map = {'weekly': config.stripe.price.weekly, 'monthly': config.stripe.price.monthly, 'semiannual': config.stripe.price.semiannual}
        amount = amount_map.get(subscription_type, 0)
        currency = 'USD'
    else:
        amount_map = {'weekly': config.cloudpayments.price.weekly, 'monthly': config.cloudpayments.price.monthly, 'semiannual': config.cloudpayments.price.semiannual}
        amount = amount_map.get(subscription_type, 0)
        currency = 'RUB'

    if payment_type == 'stripe':
        checkout_data = await create_stripe_subscription(
            account_id=str(user['telegram_id']),
            subscription_type=subscription_type
        )
        if checkout_data:
            # Логируем создание платежной ссылки (КРИТИЧНАЯ ТОЧКА!)
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
                    'link_created_successfully': True
                }
            )
            await callback.message.edit_text(
                text=text,
                reply_markup=bill_keyboard(i18n, checkout_data['url'], oferta_confirm=False, payment_method='stripe')
            )
        else:
            await callback.message.edit_text(text=i18n.payment_error())
    else:  # CloudPayments
        # CloudPayments flow requires oferta confirmation
        bill_url = await create_bill_direct(subscription_type=subscription_type, account_id=user['telegram_id'], i18n=i18n)
        # Логируем создание платежной ссылки (КРИТИЧНАЯ ТОЧКА!)
        # Для CloudPayments ссылка создана, но недоступна до подтверждения оферты
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
                'oferta_confirmed': False,  # Важно: ссылка создана, но оферта не подтверждена
                'link_created_successfully': True
            }
        )
        text += '\n' + i18n.read_offerta()
        await callback.message.edit_text(
            text=text,
            reply_markup=bill_keyboard(i18n, bill_url, oferta_confirm=False, payment_method='cloudpayments'),
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )

@router.message(CaptchaVerification.waiting_for_captcha, F.text)
async def process_captcha_verification(message: Message, state: FSMContext, i18n: TranslatorRunner, user: dict):
    """Handle CAPTCHA verification"""
    try:
        # Get user input
        user_input = message.text
        
        # Get state data including the previous captcha message
        data = await state.get_data()
        payment_type = data.get('payment_type')
        subscription_type = data.get('subscription_type')
        captcha_message_id = data.get('captcha_message_id')
        captcha_text = data.get('captcha_text')
        attempts = data.get('attempts', 0)
        
        # Try to delete the user's input message to keep the chat clean
        try:
            await message.delete()
        except Exception as e:
            print(f"Could not delete user message: {e}")
        
        if not user_input:
            # If the message doesn't contain text, generate new CAPTCHA
            captcha_text, captcha_image = create_new_captcha()
            print(f"Generated new CAPTCHA after invalid input for user {message.from_user.id}: {captcha_text}")
            
            # Send new CAPTCHA image
            captcha_photo = BufferedInputFile(
                captcha_image,
                filename="captcha.png"
            )
            
            # Delete previous captcha message if exists
            if captcha_message_id:
                try:
                    await message.bot.delete_message(chat_id=message.chat.id, message_id=captcha_message_id)
                except Exception as e:
                    print(f"Could not delete previous captcha message: {e}")
            
            # Send error message with new CAPTCHA
            new_captcha_message = await message.answer_photo(
                photo=captcha_photo,
                caption=i18n.captcha_verification(),
                reply_markup=captcha_keyboard(i18n, payment_type, subscription_type)
            )
            
            # Store the new captcha message ID and text in state
            await state.update_data(
                captcha_message_id=new_captcha_message.message_id,
                captcha_text=captcha_text
            )
            return
        
        # Increment attempt counter
        attempts += 1
        await state.update_data(attempts=attempts)
        
        # Log the verification attempt
        print(f"Verifying CAPTCHA for user {message.from_user.id}. Input: {user_input}, Attempt: {attempts}")
        
        # Verify CAPTCHA
        if verify_captcha(user_input, captcha_text):
            # Delete previous captcha message if exists
            if captcha_message_id:
                try:
                    await message.bot.delete_message(chat_id=message.chat.id, message_id=captcha_message_id)
                except Exception as e:
                    print(f"Could not delete previous captcha message: {e}")
            
            # CAPTCHA is correct, proceed with payment
            success_message = await message.answer(i18n.captcha_success())
            
            # Clear state
            await state.clear()
            
            # Process Stripe payment
            order_labels = {
                'monthly': i18n.monthly_subscription_lable(),
                'weekly': i18n.weekly_subscription_lable(),
            }
            pricing_labels = {
                'stripe': {'monthly': i18n.montly_pricing_stripe(),
                          'weekly': i18n.weekly_pricing_stripe()},
            }
            text = i18n.make_payment_description(
                order_option=order_labels[subscription_type], 
                prising=pricing_labels[payment_type][subscription_type]
            )
            checkout_data = await create_stripe_subscription(
                account_id=str(user['telegram_id']),
                subscription_type=subscription_type
            )
            
            if checkout_data:
                # Delete success message after a short delay
                try:
                    await asyncio.sleep(2)  # Wait 2 seconds before deleting the success message
                    await success_message.delete()
                except Exception as e:
                    print(f"Could not delete success message: {e}")
                
                await message.answer(
                    text=text,
                    reply_markup=bill_keyboard(i18n, checkout_data['url'], oferta_confirm=False, payment_method='stripe')
                )
            else:
                await message.answer(text=i18n.payment_error())
        else:
            # CAPTCHA is incorrect, generate a new one
            captcha_text, captcha_image = create_new_captcha()
            print(f"Generated new CAPTCHA after failed verification for user {message.from_user.id}: {captcha_text}")
            
            # Send new CAPTCHA image
            captcha_photo = BufferedInputFile(
                captcha_image,
                filename="captcha.png"
            )
            
            # Delete previous captcha message if exists
            if captcha_message_id:
                try:
                    await message.bot.delete_message(chat_id=message.chat.id, message_id=captcha_message_id)
                except Exception as e:
                    print(f"Could not delete previous captcha message: {e}")
            
            # Send error message with new CAPTCHA
            new_captcha_message = await message.answer_photo(
                photo=captcha_photo,
                caption=i18n.captcha_incorrect(),
                reply_markup=captcha_keyboard(i18n, payment_type, subscription_type)
            )
            
            # Store the new captcha message ID and text in state
            await state.update_data(
                captcha_message_id=new_captcha_message.message_id,
                captcha_text=captcha_text
            )
    except Exception as e:
        print(f"Error in CAPTCHA verification: {e}")
        await message.answer(text=i18n.something_went_wrong())
        await state.clear()

@router.callback_query(F.data.startswith('refresh_captcha|'))
async def process_refresh_captcha(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    """Handle CAPTCHA refresh button"""
    try:
        # Get payment details from callback data
        _, payment_type, subscription_type = callback.data.split('|')
        
        # Generate new CAPTCHA
        captcha_text, captcha_image = create_new_captcha()
        
        # Log the CAPTCHA text for debugging
        print(f"Refreshed CAPTCHA for user {callback.from_user.id}: {captcha_text}")
        
        # Send new CAPTCHA image
        captcha_photo = BufferedInputFile(
            captcha_image,
            filename="captcha.png"
        )
        
        # Update message with new CAPTCHA
        await callback.message.edit_media(
            types.InputMediaPhoto(
                media=captcha_photo,
                caption=i18n.captcha_verification()
            ),
            reply_markup=captcha_keyboard(i18n, payment_type, subscription_type)
        )
        
        # Store the updated captcha message ID and text in state
        await state.update_data(
            captcha_message_id=callback.message.message_id,
            captcha_text=captcha_text,
            attempts=0  # Reset attempts counter on refresh
        )
        
    except Exception as e:
        print(f"Error refreshing CAPTCHA: {e}")
        await callback.answer(text=i18n.something_went_wrong())

@router.callback_query(F.data.startswith('oferta_status|'))
async def process_oferta_status(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    oferta_confirm = callback.data.split('|')[-1]
    if oferta_confirm == 'True':
        oferta_confirm = False
    else:
        oferta_confirm = True

    # Логируем подтверждение/снятие подтверждения оферты (только для CloudPayments)
    if oferta_confirm:
        await log_user_action_async(
            user_id=user['id'],
            action_type='conversion_oferta_confirmed',
            action_category='conversion',
            metadata={
                'payment_method': 'cloudpayments',
                'oferta_confirmed': True,
                'trigger_source': 'oferta_checkbox',
                'has_active_subscription': user.get('subscription') == 'True'
            }
        )

    new_button_offerta = InlineKeyboardButton(text=i18n.oferta_button(oferta_status='✅' if oferta_confirm else '❌'), callback_data=f'oferta_status|{oferta_confirm}')
    if oferta_confirm:
        bill_button = InlineKeyboardButton(text=i18n.pay_button(), url=callback.message.reply_markup.inline_keyboard[1][0].callback_data.split('|')[1])
    else:
        bill_button = InlineKeyboardButton(text=i18n.pay_button(), callback_data=f'url|{callback.message.reply_markup.inline_keyboard[1][0].url}')

    new_markup = callback.message.reply_markup
    new_markup.inline_keyboard[0][0] = new_button_offerta
    new_markup.inline_keyboard[1][0] = bill_button
    await callback.message.edit_reply_markup(reply_markup=new_markup)

@router.callback_query(F.data.startswith('url|'))
async def process_url(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    await callback.answer(text=i18n.confirm_oferta())


@router.callback_query(F.data == 'renew_subscription')
async def process_renew_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    try:
        subscription_id: str = await renew_subscription(callback.from_user.id)
        if subscription_id:
            await renew_subscription_db(callback.from_user.id, subscription_id)
            await callback.message.edit_text(text=i18n.payment_renewed())
        else:
            # If renewal failed, show payment methods menu
            await callback.message.edit_text(
                text=i18n.subscription_forward(),
                reply_markup=subscription_forward(i18n)
            )
    except Exception as e:
        print(f'Ошибка при продлении подписки: {e}')
        await callback.message.edit_text(text=i18n.payment_renewed_error())

#
# @router.callback_query(F.data.startswith('choose_subscription|'))
# async def process_buy_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
#     subscription_type = callback.data.split('|')[-1]
#     await callback.message.edit_text(text='где брать',
#                                      reply_markup=payment_methods_keyboard(i18n, subscription_type))

@router.callback_query(F.data == 'payment_methods', CaptchaVerification.waiting_for_captcha)
async def process_cancel_captcha(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner):
    """Handle cancellation of CAPTCHA verification"""
    try:
        # Get the captcha message ID from state
        data = await state.get_data()
        captcha_message_id = data.get('captcha_message_id')
        
        # Clear state (this will also clear the captcha data)
        await state.clear()
        
        # Delete the captcha message
        if captcha_message_id and callback.message.message_id == captcha_message_id:
            # If the callback is from the captcha message itself, we'll show payment methods in a new message
            # and then delete the captcha message
            await callback.message.answer(
                text=i18n.payment_method_menu(),
                reply_markup=payment_methods_keyboard(i18n)
            )
            try:
                await callback.message.delete()
            except Exception as e:
                print(f"Could not delete captcha message: {e}")
        else:
            # If the callback is from another message, just show payment methods
            await callback.message.edit_text(
                text=i18n.payment_method_menu(),
                reply_markup=payment_methods_keyboard(i18n)
            )
            
            # Try to delete the captcha message if it exists
            if captcha_message_id:
                try:
                    await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=captcha_message_id)
                except Exception as e:
                    print(f"Could not delete captcha message: {e}")
    except Exception as e:
        print(f"Error canceling CAPTCHA: {e}")
        await callback.answer(text=i18n.something_went_wrong())

@router.message(CaptchaVerification.waiting_for_captcha)
async def process_non_text_captcha(message: Message, state: FSMContext, i18n: TranslatorRunner):
    """Handle non-text messages during CAPTCHA verification"""
    try:
        # Get state data including the previous captcha message
        data = await state.get_data()
        payment_type = data.get('payment_type')
        subscription_type = data.get('subscription_type')
        captcha_message_id = data.get('captcha_message_id')
        attempts = data.get('attempts', 0)
        
        # Increment attempt counter
        attempts += 1
        await state.update_data(attempts=attempts)
        
        # Try to delete the user's message to keep the chat clean
        try:
            await message.delete()
        except Exception as e:
            print(f"Could not delete user message: {e}")
        
        # Generate new CAPTCHA
        captcha_text, captcha_image = create_new_captcha()
        print(f"Generated new CAPTCHA after non-text input for user {message.from_user.id}: {captcha_text}")
        
        # Send new CAPTCHA image
        captcha_photo = BufferedInputFile(
            captcha_image,
            filename="captcha.png"
        )
        
        # Delete previous captcha message if exists
        if captcha_message_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=captcha_message_id)
            except Exception as e:
                print(f"Could not delete previous captcha message: {e}")
        
        # Send error message with new CAPTCHA
        new_captcha_message = await message.answer_photo(
            photo=captcha_photo,
            caption=i18n.captcha_incorrect(),
            reply_markup=captcha_keyboard(i18n, payment_type, subscription_type)
        )
        
        # Store the new captcha message ID and text in state
        await state.update_data(
            captcha_message_id=new_captcha_message.message_id,
            captcha_text=captcha_text
        )
    except Exception as e:
        print(f"Error handling non-text message during CAPTCHA: {e}")
        await message.answer(text=i18n.something_went_wrong())


@router.callback_query(F.data.startswith('upgrade_subscription|'))
async def process_upgrade_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    subscription_type = callback.data.split('|')[-1]
    await state.update_data(new_subscription_type=subscription_type)
    if subscription_type == 'to_monthly' and user['subscription_type'] == 'weekly':
        await callback.message.edit_text(text=i18n.upgrade_subscription_from_weekly_to_monthly(),
                                         reply_markup=sure_upgrade_keyboard(i18n))
    elif subscription_type == 'to_semiannual' and user['subscription_type'] == 'weekly':
        await callback.message.edit_text(text=i18n.upgrade_subscription_from_weekly_to_semiannual(),
                                         reply_markup=sure_upgrade_keyboard(i18n))
    elif subscription_type == 'to_semiannual' and user['subscription_type'] == 'monthly':
        await callback.message.edit_text(text=i18n.upgrade_subscription_from_monthly_to_semiannual(),
                                         reply_markup=sure_upgrade_keyboard(i18n))
    else:
        print('aboba')
    
@router.callback_query(F.data == 'sure_upgrade_subscription')
async def process_sure_upgrade_subscription(callback: CallbackQuery, state: FSMContext, i18n: TranslatorRunner, user: dict):
    data = await state.get_data()
    new_subscription_type = data.get('new_subscription_type')
    if new_subscription_type:
        new_subscription_type = new_subscription_type.lstrip('to_')
        await state.update_data(new_subscription_type=None)
    else:
        await callback.message.edit_text(text=i18n.upgrade_subscription_error())
        return

    result = await upgrade_subscription(user['telegram_id'], new_subscription_type)
    logger.info(f'SUBSCRIPTION UPGRADE: SUCCESS. User: {user["telegram_id"]}. New sub_type: {new_subscription_type}')

    if result:
        if new_subscription_type == 'monthly':
            await callback.message.edit_text(text=i18n.upgraded_subscription_from_weekly_to_monthly())
        elif new_subscription_type == 'semiannual':
            await callback.message.edit_text(text=i18n.upgraded_subscription_to_semiannual())
    else:
        await callback.message.edit_text(text=i18n.upgrade_subscription_error())