import logging
import os

import httpx
from fluentogram import TranslatorRunner
from groq import AsyncGroq

from services.init_bot import config
logger = logging.getLogger(__name__)

_proxies = {'https://': config.proxy.proxy, 'http://': config.proxy.proxy}
_groq_http_client = httpx.AsyncClient(proxies=_proxies, timeout=360)

# client = Groq(
#     api_key=os.environ.get("GROQ_API_KEY"),
# )

# chat_completion = client.chat.completions.create(
#     messages=[
#         {
#             "role": "user",
#             "content": "Explain the importance of fast language models",
#         }
#     ],
#     model="llama3-8b-8192",
# )

# print(chat_completion.choices[0].message.content)


async def summarise_text(text: str, i18n: TranslatorRunner) -> str:
    client = AsyncGroq(api_key=config.grok.api_key, http_client=_groq_http_client)

    message = i18n.summarise_text_system_prompt_gpt_oss() + '\n' +i18n.text_prompt(text=text)
    response = await client.chat.completions.create(
        messages=[
            {"role": "user", "content": message}
        ],
        reasoning_effort="low",
        model="openai/gpt-oss-120b",
        temperature=0.5,
        top_p=1.0
    )
    logging_message = f'Grok key: {client.api_key}\nGrok response: {response.choices[0].message.content}'
    logger.info(logging_message)
    return response.choices[0].message.content


async def generate_title_grok(text: str, i18n: TranslatorRunner) -> str:
    """
    Генерирует название для транскрипции через Grok.
    
    Args:
        text: Текст транскрипции
        i18n: TranslatorRunner для получения промпта на нужном языке
        
    Returns:
        str: Сгенерированное название
    """
    client = AsyncGroq(api_key=config.grok.api_key, http_client=_groq_http_client)
    
    message = i18n.generate_title_system_prompt() + '\n' + i18n.title_prompt(text=text)
    
    response = await client.chat.completions.create(
        messages=[
            {"role": "user", "content": message}
        ],
        reasoning_effort="high",
        model="openai/gpt-oss-120b",
        temperature=0.7,
        top_p=1.0
    )
    
    title = response.choices[0].message.content.strip()
    logger.info(f'TITLE GENERATION GROK: {title}')
    
    # Удаляем возможные префиксы, если LLM их добавила
    for prefix in ['TITLE:', 'Title:', 'НАЗВАНИЕ:', 'Название:', '"', "'"]:
        if title.startswith(prefix):
            title = title[len(prefix):].strip()
        if title.endswith('"') or title.endswith("'"):
            title = title[:-1].strip()
    
    # Ограничиваем длину до 90 символов
    if len(title) > 90:
        title = title[:87] + '...'
    
    return title


async def chat_function(context: list[dict], i18n: TranslatorRunner) -> str | bool:
    client = AsyncGroq(api_key=config.grok.api_key, http_client=_groq_http_client)
    if context[0]['role'] != 'system':
        context.insert(0, {'role': 'system', 'content': i18n.chat_system_prompt_gpt_oss()})
    try:
        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=context,
            reasoning_effort="high",
            temperature=1,
            top_p=1.0
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f'--- ОШИБКА --- \n'
              f'{e}')
        return False