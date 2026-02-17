import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from fluentogram import TranslatorRunner

from keyboards.user_keyboards import referral_program_keyboard, referral_invitation_keyboard, inline_main_menu
from models.orm import get_referral_code, get_referral_stats
from services.init_bot import bot
from states.states import ReferralSession

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data == 'referral_program')
async def process_referral_program(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    """Обрабатывает открытие меню реферальной программы"""
    await callback.answer()
    
    # Получаем реферальный код
    referral_code = await get_referral_code(user['telegram_id'])
    
    # Получаем статистику
    stats = await get_referral_stats(user['telegram_id'])
    
    # Генерируем реферальную ссылку
    bot_info = await bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start={referral_code}"
    
    # Формируем сообщение
    message_text = i18n.referral_program_menu(
        friends_invited=stats['friends_invited'],
        total_weeks_earned=stats['total_weeks_earned'],
        subscription_active_until=stats['subscription_active_until'],
        referral_link=referral_link
    )
    
    # Устанавливаем состояние
    await state.set_state(ReferralSession.viewing_referral_program)
    
    await callback.message.answer(
        text=message_text,
        reply_markup=referral_program_keyboard(i18n),
        disable_web_page_preview=True
    )


@router.callback_query(F.data == 'referral_send_invitation')
async def process_send_invitation(callback: CallbackQuery, state: FSMContext, user: dict, i18n: TranslatorRunner):
    """Обрабатывает отправку приглашения"""
    await callback.answer()


    bot_info = await bot.get_me()
    referral_code = await get_referral_code(user['telegram_id'])
    referral_link = f"https://t.me/{bot_info.username}?start={referral_code}"
    
    # Формируем сообщение приглашения
    invitation_text = i18n.referral_invitation_message(bot_link_with_text=f'<a href="{referral_link}">Whisper AI</a>')
    print(invitation_text)
    # Отправляем сообщение приглашения
    await callback.message.answer(
        text=invitation_text,
        reply_markup=referral_invitation_keyboard(i18n, referral_link)
    )
    
    await state.set_state(ReferralSession.sending_invitation)


@router.message(F.text.startswith('/referral'))
async def process_referral_command(message: Message, state: FSMContext, user: dict, i18n: TranslatorRunner):
    """Обрабатывает команду /referral для быстрого доступа к реферальной программе"""
    # Получаем реферальный код
    referral_code = await get_referral_code(user['telegram_id'])
    
    # Получаем статистику
    stats = await get_referral_stats(user['telegram_id'])
    
    # Генерируем реферальную ссылку
    bot_info = await bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start={referral_code}"
    
    # Формируем сообщение
    message_text = i18n.referral_program_menu(
        friends_invited=stats['friends_invited'],
        total_weeks_earned=stats['total_weeks_earned'],
        subscription_active_until=stats['subscription_active_until'],
        referral_link=referral_link
    )
    
    # Устанавливаем состояние
    await state.set_state(ReferralSession.viewing_referral_program)
    await state.update_data(referral_link=referral_link)
    
    await message.answer(
        text=message_text,
        reply_markup=referral_program_keyboard(i18n),
        disable_web_page_preview=True
    ) 