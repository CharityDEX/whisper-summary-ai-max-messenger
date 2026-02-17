import logging

from maxapi import Router, F
from maxapi.context import MemoryContext
from maxapi.types import MessageCreated, MessageCallback
from fluentogram import TranslatorRunner

from max_keyboards.user_keyboards import referral_program_keyboard, referral_invitation_keyboard
from models.orm import get_referral_code, get_referral_stats
from services.init_max_bot import max_bot
from max_states.states import ReferralSession

logger = logging.getLogger(__name__)
router = Router()


async def _get_referral_link(user_telegram_id: int) -> str:
    """Build the referral link for Max messenger."""
    referral_code = await get_referral_code(user_telegram_id)
    bot_info = max_bot.me or await max_bot.get_me()
    bot_username = bot_info.username or bot_info.first_name
    return f"https://max.ru/bot/{bot_username}?start={referral_code}"


@router.message_callback(F.callback.payload == 'referral_program')
async def process_referral_program(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    await event.answer()

    referral_link = await _get_referral_link(user['telegram_id'])
    stats = await get_referral_stats(user['telegram_id'])

    message_text = i18n.referral_program_menu(
        friends_invited=stats['friends_invited'],
        total_weeks_earned=stats['total_weeks_earned'],
        subscription_active_until=stats['subscription_active_until'],
        referral_link=referral_link,
    )

    await context.set_state(ReferralSession.viewing_referral_program)

    await event.message.answer(
        text=message_text,
        attachments=[referral_program_keyboard(i18n)],
    )


@router.message_callback(F.callback.payload == 'referral_send_invitation')
async def process_send_invitation(event: MessageCallback, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    await event.answer()

    referral_link = await _get_referral_link(user['telegram_id'])

    invitation_text = i18n.referral_invitation_message(
        bot_link_with_text=f'<a href="{referral_link}">Whisper AI</a>',
    )

    await event.message.answer(
        text=invitation_text,
        attachments=[referral_invitation_keyboard(i18n, referral_link)],
    )

    await context.set_state(ReferralSession.sending_invitation)


@router.message_created(F.message.body.text.startswith('/referral'))
async def process_referral_command(event: MessageCreated, context: MemoryContext, user: dict, i18n: TranslatorRunner):
    referral_link = await _get_referral_link(user['telegram_id'])
    stats = await get_referral_stats(user['telegram_id'])

    message_text = i18n.referral_program_menu(
        friends_invited=stats['friends_invited'],
        total_weeks_earned=stats['total_weeks_earned'],
        subscription_active_until=stats['subscription_active_until'],
        referral_link=referral_link,
    )

    await context.set_state(ReferralSession.viewing_referral_program)
    await context.update_data(referral_link=referral_link)

    await event.message.answer(
        text=message_text,
        attachments=[referral_program_keyboard(i18n)],
    )
