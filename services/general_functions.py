import datetime
from io import BytesIO
import time
from fluentogram import TranslatorRunner
from services import anthropic_functions, openai_functions
from services.deepgram_api import audio_to_text_deepgram
from services.elevateai_funcs import process_audio_elevateai
from services.fal_functions import process_audio_fal
from services.assemblyai_api import process_audio_assemblyai
from services.fireworks_stt import audio_to_text_fireworks
from services.payments import groq_functions
import logging

from services.private_module_stt import private_stt_client
from services.services import progress_bar, split_title_and_summary
from models.orm import create_llm_request, save_summary_cache, save_transcription_cache, update_llm_request, update_processing_session

logger = logging.getLogger(__name__)


def _get_model_info(llm_model: str) -> tuple[str, str]:
    """Возвращает провайдера и название модели"""
    if 'gpt' in llm_model:
        return 'openai', 'gpt-4o'
    elif 'claude' in llm_model:
        return 'anthropic', 'claude-3-5-sonnet'
    else:
        return 'openai', 'gpt-4o'  # По умолчанию


def _get_fallback_model_info(primary_model: str) -> tuple[str, str]:
    """Возвращает провайдера и название fallback модели"""
    if primary_model == 'gpt-4o':
        return 'anthropic', 'claude-3-5-sonnet'
    elif primary_model == 'claude-3-5-sonnet':
        return 'openai', 'gpt-4o'
    else:
        return 'anthropic', 'claude-3-5-sonnet'

async def process_chat_request(context: list[dict], user: dict, i18n: TranslatorRunner, session_id: str | None = None) -> str | bool:
    """Обрабатывает запрос чата с логированием LLM запросов"""
    start_time = time.time()
    
    # Вычисляем длину контекста
    context_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in context])
    context_length = len(context_text)
    user_message = context[-1]['content'] if context else ""
    prompt_length = len(user_message)
    
    # Определяем модель и провайдера
    model_provider, model_name = _get_model_info(user['llm_model'])
    
    # Создаем LLM запрос
    llm_request_id = await create_llm_request(
        user_id=user['id'],
        session_id=session_id,
        request_type='chat',
        model_provider=model_provider,
        model_name=model_name,
        prompt_length=prompt_length,
        context_length=context_length
    )
    
    # Конфигурация моделей по приоритету
    chat_options = {}

    # Приоритетная модель пользователя
    if user['llm_model'] == 'gpt-4o':
        chat_options['primary'] = {
            'function': openai_functions.chat_function,
            'args': {'context': context, 'i18n': i18n},
            'provider': 'openai',
            'model': 'gpt-4o'
        }
        chat_options['groq'] = {
            'function': groq_functions.chat_function,
            'args': {'context': context, 'i18n': i18n},
            'provider': 'groq',
            'model': 'gpt-oss-120b'
        }
        chat_options['fallback'] = {
            'function': anthropic_functions.chat_function,
            'args': {'context': context, 'i18n': i18n},
            'provider': 'anthropic',
            'model': 'claude-3-5-sonnet'
        }
    else:
        chat_options['primary'] = {
            'function': groq_functions.chat_function,
            'args': {'context': context, 'i18n': i18n},
            'provider': 'groq',
            'model': 'gpt-oss-120b'
        }
        chat_options['fallback'] = {
            'function': anthropic_functions.chat_function,
            'args': {'context': context, 'i18n': i18n},
            'provider': 'anthropic',
            'model': 'claude-3-5-sonnet'
        }
        chat_options['fallback'] = {
            'function': openai_functions.chat_function,
            'args': {'context': context, 'i18n': i18n},
            'provider': 'openai',
            'model': 'gpt-4o'
        }

    last_error = None
    for service, options in chat_options.items():
        try:
            result = await options['function'](**options['args'])
            if result and result is not False:  # Проверяем, что результат не пустой
                logger.debug(f'Successfully processed chat request with {service}')

                # Записываем успешный результат
                if llm_request_id:
                    await update_llm_request(
                        request_id=llm_request_id,
                        response_length=len(str(result)),
                        processing_duration=time.time() - start_time,
                        success=True,
                        model_provider=options['provider'],
                        model_name=options['model']
                    )
                return result
        except Exception as e:
            logger.error(f'Failed to process chat request with {service}: {e}')
            last_error = e
            continue

    # Если все модели не сработали
    error_msg = f'All chat models failed. Last error: {last_error}'
    logger.error(error_msg)

    # Записываем неуспешный результат
    if llm_request_id:
        await update_llm_request(
            request_id=llm_request_id,
            processing_duration=time.time() - start_time,
            success=False,
            error_message=error_msg
        )
    return False


