"""Stremio's per-episode watched marks, the way stremio-core's WatchedBitField stores them in a library item's
`state.watched`: "<anchor video id>:<anchor position + 1>:<base64 of the zlib-compressed bits>". One bit per video,
least significant bit first, in the order Stremio itself puts the title's videos (stremio_order: the list from your
Stremio's first addon with metadata for the title, duplicates dropped, sorted by season and episode with the
specials last, as stremio-core's MetaItem does). The anchor is the last watched video: when episodes are added to
the list, the bits are lined up on it again.

Marks are only rewritten when they read the same way whichever order wrote them: as stremio-core writes them (the
anchor is the last watched video in its order), or the same in its order and in the addon's own (older clients
used that). Anything else (odd marks some client left, an anchor not in the list) is left alone."""
import base64
import binascii
import zlib


def stremio_order(videos: list[dict]) -> list[str]:
    """Video ids in stremio-core's order: unique by id (the first kept), episodes by season and episode with season
    0 (specials) last, then anything without a season and episode, oldest first."""
    seen, unique = set(), []
    for v in videos:
        if isinstance(v, dict) and v.get("id") and v["id"] not in seen:
            seen.add(v["id"])
            unique.append(v)

    def key(v: dict):
        s, e = v.get("season"), v.get("episode")
        if isinstance(s, int) and isinstance(e, int):
            return 0, s if s else float("inf"), e, ""
        return 1, 0, 0, str(v.get("released") or "~")      # undated after dated
    return [v["id"] for v in sorted(unique, key=key)]


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


def _as_written(serialized: str, ids: list[str], bits: list[bool]) -> bool:
    """Whether these bits are how a client using this order wrote them: its anchor is the last watched video."""
    anchor = serialized.rsplit(":", 2)[0]
    if anchor not in ids:
        return False
    i = ids.index(anchor)
    return bits[i] and not any(bits[i + 1:])


def mark(serialized: str | None, videos: list[dict], chosen: list[str], on: bool = True) -> str | None:
    """The marks with the chosen videos set (or cleared), in stremio-core's order; None when they can't be updated
    safely (see the module docstring)."""
    ids = stremio_order(videos)
    bits = decode(serialized, ids)
    if bits is None or any(v not in ids for v in chosen):
        return None
    if serialized and not _as_written(serialized, ids, bits):
        raw = list(dict.fromkeys(v["id"] for v in videos if isinstance(v, dict) and v.get("id")))
        other = decode(serialized, raw)
        if other is None or {ids[i] for i, b in enumerate(bits) if b} != {raw[i] for i, b in enumerate(other) if b}:
            return None
    for v in chosen:
        bits[ids.index(v)] = on
    return encode(bits, ids)
