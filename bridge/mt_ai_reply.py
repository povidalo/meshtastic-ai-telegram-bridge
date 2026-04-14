"""Background AI auto-reply via OpenAI-compatible local llama server."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple, Union
from collections import deque

import requests

from meshtastic import BROADCAST_ADDR

import config

from . import mt_state
from . import mt_weather
from .mt_mesh_send import send_mesh_text
from .mt_packets import MeshMessageDetails, origin_of_mesh_text_packet
from .mt_telegram import MeshAutoReplySource, format_telegram_mesh_auto_reply


@dataclass(frozen=True)
class AiContextKey:
    """Broadcast: dm_peer is None. DM: dm_peer is the remote sender node id."""

    channel_index: int
    dm_peer: Optional[int]


@dataclass
class _LatestReplySpec:
    channel_index: int
    destination_id: Union[int, str]
    reply_id: Optional[int]
    pki_encrypted: bool
    details: MeshMessageDetails


@dataclass
class _AiContext:
    messages: Deque[dict] = field(init=False)
    generation: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    busy: bool = False
    wake_again: bool = False
    latest_spec: Optional[_LatestReplySpec] = None

    def __post_init__(self) -> None:
        maxlen = max(1, int(config.AI_CONTEXT_MAX_MESSAGES))
        self.messages = deque(maxlen=maxlen)


_contexts: Dict[AiContextKey, _AiContext] = {}
_contexts_guard = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mesh_ai")

_ai_ignore_compiled: Optional[List[re.Pattern[str]]] = None
_automated_pong_re: Optional[re.Pattern[str]] = None
_automated_pong_re_initialized = False
_automated_weather_re: Optional[re.Pattern[str]] = None
_automated_weather_re_initialized = False
_broadcast_direct_mention_re: Optional[re.Pattern[str]] = None
_broadcast_direct_mention_re_initialized = False


def _get_broadcast_direct_mention_re() -> Optional[re.Pattern[str]]:
    global _broadcast_direct_mention_re, _broadcast_direct_mention_re_initialized
    if _broadcast_direct_mention_re_initialized:
        return _broadcast_direct_mention_re
    _broadcast_direct_mention_re_initialized = True
    raw = getattr(config, "AI_BROADCAST_DIRECT_MENTION_RE", None)
    if not isinstance(raw, str) or not raw.strip():
        _broadcast_direct_mention_re = None
        return None
    try:
        _broadcast_direct_mention_re = re.compile(raw)
    except re.error as ex:
        mt_state.log.log("log", f"AI_BROADCAST_DIRECT_MENTION_RE invalid: {ex}")
        _broadcast_direct_mention_re = None
    return _broadcast_direct_mention_re


def _message_has_broadcast_direct_mention(text: str) -> bool:
    pat = _get_broadcast_direct_mention_re()
    if pat is None:
        return False
    return pat.search(text) is not None


def _broadcast_automation_blocked_by_side_thread(
    details: MeshMessageDetails, interface: Any
) -> bool:
    """Broadcast: skip pong/weather when message is a threaded reply to another node's packet."""
    if details.is_direct_message:
        return False
    if details.reply_to_packet_id is None:
        return False
    parent_sender = origin_of_mesh_text_packet(details.reply_to_packet_id)
    if parent_sender is None:
        return False
    if interface.myInfo is None:
        return True
    try:
        my_num = int(interface.myInfo.my_node_num)
    except (TypeError, ValueError):
        return True
    return parent_sender != my_num


def _compiled_ai_ignore_patterns() -> List[re.Pattern[str]]:
    global _ai_ignore_compiled
    if _ai_ignore_compiled is not None:
        return _ai_ignore_compiled
    compiled: List[re.Pattern[str]] = []
    for raw in getattr(config, "AI_IGNORE_MESSAGE_REGEXES", ()) or ():
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            compiled.append(re.compile(raw))
        except re.error as ex:
            mt_state.log.log(
                "log",
                f"AI_IGNORE_MESSAGE_REGEXES invalid pattern {raw!r}: {ex}",
            )
    _ai_ignore_compiled = compiled
    return compiled