async def summarise_text(text: str, user: dict, i18n: TranslatorRunner, session_id: str | None = None, transcription_id: int | None = None) -> dict:
    """Создает summary с логированием LLM запроса. Возвращает dict с ключами: 'summary_text', 'generated_title', 'model_provider', 'model_name'"""
    prompt_length = len(text)
    
    # Определяем модель и провайдера
    model_provider, model_name = _get_model_info(user['llm_model'])
    chat_options = {}
    if user['llm_model'] == 'gpt-4o':
        chat_options['primary'] = {
            'function': openai_functions.summarise_text_openai,
            'args': {'text': text, 'i18n': i18n},
            'provider': 'openai',
            'model': 'gpt-4o'
        }
        chat_options['groq'] = {
            'function': groq_functions.summarise_text,
            'args': {'text': text, 'i18n': i18n},
            'provider': 'groq',
            'model': 'gpt-oss-120b'
        }
        chat_options['fallback'] = {
            'function': anthropic_functions.summarise_text_anthropic,
            'args': {'text': text, 'i18n': i18n},
            'provider': 'anthropic',
            'model': 'claude-3-5-sonnet'
        }
    else:
        chat_options['groq'] = {
            'function': groq_functions.summarise_text,
            'args': {'text': text, 'i18n': i18n},
            'provider': 'groq',
            'model': 'gpt-oss-120b'
        }
        chat_options['primary'] = {
            'function': anthropic_functions.summarise_text_anthropic,
            'args': {'text': text, 'i18n': i18n},
            'provider': 'anthropic',
            'model': 'claude-3-5-sonnet'
        }
        chat_options['fallback'] = {
            'function': openai_functions.summarise_text_openai,
            'args': {'text': text, 'i18n': i18n},
            'provider': 'openai',
            'model': 'gpt-4o'
        }

    final_model_provide = None
    final_model_name = None
    result = None
    started_at = time.time()

    for service, options in chat_options.items():
        try:
            result = await options['function'](**options['args'])
            if result and result is not False:  # Проверяем, что результат не пустой
                logger.debug(f'Successfully processed summarise text request with {service}. Session: {session_id}')
                final_model_provide = options['provider']
                final_model_name = options['model']
                break
        except Exception as e:
            logger.error(f'Failed to process summarise text request with {service}. Session: {session_id}. Error: {e}')
            continue

    # Успешный результат
    if result:
        generated_title, clean_summary = await split_title_and_summary(text=result, i18n=i18n)
        llm_request_id = await create_llm_request(
            user_id=user['id'],
            session_id=session_id,
            request_type='summary',
            model_provider=final_model_provide,
            model_name=final_model_name,
            prompt_length=prompt_length,
            context_length=prompt_length,
            success=True,
            started_at=started_at
        )

        await save_summary_cache(
            transcription_id=transcription_id,
            language_code=user['user_language'],
            llm_provider=final_model_provide,
            llm_model=final_model_name,
            system_prompt=i18n.summarise_text_base_system_prompt_openai() if final_model_provide == 'openai' else i18n.summarise_text_system_prompt_gpt_oss(),
            summary_text=clean_summary,
            session_id=session_id,
            generated_title=generated_title,
            llm_request_id=llm_request_id
        )



        return {
            'summary_text': clean_summary,
            'generated_title': generated_title,
            'model_provider': model_provider,
            'model_name': model_name
        }
    else:
        llm_request_id = await create_llm_request(
            user_id=user['id'],
            session_id=session_id,
            request_type='summary',
            model_provider=final_model_provide,
            model_name=final_model_name,
            prompt_length=prompt_length,
            context_length=prompt_length,
            success=False,
            started_at=started_at
        )
        raise Exception("Models returned empty result. Session: {session_id}")
    

