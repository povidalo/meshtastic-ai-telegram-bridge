"""Persistent bridge message statistics for prompt context and diagnostics."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

import config

from . import mt_state


@dataclass
class _Stats:
    sent_total: int = 0
    received_total: int = 0
    ping_test_received_total: int = 0
    active_user_ids: Set[int] = field(default_factory=set)


_stats = _Stats()
_lock = threading.Lock()
_pong_re: Optional[re.Pattern[str]] = None
_pong_re_initialized = False


def _compile_pong_re() -> Optional[re.Pattern[str]]:
    global _pong_re, _pong_re_initialized
    if _pong_re_initialized:
        return _pong_re
    _pong_re_initialized = True
    raw = getattr(config, "MESH_AUTOMATED_PONG_REGEX", None)
    if not isinstance(raw, str) or not raw.strip():
        _pong_re = None
        return None
    try:
        _pong_re = re.compile(raw)
    except re.error as ex:
        mt_state.log.log("log", f"MESH_AUTOMATED_PONG_REGEX invalid for stats: {ex}")
        _pong_re = None
    return _pong_re


def _cache_payload() -> Dict[str, object]:
    return {
        "sent_total": int(_stats.sent_total),
        "received_total": int(_stats.received_total),
        "ping_test_received_total": int(_stats.ping_test_received_total),
        "active_user_ids": sorted(_stats.active_user_ids),
    }


def _write_cache_file() -> None:
    path = config.BRIDGE_STATS_CACHE_FILE
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(_cache_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as ex:
        mt_state.log.log("log", f"stats cache write failed: {ex}")


def load_stats_from_disk() -> bool:
    """Load stats cache if present. Returns True when file parsed successfully."""
    path = config.BRIDGE_STATS_CACHE_FILE
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        mt_state.log.log("log", f"stats cache read failed: {ex}")
        return False
    if not isinstance(data, dict):
        return False

    sent = data.get("sent_total", 0)
    received = data.get("received_total", 0)
    ping = data.get("ping_test_received_total", 0)
    users_raw = data.get("active_user_ids", [])
    users: Set[int] = set()
    if isinstance(users_raw, list):
        for item in users_raw:
            try:
                users.add(int(item))
            except (TypeError, ValueError):
                continue

    with _lock:
        try:
            _stats.sent_total = max(0, int(sent))
        except (TypeError, ValueError):
            _stats.sent_total = 0
        try:
            _stats.received_total = max(0, int(received))
        except (TypeError, ValueError):
            _stats.received_total = 0
        try:
            _stats.ping_test_received_total = max(0, int(ping))
        except (TypeError, ValueError):
            _stats.ping_test_received_total = 0
        _stats.active_user_ids = users
    return True


def record_sent_message() -> None:
    with _lock:
        _stats.sent_total += 1
        _write_cache_file()


def record_received_message(*, sender_node_id: int, message: str) -> None:
    pat = _compile_pong_re()
    is_ping = bool(pat is not None and pat.search((message or "").strip()))
    with _lock:
        _stats.received_total += 1
        _stats.active_user_ids.add(int(sender_node_id))
        if is_ping:
            _stats.ping_test_received_total += 1
        _write_cache_file()


def prompt_summary_block() -> str:
    """Compact stats block for chat AI prompts."""
    with _lock:
        sent_total = _stats.sent_total
        received_total = _stats.received_total
        ping_total = _stats.ping_test_received_total
        active_users_count = len(_stats.active_user_ids)
    return (
        "Статистика работы бота:\n"
        f"  отправлено сообщений: {sent_total}\n"
        f"  получено сообщений: {received_total}\n"
        f"  получено ping/test: {ping_total}\n"
        f"  активных пользователей: {active_users_count}"
    )
