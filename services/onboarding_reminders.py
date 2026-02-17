import logging
import asyncio
from datetime import datetime

from fluentogram import TranslatorRunner

from config_data.config import get_config
from models.orm import (
    get_users_for_onboarding_day1,
    get_users_for_onboarding_day3,
    get_users_for_first_upload_reminder,
    log_user_action_async
)
from services.bot_provider import get_bot
from services.static_files_cache import send_intro_video_to_chat
from utils.i18n import create_translator_hub

logger = logging.getLogger(__name__)
config = get_config()

async def send_onboarding_reminders():
    """
    Main function to send all onboarding reminders.
    Scheduled to run daily at 12:00 MSK.
    """
    logger.info("Starting onboarding reminders check...")
    
    try:
        await _send_day1_reminders()
        await _send_day3_reminders()
        await _send_first_upload_reminders()
        
        logger.info("Onboarding reminders check completed.")
    except Exception as e:
        logger.error(f"Error in send_onboarding_reminders: {e}", exc_info=True)

async def _send_day1_reminders():
    """
    Sends Day 1 reminder (Text + Video) to users who joined yesterday but haven't uploaded anything.
    Processes users in batches until no more eligible users are found.
    """
    try:
        total_sent = 0

        while True:
            users = await get_users_for_onboarding_day1(batch_size=100)
            if not users:
                logger.info(f"No more users found for Day 1 onboarding reminder. Total sent: {total_sent}")
                break

            logger.info(f"Found batch of {len(users)} users for Day 1 onboarding reminder.")
            translator_hub = create_translator_hub()

            for user in users:
                try:
                    user_id = user['id']
                    telegram_id = user['telegram_id']
                    user_language = user.get('user_language', 'ru')
                    i18n = translator_hub.get_translator_by_locale(user_language)

                    # Отправляем видео с использованием централизованного кэша
                    await send_intro_video_to_chat(
                        bot=get_bot(),
                        chat_id=telegram_id,
                        lang=user_language,
                        caption=i18n.first_onboard_notification(),
                        parse_mode='HTML'
                    )

                    # Log action
                    await log_user_action_async(
                        user_id=user_id,
                        action_type='onboarding_reminder_day1_sent',
                        action_category='onboarding',
                        metadata={
                            'sent_at': datetime.utcnow().isoformat(),
                            'telegram_id': telegram_id
                        }
                    )
                    logger.info(f"Day 1 reminder sent to user {telegram_id}")
                    total_sent += 1

                    # Sleep to avoid hitting limits
                    await asyncio.sleep(0.5)

                except Exception as e:
                    logger.error(f"Failed to send Day 1 reminder to user {user.get('telegram_id')}: {e}")
                    # Log failed attempt to prevent infinite loop
                    await log_user_action_async(
                        user_id=user_id,
                        action_type='onboarding_reminder_day1_failed',
                        action_category='onboarding',
                        metadata={
                            'failed_at': datetime.utcnow().isoformat(),
                            'telegram_id': telegram_id,
                            'error': str(e)
                        }
                    )

            # Small pause between batches
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Error in _send_day1_reminders: {e}", exc_info=True)

async def _send_day3_reminders():
    """
    Sends Day 3 reminder (Text only) to users who received Day 1 reminder 2 days ago and still inactive.
    Processes users in batches until no more eligible users are found.
    """
    try:
        total_sent = 0
        while True:
            users = await get_users_for_onboarding_day3(batch_size=100)
            if not users:
                logger.info(f"No more users found for Day 3 onboarding reminder. Total sent: {total_sent}")
                break

            logger.info(f"Found batch of {len(users)} users for Day 3 onboarding reminder.")
            translator_hub = create_translator_hub()
            for user in users:
                try:
                    user_id = user['id']
                    telegram_id = user['telegram_id']

                    user_language = user.get('user_language', 'ru')
                    i18n = translator_hub.get_translator_by_locale(user_language)

                    await get_bot().send_message(
                        chat_id=telegram_id,
                        text=i18n.second_onboard_notification(),
                        parse_mode='HTML'
                    )

                    await log_user_action_async(
                        user_id=user_id,
                        action_type='onboarding_reminder_day3_sent',
                        action_category='onboarding',
                        metadata={
                            'sent_at': datetime.utcnow().isoformat(),
                            'telegram_id': telegram_id
                        }
                    )
                    logger.info(f"Day 3 reminder sent to user {telegram_id}")
                    total_sent += 1
                    await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error(f"Failed to send Day 3 reminder to user {user.get('telegram_id')}: {e}")
                    # Log failed attempt to prevent infinite loop
                    await log_user_action_async(
                        user_id=user_id,
                        action_type='onboarding_reminder_day3_failed',
                        action_category='onboarding',
                        metadata={
                            'failed_at': datetime.utcnow().isoformat(),
                            'telegram_id': telegram_id,
                            'error': str(e)
                        }
                    )

            # Small pause between batches
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Error in _send_day3_reminders: {e}", exc_info=True)

async def _send_first_upload_reminders():
    """
    Sends reminder 2 days after the first upload (if user has exactly 1 upload).
    Processes users in batches until no more eligible users are found.
    """
    try:
        total_sent = 0
        while True:
            users = await get_users_for_first_upload_reminder(batch_size=100)
            if not users:
                logger.info(f"No more users found for First Upload reminder. Total sent: {total_sent}")
                break

            logger.info(f"Found batch of {len(users)} users for First Upload reminder.")
            translator_hub = create_translator_hub()
            for user in users:
                try:
                    user_id = user['id']
                    telegram_id = user['telegram_id']

                    user_language = user.get('user_language', 'ru')
                    i18n = translator_hub.get_translator_by_locale(user_language)

                    await get_bot().send_message(
                        chat_id=telegram_id,
                        text=i18n.first_file_onboard_notification(),
                        parse_mode='HTML'
                    )

                    await log_user_action_async(
                        user_id=user_id,
                        action_type='onboarding_reminder_first_upload_sent',
                        action_category='onboarding',
                        metadata={
                            'sent_at': datetime.utcnow().isoformat(),
                            'telegram_id': telegram_id
                        }
                    )
                    logger.info(f"First Upload reminder sent to user {telegram_id}")
                    total_sent += 1
                    await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error(f"Failed to send First Upload reminder to user {user.get('telegram_id')}: {e}")
                    # Log failed attempt to prevent infinite loop
                    await log_user_action_async(
                        user_id=user_id,
                        action_type='onboarding_reminder_first_upload_failed',
                        action_category='onboarding',
                        metadata={
                            'failed_at': datetime.utcnow().isoformat(),
                            'telegram_id': telegram_id,
                            'error': str(e)
                        }
                    )

            # Small pause between batches
            await asyncio.sleep(1)

    except Exception as e:
        logger.error(f"Error in _send_first_upload_reminders: {e}", exc_info=True)


if __name__ == '__main__':

    asyncio.run(_send_day1_reminders())