def _message_ignored_for_ai(text: str) -> bool:
    for pat in _compiled_ai_ignore_patterns():
        if pat.search(text):
            return True
    return False


def _get_automated_pong_re() -> Optional[re.Pattern[str]]:
    """None if regex unset, empty, or invalid."""
    global _automated_pong_re, _automated_pong_re_initialized
    if _automated_pong_re_initialized:
        return _automated_pong_re
    _automated_pong_re_initialized = True
    raw = getattr(config, "MESH_AUTOMATED_PONG_REGEX", None)
    if not isinstance(raw, str) or not raw.strip():
        _automated_pong_re = None
        return None
    try:
        _automated_pong_re = re.compile(raw)
    except re.error as ex:
        mt_state.log.log("log", f"MESH_AUTOMATED_PONG_REGEX invalid: {ex}")
        _automated_pong_re = None
    return _automated_pong_re


def _get_automated_weather_re() -> Optional[re.Pattern[str]]:
    global _automated_weather_re, _automated_weather_re_initialized
    if _automated_weather_re_initialized:
        return _automated_weather_re
    _automated_weather_re_initialized = True
    raw = getattr(config, "MESH_AUTOMATED_WEATHER_REGEX", None)
    if not isinstance(raw, str) or not raw.strip():
        _automated_weather_re = None
        return None
    try:
        _automated_weather_re = re.compile(raw)
    except re.error as ex:
        mt_state.log.log("log", f"MESH_AUTOMATED_WEATHER_REGEX invalid: {ex}")
        _automated_weather_re = None
    return _automated_weather_re


def maybe_automated_pong(details: MeshMessageDetails, interface: Any) -> bool:
    """If message is a ping/test probe, send pong + RF stats on mesh and skip LLM. Returns True when handled."""
    if not config.AUTO_REPLY_ENABLED:
        return False
    if _broadcast_automation_blocked_by_side_thread(details, interface):
        return False
    pat = _get_automated_pong_re()
    if pat is None or not pat.search(details.message.strip()):
        return False

    rid = details.mesh_packet_id if config.AUTO_REPLY_USE_THREAD else None
    if details.is_direct_message:
        dest: Union[int, str] = details.sender_node_id
        use_pki = details.pki_encrypted
    else:
        dest = BROADCAST_ADDR
        use_pki = False

    pong_word = getattr(config, "MESH_AUTOMATED_PONG_TEXT", None) or "Pong 🏓"
    body = f"{pong_word.strip()}\n{_format_rf_meta(details)}"

    try:
        sent_pkt = send_mesh_text(
            interface,
            body,
            channel_index=details.channel_index,
            destination_id=dest,
            reply_id=rid,
            pki_encrypted=use_pki,
        )
    except Exception as ex:
        mt_state.log.log("log", f"automated pong mesh send failed: {ex}")
        return True

    if config.TELEGRAM_NOTIFY_MESH_AUTO_REPLY and sent_pkt is not None:
        out_id = getattr(sent_pkt, "id", None)
        try:
            mt_state.notifier.send(
                format_telegram_mesh_auto_reply(
                    details, body, out_id, source="automated"
                ),
                config.TELEGRAM_CHAT_ID,
            )
        except Exception as ex:
            mt_state.log.log("log", f"telegram automated-pong notify failed: {ex}")

    return True


