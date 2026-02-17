import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from maxapi.filters.middleware import BaseMiddleware
from maxapi.types.updates.message_created import MessageCreated
from maxapi.types.updates.message_callback import MessageCallback
from maxapi.types.updates.bot_started import BotStarted
from maxapi.types.users import User as MaxUser
from fluentogram import TranslatorHub

from models.orm import get_user, create_new_user
from services.init_max_bot import config

logger = logging.getLogger(__name__)


def _extract_max_user(event: Any) -> Optional[MaxUser]:
    """Extract user object from various Max event types."""
    if isinstance(event, MessageCreated):
        return event.message.sender
    elif isinstance(event, MessageCallback):
        return event.callback.user
    elif isinstance(event, BotStarted):
        return event.user
    # For other event types, try common attributes
    if hasattr(event, 'from_user') and event.from_user is not None:
        return event.from_user
    return None


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event_object: Any,
        data: Dict[str, Any]
    ) -> Any:

        max_user: Optional[MaxUser] = _extract_max_user(event_object)

        if max_user is None:
            return await handler(event_object, data)

        user: dict = await get_user(telegram_id=max_user.user_id)

        hub: TranslatorHub = data.get('_translator_hub')
        if hub is not None:
            if user is not None:
                data['i18n'] = hub.get_translator_by_locale(
                    locale=user.get('user_language', config.max_bot.default_lang)
                )
            else:
                data['i18n'] = hub.get_translator_by_locale(
                    locale=config.max_bot.default_lang
                )
        else:
            data['i18n'] = None
        data['user'] = user

        new_user = False
        if user is None:
            user = await create_new_user(max_user=max_user)
            new_user = True
        elif user['created_at'] is None:
            user = await create_new_user(max_user=max_user)
        elif user['subscription_id'] is not None and user['created_at'] is None:
            user = await create_new_user(max_user=max_user, active_sub=True)
        data['user'] = user
        data['user']['new_user'] = new_user

        return await handler(event_object, data)