async def get_transcript(waiting_message, i18n: TranslatorRunner,
                         user_data: dict, language_code: str = None, audio_bytes: bytes | None = None, file_path: str = None,
                        audio_length: int = None, progress_manager=None, session_id: str = None,
                        file_data: dict = None, use_quality_model: bool = False) -> tuple[str, str, int]:
    """
    Get transcript from audio file
    file_data: dict = {
        'source_type': str,
        'original_identifier': str,
        'specific_source': str,
        'original_file_size': int,
        'audio_duration': float
    }
    """

    # Determine if we should suppress individual service progress updates
    use_dynamic_progress = progress_manager is not None

    # Базовые опции транскрипции
    base_options = {
        'private_stt': {
            'function': private_stt_client.process_audio,
            'args': {
                'file_buffer': audio_bytes if audio_bytes else None,
                'file_path': file_path if file_path else None,
                'waiting_message': waiting_message,
                'i18n': i18n,
                'suppress_progress': use_dynamic_progress,
                'session_id': session_id,
            }
        },
        'elevateai': {
            'function': process_audio_elevateai,
            'args': {
                'file_buffer': audio_bytes if audio_bytes else None,
                'file_path': file_path if file_path else None,
                'filename': 'audio_file.mp3',
                'language': language_code,
                'waiting_message': waiting_message,
                'i18n': i18n,
                'audio_length': audio_length,
                'suppress_progress': use_dynamic_progress,
            }
        },
        'deepgram': {
            'function': audio_to_text_deepgram,
            'args': {
                'file_bytes': audio_bytes if audio_bytes else None,
                'file_path': file_path if file_path else None,
                'waiting_message': waiting_message,
                'language_code': language_code,
                'i18n': i18n,
                'suppress_progress': use_dynamic_progress,
            }
        },
        'openai': {
            'function': openai_functions.audio_to_text,
            'args': {
                'file_bytes': audio_bytes if audio_bytes else None,
                'file_path': file_path if file_path else None,
                'waiting_message': waiting_message,
                'i18n': i18n,
                'suppress_progress': use_dynamic_progress,
            }
        },
        'fal': {
            'function': process_audio_fal,
            'args': {
                'audio_bytes': audio_bytes if audio_bytes else None,
                'file_path': file_path if file_path else None,
                'waiting_message': waiting_message,
                'i18n': i18n,
                'suppress_progress': use_dynamic_progress,
            }
        },
        'fireworks': {
            'function': audio_to_text_fireworks,
            'args': {
                'file_bytes': audio_bytes if audio_bytes else None,
                'file_path': file_path if file_path else None,
                'waiting_message': waiting_message,
                'i18n': i18n,
                'suppress_progress': use_dynamic_progress,
                'language_code': language_code,
            }
        },
        'assemblyai': {
            'function': process_audio_assemblyai,
            'args': {
                'audio_bytes': audio_bytes if audio_bytes else None,
                'file_path': file_path if file_path else None,
                'waiting_message': waiting_message,
                'i18n': i18n,
                'language_code': language_code,
                'suppress_progress': use_dynamic_progress,
            }
        }
    }

    # Определяем приоритетный порядок сервисов в зависимости от длительности аудио
    if use_quality_model:
        priority_order = ['deepgram', 'fireworks', 'openai', 'fal']
    else:
        if audio_length and audio_length > 300:  # Больше 5 минут - приоритет elevateai
            priority_order = ['fireworks', 'assemblyai' , 'deepgram', 'openai', 'fal']
            logger.debug(f'Audio length: {audio_length} seconds, priority order: {priority_order}')
        else:  # 5 минут или меньше - приоритет deepgram
            priority_order = ['fireworks', 'assemblyai', 'deepgram' , 'openai', 'fal']
            logger.debug(f'Audio length: {audio_length} seconds. Use deepgram. Priority order: {priority_order}')
    logger.info(f'Priority order: {priority_order}')

    # Создаем упорядоченный словарь согласно приоритету
    transcription_options = {service: base_options[service] for service in priority_order if service in base_options}

    last_error = None
    for service, options in transcription_options.items():
        try:
            timecoded_text, text = await options['function'](**options['args'])
            if timecoded_text and text:  # Проверяем, что результат не пустой
                if service == 'fireworks':
                    if len(text.split()) < 5:
                        raise ValueError(f"Fireworks STT returned small result. Session: {session_id}")
                logger.debug(f'Successfully processed audio with {service}')

                transcription_id = None
                try:
                    transcription_id = await save_transcription_cache(
                        source_type=file_data['source_type'],
                        original_identifier=file_data['original_identifier'],
                        transcript_raw=text,
                        transcript_timecoded=timecoded_text,
                        transcription_provider=service,
                        session_id=session_id,
                        specific_source=file_data['specific_source'],
                        file_hash=file_data.get('file_hash'),
                        file_size_bytes=file_data['original_file_size'],
                        audio_duration=file_data['audio_duration']
                    )
                    await update_processing_session(
                    session_id=session_id,
                    transcription_id=transcription_id
                    )
                except Exception as e:
                    logger.error(f'Failed to save transcription to cache: {e}')

                return text, timecoded_text, transcription_id
        except Exception as e:
            logger.error(f'Failed to process audio file. Service: {service}. Session: {session_id}. Error: {e}')
            last_error = e
            continue

    # Если все сервисы не сработали
    error_msg = f'All transcription services failed. Last error: {last_error}'
    logger.error(error_msg)
    raise Exception(error_msg)


