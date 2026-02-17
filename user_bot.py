import asyncio
import io
import logging

import aiogram
from aiogram import Bot
from telethon.tl.types import Message

from services.init_bot import config

from telethon.sync import TelegramClient

logger = logging.getLogger(__name__)

device_model = 'Desktop'
system_version = 'Linux ubuntu X11 glibc 2.35'
app_version = '4.8.3 Snap'
system_lang_code = 'ru-RU'
lang_code = 'ru'



class UserBot:
    device_model = 'Desktop'
    system_version = 'Linux ubuntu X11 glibc 2.35'
    app_version = '4.8.3 Snap'
    system_lang_code = 'ru-RU'
    lang_code = 'ru'

    async def get_file(self, bot: Bot, user_message: aiogram.types.Message) -> io.BytesIO:
        async with TelegramClient(session=config.user_bot.session_name,
                                  api_id=config.user_bot.api_id,
                                  api_hash=config.user_bot.api_hash,
                                  device_model=device_model,
                                  system_version=system_version,
                                  app_version=app_version,
                                  system_lang_code=system_lang_code,
                                  lang_code=lang_code) as client:
            # Отправляем сообщение из бота юзер-боту
            await bot.forward_message(chat_id=config.user_bot.user_bot_id,
                                      message_id=user_message.message_id,
                                      from_chat_id=user_message.from_user.id)

            client: TelegramClient = client
            messages: list[Message] = await client.get_messages(int(config.tg_bot.bot_id), limit=1)
            messages = await client.get_messages()
            print(messages)

            for message in messages[:1]:
                print(message)
                buffer = io.BytesIO()
                print('начинаю скачку')
                await client.download_media(message, file=buffer)
                print('скачал')
                buffer.seek(0)
                return buffer

    def create_session(self):
        with TelegramClient(session=config.user_bot.session_name,
                                      api_id=config.user_bot.api_id,
                                      api_hash=config.user_bot.api_hash,
                                      device_model=device_model,
                                      system_version=system_version,
                                      app_version=app_version,
                                      system_lang_code=system_lang_code,
                                      lang_code=lang_code) as client:
            print('a')


# user_bot = UserBot()

if __name__ == '__main__':
    # user_bot.create_session()
    pass
