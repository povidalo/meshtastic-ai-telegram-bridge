"""Yandex Weather v2: cache file, 24h staleness, daily MSK narrative broadcast (time from config)."""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from meshtastic import BROADCAST_ADDR

import config

from . import mt_state
from .mt_mesh_send import send_mesh_text

_MSK = ZoneInfo(config.WEATHER_TZ)
_state_lock = threading.Lock()
_raw: Optional[Dict[str, Any]] = None
_fetched_at: Optional[datetime] = None

_PART_ORDER: tuple[tuple[str, str], ...] = (
    ("night", "ночь"),
    ("morning", "утро"),
    ("day", "день"),
    ("evening", "вечер"),
)


def _parse_iso_dt(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _season_name_ru(d: date) -> str:
    m = d.month
    if m in (12, 1, 2):
        return "зима"
    if m in (3, 4, 5):
        return "весна"
    if m in (6, 7, 8):
        return "лето"
    return "осень"


def _cloudness_ru(value: Any) -> str:
    if value is None:
        return "н/д"
    if isinstance(value, str):
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v <= 0.25:
        return "ясно / малооблачно"
    if v <= 0.5:
        return "переменная облачность"
    if v <= 0.75:
        return "облачно"
    return "пасмурно"


def _temp_line(part: Dict[str, Any]) -> str:
    t_min = part.get("temp_min")
    t_max = part.get("temp_max")
    t_avg = part.get("temp_avg")
    if t_avg is None and part.get("temp") is not None:
        t_avg = part.get("temp")
    if t_avg is not None:
        core = f"{t_avg}°C"
    elif t_min is not None and t_max is not None:
        core = f"{t_min}…{t_max}°C"
    elif t_min is not None:
        core = f"мин {t_min}°C"
    elif t_max is not None:
        core = f"макс {t_max}°C"
    else:
        return "температура: н/д"
    fl = part.get("feels_like")
    if fl is not None:
        return f"температура: {core}, ощущ. {fl}°C"
    return f"температура: {core}"


def _wind_line(part: Dict[str, Any]) -> str:
    ws = part.get("wind_speed")
    wg = part.get("wind_gust")
    wd = part.get("wind_dir")
    bits: List[str] = []
    if ws is not None:
        bits.append(f"ветер {ws} м/с")
    else:
        bits.append("ветер н/д")
    if wg is not None:
        bits.append(f"порывы до {wg} м/с")
    if wd:
        bits.append(str(wd))
    return ", ".join(bits)


def _format_part_block(label_ru: str, part: Dict[str, Any]) -> str:
    lines = [
        f"— {label_ru}: {_temp_line(part)}",
        f"  облачность/условие: {_cloudness_ru(part.get('cloudness'))}",
        f"  {_wind_line(part)}",
    ]
    return "\n".join(lines)


def _today_parts(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    forecasts = raw.get("forecasts")
    if not isinstance(forecasts, list) or not forecasts:
        return None
    today = datetime.now(_MSK).date()
    for day in forecasts:
        if not isinstance(day, dict):
            continue
        ds = day.get("date")
        if isinstance(ds, str):
            try:
                d = date.fromisoformat(ds)
            except ValueError:
                continue
            if d == today:
                return day.get("parts") if isinstance(day.get("parts"), dict) else None
    first = forecasts[0]
    if isinstance(first, dict) and isinstance(first.get("parts"), dict):
        return first["parts"]
    return None


def _format_fact_block(raw: Dict[str, Any]) -> str:
    fact = raw.get("fact")
    if not isinstance(fact, dict):
        return ""
    lines = [
        "Сейчас (факт):",
        f"  {_temp_line(fact)}",
        f"  облачность/условие: {_cloudness_ru(fact.get('cloudness'))}",
        f"  {_wind_line(fact)}",
    ]
    return "\n".join(lines).strip()


def _build_human_block(raw: Dict[str, Any], when: datetime, *, include_fact: bool) -> str:
    d_msk = when.astimezone(_MSK).date()
    season = _season_name_ru(d_msk)
    header = (
        f"Погода (Дубна, данные Яндекс; дата по Москве: {d_msk.isoformat()}, "
        f"время года: {season}):"
    )
    body_parts: List[str] = [header]
    if include_fact:
        fb = _format_fact_block(raw)
        if fb:
            body_parts.append(fb)
    parts = _today_parts(raw)
    if parts:
        body_parts.append("Сегодня по частям суток:")
        for key, label in _PART_ORDER:
            p = parts.get(key)
            if isinstance(p, dict):
                body_parts.append(_format_part_block(label, p))
    if len(body_parts) == 1:
        body_parts.append("(детальный прогноз недоступен в кэше)")
    return "\n".join(body_parts)


def has_weather_payload() -> bool:
    with _state_lock:
        return _raw is not None


def format_weather_for_system_prompt() -> str:
    with _state_lock:
        raw = _raw
        fetched = _fetched_at
    if not raw:
        return "Погода: данные пока недоступны (кэш пуст или ошибка загрузки)."
    when = fetched or datetime.now(_MSK)
    block = _build_human_block(raw, when, include_fact=True)
    if fetched:
        block += f"\n(данные получены: {fetched.astimezone(_MSK).strftime('%Y-%m-%d %H:%M %Z')})"
    return block


def format_weather_facts_for_narrative(*, include_fact: bool = False) -> str:
    """Structured facts for weather narrative LLM; optionally include current fact block."""
    with _state_lock:
        raw = _raw
        fetched = _fetched_at
    if not raw:
        return "Погода: данные пока недоступны (кэш пуст или ошибка загрузки)."
    when = fetched or datetime.now(_MSK)
    block = _build_human_block(raw, when, include_fact=include_fact)
    if fetched:
        block += f"\n(данные получены: {fetched.astimezone(_MSK).strftime('%Y-%m-%d %H:%M %Z')})"
    return block


def _cache_payload() -> Dict[str, Any]:
    with _state_lock:
        return {
            "fetched_at": _fetched_at.isoformat() if _fetched_at else None,
            "raw": _raw,
        }


def _write_cache_file() -> None:
    path = config.WEATHER_CACHE_FILE
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = _cache_payload()
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as ex:
        mt_state.log.log("log", f"weather cache write failed: {ex}")


def load_cache_from_disk() -> bool:
    """Load JSON cache if present. Returns True if file was read successfully."""
    global _raw, _fetched_at
    path = config.WEATHER_CACHE_FILE
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        mt_state.log.log("log", f"weather cache read failed: {ex}")
        return False
    if not isinstance(data, dict):
        return False
    raw = data.get("raw")
    if not isinstance(raw, dict):
        return False
    fetched = _parse_iso_dt(data.get("fetched_at")) if data.get("fetched_at") else None
    if fetched is not None and fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=_MSK)
    with _state_lock:
        _raw = raw
        _fetched_at = fetched
    return True


def _is_stale(fetched: Optional[datetime]) -> bool:
    if fetched is None:
        return True
    now = datetime.now(fetched.tzinfo or _MSK)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=_MSK)
    return now - fetched >= timedelta(hours=config.WEATHER_STALE_HOURS)


