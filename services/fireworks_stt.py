import io
import os
import logging
from typing import Optional, Tuple, Dict, Any, List

import aiohttp
import aiofiles
import mimetypes
from fluentogram import TranslatorRunner

from services.services import progress_bar, format_time
from services.init_bot import config
from services.transcription_grouper import group_transcription_smart, extract_plain_text


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


FIREWORKS_API_URL = "https://audio-turbo.api.fireworks.ai/v1/audio/transcriptions"


def _get_fireworks_api_key() -> str:
    """
    Returns Fireworks API key from app config.
    """
    api_key = getattr(getattr(config, 'fireworks', None), 'api_key', None)
    if not api_key:
        # Backward-compatible fallback to env var if config not set
        api_key = os.getenv("FIREWORKS_API_KEY")
    if not api_key:
        raise RuntimeError("Fireworks API key is not configured. Set config.fireworks.api_key or FIREWORKS_API_KEY")
    return api_key


def _guess_content_type(filename: str) -> str:
    content_type = mimetypes.guess_type(filename)[0]
    return content_type or "application/octet-stream"


async def _build_form_from_bytes(file_bytes: bytes, filename: str = "audio.mp3", 
                                 model: str = "whisper-v3-turbo",
                                 temperature: str = "0",
                                 vad_model: str = "silero",
                                 extra_fields: Optional[Dict[str, Any]] = None) -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field(
        "file",
        file_bytes,
        filename=filename,
        content_type=_guess_content_type(filename)
    )
    form.add_field("model", model)
    form.add_field("temperature", str(temperature))
    form.add_field("vad_model", vad_model)
    if extra_fields:
        for k, v in extra_fields.items():
            if v is not None:
                form.add_field(k, str(v))
    return form


async def _build_form_from_path(file_path: str, 
                                model: str = "whisper-v3-turbo",
                                temperature: str = "0",
                                vad_model: str = "silero",
                                extra_fields: Optional[Dict[str, Any]] = None) -> aiohttp.FormData:
    filename = os.path.basename(file_path) or "audio.mp3"
    # Ensure the file has an extension for proper content-type guessing
    if not os.path.splitext(filename)[1]:
        filename = f"{filename}.mp3"

    async with aiofiles.open(file_path, 'rb') as f:
        data = await f.read()

    return await _build_form_from_bytes(
        data,
        filename=filename,
        model=model,
        temperature=temperature,
        vad_model=vad_model,
        extra_fields=extra_fields,
    )


def _build_timecoded_from_segments(segments: List[Dict[str, Any]]) -> Tuple[str, str]:
    timecoded_text_parts: List[str] = []
    plain_parts: List[str] = []
    for seg in segments:
        start = seg.get("start") or seg.get("start_time") or seg.get("startTime")
        end = seg.get("end") or seg.get("end_time") or seg.get("endTime")
        text = seg.get("text") or seg.get("phrase") or ""
        try:
            # Fireworks/OpenAI style timestamps might be in seconds already
            start_s = float(start) if start is not None else 0.0
            end_s = float(end) if end is not None else start_s
        except Exception:
            start_s, end_s = 0.0, 0.0
        timecoded_text_parts.append(f"[{format_time(start_s)} - {format_time(end_s)}] SPEAKER\n{text}\n\n")
        plain_parts.append(text)
    return "".join(timecoded_text_parts).strip(), " ".join(plain_parts).strip()


def _normalize_speaker_id(speaker: Optional[str]) -> str:
    if not speaker:
        return "SPEAKER"
    # Examples: SPEAKER_00, SPEAKER_01 -> SPEAKER_1, SPEAKER_2
    try:
        if speaker.startswith("SPEAKER_"):
            num = speaker.split("_")[-1]
            value = int(num)
            return f"SPEAKER_{value + 1}"
    except Exception as e:
        logger.debug(f"Fireworks STT: speaker id normalize error: {e}")
        pass
    return speaker


def _lines_from_words(words: List[Dict[str, Any]], pause_threshold: float = 1.0) -> str:
    """
    Build raw transcription lines from word-level timestamps.
    Each line format: [MM:SS.ss - MM:SS.ss] (SPEAKER_X) text
    Lines are split on speaker change or long pauses between words.
    """
    if not words:
        return ""

    # Ensure ordered by start time
    words_sorted = sorted(words, key=lambda w: (w.get("start", 0.0) or 0.0))

    lines: List[str] = []
    current_speaker: Optional[str] = None
    current_start: Optional[float] = None
    current_end: Optional[float] = None
    current_tokens: List[str] = []

    for idx, w in enumerate(words_sorted):
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        start = float(w.get("start") or 0.0)
        end = float(w.get("end") or start)
        speaker = _normalize_speaker_id(w.get("speaker_id"))

        is_new_segment = False
        if current_speaker is None:
            is_new_segment = True
        else:
            # Change if speaker switched or pause exceeded threshold
            gap = (start - (current_end or start))
            if speaker != current_speaker or gap > pause_threshold:
                is_new_segment = True

        if is_new_segment and current_speaker is not None:
            # Flush previous segment
            # IMPORTANT: use raw seconds to match parser in transcription_grouper
            line = f"[{current_start:.2f} - {current_end:.2f}] ({current_speaker}) {' '.join(current_tokens)}"
            lines.append(line)
            current_tokens = []
            current_start = None
            current_end = None

        # Start new or continue
        if current_speaker is None or is_new_segment:
            current_speaker = speaker
            current_start = start
            current_end = end
            current_tokens = [text]
        else:
            current_end = end
            current_tokens.append(text)

    # Flush last
    if current_speaker is not None and current_tokens:
        line = f"[{current_start:.2f} - {current_end:.2f}] ({current_speaker}) {' '.join(current_tokens)}"
        lines.append(line)

    return "\n".join(lines)


