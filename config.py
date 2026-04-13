"""Bridge configuration (no CLI; edit here)."""

import re
from pathlib import Path

from meshtastic.protobuf import mesh_pb2

# --- Yandex Weather (forecast API for scheduled/on-demand weather on the mesh) ---
YANDEX_WEATHER_API_KEY = ""  # REDACTED — paste your Yandex Weather API key.
YANDEX_WEATHER_LAT = 51.7558  # REDACTED — forecast latitude (decimal degrees); example point only.
YANDEX_WEATHER_LON = 32.6176  # REDACTED — forecast longitude (decimal degrees).
YANDEX_WEATHER_FORECAST_URL = "https://api.weather.yandex.ru/v2/forecast"  # Base URL for forecast HTTP calls.
YANDEX_WEATHER_REQUEST_TIMEOUT_SEC = 25.0  # HTTP client timeout for each weather API request.

_CONFIG_DIR = Path(__file__).resolve().parent  # Directory containing this file (used for cache paths).
WEATHER_CACHE_FILE = _CONFIG_DIR / "weather_cache.json"  # On-disk cache for API responses to avoid hammering Yandex.
# Maps mesh packet id → original sender node id so threaded replies resolve correctly; survives restarts.
MESH_PACKET_ORIGIN_CACHE_FILE = _CONFIG_DIR / "mesh_packet_origin_cache.json"
WEATHER_STALE_HOURS = 24  # Refetch forecast when cached data is older than this many hours.
WEATHER_BROADCAST_CHANNEL_INDEX = 0  # Primary channel index for scheduled weather broadcasts.
WEATHER_TZ = "Europe/Moscow"  # Time zone name for interpreting “today” and local times in weather logic.
# Daily AI weather narrative on mesh + Telegram at this local wall time (WEATHER_TZ).
WEATHER_SCHEDULE_HOUR = 7  # 0–23
WEATHER_SCHEDULE_MINUTE = 0  # 0–59
# Appended after the scheduled MSK AI weather on mesh and Telegram; empty string to omit.
WEATHER_MORNING_AI_FOOTNOTE = (
    "Я нейросеть. Пиши в DM, ответь на мое сообщение или позови @fuz."
)

# --- Meshtastic device link (TCP to a network device or USB serial) ---
MESH_CONNECTION_MODE = "tcp"  # How to reach the radio: "tcp" or "serial".
# MESH_CONNECTION_MODE = "serial"
MESH_TCP_HOST = "192.168.1.100"  # REDACTED — node IP or hostname when using TCP.
MESH_TCP_PORT = 4403  # TCP port the node’s API listens on.
SERIAL_PORT = "/dev/ttyACM0"  # Device path when using USB serial.

# --- Telegram (mirror mesh text; optional notifications for AI replies) ---
TELEGRAM_NOTIFIER_NAME = "Notifier"  # Label for the background notifier thread (logging/debug only).
TELEGRAM_BOT_HANDLE = "your_bot_username"  # REDACTED — bot @username without @ (reference only).
TELEGRAM_BOT_TOKEN = ""  # REDACTED — HTTP API token from @BotFather.
TELEGRAM_CHAT_ID = 0  # REDACTED — destination chat id for forwarded mesh messages.
LOG_DIR = "mesh_logs"  # Subdirectory (under cwd) for per-key log files written by the threaded logger.
LOGGER_POOL_SIZE = 5  # Number of concurrent logger worker threads.

AUTO_REPLY_ENABLED = True  # Master switch: run AI / automated mesh replies; Telegram mirroring stays on.
AUTO_REPLY_USE_THREAD = True  # Prefer Meshtastic threaded replies when the stack supports them.

# --- Local LLM (OpenAI-compatible HTTP API, e.g. llama.cpp server) ---
LLAMA_BASE_URL = "http://127.0.0.1:8080"  # REDACTED — OpenAI-compatible server root (no trailing path).
LLAMA_MODEL = "gpt-3.5-turbo"  # Model id string the server expects in API calls.
LLAMA_MAX_TOKENS = 192  # Upper bound on generated tokens per completion.
LLAMA_TEMPERATURE = 0.8  # Sampling temperature for local completions.
LLAMA_CONNECT_TIMEOUT_SEC = 30.0  # Seconds to wait when opening the HTTP connection to the LLM.
LLAMA_READ_TIMEOUT_SEC = 600.0  # Seconds to wait for the full response body (slow local inference).

