"""Send mesh text with UTF-8 chunking, retries, and multi-part delays."""

import time
from typing import Any, Callable, List, Optional, Tuple, Union

from meshtastic import BROADCAST_ADDR
from meshtastic import BROADCAST_NUM
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import portnums_pb2

import config

from . import mt_state
from . import mt_stats
from .mt_mesh_split import prepare_mesh_send_chunks
from .mt_packets import remember_mesh_text_packet_origin


def _retry_backoff_sec(attempt: int) -> float:
    """Exponential backoff: base * 2^attempt (attempt is 0-based)."""
    return config.MESH_MULTI_PART_DELAY_SEC * (2**attempt)


def _drop_last_mesh_sentence(text: str) -> Optional[str]:
    """Drop the trailing segment split like mt_mesh_split (after . ! ? … + whitespace)."""
    parts = [p.strip() for p in config.MESH_SENTENCE_SPLIT_RE.split(text) if p.strip()]
    if len(parts) <= 1:
        return None
    shorter = " ".join(parts[:-1]).strip()
    return shorter or None


def _prepare_mesh_send_chunks_with_tail_truncation(
    text: str, max_bytes: int, max_parts: int
) -> Optional[List[str]]:
    """Try prepare_mesh_send_chunks; if None, remove last sentence(s) until it fits or nothing left."""
    t = text.replace("\r\n", "\n").strip()
    if not t:
        return prepare_mesh_send_chunks(text, max_bytes, max_parts)
    truncated = False
    while t:
        chunks = prepare_mesh_send_chunks(t, max_bytes, max_parts)
        if chunks is not None:
            if truncated:
                mt_state.log.log(
                    "log",
                    f"mesh send: truncated trailing sentence(s) to fit ≤{max_parts} parts "
                    f"(max {max_bytes} UTF-8 B each)",
                )
            return chunks
        shorter = _drop_last_mesh_sentence(t)
        if shorter is None or shorter == t:
            return None
        t = shorter
        truncated = True
    return None


def _clear_stale_route_handler(interface: MeshInterface, packet_id: Optional[int]) -> bool:
    """If waitForAckNak times out, meshtastic leaves onResponse in responseHandlers — remove it."""
    if packet_id is None:
        return False
    try:
        pid = int(packet_id)
    except (TypeError, ValueError):
        return False
    popped = interface.responseHandlers.pop(pid, None)
    return popped is not None


def _wait_for_routing_ack(interface: MeshInterface) -> None:
    """waitForAckNak with a bounded timeout (restores interface timeout after)."""
    tout = interface._timeout
    prev = tout.expireTimeout
    tout.expireTimeout = config.MESH_ROUTING_ACK_TIMEOUT_SEC
    try:
        interface.waitForAckNak()
    finally:
        tout.expireTimeout = prev


def _routing_ack_callback_with_nak_flag(
    interface: MeshInterface,
) -> Tuple[Callable[[dict], None], List[bool]]:
    """Callback for sendData onResponse; mirrors node.onAckNak without printing."""
    nak: List[bool] = [False]

    def on_routing(p: dict) -> None:
        try:
            err = p["decoded"]["routing"]["errorReason"]
        except (KeyError, TypeError):
            nak[0] = True
            interface._acknowledgment.receivedNak = True
            return
        if err != "NONE":
            nak[0] = True
            interface._acknowledgment.receivedNak = True
            return
        try:
            from_num = int(p["from"])
        except (KeyError, TypeError, ValueError):
            interface._acknowledgment.receivedAck = True
            return
        if from_num == interface.localNode.nodeNum:
            interface._acknowledgment.receivedImplAck = True
        else:
            interface._acknowledgment.receivedAck = True

    return on_routing, nak


def _dispatch_one_mesh_text_chunk(
    interface: MeshInterface,
    chunk: str,
    *,
    destination_id: Union[int, str],
    channel_index: int,
    reply_id: Optional[int],
    pki_encrypted: bool,
    on_response: Callable[[dict], None],
) -> Any:
    if pki_encrypted:
        return interface.sendData(
            chunk.encode("utf-8"),
            destinationId=destination_id,
            portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            wantAck=True,
            channelIndex=channel_index,
            replyId=reply_id,
            pkiEncrypted=True,
            onResponse=on_response,
            onResponseAckPermitted=True,
        )
    return interface.sendData(
        chunk.encode("utf-8"),
        destinationId=destination_id,
        portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
        wantAck=True,
        channelIndex=channel_index,
        replyId=reply_id,
        onResponse=on_response,
        onResponseAckPermitted=True,
    )


