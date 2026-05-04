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
_EVENT_RENAME_GREET_SENT = "rename_greet_sent"

_MSHT_DEFAULT_LONG_RE = re.compile(r"(?i)^Meshtastic\s+(.+)$")

_rename_greet_done_ids: Optional[Set[int]] = None
_rename_greet_done_lock = threading.Lock()


def _rename_greet_done_ids_from_events() -> Set[int]:
    """Node ids with a rename_greet_sent line in the stats JSONL."""
    path = config.BRIDGE_STATS_EVENTS_FILE
    if not path.is_file():
        return set()
    out: Set[int] = set()
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
                if not isinstance(row, dict) or row.get("event") != _EVENT_RENAME_GREET_SENT:
                    continue
                params = row.get("params")
                if not isinstance(params, dict):
                    continue
                try:
                    out.add(int(params.get("node_id")))
                except (TypeError, ValueError):
                    continue
    except OSError as ex:
        mt_state.log.log("log", f"stats events read failed: {ex}")
    return out


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


def _first_node_discovered_ts_and_names(
    node_id: int,
) -> Optional[tuple[int, str, str]]:
    """First node_discovered line for this node: (timestamp_ms, long_name, short_name)."""
    path = config.BRIDGE_STATS_EVENTS_FILE
    if not path.is_file():
        return None
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
                if not isinstance(row, dict) or row.get("event") != _EVENT_NODE_DISCOVERED:
                    continue
                params = row.get("params")
                if not isinstance(params, dict):
                    continue
                try:
                    nid = int(params.get("node_id"))
                except (TypeError, ValueError):
                    continue
                if nid != node_id:
                    continue
                try:
                    ts_ms = int(row.get("timestamp"))
                except (TypeError, ValueError):
                    continue
                long_name_raw = params.get("long_name")
                short_name_raw = params.get("short_name")
                long_name = long_name_raw.strip() if isinstance(long_name_raw, str) else ""
                short_name = short_name_raw.strip() if isinstance(short_name_raw, str) else ""
                return (ts_ms, long_name, short_name)
    except OSError as ex:
        mt_state.log.log("log", f"stats events read failed: {ex}")
        return None
    return None


def _file_has_default_to_custom_rename(node_id: int) -> bool:
    """True if any node_renamed event shows default long -> non-default for this node."""
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
                if not isinstance(row, dict) or row.get("event") != _EVENT_NODE_RENAMED:
                    continue
                params = row.get("params")
                if not isinstance(params, dict):
                    continue
                try:
                    nid = int(params.get("node_id"))
                except (TypeError, ValueError):
                    continue
                if nid != node_id:
                    continue
                pl = params.get("previous_long_name")
                ps = params.get("previous_short_name")
                nl = params.get("long_name")
                ns = params.get("short_name")
                prev_long = pl.strip() if isinstance(pl, str) else ""
                prev_short = ps.strip() if isinstance(ps, str) else ""
                new_long = nl.strip() if isinstance(nl, str) else ""
                new_short = ns.strip() if isinstance(ns, str) else ""
                if _is_default_meshtastic_long_name(
                    prev_long, prev_short
                ) and not _is_default_meshtastic_long_name(new_long, new_short):
                    return True
    except OSError as ex:
        mt_state.log.log("log", f"stats events read failed: {ex}")
        return False
    return False


def _load_rename_greet_done_ids() -> Set[int]:
    global _rename_greet_done_ids
    with _rename_greet_done_lock:
        if _rename_greet_done_ids is not None:
            return _rename_greet_done_ids
        _rename_greet_done_ids = _rename_greet_done_ids_from_events()
        return _rename_greet_done_ids


def _mark_rename_greet_done(node_id: int) -> None:
    global _rename_greet_done_ids
    nid = int(node_id)
    with _rename_greet_done_lock:
        ids = set(
            _rename_greet_done_ids
            if _rename_greet_done_ids is not None
            else _rename_greet_done_ids_from_events()
        )
        if nid in ids:
            return
        ids.add(nid)
        _rename_greet_done_ids = ids
    _append_event(_EVENT_RENAME_GREET_SENT, {"node_id": nid})


def _is_default_meshtastic_long_name(long_name: str, short_name: str) -> bool:
    """True when long name is the firmware default 'Meshtastic <short>'."""
    ln = (long_name or "").strip()
    sn = (short_name or "").strip()
    if not sn:
        return False
    m = _MSHT_DEFAULT_LONG_RE.match(ln)
    if not m:
        return False
    rest = (m.group(1) or "").strip()
    return rest.casefold() == sn.casefold()


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


