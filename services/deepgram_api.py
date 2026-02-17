from io import BytesIO

import aiofiles
import deepgram
import httpx
from fluentogram import TranslatorRunner

from services.services import progress_bar, format_time

deepgram_key = '72432bd1465385df9c3926bf18857dcd5e137159'

async def audio_to_text_deepgram(file_bytes: bytes, waiting_message, i18n: TranslatorRunner,
                                 file_path: str = None,
                                 language_code: str = None,
                                 suppress_progress: bool = False,
                                 ) -> (str, str):
    
    deepgram_client = deepgram.DeepgramClient(api_key=deepgram_key)

    if file_path:
        async with aiofiles.open(file_path, 'rb') as f:
            buffer_data = await f.read()
    else:
        buffer_data = file_bytes

    payload: deepgram.FileSource = {
        "buffer": buffer_data,
    }

    # STEP 2: Configure Deepgram options for audio analysis
    if language_code:
        options = deepgram.PrerecordedOptions(
            language=language_code,
            model="nova-2",
            smart_format=True,
            diarize=True,
            utterances=True,
        )
    else:
        options = deepgram.PrerecordedOptions(
            detect_language=True,
            model="nova-2",
            smart_format=True,
            diarize=True,
            utterances=True,
        )
    if not suppress_progress:
        await waiting_message.edit_text(text=i18n.transcribe_audio_progress(progress=progress_bar(39, i18n)))
    # STEP 3: Call the transcribe_file method with the text payload and options
    response = await deepgram_client.listen.asyncrest.v("1").transcribe_file(payload, options,
                                                                             timeout=httpx.Timeout(600.0, connect=10.0))
    timecodes_speaker_text = ''
    for i in response.results.channels[0].alternatives[0].paragraphs.paragraphs:
        text = ' '.join([sent.text for sent in i.sentences])
        timecodes_speaker_text += (f'[{format_time(i.start)} - {format_time(i.end)}] SPEAKER: {i.speaker}\n'
                                   f'{text}\n\n')
    return timecodes_speaker_text, response.results.channels[0].alternatives[0].transcript