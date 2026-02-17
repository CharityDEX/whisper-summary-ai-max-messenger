import enum
from datetime import datetime, timedelta

from sqlalchemy import Column, Integer, String, ForeignKey, Enum as DBEnum, DateTime, Boolean, Date, BigInteger, Float, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(String, nullable=False)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)

    # subscription = Column(Enum(None, 'basic', 'advanced', 'premium'), nullable=False, default=None)
    subscription = Column(String, default='False')
    subscription_id = Column(String, default=None)
    start_date = Column(DateTime, default=datetime.utcnow)
    end_date = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=3))
    audio_uses = Column(Integer, default=0)
    gpt_uses = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String)
    subscription_type = Column(String, default=None)
    subscription_autopay = Column(Boolean, default=False)

    user_language = Column(String, default='ru')
    llm_model = Column(String, default='gpt-4o')
    specify_audio_language = Column(Boolean, default=False)
    download_video = Column(Boolean, default=True)
    transcription_format = Column(String, default='docx')
    is_bot_blocked = Column(Boolean, default=False)

    # Referral system fields
    referral_registered = Column(Integer, default=0)  # Количество заработанных недель
    referral_given = Column(Integer, default=0)  # Количество выданных недель


class Referral(Base):
    __tablename__ = 'referrals'
    id = Column(Integer, primary_key=True, autoincrement=True)
    referrer_id = Column(Integer, ForeignKey('users.id'), nullable=False)  # Кто пригласил
    referred_id = Column(Integer, ForeignKey('users.id'), nullable=False)  # Кого пригласили
    created_at = Column(DateTime, default=datetime.utcnow)
    reward_given = Column(Boolean, default=False)  # Была ли выдана награда
    made_payment = Column(Boolean, default=False)  # Был ли оплачен платное объект
    payment_date = Column(DateTime, nullable=True)  # Когда было оплачено платное объект
    reward_date = Column(DateTime, nullable=True)  # Когда была выдана награда
    subscription_type = Column(String, nullable=True)  # Тип подписки, за которую дали награду
    
    # Relationships
    referrer = relationship("User", foreign_keys=[referrer_id])
    referred = relationship("User", foreign_keys=[referred_id])


class NotificationStatusEnum(enum.Enum):
    pending = 'pending'
    sent = 'sent'
    not_required = 'not_required'
    failed = 'failed'

class RecoveryStatusEnum(enum.Enum):
    pending_decision = 'pending_decision'
    retry_scheduled = 'retry_scheduled'
    retried = 'retried'
    notification_only = 'notification_only'
    ignored = 'ignored'
    manual_review_required = 'manual_review_required'

class ProcessingSession(Base):
    """Сводная таблица для отслеживания полного цикла обработки файла"""
    __tablename__ = 'processing_sessions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, unique=True, nullable=False, index=True)  # UUID
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    # Исходные данные (что пользователь отправил)
    original_identifier = Column(String, nullable=False)  # original URL/file_id
    source_type = Column(String, nullable=False)  # 'url', 'telegram'
    specific_source = Column(String)  # 'youtube', 'instagram', etc.
    
    # Временные метки end-to-end
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime)
    total_duration = Column(Float)  # от отправки до готового ответа в секундах
    
    # Финальный результат (главная метрика!)
    final_status = Column(String)  # 'success', 'failed', 'interrupted'
    error_stage = Column(String)   # 'download', 'audio_extraction', 'transcription', 'summary'
    error_message = Column(String)

    # Статус уведомления, использующий наш новый тип ENUM
    notification_status = Column(
        ENUM(NotificationStatusEnum, name='notification_status_enum', create_type=False),
        nullable=False,
        server_default=NotificationStatusEnum.not_required.value
    )

    recovery_status = Column(
        ENUM(RecoveryStatusEnum, name='recovery_status_enum', create_type=False),
        nullable=False,
        server_default=RecoveryStatusEnum.ignored.value
    )
    
    # Метрики
    original_file_size = Column(BigInteger)  # размер исходного файла
    total_download_attempts = Column(Integer, default=0)
    # ID сообщения ожидания в Telegram (для редактирования/удаления после рестартов)
    waiting_message_id = Column(BigInteger)
    user_original_message_id = Column(BigInteger)

    # Связь с транскрипцией (может быть из кэша или новая)
    transcription_id = Column(Integer, ForeignKey('transcriptions.id'))

    # Relationships
    user = relationship("User")
    transcription = relationship("Transcription", foreign_keys=[transcription_id])
    downloads = relationship("FileDownload", back_populates="session")
    audio = relationship("Audio", back_populates="session", uselist=False)
    llm_requests = relationship("LLMRequest", back_populates="session")


