import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from fluentogram import TranslatorHub


from models.orm import get_user, create_new_user

logger = logging.getLogger(__name__)
from services.init_bot import config


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:

        telegram_user: User = data.get('event_from_user')

        if telegram_user is None:
            return await handler(event, data)
            
        user: dict = await get_user(telegram_id=telegram_user.id)

        hub: TranslatorHub = data.get('_translator_hub')
        if hub is not None:
            if user is not None:
                data['i18n'] = hub.get_translator_by_locale(locale=user.get('user_language', config.tg_bot.default_lang))
            else:
                data['i18n'] = hub.get_translator_by_locale(locale=config.tg_bot.default_lang)
        else:
            # Fallback если translator_hub не доступен
            data['i18n'] = None
        data['user']: dict = user

        new_user = False
        if user is None:
            if event.event_type == 'message':
                user = await create_new_user(message=event.message)
            else:
                user = await create_new_user(telegram_user=telegram_user)
            new_user = True
        elif user['created_at'] is None:
            user = await create_new_user(telegram_user=telegram_user)
        elif user['subscription_id'] is not None and user['created_at'] is None:
            user = await create_new_user(telegram_user=telegram_user, active_sub=True)
        data['user']: dict = user
        data['user']['new_user'] = new_user

        return await handler(event, data)
