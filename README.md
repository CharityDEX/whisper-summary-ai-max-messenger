# Whisper Summary Max Bot

Audio/video transcription and AI summarization bot for **Max Messenger**, adapted from the original Telegram bot. Users send audio files, video files, or URLs — the bot transcribes the content and generates an AI-powered summary with follow-up Q&A chat.

## What the Bot Does

1. User sends an audio/video file or a URL (YouTube, etc.)
2. Bot downloads and extracts audio (ffmpeg)
3. Bot transcribes audio via STT APIs (AssemblyAI, Deepgram, Fireworks, etc.)
4. Bot summarizes the transcription using LLMs (OpenAI GPT-4o, Anthropic Claude, etc.)
5. Bot returns a formatted summary with options to:
   - Download as DOCX/PDF/TXT
   - Ask follow-up questions about the content (dialogue mode)
   - Export to Google Docs

Additional features: subscription plans (Stripe/CloudPayments), referral program, multi-language support (ru/en), admin panel, caching, audio processing queue.

## Project Structure

```
├── main.py                    # Telegram bot entry point (webhook mode)
├── max_main.py                # Max bot entry point (polling mode)
├── config_data/config.py      # Unified config for both platforms
├── handlers/                  # Telegram bot handlers (aiogram)
├── max_handlers/              # Max bot handlers (maxapi)
├── keyboards/                 # Telegram keyboards
├── max_keyboards/             # Max keyboards (inline only)
├── middlewares/                # Telegram middleware
├── max_middlewares/            # Max middleware
├── states/                    # Telegram FSM states
├── max_states/                # Max FSM states
├── models/                    # SQLAlchemy ORM (shared database)
├── services/                  # Shared business logic (STT, LLM, payments, etc.)
├── locales/                   # i18n translation files (Fluent format)
├── lexicon/                   # Static translation strings
└── requirements.txt
```

## Key Differences: Max vs Telegram

This section is for developers familiar with the Telegram (aiogram) codebase.

### Framework

| | Telegram | Max |
|--|----------|-----|
| Library | aiogram 3.8 | maxapi 0.9.13 |
| Startup | Webhook (aiohttp on port 3000) | Polling (`dp.start_polling()`) |
| API server | Local Bot API (localhost:8081) | Direct Max API |

### Event Types

| aiogram | maxapi | Notes |
|---------|--------|-------|
| `Message` | `MessageCreated` | `event.message` contains the Message object |
| `CallbackQuery` | `MessageCallback` | `event.callback` for callback data, `event.message` for the message |
| — | `BotStarted` | Fires when user opens bot for the first time |

### Handler Registration

```python
# Telegram (aiogram)
@router.message(Command('start'))
@router.callback_query(F.data == 'cancel')
@router.message(UserAudioSession.dialogue)

# Max (maxapi)
@router.message_created(Command('start'))
@router.message_callback(F.payload == 'cancel')    # payload, not data
@router.message_created(UserAudioSession.dialogue)
```

### FSM Context

```python
# Telegram — FSMContext injected by aiogram
async def handler(message: Message, state: FSMContext):
    await state.set_state(UserAudioSession.dialogue)
    await state.update_data(session_id=sid)
    data = await state.get_data()

# Max — MemoryContext injected by maxapi
async def handler(event: MessageCreated, context: MemoryContext):
    await context.set_state(UserAudioSession.dialogue)
    await context.update_data(session_id=sid)
    data = await context.get_data()
```

MemoryContext is **in-memory only** — all state is lost on bot restart. There is no Redis/persistent storage equivalent in maxapi yet.

### Keyboards

Max has **no ReplyKeyboardMarkup**. All keyboards are inline, returned as `Attachment` objects.

```python
# Telegram
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text='Cancel', callback_data='cancel')]
])
await message.answer('Text', reply_markup=keyboard)

# Max
from maxapi.types.attachments import CallbackButton, LinkButton
from maxapi.types.keyboards import ButtonsPayload

def _kb(*rows):
    return ButtonsPayload(buttons=list(rows)).pack()

keyboard = _kb([CallbackButton(text='Cancel', payload='cancel')])
await message.answer(text='Text', attachments=[keyboard])
```

