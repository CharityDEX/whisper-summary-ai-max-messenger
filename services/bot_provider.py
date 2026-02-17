"""
Platform-aware bot provider.

Returns the appropriate bot instance based on which platform is active.
Used by shared services (payment notifications, onboarding reminders, etc.)
that need to send proactive messages to users.

Usage:
    In main.py (Telegram):
        from services.bot_provider import register_bot
        register_bot('telegram', bot)

    In max_main.py (Max):
        from services.bot_provider import register_bot
        register_bot('max', max_bot)

    In any shared service:
        from services.bot_provider import get_bot
        bot = get_bot()  # returns whichever bot was registered
"""

import logging

logger = logging.getLogger(__name__)

_bots = {}


def register_bot(platform: str, bot):
    """Register a bot instance for a given platform ('telegram' or 'max')."""
    _bots[platform] = bot
    logger.info(f"Bot registered for platform: {platform}")


def get_bot(platform: str = None):
    """
    Get the bot instance.

    If platform is specified, returns that platform's bot.
    Otherwise, returns the first available bot (telegram preferred).
    """
    if platform:
        bot = _bots.get(platform)
        if bot:
            return bot
        raise RuntimeError(f"No bot registered for platform: {platform}")

    # Default: prefer telegram, fallback to max
    for p in ('telegram', 'max'):
        if p in _bots:
            return _bots[p]

    # Backward compatible fallback: import telegram bot directly
    # This allows existing code to work without explicit registration
    try:
        from services.init_bot import bot
        return bot
    except Exception:
        pass

    raise RuntimeError("No bot registered. Call register_bot() at startup.")


def get_all_bots():
    """Returns dict of all registered bots."""
    return dict(_bots)


def has_bot(platform: str) -> bool:
    """Check if a bot is registered for the given platform."""
    return platform in _bots