def maybe_automated_weather_forecast(details: MeshMessageDetails, interface: Any) -> bool:
    """Weather keywords: LLM narrative + mesh send (DM or broadcast like pong)."""
    if not config.AUTO_REPLY_ENABLED:
        return False
    if _broadcast_automation_blocked_by_side_thread(details, interface):
        return False
    pat = _get_automated_weather_re()
    if pat is None or not pat.search(details.message.strip()):
        return False

    mt_weather.refresh_if_stale()
    if not mt_weather.has_weather_payload():
        body = "Погода: не удалось получить данные. Проверь ключ API и сеть."
        rid = details.mesh_packet_id if config.AUTO_REPLY_USE_THREAD else None
        if details.is_direct_message:
            dest: Union[int, str] = details.sender_node_id
            use_pki = details.pki_encrypted
        else:
            dest = BROADCAST_ADDR
            use_pki = False
        try:
            sent_pkt = send_mesh_text(
                interface,
                body,
                channel_index=details.channel_index,
                destination_id=dest,
                reply_id=rid,
                pki_encrypted=use_pki,
            )
        except Exception as ex:
            mt_state.log.log("log", f"automated weather mesh send failed: {ex}")
            return True
        if config.TELEGRAM_NOTIFY_MESH_AUTO_REPLY and sent_pkt is not None:
            out_id = getattr(sent_pkt, "id", None)
            try:
                mt_state.notifier.send(
                    format_telegram_mesh_auto_reply(
                        details, body, out_id, source="automated"
                    ),
                    config.TELEGRAM_CHAT_ID,
                )
            except Exception as ex:
                mt_state.log.log("log", f"telegram automated-weather notify failed: {ex}")
        return True

    weather_source: MeshAutoReplySource = "automated"
    try:
        narrative, prov = complete_weather_narrative(
            mt_weather.format_weather_facts_for_narrative(),
            is_direct_message=details.is_direct_message,
        )
        weather_source = prov
    except Exception as ex:
        mt_state.log.log("log", f"weather narrative LLM failed: {ex}")
        narrative = ""

    narrative = (narrative or "").strip()
    if not narrative:
        narrative = "Погода: модель не вернула текст."

    rid = details.mesh_packet_id if config.AUTO_REPLY_USE_THREAD else None
    if details.is_direct_message:
        dest = details.sender_node_id
        use_pki = details.pki_encrypted
    else:
        dest = BROADCAST_ADDR
        use_pki = False

    try:
        sent_pkt = send_mesh_text(
            interface,
            narrative,
            channel_index=details.channel_index,
            destination_id=dest,
            reply_id=rid,
            pki_encrypted=use_pki,
        )
    except Exception as ex:
        mt_state.log.log("log", f"automated weather mesh send failed: {ex}")
        return True

    if config.TELEGRAM_NOTIFY_MESH_AUTO_REPLY and sent_pkt is not None:
        out_id = getattr(sent_pkt, "id", None)
        try:
            mt_state.notifier.send(
                format_telegram_mesh_auto_reply(
                    details, narrative, out_id, source=weather_source
                ),
                config.TELEGRAM_CHAT_ID,
            )
        except Exception as ex:
            mt_state.log.log("log", f"telegram automated-weather notify failed: {ex}")

    return True


def _context_key_from_details(d: MeshMessageDetails) -> AiContextKey:
    if d.is_direct_message:
        return AiContextKey(channel_index=d.channel_index, dm_peer=d.sender_node_id)
    return AiContextKey(channel_index=d.channel_index, dm_peer=None)


def _get_or_create_context(key: AiContextKey) -> _AiContext:
    with _contexts_guard:
        ctx = _contexts.get(key)
        if ctx is None:
            ctx = _AiContext()
            _contexts[key] = ctx
        return ctx


def _invalidate_broadcast_ai_for_side_thread(channel_index: int) -> None:
    """Drop queued broadcast AI work and bump generation so in-flight replies are abandoned."""
    key = AiContextKey(channel_index=channel_index, dm_peer=None)
    with _contexts_guard:
        ctx = _contexts.get(key)
    if ctx is None:
        return
    with ctx.lock:
        ctx.generation += 1
        ctx.messages.clear()
        ctx.latest_spec = None


_MSK = ZoneInfo("Europe/Moscow")


def _format_msk_now() -> str:
    return datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S %Z")


def _build_system_prompt(*, use_gemini_prompt: bool) -> str:
    wx = mt_weather.format_weather_for_system_prompt()
    base_prompt = (
        config.GEMINI_SYSTEM_PROMPT if use_gemini_prompt else config.LLAMA_SYSTEM_PROMPT
    )
    return (
        f"{base_prompt}\n\n"
        f"{wx}\n\n"
        f"Время сейчас в Дубне: {_format_msk_now()}."
    )