# --- Google Gemini (cloud; preferred for broadcast, optional for DMs) ---
GEMINI_API_KEY = ""  # REDACTED — Google AI API key for Gemini.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"  # REST base for generateContent.
GEMINI_MODEL = "gemini-3.1-flash-lite-preview"  # Model id for generateContent requests.
GEMINI_CONNECT_TIMEOUT_SEC = 30.0  # HTTP connect timeout for Gemini.
GEMINI_READ_TIMEOUT_SEC = 120.0  # HTTP read timeout for Gemini responses.

AI_USE_GEMINI_IN_DM = True  # If True, try Gemini for DMs then fall back to llama; if False, DMs use only llama.

AI_CONTEXT_MAX_MESSAGES = 15  # Max messages kept per chat context (user/assistant pairs); should work well with pairing.

# Broadcast debounce: wait at least this many seconds after starting generation before transmitting the reply.
# New mesh traffic in that window cancels the in-flight reply and starts a fresh turn.
AI_BROADCAST_MIN_SEC_BEFORE_SEND = 20.0

# On broadcast, still run AI if the message matches this regex (e.g. @node) when thread parent is someone else or unknown.
AI_BROADCAST_DIRECT_MENTION_RE = r"(?i)@your_node_shortname\b"  # Add alternatives with | as needed.

# If any regex matches the plain mesh text, skip AI for that packet (Telegram still receives the message).
AI_IGNORE_MESSAGE_REGEXES: tuple[str, ...] = (
    r"LOCAL_WEATHER_PREFIX\s*:",  # Example: skip beacons matching this pattern (adjust to your mesh).
)

# --- Automated mesh replies (no LLM): ping/pong and weather keywords ---
MESH_AUTOMATED_PONG_REGEX = r"(?i)^\s*/?\s*(ping|test|тест|пинг)\s*[!!.…]*\s*$"  # Whole-message match for ping-like probes.
MESH_AUTOMATED_PONG_TEXT = "Pong 🏓"  # Fixed text sent back when the pong regex matches.

MESH_AUTOMATED_WEATHER_REGEX = (  # Whole-message match for short weather/forecast requests.
    r"(?i)^\s*/?\s*(прогноз(?:\s+погоды)?|погода|forecast|weather)\s*[!!.…]*\s*$"
)

MODEL_NAME = "MeshAssist"  # Display name woven into the llama system prompt (persona label).

# System instructions for the local LLM on general mesh Q&A (Russian, brevity, node/signal hints).
LLAMA_SYSTEM_PROMPT = (
    f"Ты — нейросеть {MODEL_NAME}, которая отвечает через базовый стационарный узел сети Meshtastic "
    "(укажи в config.py короткое имя узла и город/регион под свою установку).\n"
    "Отвечай **очень кратко**, не более 2 предложений или 400 символов.\n"
    "Отвечай по-русски, дружелюбно, позитивно.\n"
    "\n"
    "Не придумывай факты, которых не знаешь.\n"
    "Если тебя спросят, кто ты, отвечай кратко, не притворяйся.\n"
    "Не начинай сам разговор о качестве сигнала; если спросят — ориентируйся на шкалы ниже.\n"
    "\n"
    "Шкала силы связи (Meshtastic, ориентиры):\n"
    "    SNR в дБ — хорошо > -5, нормально -10…-5, слабо < -10\n"
    "    RSSI в дБм — хорошо > −100, нормально −115…−100, слабо <−115.\n"
    "\n"
    "Дополнительная информация (замени на факты о своём узле и железе, если нужно модели):\n"
    "    Твой узел: модель радио, антенна и примерное расположение — задай вручную в этом промпте.\n"
    f"    Нейросеть {MODEL_NAME} работает на локальном или облачном бэкенде, который настроил оператор сети.\n"
)

