"""Detect tapbacks / single-emoji-only mesh text to ignore."""

import unicodedata
from typing import Any


def _char_is_emoji_codepoint(o: int) -> bool:
    if o in (0x2B50, 0x2B55, 0x2764, 0x2763, 0x2122, 0x2139):
        return True
    if 0x1F300 <= o <= 0x1FAFF:
        return True
    if 0x2600 <= o <= 0x26FF:
        return True
    if 0x2700 <= o <= 0x27BF:
        return True
    if 0x1F600 <= o <= 0x1F64F:
        return True
    if 0x1F680 <= o <= 0x1F6FF:
        return True
    if 0x1F1E6 <= o <= 0x1F1FF:
        return True
    if 0x2300 <= o <= 0x23FF:
        return True
    if 0x2B00 <= o <= 0x2BFF:
        return True
    if 0x1F900 <= o <= 0x1F9FF:
        return True
    if 0x1FA70 <= o <= 0x1FAFF:
        return True
    return False


def _iter_grapheme_clusters(s: str):
    """Split into simple clusters: ZWJ sequences, skin tones, VS, regional-flag pairs."""
    i = 0
    n = len(s)
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        start = i
        i += 1
        if (
            i < n
            and 0x1F1E6 <= ord(s[start]) <= 0x1F1FF
            and 0x1F1E6 <= ord(s[i]) <= 0x1F1FF
        ):
            i += 1
        while i < n:
            o = ord(s[i])
            cat = unicodedata.category(s[i])
            if s[i] == "\u200d" and i + 1 < n:
                i += 2
                continue
            if cat in ("Mn", "Me", "Mc") or (0xFE00 <= o <= 0xFE0F) or (
                0x1F3FB <= o <= 0x1F3FF
            ):
                i += 1
                continue
            break
        yield s[start:i]


def _cluster_looks_like_emoji(cluster: str) -> bool:
    for ch in cluster:
        o = ord(ch)
        if ch in "\u200d\ufe0f":
            continue
        if 0x1F3FB <= o <= 0x1F3FF:
            continue
        cat = unicodedata.category(ch)
        if cat in ("Mn", "Me", "Mc"):
            continue
        if _char_is_emoji_codepoint(o):
            continue
        return False
    return len(cluster) > 0


def is_single_grapheme_emoji_only_message(text: str) -> bool:
    """True if stripped text is exactly one emoji cluster (👍 yes; 👍👎 or 👍 👎 no)."""
    t = text.strip()
    if not t:
        return False
    clusters = list(_iter_grapheme_clusters(t))
    if len(clusters) != 1:
        return False
    return _cluster_looks_like_emoji(clusters[0])


def is_ignored_mesh_noise_packet(packet: dict[str, Any]) -> bool:
    """Reactions / tapbacks and lone emoji: no Telegram, no auto-reply."""
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return False
    if decoded.get("emoji"):
        return True
    if decoded.get("portnum") == "REPLY_APP":
        return True
    text = decoded.get("text")
    if text is None:
        return False
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return False
    if is_single_grapheme_emoji_only_message(text):
        return True
    return False