def _implied_mqtt_path(d: MeshMessageDetails) -> bool:
    if d.via_mqtt:
        return True
    return d.snr is None and d.rssi is None and d.hop_count == 0


def _format_rf_meta(d: MeshMessageDetails) -> str:
    if _implied_mqtt_path(d):
        return "(Сообщение пришло через MQTT-сервер)"
    parts: List[str] = []
    # SNR/RSSI appended for the model; bands in config.LLAMA_SYSTEM_PROMPT
    if d.snr is not None:
        parts.append(f"Сигнал/шум={d.snr:.1f}dB")
    else:
        parts.append("Сигнал/шум=н/д")
    if d.rssi is not None:
        parts.append(f"RSSI={d.rssi:.0f}dBm")
    else:
        parts.append("RSSI=н/д")
    if d.hop_count is None:
        parts.append("прыжки=н/д")
    else:
        parts.append(f"прыжки={d.hop_count}")
    return "(" + ", ".join(parts) + ")"


def _format_user_message(d: MeshMessageDetails) -> str:
    return f"{d.sender_display_name}: {d.message}  {_format_rf_meta(d)}"


def _call_llama(
    messages: List[dict],
    *,
    system_prompt: Optional[str] = None,
) -> str:
    url = f"{config.LLAMA_BASE_URL.rstrip('/')}/v1/chat/completions"
    sys_content = (
        _build_system_prompt(use_gemini_prompt=False)
        if system_prompt is None
        else system_prompt
    )
    full_messages: List[dict] = [
        {"role": "system", "content": sys_content},
        *messages,
    ]
    payload = {
        "model": config.LLAMA_MODEL,
        "messages": full_messages,
    }

    mt_state.log.log(
        "log",
        f"llama request start url={url} model={config.LLAMA_MODEL}",
    )
    try:
        msgs_dump = json.dumps(full_messages, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        msgs_dump = repr(full_messages)
    mt_state.log.log("log", f"llama request messages (system + history):\n{msgs_dump}")

    r = requests.post(
        url,
        json=payload,
        timeout=(config.LLAMA_CONNECT_TIMEOUT_SEC, config.LLAMA_READ_TIMEOUT_SEC),
    )
    raw_text = r.text
    try:
        data = r.json()
        body_dump = json.dumps(data, ensure_ascii=False, indent=2)
    except ValueError:
        data = None
        body_dump = raw_text if len(raw_text) <= 32000 else raw_text[:32000] + "\n…[truncated]"

    mt_state.log.log(
        "log",
        f"llama response status={r.status_code} bytes={len(raw_text)}\n{body_dump}",
    )

    r.raise_for_status()
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if content is None:
        return ""
    return str(content).strip()


def _gemini_contents_from_messages(messages: List[dict]) -> List[dict]:
    contents: List[dict] = []
    for msg in messages:
        role = str(msg.get("role", "user"))
        text = msg.get("content")
        if text is None:
            continue
        content_text = str(text)
        if not content_text.strip():
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append(
            {
                "role": gemini_role,
                "parts": [{"text": content_text}],
            }
        )
    return contents


def _call_gemini(
    messages: List[dict],
    *,
    model_name: str,
    system_prompt: Optional[str] = None,
) -> str:
    sys_content = (
        _build_system_prompt(use_gemini_prompt=True)
        if system_prompt is None
        else system_prompt
    )
    url = (
        f"{config.GEMINI_API_BASE_URL.rstrip('/')}/models/{model_name}"
        f":generateContent?key={config.GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": sys_content}]},
        "contents": _gemini_contents_from_messages(messages),
    }

    mt_state.log.log(
        "log",
        f"gemini request start url={url.split('?')[0]} model={model_name}",
    )
    try:
        body_dump = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        body_dump = repr(payload)
    mt_state.log.log("log", f"gemini request payload:\n{body_dump}")

    r = requests.post(
        url,
        json=payload,
        timeout=(config.GEMINI_CONNECT_TIMEOUT_SEC, config.GEMINI_READ_TIMEOUT_SEC),
    )
    raw_text = r.text
    try:
        data = r.json()
        response_dump = json.dumps(data, ensure_ascii=False, indent=2)
    except ValueError:
        data = None
        response_dump = raw_text if len(raw_text) <= 32000 else raw_text[:32000] + "\n…[truncated]"
    mt_state.log.log(
        "log",
        f"gemini response status={r.status_code} bytes={len(raw_text)}\n{response_dump}",
    )

    r.raise_for_status()
    if not isinstance(data, dict):
        return ""
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if text is None:
            continue
        chunks.append(str(text))
    return "".join(chunks).strip()


