"""
Cancellation survey service for Max messenger bot.

Mirrors services/survey_service.py using maxapi keyboard types.
"""

import logging

from maxapi.types import CallbackButton, LinkButton
from maxapi.types.attachments.attachment import ButtonsPayload
from fluentogram import TranslatorRunner

from models.orm import is_first_subscription_cancellation
from services import config

logger = logging.getLogger(__name__)


def _kb(*rows: list):
    return ButtonsPayload(buttons=list(rows)).pack()


async def send_cancellation_survey(event, user: dict, i18n: TranslatorRunner) -> None:
    """
    Send cancellation survey if this is the user's first manual cancellation.

    Args:
        event: maxapi MessageCallback event
        user: User dict
        i18n: TranslatorRunner for localisation
    """
    survey_url = config.tg_bot.subscription_survey_url
    if not survey_url:
        return

    is_first = await is_first_subscription_cancellation(user['id'])
    if not is_first:
        logger.info(f"Skipping survey for user {user['telegram_id']} - not first cancellation")
        return

    survey_text = i18n.subscription_survey_message()
    survey_kb = _kb([LinkButton(text=i18n.subscription_survey_button(), url=survey_url)])

    await event.message.answer(text=survey_text, attachments=[survey_kb])
    logger.info(f"Sent cancellation survey to user {user['telegram_id']}")