class Audio(Base):
    __tablename__ = 'audio'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    length = Column(Float)  # длительность аудио в секундах
    
    # Связь с session
    session_id = Column(String, ForeignKey('processing_sessions.session_id'), nullable=True)
    
    # Поля для обратной совместимости (можно удалить после миграции)
    source_type = Column(String)  # 'telegram', 'url'
    source_identifier = Column(String)  # file_id или URL
    specific_source = Column(String)
    file_size_bytes = Column(BigInteger)  # размер файла
    processing_duration = Column(Float)  # время обработки в секундах
    success = Column(Boolean, default=False)  # успешно ли обработано
    error_message = Column(String)  # текст ошибки если была
    
    # Relationships
    user = relationship("User")
    session = relationship("ProcessingSession", back_populates="audio")

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    source = Column(String)
    token = Column(String)
    transaction_type = Column(String)
    transaction_id = Column(String, nullable=True, index=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    amount = Column(Float)
    status = Column(String)
    
    
# Enum for download status
class DownloadStatus(enum.Enum):
    PENDING = 'pending'
    DOWNLOADING = 'downloading' # Optional: more granular status
    DOWNLOADED = 'downloaded'
    PROCESSING = 'processing' # Optional: if there's a step after download
    COMPLETED = 'completed' # Optional: final success state
    ERROR = 'error'


class LLMRequest(Base):
    __tablename__ = 'llm_requests'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey('processing_sessions.session_id'), nullable=True)  # Связь с ProcessingSession
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    # Основная информация о запросе
    request_type = Column(String(50), nullable=False)  # 'summary', 'chat', 'title_generation', etc.
    model_provider = Column(String(50), nullable=False)  # 'openai', 'anthropic', 'groq', etc.
    model_name = Column(String(100), nullable=False)  # 'gpt-4', 'claude-3-sonnet', etc.
    
    # Контент и размеры
    prompt_length = Column(Integer, nullable=True)  # Длина промпта в символах
    context_length = Column(Integer, nullable=True)  # Длина всего контекста в символах
    response_length = Column(Integer, nullable=True)  # Длина ответа в символах
    
    # Токены (если доступны)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    
    # Время выполнения
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    processing_duration = Column(Float, nullable=True)  # В секундах
    
    # Результат
    success = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)
    
    # Стоимость (если известна)
    estimated_cost_usd = Column(Float, nullable=True)
    
    # Связи
    session = relationship("ProcessingSession", back_populates="llm_requests")
    user = relationship("User")


class AnonymousChatMessage(Base):
    """
    Анонимное хранение сообщений чата для маркетингового анализа.
    Полностью отделена от пользователей - только текст и структура диалогов.
    """
    __tablename__ = 'anonymous_chat_messages'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Анонимный идентификатор сессии чата (UUID)
    chat_session = Column(String(36), nullable=False, index=True)
    
    # Роль отправителя сообщения
    message_from = Column(String(20), nullable=False)  # 'user' или 'assistant'
    
    # Содержимое сообщения
    text = Column(Text, nullable=False)
    
    # Порядковый номер сообщения в диалоге (для восстановления последовательности)
    message_order = Column(Integer, nullable=False)
    
    # Временная метка (без связи с конкретным пользователем)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Мета-информация (опционально)
    message_length = Column(Integer, nullable=True)  # Длина сообщения
    
    # Индексы для аналитики - используем Index вместо Column в __table_args__
    # Составной индекс создается через SQL миграцию


class Transcription(Base):
    """Кэш транскрипций для переиспользования"""
    __tablename__ = 'transcriptions'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Идентификация источника
    source_type = Column(String(50), nullable=False)  # 'url', 'telegram'
    original_identifier = Column(Text, nullable=False)  # оригинальная ссылка/file_id
    source_key = Column(String(500), unique=True, nullable=False, index=True)  # нормализованный ключ
    specific_source = Column(String(50))  # 'youtube', 'instagram', etc.

    # Хэш файла (опционально)
    file_hash = Column(String(64), index=True)  # SHA256
    file_size_bytes = Column(BigInteger)
    audio_duration = Column(Float)

    # Результаты транскрипции
    transcript_raw = Column(Text, nullable=False)  # без таймкодов
    transcript_timecoded = Column(Text, nullable=False)  # с таймкодами

    # Метаданные транскрипции
    transcription_provider = Column(String(50), nullable=False)  # 'whisper', 'deepgram'
    transcription_model = Column(String(100))  # 'whisper-large-v3'
    language_detected = Column(String(10))  # 'ru', 'en'

    # Связь с первой сессией
    created_by_session_id = Column(String(36), ForeignKey('processing_sessions.session_id'))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Статистика использования
    reuse_count = Column(Integer, default=0)
    last_reused_at = Column(DateTime)

    # Relationships
    created_by_session = relationship("ProcessingSession", foreign_keys=[created_by_session_id])
    summaries = relationship("Summary", back_populates="transcription")