def _summarize_request_exception(ex: Exception) -> str:
    """Extract concise HTTP/API details from requests exceptions for logs."""
    if not isinstance(ex, requests.RequestException):
        return str(ex)

    base = str(ex).strip()
    response = getattr(ex, "response", None)
    if response is None:
        return base or ex.__class__.__name__

    status_code = getattr(response, "status_code", None)
    reason = getattr(response, "reason", None)
    parts: List[str] = []
    if status_code is not None:
        parts.append(f"status={status_code}")
    if reason:
        parts.append(f"reason={reason}")

    api_status: Optional[str] = None
    api_message: Optional[str] = None
    response_text: Optional[str] = None

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            raw_status = err.get("status")
            if raw_status is not None:
                api_status = str(raw_status).strip() or None
            raw_message = err.get("message")
            if raw_message is not None:
                api_message = str(raw_message).strip() or None
        if api_status:
            parts.append(f"api_status={api_status}")
        if api_message:
            parts.append(f"api_message={api_message}")
    else:
        txt = getattr(response, "text", "")
        if txt:
            response_text = txt.strip()

    details = ", ".join(parts)
    if details:
        return details
    if response_text:
        if len(response_text) > 400:
            response_text = response_text[:400] + "…[truncated]"
        return f"response_body={response_text}"
    return base or ex.__class__.__name__


def _is_network_failure_exception(ex: Exception) -> bool:
    """True when requests failed before receiving an HTTP response."""
    if not isinstance(ex, requests.RequestException):
        return False
    return getattr(ex, "response", None) is None


def _gemini_model_candidates() -> List[str]:
    """Normalize GEMINI_MODEL config into an ordered non-empty model list."""
    raw = getattr(config, "GEMINI_MODEL", None)
    if isinstance(raw, str):
        model = raw.strip()
        return [model] if model else []
    if isinstance(raw, (list, tuple)):
        models = [str(item).strip() for item in raw if str(item).strip()]
        return models
    return []


def _use_gemini_for_request(*, is_direct_message: bool) -> bool:
    if not is_direct_message:
        return True
    return bool(getattr(config, "AI_USE_GEMINI_IN_DM", False))


def _call_ai_with_routing(
    messages: List[dict],
    *,
    is_direct_message: bool,
    system_prompt: Optional[str] = None,
    gemini_system_prompt: Optional[str] = None,
    gemini_max_retries: int = 0,
    gemini_retry_initial_delay_sec: float = 15.0,
) -> Tuple[str, Literal["gemini", "llama"]]:
    """If gemini_system_prompt is set, Gemini uses it; llama always uses system_prompt."""
    gemini_sys = gemini_system_prompt if gemini_system_prompt is not None else system_prompt
    use_gemini = _use_gemini_for_request(is_direct_message=is_direct_message)
    if use_gemini:
        retries = max(0, int(gemini_max_retries))
        base_delay = max(0.0, float(gemini_retry_initial_delay_sec))
        gemini_models = _gemini_model_candidates()
        if not gemini_models:
            mt_state.log.log("log", "gemini disabled: GEMINI_MODEL has no usable values")
        for attempt in range(retries + 1):
            network_failure_this_attempt = False
            for model_name in gemini_models:
                try:
                    reply = _call_gemini(
                        messages, model_name=model_name, system_prompt=gemini_sys
                    ).strip()
                    if reply:
                        return reply, "gemini"
                    mt_state.log.log(
                        "log",
                        f"gemini returned empty reply for model={model_name}",
                    )
                except Exception as ex:
                    mt_state.log.log(
                        "log",
                        "gemini request failed for "
                        f"model={model_name}: {_summarize_request_exception(ex)}",
                    )
                    if _is_network_failure_exception(ex):
                        network_failure_this_attempt = True
                        break
            if network_failure_this_attempt:
                mt_state.log.log(
                    "log",
                    "gemini network failure detected; skipping model cycling this attempt",
                )
            if attempt >= retries:
                break
            delay = base_delay * (2**attempt)
            mt_state.log.log(
                "log",
                f"gemini retry in {delay:.1f}s (attempt {attempt + 1}/{retries})",
            )
            if delay > 0:
                time.sleep(delay)
        mt_state.log.log("log", "gemini failed; falling back to llama")

    return _call_llama(messages, system_prompt=system_prompt).strip(), "llama"