async def process_audio(waiting_message, user: dict, i18n: TranslatorRunner,
                        language_code: str = None, file_bytes: BytesIO | None | bytes = None, file_path: str = None, session_id: str | None = None, audio_length: int = None,
                        progress_manager=None, file_data: dict = None, use_quality_model: bool = False, force_return_summary: bool = False, no_summary_threshold: int = 2000, audio_file_source_type: str = None) -> dict:
    """
    Process audio file and return summary, transcript and timecoded text
    file_data: dict = {
        'source_type': str,
        'original_identifier': str,
        'specific_source': str,
        'original_file_size': int,
        'audio_duration': float
    }
    return: dict[str, str, str, int, str] | dict[str, str, str]
    if dict[str, str, str, int, str] - return summary, transcript, timecoded text, transcription id, generated title
    if dict[str, str, str] - return transcript, timecoded text, transcription id <- if trascript is less than no_summary_threshold and force_return_summary is False
    """
    if not file_bytes and not file_path:
        raise ValueError("Either file_bytes or file_path must be provided")

    if file_bytes:
        if isinstance(file_bytes, BytesIO):
            audio_bytes = file_bytes.read()
        elif isinstance(file_bytes, bytes):
            audio_bytes = file_bytes
        else:
            raise ValueError("file_bytes must be a BytesIO or bytes")
    else:
        audio_bytes = None

    try:
        text, timecoded_text, transcription_id = await get_transcript(audio_bytes=audio_bytes if audio_bytes else None,
                                                    file_path=file_path if file_path else None,
                                                    waiting_message=waiting_message, i18n=i18n,
                                                    language_code=language_code,
                                                    audio_length=audio_length,
                                                    progress_manager=progress_manager,
                                                    session_id=session_id,
                                                    file_data=file_data,
                                                    use_quality_model=use_quality_model,
                                                                      user_data=user)

        logger.debug(i18n.text_content(text=text))

        # Update to summarization phase if progress manager is provided
        if progress_manager:
            from services.dynamic_progress_manager import ProgressPhase
            await progress_manager.start_phase(ProgressPhase.SUMMARIZING, 85, audio_length)
        else:
            # Fallback to old-style progress bar
            await waiting_message.edit_text(text=i18n.summarise_text(progress=progress_bar(85, i18n)))

        target_date = datetime.datetime(2025, 12, 9, 7, 0, 0)

        should_skip_summary = False

        # 1. New users don't get summary initially
        if user['created_at'] > target_date:
            should_skip_summary = True

        # 2. Short voice/video notes don't get summary (unless forced)
        if len(text) < no_summary_threshold and \
           audio_file_source_type in ['voice', 'video_note'] and \
           not force_return_summary:
            should_skip_summary = True

        if should_skip_summary and not force_return_summary:
            return {
                'raw_transcript': text,
                'timecoded_transcript': timecoded_text,
                'transcription_id': transcription_id
            }
        summary: dict = await summarise_text(text=text, user=user, i18n=i18n, session_id=session_id, transcription_id=transcription_id)
        voice_summary = summary['summary_text']
        generated_title = summary['generated_title']

        if not summary or not timecoded_text:
            raise ValueError(i18n.no_summary_transcript_error())

        return {
            'summary': voice_summary,
            'raw_transcript': text,
            'timecoded_transcript': timecoded_text,
            'transcription_id': transcription_id,
            'generated_title': generated_title
        }
    except Exception as e:
        logger.error(i18n.process_audio_error(error=str(e)))
        raise


