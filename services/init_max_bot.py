from maxapi import Bot
from maxapi.enums.parse_mode import ParseMode

from config_data.config import Config, get_config

config: Config = get_config()

__all__ = ['max_bot', 'config']

if config.max_bot is None or not config.max_bot.token:
    raise SystemExit("MAX_BOT_TOKEN not set in .env. Cannot start Max bot.")

max_bot: Bot = Bot(token=config.max_bot.token, parse_mode=ParseMode.HTML)