def complete_weather_narrative(
    facts_block: str,
    *,
    is_direct_message: bool = False,
    extra_system_instruction: Optional[str] = None,
    gemini_max_retries: int = 0,
    gemini_retry_initial_delay_sec: float = 15.0,
) -> Tuple[str, Literal["gemini", "llama"]]:
    """One-shot forecast text; uses dedicated system prompt (not mesh persona + full weather system block)."""
    llama_prompt = config.LLAMA_WEATHER_NARRATIVE_SYSTEM_PROMPT
    gemini_prompt = config.GEMINI_WEATHER_NARRATIVE_SYSTEM_PROMPT
    extra = (extra_system_instruction or "").strip()
    if extra:
        llama_prompt = f"{llama_prompt}\n{extra}"
        gemini_prompt = f"{gemini_prompt}\n{extra}"
    return _call_ai_with_routing(
        [{"role": "user", "content": facts_block}],
        is_direct_message=is_direct_message,
        system_prompt=llama_prompt,
        gemini_system_prompt=gemini_prompt,
        gemini_max_retries=gemini_max_retries,
        gemini_retry_initial_delay_sec=gemini_retry_initial_delay_sec,
    )


def _process_loop(key: AiContextKey) -> None:
    ctx = _get_or_create_context(key)
    try:
        guard = 0
        while True:
            guard += 1
            if guard > 250:
                mt_state.log.log("log", "ai context loop guard tripped")
                break
            with ctx.lock:
                ctx.wake_again = False
                gen_snapshot = ctx.generation
                msgs = list(ctx.messages)
                spec = ctx.latest_spec

            if spec is None:
                break

            is_broadcast = not spec.details.is_direct_message
            ai_started = time.monotonic() if is_broadcast else None

            try:
                reply_text, reply_source = _call_ai_with_routing(
                    msgs,
                    is_direct_message=spec.details.is_direct_message,
                )
            except Exception as ex:
                mt_state.log.log("log", f"ai chat failed: {ex}")
                with ctx.lock:
                    if ctx.wake_again:
                        continue
                break

            with ctx.lock:
                if ctx.generation != gen_snapshot or ctx.wake_again:
                    continue

            reply_text = (reply_text or "").strip()
            if not reply_text:
                with ctx.lock:
                    if ctx.wake_again:
                        continue
                break

            if is_broadcast and ai_started is not None:
                delay_end = ai_started + float(config.AI_BROADCAST_MIN_SEC_BEFORE_SEND)
                delay_aborted = False
                while time.monotonic() < delay_end:
                    with ctx.lock:
                        if ctx.generation != gen_snapshot or ctx.wake_again:
                            delay_aborted = True
                            break
                    remaining = delay_end - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.5, remaining))
                if delay_aborted:
                    continue
                with ctx.lock:
                    if ctx.generation != gen_snapshot or ctx.wake_again:
                        continue

            iface: Any = None
            with mt_state._iface_lock:
                iface = mt_state._iface_ref[0]
            if iface is None:
                mt_state.log.log("log", "ai mesh reply skipped: no mesh interface")
                with ctx.lock:
                    if ctx.wake_again:
                        continue
                break

            try:
                sent_pkt = send_mesh_text(
                    iface,
                    reply_text,
                    channel_index=spec.channel_index,
                    destination_id=spec.destination_id,
                    reply_id=spec.reply_id,
                    pki_encrypted=spec.pki_encrypted,
                )
            except Exception as ex:
                mt_state.log.log("log", f"ai mesh send failed: {ex}")
                with ctx.lock:
                    if ctx.wake_again:
                        continue
                break

            with ctx.lock:
                if ctx.generation != gen_snapshot or ctx.wake_again:
                    if ctx.wake_again:
                        continue
                    break
                ctx.messages.append({"role": "assistant", "content": reply_text})

            if config.TELEGRAM_NOTIFY_MESH_AUTO_REPLY and sent_pkt is not None:
                out_id = getattr(sent_pkt, "id", None)
                try:
                    mt_state.notifier.send(
                        format_telegram_mesh_auto_reply(
                            spec.details,
                            reply_text,
                            out_id,
                            source=reply_source,
                        ),
                        config.TELEGRAM_CHAT_ID,
                    )
                except Exception as ex:
                    mt_state.log.log("log", f"telegram ai-reply notify failed: {ex}")

            with ctx.lock:
                if ctx.wake_again:
                    continue
            break
    finally:
        with ctx.lock:
            ctx.busy = False
            if ctx.wake_again:
                ctx.busy = True
                _executor.submit(_process_loop, key)


