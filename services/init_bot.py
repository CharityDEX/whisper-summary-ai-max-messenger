from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from config_data.config import Config, get_config

# Инициализируем конфигурацию один раз при загрузке модуля
config: Config = get_config()

# Экспортируем конфигурацию для использования в других модулях
__all__ = ['bot', 'config']

session = AiohttpSession(
    api=TelegramAPIServer.from_base('http://localhost:8081')
)
session.timeout = 1200
bot: Bot = Bot(token=config.tg_bot.token, default=DefaultBotProperties(parse_mode='HTML'), session=session)
