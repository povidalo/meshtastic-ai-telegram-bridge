"""Decode mesh packets into MeshMessageDetails."""

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from meshtastic import BROADCAST_NUM
from meshtastic.mesh_interface import MeshInterface

import config

# Maps mesh text packet id -> original sender node id (for threaded replies).
_MESH_TEXT_PACKET_ORIGIN_MAX = 512
_mesh_text_packet_origins: OrderedDict[int, int] = OrderedDict()
_packet_origins_lock = threading.Lock()


def _write_mesh_packet_origin_cache_file(pairs: list[list[int]]) -> None:
    path = config.MESH_PACKET_ORIGIN_CACHE_FILE
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"version": 1, "origins": pairs}
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as ex:
        try:
            import mt_state

            mt_state.log.log("log", f"mesh packet origin cache write failed: {ex}")
        except (AttributeError, RuntimeError):
            pass


def load_mesh_packet_origin_cache() -> None:
    """Restore packet-id → sender map from disk (call after logger is ready)."""
    path = config.MESH_PACKET_ORIGIN_CACHE_FILE
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        try:
            import mt_state

            mt_state.log.log("log", f"mesh packet origin cache read failed: {ex}")
        except (AttributeError, RuntimeError):
            pass
        return
    pairs = data.get("origins")
    if not isinstance(pairs, list):
        return
    with _packet_origins_lock:
        _mesh_text_packet_origins.clear()
        for item in pairs:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                pid, fid = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            if pid == 0:
                continue
            _mesh_text_packet_origins.pop(pid, None)
            _mesh_text_packet_origins[pid] = fid
        while len(_mesh_text_packet_origins) > _MESH_TEXT_PACKET_ORIGIN_MAX:
            _mesh_text_packet_origins.popitem(last=False)


def remember_mesh_text_packet_origin(packet_id: int, from_node_id: int) -> None:
    """Remember who sent a text packet so broadcast reply threads can be resolved."""
    if packet_id is None or int(packet_id) == 0:
        return
    pid = int(packet_id)
    fid = int(from_node_id)
    with _packet_origins_lock:
        _mesh_text_packet_origins.pop(pid, None)
        _mesh_text_packet_origins[pid] = fid
        while len(_mesh_text_packet_origins) > _MESH_TEXT_PACKET_ORIGIN_MAX:
            _mesh_text_packet_origins.popitem(last=False)
        snapshot = [[p, n] for p, n in _mesh_text_packet_origins.items()]
    _write_mesh_packet_origin_cache_file(snapshot)


def origin_of_mesh_text_packet(packet_id: int) -> Optional[int]:
    """Sender node id of the packet this message replies to, if known."""
    if packet_id is None or int(packet_id) == 0:
        return None
    with _packet_origins_lock:
        return _mesh_text_packet_origins.get(int(packet_id))


def _decoded_reply_packet_id(decoded: dict[str, Any]) -> Optional[int]:
    rid = decoded.get("replyId")
    if rid is None:
        rid = decoded.get("reply_id")
    if rid is None:
        return None
    try:
        i = int(rid)
    except (TypeError, ValueError):
        return None
    if i == 0:
        return None
    return i


@dataclass
class MeshMessageDetails:
    channel_index: int
    channel_name: str
    sender_node_id: int
    sender_display_name: str
    received_at: datetime
    hop_count: Optional[int]
    rssi: Optional[float]
    snr: Optional[float]
    message: str
    mesh_packet_id: Optional[int]
    to_node: int
    from_id_str: Optional[str]
    is_direct_message: bool
    pki_encrypted: bool
    reply_to_packet_id: Optional[int]


def _is_dm_to_local(packet: dict[str, Any], interface: MeshInterface) -> bool:
    if interface.myInfo is None:
        return False
    try:
        my_num = int(interface.myInfo.my_node_num)
        to_node = int(packet.get("to", 0))
    except (TypeError, ValueError):
        return False
    if to_node in (0, BROADCAST_NUM):
        return False
    return to_node == my_num