def schedule_ai_reply(details: MeshMessageDetails, interface: Any) -> None:
    """Append user turn, bump generation; one worker per context coalesces rapid messages."""
    if not config.AUTO_REPLY_ENABLED:
        return
    if _message_ignored_for_ai(details.message):
        return

    if not details.is_direct_message:
        mention = _message_has_broadcast_direct_mention(details.message)

        if details.reply_to_packet_id is None:
            if not mention:
                mt_state.log.log(
                    "log",
                    "broadcast AI skipped: no reply thread and no direct mention",
                )
                _invalidate_broadcast_ai_for_side_thread(details.channel_index)
                return
        else:
            parent_sender = origin_of_mesh_text_packet(details.reply_to_packet_id)
            my_num: Optional[int] = None
            if interface.myInfo is not None:
                try:
                    my_num = int(interface.myInfo.my_node_num)
                except (TypeError, ValueError):
                    my_num = None

            allow = False
            if parent_sender is not None and my_num is not None and parent_sender == my_num:
                allow = True
            elif mention:
                allow = True

            if not allow:
                if parent_sender is None:
                    mt_state.log.log(
                        "log",
                        "broadcast AI skipped: threaded reply, parent unknown in cache "
                        f"(reply_to={details.reply_to_packet_id}); no direct mention",
                    )
                elif my_num is None:
                    mt_state.log.log(
                        "log",
                        "broadcast AI skipped: threaded reply, cannot read local node id; "
                        "no direct mention",
                    )
                else:
                    mt_state.log.log(
                        "log",
                        "broadcast AI skipped: threaded reply targets another node's message "
                        f"(parent_node={parent_sender}, us={my_num}); no direct mention",
                    )
                _invalidate_broadcast_ai_for_side_thread(details.channel_index)
                return

    key = _context_key_from_details(details)
    rid = details.mesh_packet_id if config.AUTO_REPLY_USE_THREAD else None
    if details.is_direct_message:
        dest: Union[int, str] = details.sender_node_id
        use_pki = details.pki_encrypted
    else:
        dest = BROADCAST_ADDR
        use_pki = False

    spec = _LatestReplySpec(
        channel_index=details.channel_index,
        destination_id=dest,
        reply_id=rid,
        pki_encrypted=use_pki,
        details=details,
    )
    user_line = _format_user_message(details)

    ctx = _get_or_create_context(key)
    with ctx.lock:
        ctx.messages.append({"role": "user", "content": user_line})
        ctx.generation += 1
        ctx.latest_spec = spec
        if ctx.busy:
            ctx.wake_again = True
            return
        ctx.busy = True

    _executor.submit(_process_loop, key)
