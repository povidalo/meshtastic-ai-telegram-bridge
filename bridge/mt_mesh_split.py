"""Split outbound mesh text into UTF-8-safe chunks under the device payload limit.

Limits are **UTF-8 byte length**, not Unicode character count (e.g. Cyrillic is often 2 B/char).
Meshtastic allows at most ``MESH_PROTO_DATA_PAYLOAD_MAX_BYTES`` (233) per ``Data.payload``;
chunking uses ``MESH_TEXT_MAX_PAYLOAD_BYTES`` = that minus a safety margin.
"""

from typing import Callable, List, Optional

import config


def mesh_utf8_payload_len(s: str) -> int:
    return len(s.encode("utf-8"))


def hard_split_utf8(s: str, max_bytes: int) -> List[str]:
    """Split s into segments each <= max_bytes in UTF-8, never breaking inside a codepoint."""
    if max_bytes <= 0:
        return []
    out: List[str] = []
    cur = ""
    for ch in s:
        cand = cur + ch
        if mesh_utf8_payload_len(cand) <= max_bytes:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = ch
    if cur:
        out.append(cur)
    return out


def split_mesh_by_words_only(s: str, max_bytes: int) -> List[str]:
    words = s.split()
    out: List[str] = []
    cur = ""
    for w in words:
        if mesh_utf8_payload_len(w) > max_bytes:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(hard_split_utf8(w, max_bytes))
            continue
        cand = (cur + " " + w) if cur else w
        if mesh_utf8_payload_len(cand) <= max_bytes:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def split_mesh_oversized_string(s: str, max_bytes: int) -> List[str]:
    s = s.strip()
    if not s:
        return []
    if mesh_utf8_payload_len(s) <= max_bytes:
        return [s]
    parts = config.MESH_SENTENCE_SPLIT_RE.split(s)
    if len(parts) > 1:
        acc: List[str] = []
        for p in parts:
            p = p.strip()
            if p:
                acc.extend(split_mesh_oversized_string(p, max_bytes))
        return acc
    return split_mesh_by_words_only(s, max_bytes)


def _mesh_text_unit_stream_with_splitter(
    normalized_text: str,
    max_bytes: int,
    split_line: Callable[[str, int], List[str]],
) -> List[str]:
    units: List[str] = []
    lines = normalized_text.split("\n")
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line:
            units.extend(split_line(line, max_bytes))
        if i < len(lines) - 1:
            units.append("\n")
    while units and units[-1] == "\n":
        units.pop()
    return units


def mesh_text_unit_stream(normalized_text: str, max_bytes: int) -> List[str]:
    return _mesh_text_unit_stream_with_splitter(
        normalized_text, max_bytes, split_mesh_oversized_string
    )


def mesh_text_word_unit_stream(normalized_text: str, max_bytes: int) -> List[str]:
    """Like mesh_text_unit_stream but splits lines by words only (no sentence regex)."""
    return _mesh_text_unit_stream_with_splitter(
        normalized_text, max_bytes, split_mesh_by_words_only
    )


def mesh_join_chunk(cur: str, u: str) -> str:
    if not cur:
        return u
    if u == "\n":
        return cur + "\n"
    if cur.endswith("\n"):
        return cur + u
    return cur + " " + u


def pack_mesh_units_to_chunks(
    units: List[str], max_bytes: int, max_chunks: int
) -> Optional[List[str]]:
    if not units:
        return []
    msgs: List[str] = []
    cur = ""
    for u in units:
        cand = mesh_join_chunk(cur, u)
        if mesh_utf8_payload_len(cand) <= max_bytes:
            cur = cand
            continue
        if cur:
            msgs.append(cur)
            if len(msgs) >= max_chunks:
                return None
        cur = u
        if mesh_utf8_payload_len(cur) > max_bytes:
            return None
    if cur:
        msgs.append(cur)
    if len(msgs) > max_chunks:
        return None
    return msgs


def prepare_mesh_send_chunks(
    text: str, max_bytes: int, max_chunks: int
) -> Optional[List[str]]:
    t = text.replace("\r\n", "\n").strip()
    if not t:
        return []
    if max_bytes > config.MESH_PROTO_DATA_PAYLOAD_MAX_BYTES:
        max_bytes = config.MESH_PROTO_DATA_PAYLOAD_MAX_BYTES
    cap = config.MESH_PROTO_DATA_PAYLOAD_MAX_BYTES

    # Sentence-aware units read well but can yield too many fragments to pack into max_chunks;
    # fall back to word-sized units for denser packing.
    for stream in (
        mesh_text_unit_stream(t, max_bytes),
        mesh_text_word_unit_stream(t, max_bytes),
    ):
        out = pack_mesh_units_to_chunks(stream, max_bytes, max_chunks)
        if out is None:
            continue
        if any(mesh_utf8_payload_len(piece) > cap for piece in out):
            continue
        return out
    return None