def _channel_name_for_index(interface: MeshInterface, channel_index: int) -> str:
    ln = interface.localNode
    if ln is None or not ln.channels:
        return f"channel {channel_index}"
    ch = ln.getChannelByChannelIndex(channel_index)
    if ch is None:
        return f"channel {channel_index}"
    name = getattr(ch.settings, "name", "") if ch.settings else ""
    if name:
        return str(name)
    if channel_index == 0:
        return "Primary"
    return f"channel {channel_index}"


def _sender_display_name(interface: MeshInterface, sender_num: int) -> str:
    if interface.nodesByNum is None:
        return f"!{sender_num:08x}"
    node = interface.nodesByNum.get(sender_num)
    if not node:
        return f"!{sender_num:08x}"
    user = node.get("user") or {}
    long_n = user.get("longName") or ""
    short_n = user.get("shortName") or ""
    if long_n and short_n:
        return f"{long_n} ({short_n})"
    if long_n:
        return str(long_n)
    if short_n:
        return str(short_n)
    return f"!{sender_num:08x}"


def _hop_count(packet: dict[str, Any]) -> Optional[int]:
    hs = packet.get("hopStart")
    hl = packet.get("hopLimit")
    if hs is None or hl is None:
        return None
    try:
        return int(hs) - int(hl)
    except (TypeError, ValueError):
        return None


def _parse_rx_time(packet: dict[str, Any]) -> datetime:
    rt = packet.get("rxTime")
    if rt is not None:
        try:
            return datetime.fromtimestamp(int(rt), tz=timezone.utc).astimezone()
        except (OSError, OverflowError, ValueError, TypeError):
            pass
    return datetime.now().astimezone()


def packet_to_details(
    packet: dict[str, Any], interface: MeshInterface
) -> Optional[MeshMessageDetails]:
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return None
    text = decoded.get("text")
    if text is None:
        return None
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return None

    from_num = packet.get("from")
    if from_num is None:
        return None
    try:
        sender_node_id = int(from_num)
    except (TypeError, ValueError):
        return None

    channel_index = int(packet.get("channel", 0))
    mesh_id = packet.get("id")
    mid: Optional[int]
    try:
        mid = int(mesh_id) if mesh_id is not None else None
    except (TypeError, ValueError):
        mid = None

    to_raw = packet.get("to", 0)
    try:
        to_node = int(to_raw)
    except (TypeError, ValueError):
        to_node = 0

    rssi = packet.get("rxRssi")
    snr = packet.get("rxSnr")
    try:
        rssi_f = float(rssi) if rssi is not None else None
    except (TypeError, ValueError):
        rssi_f = None
    try:
        snr_f = float(snr) if snr is not None else None
    except (TypeError, ValueError):
        snr_f = None

    is_dm = _is_dm_to_local(packet, interface)
    ch_name = "DM" if is_dm else _channel_name_for_index(interface, channel_index)
    pki_enc = packet.get("pkiEncrypted") is True

    reply_to = _decoded_reply_packet_id(decoded)
    if mid is not None:
        remember_mesh_text_packet_origin(mid, sender_node_id)

    return MeshMessageDetails(
        channel_index=channel_index,
        channel_name=ch_name,
        sender_node_id=sender_node_id,
        sender_display_name=_sender_display_name(interface, sender_node_id),
        received_at=_parse_rx_time(packet),
        hop_count=_hop_count(packet),
        rssi=rssi_f,
        snr=snr_f,
        message=text,
        mesh_packet_id=mid,
        to_node=to_node,
        from_id_str=packet.get("fromId") if isinstance(packet.get("fromId"), str) else None,
        is_direct_message=is_dm,
        pki_encrypted=pki_enc,
        reply_to_packet_id=reply_to,
    )
