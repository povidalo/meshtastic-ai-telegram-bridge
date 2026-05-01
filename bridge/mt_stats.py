"""Append-only bridge event stream and derived in-memory statistics."""

from __future__ import annotations

import json
import re
import threading
import time
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
    known_node_ids: Set[int] = field(default_factory=set)


_stats = _Stats()
_lock = threading.Lock()
_pong_re: Optional[re.Pattern[str]] = None
_pong_re_initialized = False
_node_discovery_thread: Optional[threading.Thread] = None

_EVENT_MSG_BROADCAST_IN = "msg_boadcast_in"
_EVENT_MSG_BROADCAST_OUT = "msg_boadcast_out"
_EVENT_MSG_DM_IN = "msg_dm_in"
_EVENT_MSG_DM_OUT = "msg_dm_out"
_EVENT_MSG_PING = "msg_ping"
_EVENT_NODE_DISCOVERED = "node_discovered"
_EVENT_NODE_RENAMED = "node_renamed"


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


def _event_timestamp_ms() -> int:
    return int(time.time() * 1000)


def _append_event_line(event: Dict[str, object]) -> None:
    path = config.BRIDGE_STATS_EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as ex:
        mt_state.log.log("log", f"stats event write failed: {ex}")


def _build_stats_from_events_file(*, since_ts_ms: Optional[int] = None) -> _Stats:
    stats = _Stats()
    path = config.BRIDGE_STATS_EVENTS_FILE
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                event = row.get("event")
                if not isinstance(event, str):
                    continue
                ts_raw = row.get("timestamp")
                try:
                    ts_ms = int(ts_raw)
                except (TypeError, ValueError):
                    ts_ms = None
                if since_ts_ms is not None and (ts_ms is None or ts_ms < since_ts_ms):
                    continue
                params = row.get("params")
                if not isinstance(params, dict):
                    params = {}
                if event in (_EVENT_MSG_BROADCAST_OUT, _EVENT_MSG_DM_OUT):
                    stats.sent_total += 1
                if event in (_EVENT_MSG_BROADCAST_IN, _EVENT_MSG_DM_IN):
                    stats.received_total += 1
                    sender_node_id = params.get("sender_node_id")
                    try:
                        stats.active_user_ids.add(int(sender_node_id))
                    except (TypeError, ValueError):
                        pass
                if event == _EVENT_MSG_PING:
                    stats.ping_test_received_total += 1
                if event == _EVENT_NODE_DISCOVERED:
                    node_id = params.get("node_id")
                    try:
                        stats.known_node_ids.add(int(node_id))
                    except (TypeError, ValueError):
                        pass
                if event in (_EVENT_MSG_BROADCAST_IN, _EVENT_MSG_DM_IN, _EVENT_MSG_PING):
                    sender_node_id = params.get("sender_node_id")
                    try:
                        stats.active_user_ids.add(int(sender_node_id))
                    except (TypeError, ValueError):
                        pass
    except OSError as ex:
        mt_state.log.log("log", f"stats events read failed: {ex}")
    return stats


def _recent_node_short_names(*, since_ts_ms: int) -> list[str]:
    discovered_node_ids: set[int] = set()
    latest_short_by_id: dict[int, str] = {}
    path = config.BRIDGE_STATS_EVENTS_FILE
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                event = row.get("event")
                if event not in (_EVENT_NODE_DISCOVERED, _EVENT_NODE_RENAMED):
                    continue
                params = row.get("params")
                if not isinstance(params, dict):
                    continue
                try:
                    node_id = int(params.get("node_id"))
                except (TypeError, ValueError):
                    continue
                short_name_raw = params.get("short_name")
                short_name = short_name_raw.strip() if isinstance(short_name_raw, str) else ""
                latest_short_by_id[node_id] = short_name or f"{node_id:08x}"[-4:]
                if event != _EVENT_NODE_DISCOVERED:
                    continue
                try:
                    ts_ms = int(row.get("timestamp"))
                except (TypeError, ValueError):
                    continue
                if ts_ms < since_ts_ms:
                    continue
                discovered_node_ids.add(node_id)
    except OSError as ex:
        mt_state.log.log("log", f"stats events read failed: {ex}")
        return []
    names = [latest_short_by_id.get(node_id, f"{node_id:08x}"[-4:]) for node_id in discovered_node_ids]
    return sorted(names, key=lambda n: n.lower())


