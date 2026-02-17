import re
import logging
from typing import List, Tuple, Dict

logger = logging.getLogger(__name__)


def parse_transcription_line(line: str) -> Tuple[float, float, str, str] | None:
    """
    Парсит строку транскрипции в формате [start - end] (speaker) text
    
    Args:
        line: Строка транскрипции
        
    Returns:
        Tuple (start_time, end_time, speaker, text) или None если строка не парсится
    """
    # Паттерн для парсинга: [36.02 - 36.96] (spk_1) Hey
    pattern = r'\[(\d+\.?\d*)\s*-\s*(\d+\.?\d*)\]\s*\(([^)]+)\)\s*(.+)'
    
    match = re.match(pattern, line.strip())
    if match:
        start_time = float(match.group(1))
        end_time = float(match.group(2))
        speaker = match.group(3).strip()
        text = match.group(4).strip()
        return start_time, end_time, speaker, text
    
    return None


def group_transcription_by_blocks(transcription_text: str, block_duration: float = 20.0) -> str:
    """
    Группирует транскрипцию в блоки по времени (минимум block_duration секунд) или при смене спикера.
    
    Правила группировки:
    1. Если говорящий один и тот же - группируем минимум по block_duration секунд
    2. При смене спикера - начинаем новый блок независимо от времени
    3. Если блок превышает block_duration секунд, можно продолжать до естественной паузы
    
    Args:
        transcription_text: Исходный текст транскрипции
        block_duration: Минимальная длительность блока в секундах (по умолчанию 20)
        
    Returns:
        Сгруппированный текст транскрипции
    """
    if not transcription_text:
        return ""
    
    lines = transcription_text.strip().split('\n')
    parsed_lines = []
    
    # Парсим все строки
    for line in lines:
        if line.strip():
            parsed = parse_transcription_line(line)
            if parsed:
                parsed_lines.append(parsed)
    
    if not parsed_lines:
        return transcription_text
    
    # Группируем строки
    grouped_blocks = []
    current_block = {
        'start_time': None,
        'end_time': None,
        'speaker': None,
        'texts': []
    }
    
    for start_time, end_time, speaker, text in parsed_lines:
        
        # Проверяем, нужно ли начать новый блок
        should_start_new_block = False
        
        if current_block['speaker'] is None:
            # Первый элемент
            should_start_new_block = False
        elif current_block['speaker'] != speaker:
            # Смена спикера - всегда новый блок
            should_start_new_block = True
        elif current_block['start_time'] is not None:
            # Тот же спикер - проверяем время
            block_current_duration = current_block['end_time'] - current_block['start_time']
            if block_current_duration >= block_duration:
                # Блок уже достаточно длинный, можно начать новый
                # Но проверим, есть ли естественная пауза (больше 2 секунд между фразами)
                time_gap = start_time - current_block['end_time']
                if time_gap > 2.0:
                    should_start_new_block = True
        
        if should_start_new_block and current_block['speaker'] is not None:
            # Сохраняем текущий блок
            block_text = ' '.join(current_block['texts'])
            block_start = format_time(current_block['start_time'])
            block_end = format_time(current_block['end_time'])
            
            grouped_blocks.append(f"[{block_start} - {block_end}] ({current_block['speaker']}) {block_text}")
            
            # Начинаем новый блок
            current_block = {
                'start_time': start_time,
                'end_time': end_time,
                'speaker': speaker,
                'texts': [text]
            }
        else:
            # Добавляем к текущему блоку
            if current_block['speaker'] is None:
                current_block['start_time'] = start_time
                current_block['speaker'] = speaker
            
            current_block['end_time'] = end_time
            current_block['texts'].append(text)
    
    # Добавляем последний блок
    if current_block['speaker'] is not None:
        block_text = ' '.join(current_block['texts'])
        block_start = format_time(current_block['start_time'])
        block_end = format_time(current_block['end_time'])
        
        grouped_blocks.append(f"[{block_start} - {block_end}] ({current_block['speaker']}) {block_text}")
    
    return '\n\n'.join(grouped_blocks)


def format_time(seconds: float) -> str:
    """
    Форматирует время в секундах в строку MM:SS.ss
    
    Args:
        seconds: Время в секундах
        
    Returns:
        Отформатированная строка времени
    """
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes:02d}:{secs:05.2f}"


def extract_plain_text(transcription_text: str) -> str:
    """
    Извлекает только чистый текст из транскрипции, убирая таймкоды и информацию о спикерах.
    
    Args:
        transcription_text: Исходный текст транскрипции в формате [start - end] (speaker) text
        
    Returns:
        Чистый текст без таймкодов и спикеров
    """
    if not transcription_text:
        return ""
    
    lines = transcription_text.strip().split('\n')
    plain_texts = []
    
    # Парсим все строки и извлекаем только текст
    for line in lines:
        if line.strip():
            parsed = parse_transcription_line(line)
            if parsed:
                _, _, _, text = parsed
                plain_texts.append(text)
    
    # Объединяем весь текст в одну строку
    return ' '.join(plain_texts)