async def audio_to_text_fireworks(
    file_bytes: Optional[bytes],
    waiting_message,
    i18n: TranslatorRunner,
    file_path: Optional[str] = None,
    language_code: Optional[str] = None,
    suppress_progress: bool = False,
) -> Tuple[str, str]:
    """
    Transcribe audio using Fireworks.ai Whisper endpoint.

    Returns (timecoded_text, plain_text) to match the existing STT interface.
    Accepts either in-memory bytes or a file path on disk.
    """
    if not file_bytes and not file_path:
        raise ValueError("Either file_bytes or file_path must be provided")

    if not suppress_progress:
        try:
            await waiting_message.edit_text(text=i18n.transcribe_audio_progress(progress=progress_bar(39, i18n)))
        except Exception:
            pass

    logger.debug('Fireworks STT: starting request')

    api_key = _get_fireworks_api_key()

    headers = {
        "Authorization": f"Bearer {api_key}"
    }

    # Use httpx with proxy to bypass geo-blocking (Fireworks is US-based, blocked from Russia)
    import httpx
    proxy_url = getattr(getattr(config, 'proxy', None), 'proxy', None)

    try:
        transport_kwargs = {}
        if proxy_url:
            logger.debug(f'Fireworks STT: using proxy {proxy_url}')
            transport_kwargs['proxy'] = proxy_url

        async with httpx.AsyncClient(timeout=httpx.Timeout(360.0, connect=60.0), **transport_kwargs) as client:
            logger.debug('Fireworks STT: sending POST request via httpx')

            # Build multipart files/data for httpx
            if file_path:
                import aiofiles as _af
                async with _af.open(file_path, 'rb') as f:
                    file_data = await f.read()
                fname = os.path.basename(file_path) or "audio.mp3"
            elif file_bytes:
                file_data = file_bytes
                fname = "audio.mp3"
            else:
                raise ValueError("No audio data")

            files_dict = {"file": (fname, file_data, _guess_content_type(fname))}
            data_dict = {
                "model": "whisper-v3-turbo",
                "temperature": "0",
                "vad_model": "silero",
                "diarize": "True",
                "response_format": "verbose_json",
                "timestamp_granularities": "word",
            }

            resp = await client.post(FIREWORKS_API_URL, headers=headers, files=files_dict, data=data_dict)

            if resp.status_code != 200:
                snippet = resp.text[:500]
                logger.error(f"Fireworks STT HTTP {resp.status_code}. Response snippet: {snippet}")
                raise RuntimeError(f"Fireworks STT failed with status {resp.status_code}")

            try:
                data = resp.json()
            except Exception as e:
                logger.warning(f"Fireworks STT returned non-JSON body; using raw text ({e})")
                data = {"text": resp.text}
    except Exception as e:
        logger.exception(f'Fireworks STT: request failed: {e}')
        raise

    # Parse response
    timecoded_text: str = ""
    plain_text: str = ""
    if isinstance(data, dict):
        # Prefer grouping from rich structures
        if "words" in data and isinstance(data["words"], list) and len(data["words"]) > 0:
            raw_lines = _lines_from_words(data["words"], pause_threshold=1.0)
            if raw_lines:
                grouped = group_transcription_smart(raw_lines, min_block_duration=20.0, max_block_duration=60.0, pause_threshold=2.0)
                timecoded_text = grouped
                # Build clean text directly from words to avoid parser format mismatch
                try:
                    plain_text = " ".join([w.get("word", "").strip() for w in data["words"] if isinstance(w.get("word"), str)])
                    plain_text = plain_text.strip()
                except Exception:
                    plain_text = (data.get("text") or "").strip()
            else:
                # Fallback to simple text
                plain_text = (data.get("text") or "").strip()
                timecoded_text = f"[00:00 - 00:00] (SPEAKER) {plain_text}" if plain_text else ""
        elif "segments" in data and isinstance(data["segments"], list):
            timecoded_text, plain_text = _build_timecoded_from_segments(data["segments"]) 
        elif "text" in data and isinstance(data["text"], str):
            plain_text = data["text"].strip()
            # Minimal timecoded presentation when no segments are provided
            timecoded_text = f"[00:00 - 00:00] (SPEAKER) {plain_text}" if plain_text else ""
        else:
            # Try OpenAI-compatible payload shape
            alt_text = data.get("result") or data.get("transcript") or ""
            if isinstance(alt_text, str) and alt_text:
                plain_text = alt_text.strip()
                timecoded_text = f"[00:00 - 00:00] (SPEAKER) {plain_text}" if plain_text else ""
            else:
                logger.error(f"Unexpected Fireworks STT response shape: {data}")
                raise RuntimeError("Unexpected Fireworks STT response")
    else:
        logger.error(f"Unexpected Fireworks STT response type: {type(data)}")
        raise RuntimeError("Unexpected Fireworks STT response type")

    if not suppress_progress:
        try:
            await waiting_message.edit_text(text=i18n.transcribe_audio_progress_finishing(progress=progress_bar(100, i18n)))
        except Exception:
            pass

    return timecoded_text, plain_text