def _last_known_node_names() -> dict[int, tuple[str, str]]:
    names_by_id: dict[int, tuple[str, str]] = {}
    path = config.BRIDGE_STATS_EVENTS_FILE
    if not path.is_file():
        return names_by_id
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                event = row.get("event")
                if event not in (_EVENT_NODE_DISCOVERED, _EVENT_NODE_RENAMED):
                    continue
                params = row.get("params")
                if not isinstance(params, dict):
                    continue
                try:
                    node_id = int(params.get("node_id"))
                except (TypeError, ValueError):
                    continue
                long_name_raw = params.get("long_name")
                short_name_raw = params.get("short_name")
                long_name = long_name_raw.strip() if isinstance(long_name_raw, str) else ""
                short_name = short_name_raw.strip() if isinstance(short_name_raw, str) else ""
                names_by_id[node_id] = (long_name, short_name)
    except OSError as ex:
        mt_state.log.log("log", f"stats events read failed: {ex}")
    return names_by_id


def _has_any_node_discovered_events() -> bool:
    path = config.BRIDGE_STATS_EVENTS_FILE
    if not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("event") == _EVENT_NODE_DISCOVERED:
                    return True
    except OSError as ex:
        mt_state.log.log("log", f"stats events read failed: {ex}")
    return False


def load_stats_from_disk() -> bool:
    """Load historical event stream and rebuild in-memory counters."""
    if not config.BRIDGE_STATS_EVENTS_FILE.is_file():
        return False
    stats = _build_stats_from_events_file()
    with _lock:
        _stats.sent_total = stats.sent_total
        _stats.received_total = stats.received_total
        _stats.ping_test_received_total = stats.ping_test_received_total
        _stats.active_user_ids = set(stats.active_user_ids)
        _stats.known_node_ids = set(stats.known_node_ids)
    return True


def _append_event(event: str, params: Optional[Dict[str, object]] = None) -> None:
    payload: Dict[str, object] = {
        "timestamp": _event_timestamp_ms(),
        "event": event,
    }
    if params:
        payload["params"] = params
    _append_event_line(payload)


def record_sent_message(
    *,
    is_direct_message: bool,
    message: str,
    receiver_node_id: Optional[int] = None,
) -> None:
    event = _EVENT_MSG_DM_OUT if is_direct_message else _EVENT_MSG_BROADCAST_OUT
    params: Dict[str, object] = {"message": str(message)}
    if is_direct_message and receiver_node_id is not None:
        params["receiver_node_id"] = int(receiver_node_id)
    with _lock:
        _stats.sent_total += 1
        _append_event(event, params)


def record_received_message(
    *, sender_node_id: int, message: str, is_direct_message: bool
) -> None:
    event = _EVENT_MSG_DM_IN if is_direct_message else _EVENT_MSG_BROADCAST_IN
    pat = _compile_pong_re()
    is_ping = bool(pat is not None and pat.search((message or "").strip()))
    with _lock:
        sid = int(sender_node_id)
        _stats.received_total += 1
        _stats.active_user_ids.add(sid)
        _append_event(event, {"sender_node_id": sid, "message": str(message)})
        if is_ping:
            _stats.ping_test_received_total += 1
            _append_event(_EVENT_MSG_PING, {"sender_node_id": sid, "message": str(message)})


