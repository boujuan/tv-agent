"""Stremio's per-episode watched marks, the way stremio-core's WatchedBitField stores them in a library item's
`state.watched`: "<anchor video id>:<anchor position + 1>:<base64 of the zlib-compressed bits>". One bit per video,
least significant bit first, in the order of the title's episode list as your Stremio gets it (its first addon with
metadata for the title; specials first). The anchor is the last watched video: when episodes are added to the
list, the bits are lined up on it again.

Marks that can't be lined up with the list (its anchor isn't in it) are never rewritten: that would wipe them."""
import base64
import binascii
import zlib


def decode(serialized: str | None, video_ids: list[str]) -> list[bool] | None:
    """One flag per video id, or None when the marks don't fit this episode list."""
    n = len(video_ids)
    if not serialized:
        return [False] * n
    try:
        anchor, length, packed = serialized.rsplit(":", 2)        # the anchor id has colons of its own
        raw = zlib.decompress(base64.b64decode(packed))
        offset = int(length) - 1
    except (ValueError, zlib.error, binascii.Error):
        return None
    if anchor not in video_ids:
        return None if any(raw) else [False] * n
    offset -= video_ids.index(anchor)
    bits = [False] * n
    for i in range(n):
        j = i + offset
        if 0 <= j < len(raw) * 8:
            bits[i] = bool(raw[j // 8] >> (j % 8) & 1)
    return bits


def encode(bits: list[bool], video_ids: list[str]) -> str:
    last = max((i for i, on in enumerate(bits) if on), default=0)
    raw = bytearray((len(bits) + 7) // 8)
    for i, on in enumerate(bits):
        if on:
            raw[i // 8] |= 1 << (i % 8)
    return f"{video_ids[last]}:{last + 1}:{base64.b64encode(zlib.compress(bytes(raw))).decode()}"


def mark(serialized: str | None, video_ids: list[str], videos: list[str], on: bool = True) -> str | None:
    """The marks with these videos set (or cleared); None when they can't be updated safely."""
    bits = decode(serialized, video_ids)
    if bits is None or any(v not in video_ids for v in videos):
        return None
    for v in videos:
        bits[video_ids.index(v)] = on
    return encode(bits, video_ids)
