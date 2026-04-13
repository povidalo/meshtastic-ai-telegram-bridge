# Meshtastic ↔ Telegram bridge with AI and weather

**Telegram is one-way (mesh → Telegram):** received mesh text is copied into a configured chat so you can follow traffic from Telegram. **Chat messages you send in Telegram are not forwarded to the mesh** — there is no command channel from Telegram back to the radio.

This project runs next to a [Meshtastic](https://meshtastic.org/) node (over **TCP** to a network-attached device or **serial** USB) and does three things in one process:

1. **Mirrors mesh text to Telegram (one-way)** — every received text packet is formatted and sent to a configured chat, so you can follow the channel from your phone or desktop.
2. **Optional AI auto-replies on the radio** — answers broadcast and direct messages using **Google Gemini** by default (with fallback to a **local OpenAI-compatible** server, e.g. llama.cpp), with short Russian system prompts tuned for a fixed “base node” persona.
3. **Weather on demand and on a schedule** — integrates **Yandex Weather** for forecasts, caches results, can broadcast a narrative summary on a chosen channel, and handles simple keywords like “погода” / “weather” with an automated reply.

It also includes small **automation hooks**: ignore rules for noisy or bot-generated traffic, instant **ping → pong**, threading-aware replies when Meshtastic supports it, **multi-part mesh sends** with payload limits aligned to the firmware, and optional **Telegram notifications** when the bot sends an AI reply on the mesh.

Configuration is **code-only** (no CLI): edit `config.py` at the repository root of this folder. Each setting has a short comment explaining what it does.

## Using the bot on the mesh (commands, mentions, threading)

Everything below is driven by regex and flags in `config.py` — adjust patterns and names to match your node and language.

**What always happens first**

- Incoming text is **mirrored to Telegram** (unless filtered as non-text noise earlier in the pipeline).
- The bridge **never auto-replies to its own node** (echo from your own radio is ignored for automation).

**Automated replies (no general chat model)**

These run **before** the main AI and only match the **whole** message (after strip), with an optional leading `/`:

1. **Ping / test** — messages like `ping`, `test`, `тест`, `пинг` (see `MESH_AUTOMATED_PONG_REGEX`) get a fixed **pong** (`MESH_AUTOMATED_PONG_TEXT`) plus a short **RF summary** (SNR/RSSI when available).
2. **Weather keywords** — messages like `погода`, `прогноз`, `weather`, `forecast` (see `MESH_AUTOMATED_WEATHER_REGEX`) trigger a **forecast reply**: cached **Yandex** data is turned into a short narrative by the AI stack (see fallback below). If data is missing, a short error line is sent instead.

**Side threads on broadcast**

On **broadcast**, ping/pong and weather automation are **skipped** when the message is a **threaded reply to someone else’s packet** (so the bot does not jump into other people’s sub-threads). **Direct messages** are not affected by this rule.

**General AI (chat) replies**

- **Direct messages (DMs)** to the node: the model can run on every suitable message (subject to ignore rules below).
- **Broadcast channel**: the bot only starts a **general** AI turn if either:
  - the message is in a **reply thread** to a packet the bridge recently sent (so it continues *your* conversation), or
  - the text matches **`AI_BROADCAST_DIRECT_MENTION_RE`** (e.g. configured @-style mentions of your node), so people can **call the bot by name** on channel without threading.

If a broadcast message is threaded to another node and there is **no** mention match, **general AI is skipped** (automated ping/weather are already skipped in that case too).

**Ignore list (general AI only)**

If the plain text matches any pattern in **`AI_IGNORE_MESSAGE_REGEXES`**, **general AI** is skipped for that packet. **Ping/pong and weather-keyword handling do not use this list** (they run earlier in the pipeline). **Telegram mirroring still happens.**

**Broadcast timing**

On broadcast, after generation starts the bridge can **wait** up to **`AI_BROADCAST_MIN_SEC_BEFORE_SEND`** seconds before transmitting. **New mesh traffic** in that window **cancels** the pending reply and starts a **new** turn so the answer stays relevant.

**Model routing and fallback**

- For **broadcast** traffic, **Gemini is tried first** when enabled; if it **fails** or returns **empty** text, the bridge **falls back to the local OpenAI-compatible server** (llama).
- For **DMs**, whether Gemini is used at all is controlled by **`AI_USE_GEMINI_IN_DM`**. If Gemini is used, the same **failure / empty → llama** fallback applies.
- **Weather keyword** replies use the same pattern: **Gemini first** (with the weather-specific system prompt), then **llama** if needed.

**Threading on the air**

When **`AUTO_REPLY_USE_THREAD`** is on, automated and AI replies **reply in-thread** to the triggering packet when Meshtastic supports it, which keeps conversations grouped on clients that show threads.

## Layout

- `main.py` — entrypoint; wires logging, Telegram, pubsub, weather scheduler, and the mesh session.
- `config.py` — all settings and prompts.
- `bridge/` — implementation (`mt_*` modules): mesh I/O, chunking, packets, AI routing, weather, Telegram formatting.
- `utils/` — threaded file logger and Telegram sender (`pyTelegramBotAPI`).

## Setup

1. **Python 3.10+** recommended (uses `zoneinfo`, type syntax used in the code).

2. Create a virtualenv and install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Meshtastic connectivity**

   - TCP: point `MESH_TCP_HOST` / `MESH_TCP_PORT` at your node or relay (see `config.py`).
   - Serial: set `MESH_CONNECTION_MODE` to `"serial"` and set `SERIAL_PORT` (e.g. `/dev/ttyACM0`).

4. **Telegram** (outbound mirror only — mesh → chat, not chat → mesh)

   - In `config.py`, set `TELEGRAM_BOT_TOKEN` (from [@BotFather](https://t.me/BotFather)), `TELEGRAM_CHAT_ID` (destination chat), and optionally `TELEGRAM_BOT_HANDLE` / `TELEGRAM_NOTIFIER_NAME` (reference / thread label).

5. **Yandex Weather** — set `YANDEX_WEATHER_API_KEY` and coordinates in `config.py`. Forecast URL and timeouts are there as well.

6. **AI backends**

   - **Gemini:** `GEMINI_API_KEY`, `GEMINI_MODEL`, and timeout settings in `config.py`. Used for broadcast by default; `AI_USE_GEMINI_IN_DM` controls whether DMs try Gemini before llama.
   - **Local llama (OpenAI-compatible):** `LLAMA_BASE_URL`, `LLAMA_MODEL`, token/temperature/timeouts. Must match what your server exposes (e.g. model id string).

7. Run from this directory so imports resolve:

   ```bash
   python main.py
   ```

   Logs go under `LOG_DIR` (default `mesh_logs`). Runtime cache files (`weather_cache.json`, `mesh_packet_origin_cache.json`) are created next to `config.py`.

## Critical configuration (reference)

| Area | What to set |
|------|-------------|
| Radio link | `MESH_CONNECTION_MODE`, `MESH_TCP_HOST`, `MESH_TCP_PORT`, or `SERIAL_PORT` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; optional `TELEGRAM_BOT_HANDLE`, `TELEGRAM_NOTIFIER_NAME` |
| Weather | `YANDEX_WEATHER_*`, `WEATHER_BROADCAST_CHANNEL_INDEX`, `WEATHER_STALE_HOURS`, `WEATHER_MSK_TZ` |
| AI | `AUTO_REPLY_ENABLED`, `LLAMA_*`, `GEMINI_*`, `AI_USE_GEMINI_IN_DM`, `AI_CONTEXT_MAX_MESSAGES`, `AI_BROADCAST_MIN_SEC_BEFORE_SEND`, `AI_BROADCAST_DIRECT_MENTION_RE`, `AI_IGNORE_MESSAGE_REGEXES` |
| Persona / text | `MODEL_NAME`, `LLAMA_SYSTEM_PROMPT`, `GEMINI_SYSTEM_PROMPT`, weather narrative prompts |
| Mesh protocol safety | `MESH_TEXT_MAX_PAYLOAD_BYTES`, `MESH_TEXT_MAX_PARTS`, `MESH_MULTI_PART_*`, `MESH_ROUTING_ACK_TIMEOUT_SEC` |

For full semantics (regex automation, threading, notification flags), read the comments in `config.py` next to each setting.