def _parse_node_identity(node_num: int, node_info: object) -> Dict[str, object]:
    long_name = ""
    short_name = ""
    if isinstance(node_info, dict):
        user = node_info.get("user")
        if isinstance(user, dict):
            ln = user.get("longName")
            sn = user.get("shortName")
            if isinstance(ln, str):
                long_name = ln.strip()
            if isinstance(sn, str):
                short_name = sn.strip()
    return {
        "node_id": int(node_num),
        "long_name": long_name,
        "short_name": short_name,
    }


def _node_short_name_for_display(node_payload: Dict[str, object]) -> str:
    short_name_raw = node_payload.get("short_name")
    short_name = short_name_raw.strip() if isinstance(short_name_raw, str) else ""
    if short_name:
        return short_name
    try:
        node_id = int(node_payload.get("node_id"))
    except (TypeError, ValueError):
        return "node"
    return f"{node_id:08x}"[-4:]


def _greeting_for_new_nodes(new_nodes: list[Dict[str, object]]) -> Optional[str]:
    if len(new_nodes) == 0 or len(new_nodes) > 3:
        return None
    if len(new_nodes) == 1:
        node = new_nodes[0]
        short_name = _node_short_name_for_display(node)
        long_name_raw = node.get("long_name")
        long_name = long_name_raw.strip() if isinstance(long_name_raw, str) else ""
        if not long_name:
            long_name = short_name
        return f"Добро пожаловать, {long_name} ({short_name})!"
    shorts = [_node_short_name_for_display(node) for node in new_nodes]
    return f"Добро пожаловать, {', '.join(shorts)}!"


def _send_new_nodes_greeting(interface: object, new_nodes: list[Dict[str, object]]) -> None:
    text = _greeting_for_new_nodes(new_nodes)
    if not text:
        return
    try:
        from meshtastic import BROADCAST_ADDR
        from . import mt_ai_reply
        from .mt_mesh_send import send_mesh_text

        sent_pkt = send_mesh_text(
            interface,
            text,
            channel_index=config.WEATHER_BROADCAST_CHANNEL_INDEX,
            destination_id=BROADCAST_ADDR,
            record_context=False,
        )
        if sent_pkt is None:
            return
        if config.TELEGRAM_NOTIFY_MESH_AUTO_REPLY:
            try:
                mt_state.notifier.send(
                    f"[mesh greet]\n{text}",
                    config.TELEGRAM_CHAT_ID,
                )
            except Exception as ex:
                mt_state.log.log("log", f"telegram new-node greet notify failed: {ex}")
        try:
            mt_ai_reply.record_mesh_context_outgoing(
                channel_index=config.WEATHER_BROADCAST_CHANNEL_INDEX,
                destination_id=BROADCAST_ADDR,
                full_text=text,
            )
        except Exception as ex:
            mt_state.log.log("log", f"new-node greet context record failed: {ex}")
    except Exception as ex:
        mt_state.log.log("log", f"new node greeting failed: {ex}")