def send_mesh_text(
    interface: MeshInterface,
    text: str,
    *,
    channel_index: int,
    destination_id: Union[int, str] = BROADCAST_ADDR,
    reply_id: Optional[int] = None,
    pki_encrypted: bool = False,
    record_context: bool = True,
) -> Any:
    """Send text with wantAck and wait for ROUTING_APP ACK/NAK after each part."""
    dest_s = str(destination_id)
    pki_s = "pki" if pki_encrypted else "plain"
    chunks = prepare_mesh_send_chunks(
        text, config.MESH_TEXT_MAX_PAYLOAD_BYTES, config.MESH_TEXT_MAX_PARTS
    )
    if chunks is None:
        chunks = _prepare_mesh_send_chunks_with_tail_truncation(
            text, config.MESH_TEXT_MAX_PAYLOAD_BYTES, config.MESH_TEXT_MAX_PARTS
        )
    if chunks is None:
        mt_state.log.log(
            "log",
            f"mesh send ignored: needs more than {config.MESH_TEXT_MAX_PARTS} parts "
            f"(max {config.MESH_TEXT_MAX_PAYLOAD_BYTES} UTF-8 bytes each) dest={dest_s} {pki_s}",
        )
        return None
    # Normalize each chunk right before dispatch: trim leading/trailing spaces/newlines
    # and drop chunks that become empty after trimming.
    chunks = [chunk.strip() for chunk in chunks if chunk.strip()]
    if not chunks:
        mt_state.log.log(
            "log",
            f"mesh send ignored: empty message after trim dest={dest_s} {pki_s}",
        )
        return None

    result: Any = None
    had_routing_ack = False
    n = len(chunks)
    parts_ok = 0
    for i, chunk in enumerate(chunks):
        chunk_ok = False
        part_rid = reply_id if i == 0 else None
        rid_s = str(part_rid) if part_rid is not None else "none"
        preview = chunk if len(chunk) <= 120 else chunk[:117] + "..."
        for attempt in range(config.MESH_MULTI_PART_SEND_RETRIES):
            pkt_id: Optional[int] = None
            try:
                interface._acknowledgment.reset()
                on_resp, nak_flag = _routing_ack_callback_with_nak_flag(interface)

                result = _dispatch_one_mesh_text_chunk(
                    interface,
                    chunk,
                    destination_id=destination_id,
                    channel_index=channel_index,
                    reply_id=part_rid,
                    pki_encrypted=pki_encrypted,
                    on_response=on_resp,
                )
                raw_id = getattr(result, "id", None) if result is not None else None
                pkt_id = int(raw_id) if raw_id is not None else None

                try:
                    _wait_for_routing_ack(interface)
                except MeshInterface.MeshInterfaceError as ack_ex:
                    if _clear_stale_route_handler(interface, pkt_id):
                        mt_state.log.log(
                            "log",
                            f"mesh routing wait timeout, removed stale handler "
                            f"packet_id={pkt_id} ({ack_ex})",
                        )
                    interface._acknowledgment.reset()
                    raise
                if nak_flag[0]:
                    if attempt + 1 >= config.MESH_MULTI_PART_SEND_RETRIES:
                        mt_state.log.log(
                            "log",
                            f"mesh routing NAK part {i + 1}/{n} attempt "
                            f"{attempt + 1}/{config.MESH_MULTI_PART_SEND_RETRIES} "
                            f"ch={channel_index} dest={dest_s} reply_id={rid_s} {pki_s} "
                            f"skipping part, continuing to next chunk text={preview!r}",
                        )
                        break
                    backoff = _retry_backoff_sec(attempt)
                    mt_state.log.log(
                        "log",
                        f"mesh routing NAK part {i + 1}/{n} attempt "
                        f"{attempt + 1}/{config.MESH_MULTI_PART_SEND_RETRIES} "
                        f"ch={channel_index} dest={dest_s} reply_id={rid_s} {pki_s} "
                        f"backoff={backoff:.1f}s text={preview!r}",
                    )
                    time.sleep(backoff)
                    continue

                att_note = (
                    f" attempt {attempt + 1}/{config.MESH_MULTI_PART_SEND_RETRIES}"
                    if attempt > 0
                    else ""
                )
                mt_state.log.log(
                    "log",
                    f"mesh send ok part {i + 1}/{n}{att_note} ch={channel_index} dest={dest_s} "
                    f"reply_id={rid_s} {pki_s} want_ack=True packet_id={pkt_id} routing_ack=ok "
                    f"text={preview!r}",
                )
                had_routing_ack = True
                chunk_ok = True
                if (
                    pkt_id is not None
                    and destination_id == BROADCAST_ADDR
                    and interface.myInfo is not None
                ):
                    try:
                        remember_mesh_text_packet_origin(
                            pkt_id, int(interface.myInfo.my_node_num)
                        )
                    except (TypeError, ValueError):
                        pass
                break
            except Exception as ex:
                if attempt + 1 >= config.MESH_MULTI_PART_SEND_RETRIES:
                    mt_state.log.log(
                        "log",
                        f"mesh send failed part {i + 1}/{n} ch={channel_index} dest={dest_s} "
                        f"reply_id={rid_s} {pki_s} text={preview!r}: {ex}",
                    )
                    return None
                backoff = _retry_backoff_sec(attempt)
                mt_state.log.log(
                    "log",
                    f"mesh send part {i + 1}/{n} attempt {attempt + 1} error, retrying in {backoff:.1f}s: {ex}",
                )
                time.sleep(backoff)
        if chunk_ok:
            parts_ok += 1
        if i < n - 1 and n > 1 and config.MESH_MULTI_PART_DELAY_SEC > 0:
            time.sleep(config.MESH_MULTI_PART_DELAY_SEC)
    if had_routing_ack:
        try:
            is_dm = True
            try:
                dest_num = int(destination_id)
                is_dm = dest_num not in (0, BROADCAST_NUM)
            except (TypeError, ValueError):
                is_dm = str(destination_id) != str(BROADCAST_ADDR)
            mt_stats.record_sent_message(is_direct_message=is_dm)
        except Exception as ex:
            mt_state.log.log("log", f"stats record sent failed: {ex}")
    if record_context and parts_ok == n and n > 0:
        assembled = "".join(chunks)
        if assembled.strip():
            try:
                from . import mt_ai_reply as _mt_ai_reply

                _mt_ai_reply.record_mesh_context_outgoing(
                    channel_index=channel_index,
                    destination_id=destination_id,
                    full_text=assembled,
                )
            except Exception as ex:
                mt_state.log.log("log", f"mesh context outgoing record failed: {ex}")
    return result