def _nodes_eligible_for_discovery_greet(
    nodes: list[Dict[str, object]],
) -> list[Dict[str, object]]:
    """Skip nodes whose long name is the default 'Meshtastic <short>'."""
    out: list[Dict[str, object]] = []
    for node in nodes:
        ln_raw = node.get("long_name")
        sn_raw = node.get("short_name")
        long_name = ln_raw.strip() if isinstance(ln_raw, str) else ""
        short_name = sn_raw.strip() if isinstance(sn_raw, str) else ""
        if _is_default_meshtastic_long_name(long_name, short_name):
            continue
        out.append(node)
    return out


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


def _process_rename_greets_from_file(
    interface: object, renamed_this_poll: list[Dict[str, object]]
) -> None:
    """Greet after default-name discovery + rename + stable poll; state from events file only."""
    now_ms = _event_timestamp_ms()
    window_ms = 24 * 60 * 60 * 1000
    nodes_by_num = getattr(interface, "nodesByNum", None)
    if not isinstance(nodes_by_num, dict):
        return
    renamed_ids = {int(r["node_id"]) for r in renamed_this_poll if "node_id" in r}
    last_from_file = _last_known_node_names()
    done_ids = _load_rename_greet_done_ids()

    for node_num in nodes_by_num.keys():
        try:
            node_id = int(node_num)
        except (TypeError, ValueError):
            continue
        if node_id in done_ids:
            continue
        first = _first_node_discovered_ts_and_names(node_id)
        if first is None:
            continue
        disc_ts, disc_long, disc_short = first
        if now_ms - disc_ts > window_ms:
            continue
        if not _is_default_meshtastic_long_name(disc_long, disc_short):
            continue
        if not _file_has_default_to_custom_rename(node_id):
            continue
        if node_id in renamed_ids:
            continue
        info = nodes_by_num.get(node_id)
        cur = _parse_node_identity(node_id, info if isinstance(info, dict) else {})
        cur_long = str(cur.get("long_name") or "")
        cur_short = str(cur.get("short_name") or "")
        fl = last_from_file.get(node_id)
        if fl is None:
            continue
        file_long, file_short = fl
        if (cur_long, cur_short) != (file_long, file_short):
            continue
        if _is_default_meshtastic_long_name(cur_long, cur_short):
            continue
        eligible = _nodes_eligible_for_discovery_greet([cur])
        if not eligible:
            continue
        _send_new_nodes_greeting(interface, eligible)
        _mark_rename_greet_done(node_id)


def sync_known_nodes(
    interface: object,
) -> tuple[list[Dict[str, object]], list[Dict[str, object]]]:
    nodes_by_num = getattr(interface, "nodesByNum", None)
    if not isinstance(nodes_by_num, dict):
        return [], []
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
    return newly_seen, renamed


def _node_discovery_loop() -> None:
    while not mt_state._shutdown.is_set():
        with mt_state._iface_lock:
            iface = mt_state._iface_ref[0]
        if iface is not None:
            try:
                new_nodes, renamed = sync_known_nodes(iface)
                _process_rename_greets_from_file(iface, renamed)
                if new_nodes:
                    to_greet = _nodes_eligible_for_discovery_greet(new_nodes)
                    if to_greet:
                        _send_new_nodes_greeting(iface, to_greet)
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


def format_24h_stats_block_for_morning() -> Optional[str]:
    """24h stats for scheduled morning weather only: omit zero lines; omit block if all zero."""
    now_ms = _event_timestamp_ms()
    day_ago_ms = now_ms - (24 * 60 * 60 * 1000)
    s = _build_stats_from_events_file(since_ts_ms=day_ago_ms)
    n_new = len(s.known_node_ids)
    short_names = _recent_node_short_names(since_ts_ms=day_ago_ms)
    lines: list[str] = []
    if s.sent_total > 0:
        lines.append(f"отправлено сообщений: {s.sent_total}")
    if s.received_total > 0:
        lines.append(f"получено сообщений: {s.received_total}")
    if s.ping_test_received_total > 0:
        lines.append(f"получено ping/test: {s.ping_test_received_total}")
    if len(s.active_user_ids) > 0:
        lines.append(f"активных пользователей: {len(s.active_user_ids)}")
    if n_new > 0:
        if n_new <= 5 and short_names:
            lines.append(
                f"новые ноды: {n_new} ({', '.join(short_names)})"
            )
        else:
            lines.append(f"новые ноды: {n_new}")
    if not lines:
        return None
    return "Статистика за 24 ч:\n" + "\n".join(lines)


# Stripped-line starts to drop from LLM weather preface if it echoes 24h stats (morning job).
WEATHER_PREFACE_STATS_ECHO_PREFIXES: tuple[str, ...] = (
    "Статистика за 24 ч",
    "отправлено сообщений:",
    "получено сообщений:",
    "получено ping/test:",
    "активных пользователей:",
    "новые ноды:",
)


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