def fetch_from_api() -> Optional[Dict[str, Any]]:
    key = config.YANDEX_WEATHER_API_KEY
    if not key:
        mt_state.log.log("log", "Yandex weather: no API key (set YANDEX_WEATHER_API_KEY)")
        return None
    params = {"lat": config.YANDEX_WEATHER_LAT, "lon": config.YANDEX_WEATHER_LON}
    headers = {"X-Yandex-Weather-Key": key}
    try:
        r = requests.get(
            config.YANDEX_WEATHER_FORECAST_URL,
            params=params,
            headers=headers,
            timeout=config.YANDEX_WEATHER_REQUEST_TIMEOUT_SEC,
        )
        txt = r.text
        try:
            data = r.json()
        except ValueError:
            data = None
        mt_state.log.log(
            "log",
            f"yandex weather HTTP {r.status_code} bytes={len(txt)}",
        )
        r.raise_for_status()
        if not isinstance(data, dict):
            return None
        if "message" in data and "forecasts" not in data and "fact" not in data:
            mt_state.log.log("log", f"yandex weather error payload: {data!r}")
            return None
        return data
    except Exception as ex:
        mt_state.log.log("log", f"yandex weather request failed: {ex}")
        return None


def apply_fresh_payload(raw: Dict[str, Any], when: Optional[datetime] = None) -> None:
    global _raw, _fetched_at
    ts = when or datetime.now(_MSK)
    with _state_lock:
        _raw = raw
        _fetched_at = ts
    _write_cache_file()