def group_transcription_smart(transcription_text: str, 
                            min_block_duration: float = 20.0,
                            max_block_duration: float = 60.0,
                            pause_threshold: float = 2.0) -> str:
    """
    Умная группировка транскрипции с дополнительными параметрами.
    
    Args:
        transcription_text: Исходный текст транскрипции
        min_block_duration: Минимальная длительность блока в секундах
        max_block_duration: Максимальная длительность блока в секундах
        pause_threshold: Пороговая пауза для разделения блоков (секунды)
        
    Returns:
        Сгруппированный текст транскрипции
    """
    if not transcription_text:
        return ""
    
    lines = transcription_text.strip().split('\n')
    parsed_lines = []
    
    # Парсим все строки
    for line in lines:
        if line.strip():
            parsed = parse_transcription_line(line)
            if parsed:
                parsed_lines.append(parsed)
    
    if not parsed_lines:
        return transcription_text
    
    # Группируем строки
    grouped_blocks = []
    current_block = {
        'start_time': None,
        'end_time': None,
        'speaker': None,
        'texts': []
    }
    
    for start_time, end_time, speaker, text in parsed_lines:
        
        # Проверяем, нужно ли начать новый блок
        should_start_new_block = False
        
        if current_block['speaker'] is None:
            # Первый элемент
            should_start_new_block = False
        elif current_block['speaker'] != speaker:
            # Смена спикера - всегда новый блок
            should_start_new_block = True
        elif current_block['start_time'] is not None:
            # Тот же спикер - проверяем различные условия
            block_current_duration = current_block['end_time'] - current_block['start_time']
            time_gap = start_time - current_block['end_time']
            
            if block_current_duration >= max_block_duration:
                # Блок слишком длинный - принудительно разделяем
                should_start_new_block = True
            elif (block_current_duration >= min_block_duration and 
                  time_gap > pause_threshold):
                # Блок достаточно длинный и есть пауза
                should_start_new_block = True
        
        if should_start_new_block and current_block['speaker'] is not None:
            # Сохраняем текущий блок
            block_text = ' '.join(current_block['texts'])
            block_start = format_time(current_block['start_time'])
            block_end = format_time(current_block['end_time'])
            
            grouped_blocks.append(f"[{block_start} - {block_end}] ({current_block['speaker']}) {block_text}")
            
            # Начинаем новый блок
            current_block = {
                'start_time': start_time,
                'end_time': end_time,
                'speaker': speaker,
                'texts': [text]
            }
        else:
            # Добавляем к текущему блоку
            if current_block['speaker'] is None:
                current_block['start_time'] = start_time
                current_block['speaker'] = speaker
            
            current_block['end_time'] = end_time
            current_block['texts'].append(text)
    
    # Добавляем последний блок
    if current_block['speaker'] is not None:
        block_text = ' '.join(current_block['texts'])
        block_start = format_time(current_block['start_time'])
        block_end = format_time(current_block['end_time'])
        
        grouped_blocks.append(f"[{block_start} - {block_end}] ({current_block['speaker']}) {block_text}")
    
    return '\n\n'.join(grouped_blocks)


# Пример использования
if __name__ == "__main__":
    # Тестовые данные
    test_transcription = """[36.02 - 36.96] (spk_1) Hey
[36.96 - 37.84] (spk_1) Blake.
[37.84 - 37.92] (spk_1) Hey
[37.92 - 38.10] (spk_1) Blake.
[38.10 - 38.44] (spk_1) What's
[38.44 - 38.60] (spk_1) up
[38.60 - 39.26] (spk_1) boys?
[40.30 - 40.80] (spk_1) I'm
[40.80 - 40.94] (spk_1) doing
[40.94 - 41.72] (spk_1) well,
[41.72 - 42.70] (spk_2) yeah.
[42.70 - 43.60] (spk_2) Oh,
[43.60 - 43.72] (spk_2) you're
[43.72 - 44.18] (spk_2) prepared,
[44.18 - 44.20] (spk_2) you
[44.20 - 44.34] (spk_2) have
[44.34 - 44.50] (spk_2) a
[44.50 - 45.06] (spk_2) note-taker.
[45.06 - 45.20] (spk_2) Do
[45.20 - 45.34] (spk_2) you
[45.34 - 45.54] (spk_2) mind
[45.54 - 45.90] (spk_2) if
[45.90 - 46.06] (spk_1) we
[46.06 - 46.28] (spk_1) add
[46.28 - 46.54] (spk_1) our
[46.54 - 46.92] (spk_1) note-taker
[46.92 - 47.16] (spk_1) as
[47.16 - 47.80] (spk_1) well?"""
    
    print("Исходная транскрипция:")
    print(test_transcription)
    print("\n" + "="*50 + "\n")
    
    print("Сгруппированная транскрипция (базовая):")
    grouped = group_transcription_by_blocks(test_transcription, block_duration=20.0)
    print(grouped)
    print("\n" + "="*50 + "\n")
    
    print("Сгруппированная транскрипция (умная):")
    grouped_smart = group_transcription_smart(test_transcription, 
                                            min_block_duration=15.0,
                                            max_block_duration=45.0,
                                            pause_threshold=1.5)
    print(grouped_smart)
    print("\n" + "="*50 + "\n")
    
    print("Только чистый текст:")
    plain_text = extract_plain_text(test_transcription)
    print(plain_text)

