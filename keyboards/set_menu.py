from aiogram import Bot

from aiogram.types import BotCommand
from fluentogram import TranslatorRunner, TranslatorHub

from lexicon.lexicon_ru import LEXICON_COMMANDS
from utils.i18n import create_translator_hub


def get_commands_texts(i18n: TranslatorRunner = None, language_code: str = 'en') -> dict[str: str]:
    for command, description in LEXICON_COMMANDS.items():
        LEXICON_COMMANDS[command] = description
    return LEXICON_COMMANDS



async def set_main_menu(bot: Bot, i18n: TranslatorRunner = None, language_code: str = 'en'):
    main_menu_commands = [BotCommand(
        command=command,
        description=description) for command, description in get_commands_texts(i18n=i18n, language_code=language_code).items()]
    await bot.set_my_commands(main_menu_commands)