Key differences:
- `callback_data` → `payload`
- `reply_markup=` → `attachments=[...]` (keyboards are attachments in Max)
- `ButtonsPayload(buttons=[])` (empty keyboard) causes API 400 error — never send empty keyboards

### Message Methods

```python
# Telegram
await message.answer('text', reply_markup=kb)
await message.edit_text('new text', reply_markup=kb)
await message.delete()

# Max
await message.answer(text='text', attachments=[kb])
await message.edit(text='new text', attachments=[kb])  # edit, not edit_text
await message.delete()
```

### User Extraction

```python
# Telegram — aiogram injects event_from_user automatically
user = data.get('event_from_user')
user_id = user.id

# Max — manual extraction from event type
if isinstance(event, MessageCreated):
    user = event.message.sender
elif isinstance(event, MessageCallback):
    user = event.callback.user
elif isinstance(event, BotStarted):
    user = event.user
user_id = user.user_id  # user_id, not id
```

### get_ids() for Context Resolution

The dispatcher resolves FSM context by `(chat_id, user_id)`. Different event types return these differently:

- `MessageCreated.get_ids()` → `(chat_id, sender.user_id)`
- `MessageCallback.get_ids()` → `(chat_id, callback.user.user_id)`

### MaxMessageCompat Wrapper

Shared services (progress manager, STT pipeline) expect aiogram's `Message` interface. `MaxMessageCompat` in `max_handlers/user_handlers.py` wraps maxapi's `Message` to provide compatible methods:

```python
class MaxMessageCompat:
    """Wraps maxapi Message to provide aiogram-compatible interface."""
    def edit_text(...)   # → message.edit()
    def delete()         # → message.delete()
    .message_id          # → message.body.mid
    .text                # → message.body.text
    .chat.id             # → message.recipient.chat_id
```

### Middleware

```python
# Telegram — event_from_user is auto-injected
class UserMiddleware(BaseMiddleware):  # aiogram BaseMiddleware
    async def __call__(self, handler, event, data):
        user = data.get('event_from_user')
        ...

# Max — must extract user manually from event type
class UserMiddleware(BaseMiddleware):  # maxapi BaseMiddleware
    async def __call__(self, handler, event_object, data):
        user = _extract_max_user(event_object)  # helper function
        ...
```

### Shared Database

Both bots share the **same PostgreSQL database**. Max user IDs are stored in the `telegram_id` column (legacy naming). The `create_new_user()` function accepts either `message=` (Telegram) or `max_user=` (Max).

### What Max Bot Does NOT Have (vs Telegram)

- No webhook mode (polling only)
- No ReplyKeyboardMarkup (persistent menus)
- No onboarding/payment reminder scheduler jobs
- No health monitoring / metrics endpoint
- No persistent FSM storage (memory only, lost on restart)

## Local Setup

### Prerequisites

- Python 3.10
- PostgreSQL
- ffmpeg & ffprobe (in `~/bin` or system PATH)

### 1. Clone and install dependencies

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file with at minimum:

```env
# Max Bot (required for max_main.py)
MAX_BOT_TOKEN=your_max_bot_token
MAX_ADMIN_IDS=123456789
MAX_BOT_DEFAULT_LANG=ru
MAX_LOG_CHAT_ID=               # optional, for alerts

# Telegram Bot (required — shared services depend on it)
BOT_TOKEN=your_telegram_bot_token
BOT_ID=your_bot_id
BOT_NAME=your_bot_name
ADMIN_IDS=123456789
DEFAULT_LANG=ru
BOT_URL=https://t.me/your_bot

# Database (PostgreSQL)
DB_USER=postgres
DB_PASSWORD=your_password
DB_NAME=whisper_summary

# API Keys (at least one STT + one LLM required)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
ASSEMBLYAI_API_KEY=...

# Proxy (if needed)
PROXY=

# Fedor API
FEDOR_API_USERNAME=
FEDOR_API_PASSWORD=
```

### 3. Start the Max bot

```bash
source .venv/bin/activate
AIOHTTP_NO_EXTENSIONS=1 python max_main.py
```

The `AIOHTTP_NO_EXTENSIONS=1` flag avoids C extension issues on some platforms.

### 4. Start the Telegram bot (separate process)

```bash
source .venv/bin/activate
python main.py
```

Both bots can run simultaneously — they share the same database but poll/webhook independently.