async def generate_title(text: str, user: dict, i18n: TranslatorRunner) -> str:
    """
    Генерирует название для транскрипции через OpenAI или Grok.
    
    Args:
        text: Текст транскрипции
        user: Данные пользователя (для определения предпочитаемой модели)
        i18n: TranslatorRunner для получения промптов на нужном языке
        
    Returns:
        str: Сгенерированное название
        
    Raises:
        Exception: Если все модели не смогли сгенерировать название
    """
    
    # Определяем модель и провайдера
    title_options = {}
    
    # if user['llm_model'] == 'gpt-4o':
    title_options['primary'] = {
        'function': openai_functions.generate_title_openai,
        'args': {'text': text, 'i18n': i18n},
        'provider': 'openai',
        'model': 'gpt-4o-mini'
    }
    title_options['fallback'] = {
        'function': groq_functions.generate_title_grok,
        'args': {'text': text, 'i18n': i18n},
        'provider': 'groq',
        'model': 'gpt-oss-120b'
    }
    # else:
    #     title_options['primary'] = {
    #         'function': groq_functions.generate_title_grok,
    #         'args': {'text': text, 'i18n': i18n},
    #         'provider': 'groq',
    #         'model': 'gpt-oss-120b'
    #     }
    #     title_options['fallback'] = {
    #         'function': openai_functions.generate_title_openai,
    #         'args': {'text': text, 'i18n': i18n},
    #         'provider': 'openai',
    #         'model': 'gpt-4o-mini'
    #     }
    #
    last_error = None
    for service, options in title_options.items():
        try:
            result = await options['function'](**options['args'])
            if result and result.strip():  # Проверяем, что результат не пустой
                logger.debug(f'Successfully generated title with {service}: {result}')
                return result
        except Exception as e:
            logger.error(f'Failed to generate title with {service}: {e}')
            last_error = e
            continue
    
    # Если все модели не сработали
    error_msg = f'All title generation models failed. Last error: {last_error}'
    logger.error(error_msg)
    raise Exception(error_msg)
    