def sync_known_nodes(interface: object) -> list[Dict[str, object]]:
    nodes_by_num = getattr(interface, "nodesByNum", None)
    if not isinstance(nodes_by_num, dict):
        return []
    newly_seen: list[Dict[str, object]] = []
    renamed: list[Dict[str, object]] = []
    last_known_names = _last_known_node_names()
    with _lock:
        for node_num, node_info in nodes_by_num.items():
            try:
                node_id = int(node_num)
            except (TypeError, ValueError):
                continue
            node_payload = _parse_node_identity(node_id, node_info)
            if node_id in _stats.known_node_ids:
                last_known = last_known_names.get(node_id)
                if last_known is None:
                    continue
                prev_long_name, prev_short_name = last_known
                if (
                    prev_long_name == node_payload["long_name"]
                    and prev_short_name == node_payload["short_name"]
                ):
                    continue
                rename_payload = dict(node_payload)
                rename_payload["previous_long_name"] = prev_long_name
                rename_payload["previous_short_name"] = prev_short_name
                _append_event(_EVENT_NODE_RENAMED, rename_payload)
                renamed.append(rename_payload)
                last_known_names[node_id] = (
                    str(node_payload["long_name"]),
                    str(node_payload["short_name"]),
                )
                continue
            _stats.known_node_ids.add(node_id)
            _append_event(_EVENT_NODE_DISCOVERED, node_payload)
            newly_seen.append(node_payload)
            last_known_names[node_id] = (
                str(node_payload["long_name"]),
                str(node_payload["short_name"]),
            )
    for node in newly_seen:
        mt_state.log.log(
            "log",
            f"new mesh node discovered: id={node['node_id']} "
            f"long={node['long_name']!r} short={node['short_name']!r}",
        )
    for node in renamed:
        mt_state.log.log(
            "log",
            f"mesh node renamed: id={node['node_id']} "
            f"long {node['previous_long_name']!r}->{node['long_name']!r} "
            f"short {node['previous_short_name']!r}->{node['short_name']!r}",
        )
    return newly_seen


def _node_discovery_loop() -> None:
    while not mt_state._shutdown.is_set():
        with mt_state._iface_lock:
            iface = mt_state._iface_ref[0]
        if iface is not None:
            try:
                had_prior_discovery_events = _has_any_node_discovered_events()
                new_nodes = sync_known_nodes(iface)
                if had_prior_discovery_events and new_nodes:
                    _send_new_nodes_greeting(iface, new_nodes)
            except Exception as ex:
                mt_state.log.log("log", f"node discovery sync failed: {ex}")
            sleep_sec = max(5.0, float(config.BRIDGE_NODE_DISCOVERY_POLL_SEC))
        else:
            sleep_sec = 5.0
        mt_state._shutdown.wait(timeout=sleep_sec)


def start_node_discovery_worker() -> None:
    global _node_discovery_thread
    if _node_discovery_thread is not None and _node_discovery_thread.is_alive():
        return
    _node_discovery_thread = threading.Thread(
        target=_node_discovery_loop,
        name="mesh_node_discovery",
        daemon=True,
    )
    _node_discovery_thread.start()


def prompt_summary_block() -> str:
    """Compact all-time + 24h stats block for chat AI prompts."""
    now_ms = _event_timestamp_ms()
    day_ago_ms = now_ms - (24 * 60 * 60 * 1000)
    stats_24h = _build_stats_from_events_file(since_ts_ms=day_ago_ms)
    node_short_names_24h = _recent_node_short_names(since_ts_ms=day_ago_ms)
    node_list_line = ""
    if 0 < len(node_short_names_24h) <= 5:
        node_list_line = (
            "\n"
            f"    короткие имена новых нод: {', '.join(node_short_names_24h)}"
        )
    with _lock:
        sent_total_all = _stats.sent_total
        received_total_all = _stats.received_total
        ping_total_all = _stats.ping_test_received_total
        active_users_count_all = len(_stats.active_user_ids)
        nodes_seen_all = len(_stats.known_node_ids)
    return (
        "Статистика работы бота:\n"
        "  За всё время:\n"
        f"    отправлено сообщений: {sent_total_all}\n"
        f"    получено сообщений: {received_total_all}\n"
        f"    получено ping/test: {ping_total_all}\n"
        f"    активных пользователей: {active_users_count_all}\n"
        f"    обнаружено нод: {nodes_seen_all}\n"
        "  За последние 24 часа:\n"
        f"    отправлено сообщений: {stats_24h.sent_total}\n"
        f"    получено сообщений: {stats_24h.received_total}\n"
        f"    получено ping/test: {stats_24h.ping_test_received_total}\n"
        f"    активных пользователей: {len(stats_24h.active_user_ids)}\n"
        f"    обнаружено новых нод: {len(stats_24h.known_node_ids)}"
        f"{node_list_line}"
    )
