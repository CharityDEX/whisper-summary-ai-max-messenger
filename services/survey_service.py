import logging
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from fluentogram import TranslatorRunner

from models.orm import is_first_subscription_cancellation
from services import config

logger = logging.getLogger(__name__)


async def send_cancellation_survey(callback: CallbackQuery, user: dict, i18n: TranslatorRunner) -> None:
    """
    Отправляет форму опроса об отмене подписки, если это первая отмена пользователя.

    Проверяет через user_actions, была ли уже ручная отмена у пользователя.
    Если это первая отмена — отправляет сообщение с кнопкой на форму.

    Args:
        callback: Объект callback от Telegram
        user: Словарь с данными пользователя
        i18n: Объект TranslatorRunner для локализации
    """
    survey_url = config.tg_bot.subscription_survey_url
    if not survey_url:
        return

    # Проверяем, была ли уже ручная отмена у пользователя
    is_first = await is_first_subscription_cancellation(user['id'])
    if not is_first:
        logger.info(f"Skipping survey for user {user['telegram_id']} - not first cancellation")
        return

    # Формируем сообщение с кнопкой
    survey_text = i18n.subscription_survey_message()
    survey_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=i18n.subscription_survey_button(), url=survey_url)
    ]])

    # Отправляем опрос
    await callback.message.answer(text=survey_text, reply_markup=survey_kb)
    logger.info(f"Sent cancellation survey to user {user['telegram_id']}")


