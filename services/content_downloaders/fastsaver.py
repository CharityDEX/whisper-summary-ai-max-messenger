import os
import logging
from typing import Optional

import httpx

from services.init_bot import config
from services.content_downloaders.file_handling import download_file

# Module logger
logger = logging.getLogger(__name__)


async def get_youtube_video_id(url: str) -> str:
    logger.debug(f"Parsing YouTube video id from URL: {url}")
    if 'shorts' in url:
        video_id = url.split('/')[-1]
    elif 'live' in url:
        video_id = url.split('/')[-1]
    elif 'youtu.be' in url:
        video_id = url.split('/')[-1]
    else:
        video_id = url.split('v=')[-1]


    if '?si=' in video_id:
        video_id = video_id.split('?si=')[0]
    logger.debug(f"Parsed video id: {video_id}")
    return video_id


async def download_video_via_fastsaver(
    link: str,
    format: Optional[str] = 'mp3',
    api_token: Optional[str] = 'cAGQzIVgA2HkcNMISgfwe7jV',
    bot_username: Optional[str] = None,
    user_data: Optional[dict] = None,
    session_id: Optional[str] = None,
    attempt_number: int = 1,
    destination_type: str = 'buffer',
    temp_dir: Optional[str] = None,
    file_name: Optional[str] = None,
) -> str | bytes:
    """
    Download media using FastSaver API and return a local file path.

    Args:
        link: Original media link (e.g. YouTube/Instagram URL).
        format: Desired output format (e.g. 'mp4', 'mp3'). If None, leave empty.
        api_token: FastSaver API token. If None, uses FASTSAVER_TOKEN env var.
        bot_username: Telegram bot username (with or without leading '@'). If None, tries to derive from config or env.
        temp_dir: Optional directory for temporary file placement.
        file_name: Optional preferred filename for the temp file.

    Returns:
        Absolute path to the downloaded file on disk.
    """

    def _mask_secret(secret: Optional[str]) -> str:
        if not secret:
            return "<empty>"
        if len(secret) <= 6:
            return "*" * len(secret)
        return secret[:2] + "***" + secret[-4:]

    logger.info("Starting FastSaver download")
    resolved_token: Optional[str] = api_token or os.getenv('FASTSAVER_TOKEN')
    if not resolved_token:
        logger.error("FastSaver token is missing")
        raise ValueError("FastSaver API token is required. Provide api_token or set FASTSAVER_TOKEN env var.")

    resolved_username: Optional[str] = bot_username
    if not resolved_username:
        # Try to derive from config.tg_bot.bot_url (e.g., https://t.me/WhisperSummaryAI_bot)
        bot_url: Optional[str] = getattr(getattr(config, 'tg_bot', None), 'bot_url', None)
        if bot_url:
            # Extract the last path segment
            segment = bot_url.rsplit('/', 1)[-1]
            resolved_username = segment
        else:
            # Fallback to common env var names
            resolved_username = os.getenv('BOT_USERNAME') or os.getenv('TELEGRAM_BOT_USERNAME')

    if not resolved_username:
        logger.error("Bot username is missing")
        raise ValueError("Telegram bot username is required. Provide bot_username or set BOT_USERNAME env var, or configure tg_bot.bot_url.")

    if not resolved_username.startswith('@'):
        resolved_username = '@' + resolved_username
    
    logger.debug(f"Input link: {link}, requested format: {format}")
    video_id: str = await get_youtube_video_id(link)
    
    params = {
        "video_id": video_id,
        "format": format or "",
        "bot_username": resolved_username,
        "token": resolved_token,
    }

    url = "https://fastsaverapi.com/download"

    # Log request without leaking token
    safe_params = dict(params)
    safe_params["token"] = _mask_secret(safe_params.get("token"))
    logger.debug(f"Calling FastSaver API: {url} with params: {safe_params}")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=20.0, read=120.0, write=20.0, pool=30.0)) as client:
            response = await client.get(url, params=params)
            logger.debug(f"FastSaver HTTP status: {response.status_code}")
            if response.status_code != 200:
                snippet = response.text[:200] if hasattr(response, 'text') else '<no body>'
                logger.error(f"FastSaver non-200 response: {response.status_code}, body: {snippet}")
                raise RuntimeError(f"FastSaver HTTP {response.status_code}: {snippet}")

            data = response.json()
    except httpx.RequestError as e:
        logger.exception(f"FastSaver request error: {type(e).__name__}: {e}")
        raise

    if not isinstance(data, dict):
        raise RuntimeError("FastSaver response is not a JSON object")

    if data.get("error"):
        logger.error(f"FastSaver returned error payload: {data}")
        raise RuntimeError(f"FastSaver error: {data}")

    file_id: Optional[str] = data.get("file_id")
    logger.debug(f"FastSaver response hosting={data.get('hosting')}, media_type={data.get('media_type')}, format={data.get('format')}")
    if not file_id:
        logger.error(f"FastSaver response missing file_id: {data}")
        raise RuntimeError(f"FastSaver response missing file_id: {data}")

    # Decide filename extension based on reported format if not explicitly provided
    target_filename = file_name
    reported_format = (data.get("format") or format or "").strip().lower()
    if not target_filename and reported_format:
        target_filename = f"downloaded_media.{reported_format}"

    # Ensure we have a valid DB user id for logging
    if not user_data or not isinstance(user_data.get('id', None), int) or user_data.get('id') <= 0:
        logger.error("user_data.id is required and must be a valid existing user id")
        raise RuntimeError("Valid user_data.id is required to create download record")

    # Use existing Telegram-aware downloader to obtain a local file path or bytes
    logger.info(f"Downloading media via Telegram file_id to {destination_type}")
    result: str | bytes = await download_file(
        source_type='telegram',
        identifier=file_id,
        destination_type=destination_type,
        specific_source='youtube',
        file_name=target_filename,
        user_data=user_data,
        session_id=session_id,
        attempt_number=attempt_number,
        download_method='fastsaver',
        temp_dir=temp_dir,
        add_file_size_to_session=True,
    )
    if isinstance(result, str):
        logger.info(f"Downloaded media saved to: {result}")
    else:
        logger.info(f"Downloaded media bytes length: {len(result)}")

    return result


__all__ = ["download_video_via_fastsaver"]

if __name__ == "__main__":
    import asyncio
    asyncio.run(download_video_via_fastsaver(link="https://www.youtube.com/watch?v=YEZHU4LSUfU", user_data={'id': 3}, session_id='e549f2f6-be63-4f5b-82e4-a0c86cef6ffc', attempt_number=1))
