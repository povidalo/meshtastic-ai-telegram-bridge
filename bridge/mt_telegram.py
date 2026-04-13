"""Telegram MarkdownV2 formatting for mesh traffic."""

from typing import Literal, Optional

import config

MeshAutoReplySource = Literal["automated", "gemini", "llama"]
from .mt_packets import MeshMessageDetails

_MDV2_SUPPLEMENT = frozenset("_*[]~`#|{}")


def escape_markdown_v2_supplement(text: str) -> str:
    """Escape chars that utils.telegram Notifier does not, for Telegram MarkdownV2."""
    parts: list[str] = []
    for c in text:
        if c == "\\":
            parts.append("\\\\")
        elif c in _MDV2_SUPPLEMENT:
            parts.append("\\" + c)
        else:
            parts.append(c)
    return "".join(parts)


def format_telegram_message(d: MeshMessageDetails) -> str:
    hops = "n/a" if d.hop_count is None else str(d.hop_count)
    when = d.received_at.strftime("%Y-%m-%d %H:%M:%S %Z")

    ch = escape_markdown_v2_supplement(d.channel_name)
    who = escape_markdown_v2_supplement(d.sender_display_name)
    body = escape_markdown_v2_supplement(d.message)
    if len(body) > 3500:
        body = body[:3497] + "..."

    lines = [
        f"📡  {ch}, from *{who}*",
        f"*When:* {when}",
        f"*Hops:* {hops}",
    ]
    rf_parts: list[str] = []
    if d.rssi is not None:
        rf_parts.append(f"*RSSI:* {d.rssi:.0f}")
    if d.snr is not None:
        rf_parts.append(f"*SNR:* {d.snr:.1f}")
    if rf_parts:
        lines.append("   ".join(rf_parts))
    lines.extend(
        [
            "",
            body,
        ]
    )
    msg = "\n".join(lines)
    if len(msg) > config.TELEGRAM_MAX_LEN:
        msg = msg[: config.TELEGRAM_MAX_LEN - 3] + "..."
    return msg


def mesh_auto_reply_source_caption(source: MeshAutoReplySource) -> str:
    """Human-readable source label (not MarkdownV2-escaped)."""
    if source == "gemini":
        return "Google Gemini"
    if source == "llama":
        return "llama.cpp"
    return "automated"


def format_telegram_mesh_auto_reply(
    details: MeshMessageDetails,
    sent_text: str,
    _packet_id: Optional[int],
    *,
    source: MeshAutoReplySource,
) -> str:
    ch = escape_markdown_v2_supplement(details.channel_name)
    sent = escape_markdown_v2_supplement(sent_text)
    if details.is_direct_message:
        recipient = escape_markdown_v2_supplement(details.sender_display_name)
    else:
        recipient = escape_markdown_v2_supplement("broadcast on this channel")

    src_line = escape_markdown_v2_supplement(mesh_auto_reply_source_caption(source))
    lines = [
        f"↩️ to *{recipient}*, {ch}",
        f"*Source:* {src_line}",
        "",
        f"{sent}",
    ]
    msg = "\n".join(lines)
    if len(msg) > config.TELEGRAM_MAX_LEN:
        msg = msg[: config.TELEGRAM_MAX_LEN - 3] + "..."
    return msg