def refresh_if_stale(*, force: bool = False) -> None:
    """Fetch from API when stale or missing; keeps previous raw on failure.

    If ``force`` is True, always call the API (scheduled morning job); on failure,
    existing in-memory or disk cache is left unchanged.
    """
    with _state_lock:
        fetched = _fetched_at
        has_raw = _raw is not None
    if not has_raw:
        load_cache_from_disk()
        with _state_lock:
            fetched = _fetched_at
            has_raw = _raw is not None
    if not force and has_raw and not _is_stale(fetched):
        mt_state.log.log("log", "yandex weather: using cache (fresh enough)")
        return
    data = fetch_from_api()
    if data is not None:
        apply_fresh_payload(data)
        mt_state.log.log("log", "yandex weather: fetched and cached")
    elif not has_raw:
        mt_state.log.log("log", "yandex weather: no data after failed fetch")


def _weather_schedule_target(now: datetime) -> datetime:
    h = int(config.WEATHER_SCHEDULE_HOUR)
    m = int(config.WEATHER_SCHEDULE_MINUTE)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def _weather_schedule_time_label() -> str:
    """Human-readable slot for logs/Telegram (e.g. 07:00 MSK)."""
    now = datetime.now(_MSK)
    h = int(config.WEATHER_SCHEDULE_HOUR)
    m = int(config.WEATHER_SCHEDULE_MINUTE)
    return now.replace(hour=h, minute=m, second=0, microsecond=0).strftime("%H:%M %Z")


def _seconds_until_scheduled_weather_msk() -> tuple[float, datetime]:
    now = datetime.now(_MSK)
    target = _weather_schedule_target(now)
    return max(0.0, (target - now).total_seconds()), target


def _broadcast_weather_narrative(text: str) -> None:
    iface: Any = None
    with mt_state._iface_lock:
        iface = mt_state._iface_ref[0]
    if iface is None:
        mt_state.log.log("log", "weather broadcast skipped: no mesh interface")
        return
    try:
        send_mesh_text(
            iface,
            text.strip(),
            channel_index=config.WEATHER_BROADCAST_CHANNEL_INDEX,
            destination_id=BROADCAST_ADDR,
            reply_id=None,
            pki_encrypted=False,
        )
    except Exception as ex:
        mt_state.log.log("log", f"weather broadcast send failed: {ex}")


def _morning_job() -> None:
    refresh_if_stale(force=True)
    from .mt_ai_reply import complete_weather_narrative
    from .mt_telegram import mesh_auto_reply_source_caption

    with _state_lock:
        if _raw is None:
            mt_state.log.log("log", "weather morning job: no raw data for narrative")
            return
    facts = format_weather_facts_for_narrative(include_fact=True)
    try:
        narrative, prov = complete_weather_narrative(facts)
    except Exception as ex:
        mt_state.log.log("log", f"weather narrative LLM failed: {ex}")
        return
    narrative = (narrative or "").strip()
    if not narrative:
        mt_state.log.log("log", "weather morning job: empty narrative")
        return
    note = (config.WEATHER_MORNING_AI_FOOTNOTE or "").strip()
    out = f"{narrative}\n\n{note}" if note else narrative
    _broadcast_weather_narrative(out)
    if config.TELEGRAM_NOTIFY_MESH_AUTO_REPLY:
        try:
            mt_state.notifier.send(
                f"[mesh weather {_weather_schedule_time_label()} · {mesh_auto_reply_source_caption(prov)}]\n{out}",
                config.TELEGRAM_CHAT_ID,
            )
        except Exception as ex:
            mt_state.log.log("log", f"telegram weather broadcast notify failed: {ex}")


def _scheduler_loop() -> None:
    try:
        refresh_if_stale()
    except Exception as ex:
        mt_state.log.log("log", f"weather startup refresh error: {ex}")
    while not mt_state._shutdown.is_set():
        wait, next_at = _seconds_until_scheduled_weather_msk()
        mt_state.log.log(
            "log",
            f"weather scheduler: next {next_at.strftime('%H:%M %Z')} in {wait:.0f}s",
        )
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if mt_state._shutdown.is_set():
                return
            time.sleep(min(30.0, end - time.monotonic()))
        if mt_state._shutdown.is_set():
            return
        try:
            _morning_job()
        except Exception as ex:
            mt_state.log.log("log", f"weather morning job error: {ex}")


def start_background_scheduler() -> None:
    t = threading.Thread(
        target=_scheduler_loop,
        name="mesh_weather",
        daemon=True,
    )
    t.start()
