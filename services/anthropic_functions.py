import asyncio
import logging
import httpx
from anthropic import AsyncAnthropic, APIError
from fluentogram import TranslatorRunner



from services.init_bot import config

proxies = {'https://': config.proxy.proxy, 'http://': config.proxy.proxy}
http_client = httpx.AsyncClient(proxies=proxies, timeout=360)

client = AsyncAnthropic(
    api_key=config.anthropic.api_key,
    timeout=600,
    http_client=http_client,
)

logger = logging.getLogger(__name__)
async def summarise_text_anthropic(text: str, i18n: TranslatorRunner) -> str:
    message = i18n.text_prompt(text=text)
    system_prompt = i18n.summarise_text_base_system_prompt()

    max_attempts = 5
    delay = 5  # секунд
    
    # Create a properly formatted message with content as a list of content blocks
    messages = [
        {
            "role": "user", 
            "content": [
                {"type": "text", "text": message}
            ]
        }
    ]
    
    # Format system prompt as list of content blocks
    system_content = [{"type": "text", "text": system_prompt}]
    
    # Add logging to see what's being sent
    logger.debug(f"Sending summary request to Anthropic with system: {system_content}")
    logger.debug(f"Sending summary request to Anthropic with messages: {messages}")

    for attempt in range(max_attempts):
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                system=system_content,
                messages=messages,
                temperature=1,
                top_p=1.0
            )
            logger.debug(f'SUMMARY ANTHROPIC: {response.content[0].text}')
            return response.content[0].text
        except APIError as e:
            if attempt < max_attempts - 1:
                logger.warning(f"Попытка {attempt + 1} не удалась. Повтор через {delay} секунд...:" + str(e))
                await asyncio.sleep(delay)
            else:
                logger.error(f"Все {max_attempts} попыток не удались.")
                raise e

    # Этот код не должен выполниться, но добавим на всякий случай
    raise Exception("Неожиданная ошибка: все попытки исчерпаны")

async def chat_function(context: list[dict], i18n: TranslatorRunner) -> str | bool:
    try:
        # Ensure each message has the correct structure with content as a list of content blocks
        messages = []
        for msg in context:
            # Ensure the role and content are properly formatted
            if "role" in msg and "content" in msg:
                if msg['role'] == 'system':
                    #Skip system message, since we can't use it here.
                    continue
                # Convert the content to a content block format
                content_text = msg["content"]
                messages.append({
                    "role": msg["role"], 
                    "content": [
                        {"type": "text", "text": content_text}
                    ]
                })

        system_prompt = i18n.chat_system_prompt()
        # Format system prompt as list of content blocks
        system_content = [{"type": "text", "text": system_prompt}]

        # Add logging to see what's being sent
        logger.debug(f"Sending messages to Anthropic with system: {system_content}")
        logger.debug(f"Sending messages to Anthropic with messages: {messages}")
        
        max_attempts = 5
        delay = 5  # seconds
        
        for attempt in range(max_attempts):
            try:
                response = await client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1024,
                    messages=messages,
                    system=system_content,
                    temperature=1,
                    top_p=1.0
                )
                
                return response.content[0].text
            except APIError as e:
                if attempt < max_attempts - 1:
                    logger.warning(f"Попытка {attempt + 1} не удалась. Повтор через {delay} секунд...:" + str(e))
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Все {max_attempts} попыток не удались.")
                    raise e
    except Exception as e:
        logger.error(f'--- ERROR ANTHROPIC CHAT --- \n{e}')
        return False