# System instructions for Gemini on general mesh Q&A (tone differs slightly from llama).
GEMINI_SYSTEM_PROMPT = (
    "Ты — нейросеть, которая отвечает через базовый стационарный узел сети Meshtastic "
    "(укажи в config.py имя узла и населённый пункт под свою установку).\n"
    "Отвечай **очень кратко**, не более 2 предложений или 400 символов.\n"
    "Отвечай по-русски, дружелюбно, позитивно, с легким юмором но по делу.\n"
    "\n"
    "Не придумывай факты, которых не знаешь.\n"
    "Если тебя спросят, кто ты, отвечай кратко, не притворяйся.\n"
    "Не начинай сам разговор о качестве сигнала; если спросят — ориентируйся на шкалы ниже.\n"
    "\n"
    "Шкала силы связи (Meshtastic, ориентиры):\n"
    "    SNR в дБ — хорошо > -5, нормально -10…-5, слабо < -10\n"
    "    RSSI в дБм — хорошо > −100, нормально −115…−100, слабо <−115.\n"
    "\n"
    "Дополнительная информация (не упоминай, если прямо не спросят; замени на свои факты в config.py):\n"
    "    Твой узел: модель радио, антенна и примерное расположение — задай вручную в этом промпте.\n"
)

# System prompt for turning Yandex JSON into a short spoken-style forecast (llama path).
LLAMA_WEATHER_NARRATIVE_SYSTEM_PROMPT = (
    "Ты составляешь краткий прогноз погоды на сегодня для жителей выбранного населённого пункта (задаётся координатами в config.py).\n"
    "Ниже даны фактические данные из API Яндекс.Погоды — опирайся только на них, ничего не выдумывай.\n"
    "Ответь по-русски, дружелюбно, 2–4 коротких предложения, без списков и без заголовков.\n"
    "Уложись примерно в 500 символов.\n"
    "Обязательно укажи темературу утром, днем, вечером и ночью.\n"
    "Расскажи про осадки и облачность.\n"
    "Расскажи про ветер и порывы.\n"
)

# System prompt for turning Yandex JSON into a short forecast for mesh (Gemini path; mesh-friendly tone).
GEMINI_WEATHER_NARRATIVE_SYSTEM_PROMPT = (
    "Ты составляешь краткий прогноз погоды на сегодня для жителей выбранного населённого пункта (координаты в config.py).\n"
    "Ниже даны фактические данные из API Яндекс.Погоды — опирайся только на них, ничего не выдумывай.\n"
    "Ответь по-русски, дружелюбно, позитивно, с легким юмором но по делу, 2–4 коротких предложения, без списков и без заголовков.\n"
    "Уложись примерно в 500 символов.\n"
    "Обязательно укажи температуру утром, днём, вечером и ночью.\n"
    "Расскажи про осадки и облачность.\n"
    "Расскажи про ветер и порывы.\n"
    "Текст будет в эфире Meshtastic — избегай лишней воды и markdown.\n\n"
)

TELEGRAM_NOTIFY_MESH_AUTO_REPLY = True  # Also post AI/automated mesh replies to Telegram when enabled in code paths.

# --- Meshtastic TCP reconnect backoff ---
RECONNECT_INITIAL_DELAY_SEC = 1.0  # First sleep after a failed or dropped connection.
RECONNECT_MAX_DELAY_SEC = 60.0  # Cap for exponential backoff between reconnect attempts.

TELEGRAM_MAX_LEN = 4096  # Hard limit for a single Telegram message body (MarkdownV2 path).

# Protocol limit for one mesh Data payload (bytes); derived from firmware protobuf constant.
_PROTO_DATA_PAYLOAD_LEN = int(mesh_pb2.Constants.DATA_PAYLOAD_LEN)
MESH_PROTO_DATA_PAYLOAD_MAX_BYTES = _PROTO_DATA_PAYLOAD_LEN  # Documented maximum raw payload size for reference.
MESH_TEXT_PAYLOAD_SAFETY_MARGIN_BYTES = 24  # Shrink each chunk by this many bytes under the protocol max for RF reliability.
MESH_TEXT_MAX_PAYLOAD_BYTES = _PROTO_DATA_PAYLOAD_LEN - MESH_TEXT_PAYLOAD_SAFETY_MARGIN_BYTES  # Per-part UTF-8 budget when splitting.
MESH_TEXT_MAX_PARTS = 4  # Maximum number of mesh packets for one logical outgoing message.

MESH_MULTI_PART_DELAY_SEC = 2.0  # Pause between sending consecutive parts of a split message.
MESH_MULTI_PART_SEND_RETRIES = 3  # Retries per part when sending multi-part text.

MESH_ROUTING_ACK_TIMEOUT_SEC = 120  # Seconds to wait for routing ACK after each send (library default is often 300).

# Splits long replies on sentence boundaries before byte chunking (used by mt_mesh_split).
MESH_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