class Summary(Base):
    """Кэш саммари для разных языков и моделей"""
    __tablename__ = 'summaries'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Связь с транскрипцией
    transcription_id = Column(Integer, ForeignKey('transcriptions.id'), nullable=False)

    # Параметры генерации
    language_code = Column(String(10), nullable=False)  # 'ru', 'en'
    llm_provider = Column(String(50), nullable=False)  # 'openai', 'anthropic'
    llm_model = Column(String(100), nullable=False)  # 'gpt-4o', 'claude-3-sonnet'
    system_prompt_hash = Column(String(64), nullable=False)  # SHA256 промпта

    # Результат
    summary_text = Column(Text, nullable=False)
    generated_title = Column(Text)

    # Связь с LLM запросом
    llm_request_id = Column(Integer, ForeignKey('llm_requests.id'))

    # Связь с первой сессией
    created_by_session_id = Column(String(36), ForeignKey('processing_sessions.session_id'))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Статистика использования
    reuse_count = Column(Integer, default=0)
    last_reused_at = Column(DateTime)

    # Relationships
    transcription = relationship("Transcription", back_populates="summaries")
    llm_request = relationship("LLMRequest")
    created_by_session = relationship("ProcessingSession", foreign_keys=[created_by_session_id])


class FileDownload(Base):
    __tablename__ = 'file_downloads'

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Связь с пользователем
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    # Связь с сессией обработки
    session_id = Column(String, ForeignKey('processing_sessions.session_id'), nullable=True)
    
    # Время записи в БД (начало операции)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Детали попытки загрузки
    attempt_number = Column(Integer, default=1)  # номер попытки в рамках сессии
    download_method = Column(String)  # 'rapidapi', 'cobalt', 'direct', 'telegram'
    
    # Общий источник ('url', 'telegram')
    source_type = Column(String, nullable=False)
    # Конкретный URL или file_id Telegram (может быть промежуточный)
    identifier = Column(String, nullable=False)
    # Конкретный источник для URL ('youtube', 'instagram', etc.)
    specific_source = Column(String, nullable=True)
    # Куда скачивали ('disk', 'buffer')
    destination_type = Column(String, nullable=False)
    # Статус скачивания
    status = Column(DBEnum(DownloadStatus, name='download_status_enum', create_constraint=True), default=DownloadStatus.PENDING, nullable=False)
    # Размер файла в байтах (может быть известен до или после скачивания)
    file_size_bytes = Column(BigInteger, nullable=True)
    # Длительность скачивания/обработки в секундах (если успешно)
    duration_seconds = Column(Float, nullable=True)
    # Путь к временному файлу (если скачивали на диск)
    temp_file_path = Column(String, nullable=True)
     # Сообщение об ошибке (если status='error')
    error_message = Column(String, nullable=True)

    # Relationships
    user = relationship("User")
    session = relationship("ProcessingSession", back_populates="downloads")


class BotHealthCheck(Base):
    """Health checks monitoring table"""
    __tablename__ = 'bot_health_checks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    check_type = Column(String(50), nullable=False)  # 'command', 'audio_processing', etc.
    check_command = Column(String(100))  # '/settings', '/start', etc.

    # Timing
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime)
    response_time_ms = Column(Integer)  # время ответа в миллисекундах

    # Result
    success = Column(Boolean, default=False)
    error_message = Column(Text, nullable=True)

    # Additional context
    bot_responded = Column(Boolean, default=False)  # получили ли ответ от бота
    expected_response = Column(Text, nullable=True)  # ожидаемый ответ (для валидации)
    actual_response = Column(Text, nullable=True)  # фактический ответ

    # Metadata
    monitor_version = Column(String(50), nullable=True)  # версия монитора

    # Server metrics (JSONB) - метрики сервера во время проверки
    # Содержит: cpu_percent, memory_percent, load_avg, fd_counts, close_wait, pg_connections, etc.
    server_metrics = Column(JSONB, nullable=True)


class UserAction(Base):
    """
    Универсальная таблица для отслеживания всех действий пользователя.

    Используется для:
    - Анализа конверсионной воронки (от просмотра меню до покупки)
    - Истории жизненного цикла подписок
    - Взаимодействия с ботом
    - Обработки контента
    - Реферальной программы
    - Административных действий
    """
    __tablename__ = 'user_actions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)

    # Тип и категория действия
    action_type = Column(String(100), nullable=False, index=True)
    action_category = Column(String(50), nullable=False, index=True)

    # Гибкие дополнительные данные (JSON)
    meta = Column(name='metadata', type_=JSONB, default={}, nullable=False)

    # Опциональные связи с другими сущностями
    session_id = Column(String(36), ForeignKey('processing_sessions.session_id'), nullable=True)
    payment_id = Column(Integer, ForeignKey('payments.id'), nullable=True)
    referral_id = Column(Integer, ForeignKey('referrals.id'), nullable=True)

    # Временная метка
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    user = relationship("User")
    session = relationship("ProcessingSession")
    payment = relationship("Payment")
    referral = relationship("Referral")
