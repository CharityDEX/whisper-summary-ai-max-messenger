from dataclasses import dataclass
from typing import Optional

from environs import Env


@dataclass
class TgBot:
    token: str
    bot_id: str
    bot_name: str
    admin_ids: list
    default_lang: str
    bot_url: str
    admin_api_key: str = None
    log_chat_id: str = None
    dev_log_chat_id: str = None
    subscription_survey_url: Optional[str] = None
    service_name: str = 'whisper_bot.service'


@dataclass
class Proxy:
    proxy: str

@dataclass
class OpenAI:
    api_key: str

@dataclass
class Anthropic:
    api_key: str


@dataclass
class UserBot:
    session_name: str
    api_id: str
    api_hash: str
    user_bot_id: str


@dataclass
class Database:
    user: str
    password: str
    database: str

@dataclass
class Payment:
    public_id: str
    api_secret: str
    notification_url: str

@dataclass
class PrivateSTTConfig:
    api_url: str
    api_key: str

@dataclass
class Grok:
    api_key: str

@dataclass
class Fal:
    api_key: str

@dataclass
class ElevateAI:
    api_key: list

@dataclass
class Fireworks:
    api_key: str


@dataclass
class Price:
    weekly: int
    monthly: int
    semiannual: int
    monthly_discounted_notification: int


@dataclass
class AssemblyAI:
    api_key: str

@dataclass
class Stripe:
    secret_key: str
    public_key: str
    webhook_secret: str
    success_url: str
    cancel_url: str
    weekly_price_id: list[str]
    monthly_price_id: list[str]
    annual_price_id: str
    semiannual_price_id: list[str]
    # monthly_discounted_notification_id: str
    price: Price
    monthly_notification_coupon_id: str


@dataclass
class CloudPaymentsConfig:
    price: Price

@dataclass
class RapidApi:
    key: str


@dataclass
class GoogleAPI:
    service_account_file: str
    credentials_file: str = None


@dataclass
class HealthMonitor:
    enabled: bool
    health_chat_id: str
    check_interval_minutes: int
    check_command: str
    response_warning_seconds: int
    response_critical_seconds: int
    max_consecutive_failures: int
    # Manual check settings
    manual_check_enabled: bool = True
    manual_check_command: str = "/check_response_time"
    manual_check_allowed_users: list = None  # List of user IDs, None = all users in health_chat
    # Server metrics collection
    collect_metrics: bool = True  # Собирать метрики сервера при проверках
    extended_metrics_threshold_ms: int = 0  # Порог задержки для расширенных метрик (0 = всегда)
    bot_process_pattern: str = "python.*bot"  # Паттерн для поиска процесса бота (pgrep -f)
    local_server_process_pattern: str = "telegram-bot-api"  # Паттерн для local bot server


@dataclass
class PaymentReminders:
    """Конфигурация для напоминаний о незавершенных платежах"""
    first_reminder_hours: int = 2  # Время до первого напоминания (часы)
    second_reminder_hours: int = 24  # Время до второго напоминания (часы)
    search_window_hours: int = 1  # Окно поиска для батчинга (часы)


@dataclass
class FedorAPI:
    """Конфигурация для Fedor API (медиа обработка и транскрипция)"""
    username: str
    password: str


@dataclass
class MaxBot:
    token: str
    admin_ids: list
    default_lang: str
    log_chat_id: str = None


@dataclass
class Config:
    tg_bot: TgBot
    proxy: Proxy
    openai: OpenAI
    anthropic: Anthropic
    user_bot: UserBot
    db: Database
    payment: Payment
    grok: Grok
    fal: Fal
    elevateai: ElevateAI
    assemblyai: AssemblyAI
    fireworks: Fireworks
    stripe: Stripe
    cloudpayments: CloudPaymentsConfig
    rapidapi: RapidApi
    google_api: GoogleAPI
    private_stt: PrivateSTTConfig
    health_monitor: HealthMonitor
    payment_reminders: PaymentReminders
    fedor_api: FedorAPI
    max_bot: Optional[MaxBot] = None

# Глобальная переменная для хранения единственного экземпляра конфигурации
_config: Optional[Config] = None


def load_config(path: str | None = '.env') -> Config:
    """
    Загружает конфигурацию из .env файла.
    При первом вызове создает экземпляр конфигурации и кэширует его.
    Последующие вызовы возвращают закэшированный экземпляр.
    """
    global _config
    
    if _config is None:
        env: Env = Env()
        env.read_env(path)
        _config = Config(
            tg_bot=TgBot(
                token=env('BOT_TOKEN'),
                bot_id=env('BOT_ID'),
                admin_ids=list(map(int, env.list('ADMIN_IDS'))),
                default_lang=env('BOT_DEFAULT_LANG'),
                admin_api_key=env('ADMIN_API_KEY'),
                bot_url=env('BOT_URL'),
                log_chat_id=env('LOG_CHAT_ID', default=None),
                dev_log_chat_id=env('DEV_LOG_CHAT_ID', default=None),
                subscription_survey_url=env('SUBSCRIPTION_SURVEY_URL', default=''),
                bot_name=env('BOT_NAME', default=''),
                service_name=env('SERVICE_NAME', default='whisper_bot.service')
            ),
            proxy=Proxy(proxy=env('PROXY')),
            openai=OpenAI(api_key=env('OPENAI_API_KEY')),
            user_bot=UserBot(
                session_name=env('SESSION_NAME'),
                api_id=env('API_ID'),
                api_hash=env('API_HASH'),
                user_bot_id=env('USER_BOT_ID')
            ),
            db=Database(
                user=env('DB_USER'),
                password=env('DB_PASSWORD'),
                database=env('DB_NAME')
            ),
            payment=Payment(
                public_id=env('PAYMENT_PUBLIC_ID'),
                api_secret=env('PAYMENT_API_SECRET'),
                notification_url=env('PAYMENT_URL')
            ),
            grok=Grok(api_key=env('GROK_API_KEY')),
            fal=Fal(api_key=env('FAL_API_KEY')),
            elevateai=ElevateAI(api_key=list(env.list('ELEVATEAI_API_KEY'))),
            assemblyai=AssemblyAI(api_key=env('ASSEMBLYAI_API_KEY')),
            anthropic=Anthropic(api_key=env('ANTHROPIC_API_KEY')),
            fireworks=Fireworks(api_key=env('FIREWORKS_API_KEY')),
            stripe=Stripe(
                secret_key=env('STRIPE_SECRET_KEY'),
                public_key=env('STRIPE_PUBLIC_KEY'),
                webhook_secret=env('STRIPE_WEBHOOK_SECRET'),
                success_url=env('STRIPE_SUCCESS_URL'),
                cancel_url=env('STRIPE_CANCEL_URL'),
                weekly_price_id=list(env.list('STRIPE_WEEKLY_PRICE_ID')),
                monthly_price_id=list(env.list('STRIPE_MONTHLY_PRICE_ID')),
                annual_price_id=env('STRIPE_ANNUAL_PRICE_ID'),
                semiannual_price_id=list(env.list('STRIPE_SEMIANNUAL_PRICE_ID')),
                # monthly_discounted_notification_id=env('STRIPE_DISCOUNTED_NOTIFICATION_ID'),
                monthly_notification_coupon_id=env('STRIPE_COUPON_DISCOUNTED_NOTIFICATION_ID'),
                price=Price(
                    weekly=env.float('STRIPE_WEEKLY_PRICE', default=149),
                    monthly=env.float('STRIPE_MONTHLY_PRICE', default=349),
                    semiannual=env.float('STRIPE_SEMIANNUAL_PRICE', default=1999),
                    monthly_discounted_notification=env.float('STRIPE_DISCOUNTED_NOTIFICATION_PRICE', default=300)
                )
            ),
            cloudpayments=CloudPaymentsConfig(
                price=Price(
                    weekly=env.int('CLOUDPAYMENTS_WEEKLY_PRICE', default=149),
                    monthly=env.int('CLOUDPAYMENTS_MONTHLY_PRICE', default=349),
                    semiannual=env.int('CLOUDPAYMENTS_SEMIANNUAL_PRICE', default=1999),
                    monthly_discounted_notification=env.int('CLOUDPAYMENTS_DISCOUNTED_NOTIFICATION_PRICE')
                )
            ),
            rapidapi=RapidApi(key=env('RAPIDAPI_KEY')),
            google_api=GoogleAPI(
                service_account_file=env('GOOGLE_SERVICE_ACCOUNT_FILE'),
                credentials_file=env('GOOGLE_CREDENTIALS_FILE')
            ),
            private_stt=PrivateSTTConfig(
                api_url=env('PRIVATE_STT_API_URL'),
                api_key=env('PRIVATE_STT_API_KEY')
            ),
            health_monitor=HealthMonitor(
                enabled=env.bool('HEALTH_MONITOR_ENABLED', default=False),
                health_chat_id=env('HEALTH_CHAT_ID', default=''),
                check_interval_minutes=env.int('HEALTH_CHECK_INTERVAL_MINUTES', default=5),
                check_command=env('HEALTH_CHECK_COMMAND', default='/settings'),
                response_warning_seconds=env.int('HEALTH_CHECK_WARNING_SECONDS', default=10),
                response_critical_seconds=env.int('HEALTH_CHECK_CRITICAL_SECONDS', default=30),
                max_consecutive_failures=env.int('HEALTH_CHECK_MAX_FAILURES', default=3),
                manual_check_enabled=env.bool('HEALTH_MANUAL_CHECK_ENABLED', default=True),
                manual_check_command=env('HEALTH_MANUAL_CHECK_COMMAND', default='/check_response_time'),
                manual_check_allowed_users=[int(uid.strip()) for uid in env('HEALTH_MANUAL_CHECK_ALLOWED_USERS', default='').split(',') if uid.strip()] or None,
                collect_metrics=env.bool('HEALTH_COLLECT_METRICS', default=True),
                extended_metrics_threshold_ms=env.int('HEALTH_EXTENDED_METRICS_THRESHOLD_MS', default=0),
                bot_process_pattern=env('HEALTH_BOT_PROCESS_PATTERN', default='python.*bot'),
                local_server_process_pattern=env('HEALTH_LOCAL_SERVER_PATTERN', default='telegram-bot-api')
            ),
            payment_reminders=PaymentReminders(
                first_reminder_hours=env.int('PAYMENT_REMINDER_FIRST_HOURS', default=2),
                second_reminder_hours=env.int('PAYMENT_REMINDER_SECOND_HOURS', default=24),
                search_window_hours=env.int('PAYMENT_REMINDER_SEARCH_WINDOW_HOURS', default=1)
            ),
            fedor_api=FedorAPI(
                username=env('FEDOR_API_USERNAME'),
                password=env('FEDOR_API_PASSWORD')
            ),
            max_bot=MaxBot(
                token=env('MAX_BOT_TOKEN', default=''),
                admin_ids=list(map(int, env.list('MAX_ADMIN_IDS', default=''))) if env('MAX_ADMIN_IDS', default='') else [],
                default_lang=env('MAX_BOT_DEFAULT_LANG', default='ru'),
                log_chat_id=env('MAX_LOG_CHAT_ID', default=None),
            ) if env('MAX_BOT_TOKEN', default='') else None
        )

    return _config


def get_config() -> Config:
    """
    Возвращает глобальный экземпляр конфигурации.
    Если конфигурация еще не загружена, загружает ее из '.env'.
    """
    if _config is None:
        return load_config()
    return _config


def reload_config(path: str | None = '.env') -> Config:
    """
    Принудительно перезагружает конфигурацию из файла.
    Полезно для тестирования или при изменении конфигурации во время выполнения.
    """
    global _config
    _config = None
    return load_config(path)
