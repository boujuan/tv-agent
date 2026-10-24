"""Intros, recaps and end credits found in the files themselves, for "skip the intro", auto_skip and the next episode
at the credits on episodes the timestamp databases (skips.py) don't know. Measurements: docs/intros-2026-10-24.md.

Cheapest first:
1. Chapters. Web releases carry the service's own markers as named chapters ("Intro", "Opening Credit", "Recap",
   "End Credits"; Amazon's are scene names, the intro's being the show's name). They're in the file's header: one
   256 KB read.
2. Another copy's chapters, when the file playing has none named (a Blu-ray remux numbers its chapters): a few
   other copies of the episode are opened (their headers only). Their times are moved to this file by matching a
   few seconds of sound at the intro and at the credits.
3. The sound, as Plex and Jellyfin do: the starts (and ends) of two or three episodes of the season are compared,
   and the stretch they share is the intro (and the credits, when their music is the same). Audio only, read from
   the episodes' cheapest copies, in the background and at a capped rate; the intro's sound is kept per season, so a
   later episode needs only its own start read, and the file playing only a few seconds where the intro is.

Sound is compared by a fingerprint (Haitsma and Kalker's): 32 bits every 0.05 s, the signs of how the energy of
33 bands between 300 and 2000 Hz changes from band to band and from one moment to the next. The same sound in two
encodes (TrueHD and AAC, 5.1 and stereo) differs in ~2-6 of the 32 bits, unrelated sound in ~16.

Nothing here is on the way to playing: an episode's markers are looked for once it plays, and the next episode's
while this one plays. Links are read here and given to ffmpeg on a pipe, never on its command line or in a log.
"""
import asyncio
import base64
import json
import logging
import re
import resource
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace

import httpx
import numpy as np

from . import config as config_mod
from .skips import Segment
from .tv import thumbs
from .tv.thumbs import CHAPTERS, ThumbError, children, element, uint

log = logging.getLogger(__name__)

# ---- chapters ----

EDITION, EDITION_DEFAULT, ATOM, CHAPTER_START, DISPLAY, CHAP_STRING, HIDDEN, ENABLED = (
    0x45B9, 0x45DB, 0xB6, 0x91, 0x80, 0x85, 0x98, 0x4598)
INTRO = re.compile(r"^(?:intro(?:duction)?|opening(?: credits?| titles?| theme| sequence)?|op|main titles?|"
                   r"title sequence|theme(?: song)?)$", re.I)
RECAP = re.compile(r"^(?:recap|previously(?: on\b.*)?|previous(?:ly)? episodes?|summary)$", re.I)
CREDITS = re.compile(r"^(?:(?:end(?:ing)?|closing|final) ?credits?|credits?|ending|outro|ed|end titles?|"
                     r"credits? roll)$", re.I)
MAX_INTRO_MS = 180_000
MAX_RECAP_MS = 240_000


@dataclass(frozen=True)
class Chapter:
    start_ms: int
    name: str


def chapters(b: bytes) -> list[Chapter]:
    """The chapters of a Matroska Chapters element: the default edition's (else the first's), shown ones, in order."""
    _, a, z = element(b, 0)
    editions = []
    for eid, x, y, _ in children(b, a, z):
        if eid != EDITION:
            continue
        found, default = [], False
        for k, p, q, _ in children(b, x, y):
            if k == EDITION_DEFAULT:
                default = bool(uint(b[p:q]))
            if k != ATOM:
                continue
            start, name, shown = 0, "", True
            for kk, pp, qq, _ in children(b, p, q):
                if kk == CHAPTER_START:
                    start = uint(b[pp:qq])
                elif kk in (HIDDEN, ENABLED):
                    shown &= bool(uint(b[pp:qq])) == (kk == ENABLED)
                elif kk == DISPLAY and not name:
                    for c, s, e, _ in children(b, pp, qq):
                        if c == CHAP_STRING:
                            name = b[s:e].decode(errors="replace").strip("\x00 ")
            if shown:
                found.append(Chapter(start // 1_000_000, name))
        editions.append((default, found))
    if not editions:
        return []
    return sorted(next((f for d, f in editions if d), editions[0][1]), key=lambda c: c.start_ms)


def from_chapters(chs: list[Chapter], duration_ms: int, show: str = "") -> list[Segment]:
    """The intro, recap and credits a file's chapter names mark: each from its chapter's start to the next one's. A
    chapter named after the show in the first quarter (Amazon's scene names) is its intro."""
    out: list[Segment] = []
    show_name = re.sub(r"\W+", " ", show).strip().lower()
    for i, c in enumerate(chs):
        end = chs[i + 1].start_ms if i + 1 < len(chs) else duration_ms
        name = re.sub(r"\s+", " ", c.name).strip()
        if not name or end <= c.start_ms:
            continue
        kind = ("intro" if INTRO.match(name) else "recap" if RECAP.match(name) else "credits" if CREDITS.match(name)
                else None)
        if kind is None and show_name and re.sub(r"\W+", " ", name).strip().lower() == show_name \
                and duration_ms and c.start_ms < duration_ms / 4:
            kind = "intro"
        if kind == "intro" and not 3_000 <= end - c.start_ms <= MAX_INTRO_MS:
            continue
        if kind == "recap" and not 3_000 <= end - c.start_ms <= MAX_RECAP_MS:
            continue
        if kind == "credits":
            if duration_ms and c.start_ms < duration_ms * 0.7:
                continue
            end = duration_ms or end                # the credits run to the end, whatever chapters follow
        if kind and not any(s.kind == kind for s in out):
            out.append(Segment(kind, c.start_ms, end, "chapters", False, True))   # sure once confirmed
    return out


# ---- reading a file: its header, and the sound of a stretch of it ----

READ_MBIT = 80              # the most a detection reads (the TV plays meanwhile, on the same line and Wi-Fi)
CHUNK = 1 << 20


class Reader:
    """Range reads of one file. The link stays here: never in a log or an error."""

    def __init__(self, http: httpx.AsyncClient, url: str, headers: dict | None = None,
                 pause: Callable[[], Awaitable[None]] | None = None, mbit: float = READ_MBIT):
        self.http, self._url, self._headers = http, url, dict(headers or {})
        self.pause = pause                       # waits while the TV needs the line (its player's buffer is low)
        self.rate = mbit * 1e6 / 8
        self.read = 0                            # bytes, all reads

    async def fetch(self, start: int, length: int) -> bytes:
        try:
            r = await self.http.get(self._url, headers={**self._headers, "Range": f"bytes={start}-{start + length - 1}"},
                                    timeout=httpx.Timeout(10, connect=5))
        except httpx.HTTPError as e:
            raise thumbs.ReadError(f"reading the file: {type(e).__name__}") from None
        if r.status_code != 206:
            raise thumbs.ReadError(f"reading the file: HTTP {r.status_code}")
        self.read += len(r.content)
        return r.content

    async def chunks(self, start: int, end: int) -> AsyncIterator[bytes]:
        """The bytes from start to end, at most self.rate a second."""
        t0, got = time.monotonic(), 0
        try:
            async with self.http.stream("GET", self._url, headers={**self._headers, "Range": f"bytes={start}-{end - 1}"},
                                        timeout=httpx.Timeout(15, connect=5)) as r:
                if r.status_code != 206:
                    raise thumbs.ReadError(f"reading the file: HTTP {r.status_code}")
                async for chunk in r.aiter_bytes(CHUNK):
                    self.read += len(chunk)
                    got += len(chunk)
                    yield chunk
                    if self.pause:
                        await self.pause()
                    ahead = got / self.rate - (time.monotonic() - t0)
                    if ahead > 0:
                        await asyncio.sleep(ahead)
        except httpx.HTTPError as e:
            raise thumbs.ReadError(f"reading the file: {type(e).__name__}") from None


@dataclass
class Header:
    mkv: thumbs.Mkv
    chapters: list[Chapter]
    audio: list[tuple[int, str, bool]]          # (track number, language, default) of each audio track, in order
    prefix: bytes = b""                         # what ffmpeg reads before the clusters: EBML header, Info, Tracks

    @property
    def duration_ms(self) -> int:
        return self.mkv.duration_ms

    @property
    def mbit(self) -> float:
        """The file's bitrate, from its index (an addon's size can be another file's: a season pack's)."""
        m = self.mkv
        return (m.places[-1][0] * 8 / m.times[-1] / 1000) if m.times and m.times[-1] else 0.0

    def track(self, lang: str | None) -> int:
        """Which audio track (ffmpeg's a:N) to listen to: the language asked for, else the default, else the first."""
        if lang:
            for i, (_, code, _) in enumerate(self.audio):
                if code.lower()[:2] == lang.lower()[:2]:
                    return i
        return next((i for i, (_, _, default) in enumerate(self.audio) if default), 0)


async def header(reader: Reader) -> Header:
    """The file's header, index and chapters (ThumbError when it isn't a Matroska file with an index)."""
    m = await thumbs.Mkv.read(reader.fetch)
    raw = m.elements.get(CHAPTERS)
    if raw is None and CHAPTERS in m.seeks:
        raw = await thumbs.Mkv._whole(reader.fetch, m.segment + m.seeks[CHAPTERS], 64 << 10)
    try:
        chs = chapters(raw) if raw else []
    except (thumbs.Short, ValueError):
        chs = []
    tracks = m.elements.get(thumbs.TRACKS, b"")
    audio = []
    try:
        _, a, z = element(tracks, 0)
        for eid, x, y, _ in children(tracks, a, z):
            if eid != thumbs.ENTRY:
                continue
            f = {k: tracks[p:q] for k, p, q, _ in children(tracks, x, y)}
            if uint(f.get(thumbs.TYPE, b"")) == thumbs.AUDIO_TRACK:
                lang = (f.get(thumbs.LANGUAGE_BCP47) or f.get(thumbs.LANGUAGE) or b"eng").decode(errors="replace")
                audio.append((uint(f.get(thumbs.NUMBER, b"")), lang.strip("\x00 "), uint(f.get(0x88, b"\x01")) == 1))
    except (thumbs.Short, ValueError):
        raise ThumbError("a Matroska header I can't read") from None
    if not audio:
        raise ThumbError("no audio track")
    prefix = m.ebml + b"\x18\x53\x80\x67" + thumbs.UNKNOWN + m.elements.get(thumbs.INFO, b"") + tracks
    return Header(m, chs, audio, prefix)


RATE = 5512                 # Hz: the fingerprint looks at 300-2000 Hz
FRAME = 2048                # 0.37 s
HOP = 276                   # 0.05 s
STEP_MS = HOP / RATE * 1000
FFMPEG_S = 60               # s for ffmpeg, besides the read at the capped rate


async def sound(reader: Reader, h: Header, t0_ms: int, t1_ms: int, track: int = 0) -> tuple[np.ndarray, int]:
    """The sound from t0 to t1 ms as mono samples at RATE, and the ms its first sample is at. Only the clusters
    between the index points around the stretch are read; ffmpeg decodes the one audio track (niced)."""
    m = h.mkv
    i, j = m.cue(max(0, t0_ms)), m.cue(t1_ms) + 1
    start = m.segment + m.places[i][0]
    to_end = j >= len(m.places)
    if to_end:                                   # past the last index point: as much as the stretch can take
        end = start + int((t1_ms - m.times[i]) / 1000 * max(h.mbit, 1) * 1e6 / 8 * 1.5) + (4 << 20)
    else:
        end = m.segment + m.places[j][0]
    begin = max(t0_ms, m.times[i]) / 1000
    p = await asyncio.create_subprocess_exec(
        "nice", "-n", "15", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-threads", "1", "-copyts",
        "-f", "matroska", "-i", "pipe:0", "-map", f"0:a:{track}", "-vn", "-sn", "-dn",
        "-af", f"atrim=start={begin:.3f}:end={t1_ms / 1000:.3f},aresample={RATE}", "-ac", "1", "-f", "s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def feed():
        try:
            p.stdin.write(h.prefix)
            async for chunk in reader.chunks(start, end):
                p.stdin.write(chunk)
                await p.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass                                  # ffmpeg had all it wanted
        except thumbs.ReadError:
            if not to_end:                        # the guess past the end may ask for more than there is
                raise
        finally:
            p.stdin.close()
    try:
        out, err, _ = await asyncio.wait_for(asyncio.gather(p.stdout.read(), p.stderr.read(), feed()),
                                             FFMPEG_S + (end - start) / reader.rate * 1.5)
        await p.wait()
    except BaseException:
        p.kill()
        await p.wait()
        raise
    if p.returncode and not out:
        lines = err.decode(errors="replace").strip().splitlines()
        raise ThumbError(f"ffmpeg: {lines[-1][:160] if lines else p.returncode}")
    return np.frombuffer(out, dtype="<i2").astype(np.float32), int(round(begin * 1000))


# ---- fingerprints ----

_EDGES = None


@dataclass
class Print:
    """A stretch of sound's fingerprint: 32 bits every STEP_MS, and which steps are near silence (they match
    nothing: two silences would look alike)."""
    t0: float                                   # ms in its file of step 0
    bits: np.ndarray                            # uint32
    quiet: np.ndarray                           # bool

    def __len__(self) -> int:
        return len(self.bits)

    def ms(self, i: float) -> int:
        return int(round(self.t0 + i * STEP_MS))

    def index(self, ms: float) -> int:
        return int(round((ms - self.t0) / STEP_MS))

    def part(self, t0_ms: float, t1_ms: float) -> "Print":
        i, j = max(0, self.index(t0_ms)), min(len(self), self.index(t1_ms))
        return Print(self.ms(i), self.bits[i:j].copy(), self.quiet[i:j].copy())

    def pack(self) -> dict:
        return {"t0": round(self.t0, 1), "bits": base64.b64encode(self.bits.astype("<u4").tobytes()).decode(),
                "quiet": base64.b64encode(np.packbits(self.quiet).tobytes()).decode()}

    @classmethod
    def unpack(cls, d: dict) -> "Print":
        bits = np.frombuffer(base64.b64decode(d["bits"]), dtype="<u4").astype(np.uint32)
        quiet = np.unpackbits(np.frombuffer(base64.b64decode(d["quiet"]), dtype=np.uint8))[:len(bits)].astype(bool)
        return cls(float(d["t0"]), bits, quiet)


def fingerprint(pcm: np.ndarray, t0_ms: float = 0.0) -> Print:
    global _EDGES
    n = 1 + (len(pcm) - FRAME) // HOP if len(pcm) >= FRAME + HOP else 0
    if n < 2:
        return Print(t0_ms, np.zeros(0, np.uint32), np.zeros(0, bool))
    if _EDGES is None:
        freqs = np.fft.rfftfreq(FRAME, 1 / RATE)
        _EDGES = np.searchsorted(freqs, np.geomspace(300, 2000, 34))
    window = np.hanning(FRAME).astype(np.float32)
    energy = np.empty((n, 33), np.float64)
    loud = np.empty(n, np.float64)
    for a in range(0, n, 1024):                   # in blocks: 10 minutes at once would be 300 MB
        b = min(n, a + 1024)
        idx = np.arange(FRAME)[None, :] + HOP * np.arange(a, b)[:, None]
        frames = pcm[idx]
        spec = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
        energy[a:b] = np.add.reduceat(spec, _EDGES[:-1], axis=1)[:, :33]
        loud[a:b] = np.sqrt(np.mean(frames ** 2, axis=1))
    d = energy[:, :-1] - energy[:, 1:]
    bits = (d[1:] - d[:-1]) > 0
    weights = np.left_shift(np.uint64(1), np.arange(32, dtype=np.uint64))
    value = (bits.astype(np.uint64) * weights).sum(axis=1).astype(np.uint32)
    quiet = loud[1:] < 40.0                       # about -58 dBFS
    # step i compares frame i+1 with frame i: it's about the moment between them
    return Print(t0_ms + (FRAME / 2 + HOP / 2) / RATE * 1000, value, quiet)


LIMIT = 10.0                # mean differing bits over a second, at most, for the same sound (unrelated: ~16)
SMOOTH = 20                 # steps: a second


@dataclass
class Match:
    a: int                  # where it starts in a (steps)
    b: int                  # and in b
    n: int                  # how long (steps)
    err: float              # mean differing bits

    @property
    def seconds(self) -> float:
        return self.n * STEP_MS / 1000


def _errors(a: Print, b: Print, d: int) -> tuple[int, int, np.ndarray]:
    """For b shifted so that its step j lines up with a's step j + d: (a's first step, b's, the bits differing at each
    step; silence counts as unrelated)."""
    i0, j0 = max(d, 0), max(-d, 0)
    n = min(len(a) - i0, len(b) - j0)
    if n <= 0:
        return i0, j0, np.zeros(0, np.float32)
    e = np.bitwise_count(a.bits[i0:i0 + n] ^ b.bits[j0:j0 + n]).astype(np.float32)
    e[a.quiet[i0:i0 + n] | b.quiet[j0:j0 + n]] = 16.0
    return i0, j0, e


def _runs(e: np.ndarray, min_n: int) -> list[tuple[int, int, float]]:
    """(start, end, mean) of each stretch whose bits differ by under LIMIT over a second, gaps under 2 s closed."""
    if len(e) < SMOOTH:
        return []
    avg = np.convolve(e, np.ones(SMOOTH) / SMOOTH, mode="same")
    good = np.concatenate([[False], avg <= LIMIT, [False]])
    edges = np.flatnonzero(np.diff(good.astype(np.int8)))
    spans = [[int(s), int(t)] for s, t in zip(edges[::2], edges[1::2])]
    merged: list[list[int]] = []
    for s, t in spans:
        if merged and s - merged[-1][1] < 2 * SMOOTH:
            merged[-1][1] = t
        else:
            merged.append([s, t])
    return [(s, t, float(e[s:t].mean())) for s, t in merged if t - s >= min_n]


def _offsets(a: Print, b: Print, most: int = 8) -> list[int]:
    """The shifts at which a and b may share sound: steps whose halves (16 bits) are equal, counted per shift."""
    votes = np.zeros(len(a) + len(b) + 1, np.int64)
    ia = np.flatnonzero(~a.quiet)
    ib = np.flatnonzero(~b.quiet)
    if not len(ia) or not len(ib):
        return []
    for shift in (0, 16):
        ka = (a.bits[ia] >> shift) & 0xFFFF
        kb = (b.bits[ib] >> shift) & 0xFFFF
        order = np.argsort(kb, kind="stable")
        sk, si = kb[order], ib[order]
        lo, hi = np.searchsorted(sk, ka, "left"), np.searchsorted(sk, ka, "right")
        count = hi - lo
        keep = (count > 0) & (count <= 6)                 # common values (steady tones) say nothing
        rows = np.repeat(np.arange(len(ka))[keep], count[keep])
        starts = np.repeat(lo[keep], count[keep])
        within = np.arange(len(rows)) - np.repeat(np.cumsum(count[keep]) - count[keep], count[keep])
        js = si[starts + within]
        np.add.at(votes, ia[rows] - js + len(b), 1)
    smooth = np.convolve(votes, np.ones(5, np.int64), mode="same")
    out: list[int] = []
    for k in np.argsort(-smooth):
        if smooth[k] < 8 or len(out) == most:
            break
        if all(abs(k - o) > 10 for o in out):
            out.append(int(k))
    return [o - len(b) for o in out]


def shared(a: Print, b: Print, min_s: float = 4.0) -> list[Match]:
    """The stretches a and b share, longest first."""
    min_n = int(min_s * 1000 / STEP_MS)
    found: list[Match] = []
    for d0 in _offsets(a, b):
        best = None
        for d in range(d0 - 3, d0 + 4):            # the shift the votes peak around: the best-matching one of them
            i0, j0, e = _errors(a, b, d)
            for s, t, mean in _runs(e, min_n):
                m = Match(i0 + s, j0 + s, t - s, mean)
                if best is None or (m.n, -m.err) > (best.n, -best.err):
                    best = m
        if best and not any(abs((best.a - best.b) - (f.a - f.b)) <= 10 and best.a < f.a + f.n and f.a < best.a + best.n
                            for f in found):
            found.append(best)
    return sorted(found, key=lambda m: -m.n)


# ---- intros and credits from the sound of several episodes ----

MIN_INTRO_S = 8.0
LOGO_S = 15.0               # shared sound this short at the very start is a studio's logo
INTRO_WINDOW = (0.33, 900_000)      # the share of an episode, and at most this many ms, looked at for its intro
LEARN_STEP = 300_000               # the episodes' starts are read this much more at a time until they share one
CREDITS_WINDOW = 90_000             # the last ms looked at for credits (the shared logos are in the last 30 s)
MIN_CREDITS_S = 4.0


def _intro_matches(a: Print, b: Print) -> list[Match]:
    out = []
    for m in shared(a, b, MIN_INTRO_S):
        if m.seconds * 1000 > MAX_INTRO_MS:
            continue
        if min(a.ms(m.a), b.ms(m.b)) < 1_500 and m.seconds < LOGO_S:
            continue
        out.append(m)
    return out


def season_intro(prints: list[Print], need: int | None = None) -> tuple[Print, list[int | None]] | None:
    """The intro of a season from the starts of two or more of its episodes: the longest stretch two of them share
    that `need` of them have (all, by default). Shared by only two of three, it must be long and clean: a recap
    shows the last episode's scenes too. (Its sound, and where it starts in each episode, in ms; None for an episode
    that hasn't it.)"""
    need = len(prints) if need is None else need
    best: tuple[int, Print] | None = None
    for x in range(len(prints)):
        for y in range(x + 1, len(prints)):
            for m in _intro_matches(prints[x], prints[y])[:2]:
                ref = prints[x].part(prints[x].ms(m.a), prints[x].ms(m.a + m.n))
                found = sum(1 for k, p in enumerate(prints) if k in (x, y) or locate(ref, p) is not None)
                if found < need or (found == 2 < len(prints) and (m.seconds < 15 or m.err > 6)):
                    continue
                if best is None or found * 10_000 + m.n > best[0]:
                    best = (found * 10_000 + m.n, ref)
    if best is None:
        return None
    ref = best[1]
    return ref, [(f[0] if (f := locate(ref, p)) else None) for p in prints]


def locate(ref: Print, p: Print, share: float = 0.6) -> tuple[int, int, float] | None:
    """Where ref's sound is in p: (start, end ms in p's file, mean differing bits), when at least `share` of it is."""
    ms = shared(ref, p, max(3.0, min(MIN_INTRO_S, len(ref) * STEP_MS / 1000 * share)))
    if not ms or ms[0].n < share * len(ref):
        return None
    m = ms[0]
    start = p.ms(m.b - m.a) if m.a <= 40 else p.ms(m.b)                   # its start, if not cut short
    end = p.ms(m.b + (len(ref) - m.a)) if len(ref) - (m.a + m.n) <= 40 else p.ms(m.b + m.n)
    return start, end, m.err


def season_credits(prints: list[Print], ends: list[int]) -> tuple[Print, list[int | None]] | None:
    """The credits by sound: a stretch near the end that the episodes share. Measured on Breaking Bad and Community,
    only the studios' logos are (the music changes from one episode to the next): 4-7 s, within the last 30 s, so
    a marker found this way is some seconds into the credits. (Its sound, and where it starts in each episode.)"""
    best: tuple[int, Print] | None = None
    for x in range(len(prints)):
        for y in range(x + 1, len(prints)):
            for m in shared(prints[x], prints[y], MIN_CREDITS_S):
                if ends[x] - prints[x].ms(m.a + m.n) > 30_000 or ends[y] - prints[y].ms(m.b + m.n) > 30_000:
                    continue
                ref = prints[x].part(prints[x].ms(m.a), prints[x].ms(m.a + m.n))
                found = sum(1 for k, p in enumerate(prints) if k in (x, y) or locate(ref, p, 0.7) is not None)
                score = found * 10_000 + m.n
                if found >= min(3, len(prints)) and (best is None or score > best[0]):
                    best = (score, ref)
                break
    if best is None:
        return None
    ref = best[1]
    return ref, [(f[0] if (f := locate(ref, p, 0.7)) else None) for p in prints]


# ---- what's known, kept ----

class Store:
    """Per title (~/.local/share/tv-agent/intros/<imdb>.json): the files whose markers are known (by name and size),
    and per season the intro's and the credits' sound."""

    def __init__(self, root=None):
        self.root = root or config_mod.DATA_DIR / "intros"
        self._cache: dict[str, dict] = {}

    def _path(self, imdb: str):
        return self.root / f"{re.sub(r'[^\w]', '_', imdb)}.json"

    def title(self, imdb: str) -> dict:
        if imdb not in self._cache:
            try:
                self._cache[imdb] = json.loads(self._path(imdb).read_text())
            except (OSError, ValueError):
                self._cache[imdb] = {}
            self._cache[imdb].setdefault("files", {})
            self._cache[imdb].setdefault("seasons", {})
        return self._cache[imdb]

    def save(self, imdb: str):
        data = self.title(imdb)
        files = data["files"]
        if len(files) > 400:
            for k in sorted(files, key=lambda k: files[k].get("at", 0))[:100]:
                del files[k]
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self._path(imdb)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(path)
        except OSError as e:
            log.info("intros: %s", type(e).__name__)

    @staticmethod
    def key(filename: str, size: int) -> str:
        return f"{filename}|{size}"

    def file(self, imdb: str, filename: str, size: int) -> list[Segment] | None:
        """This file's markers, if it's been looked at ([] when nothing was found)."""
        got = self.title(imdb)["files"].get(self.key(filename, size))
        if got is None:
            return None
        return [Segment(k, int(s), int(e), src, bool(sure), True) for k, s, e, src, sure in got.get("segments", [])]

    def keep_file(self, imdb: str, video: str, filename: str, size: int, duration_ms: int, segments: list[Segment],
                  how: str = ""):
        if not filename:
            return
        self.title(imdb)["files"][self.key(filename, size)] = {
            "video": video, "ms": duration_ms, "at": int(time.time()), "how": how,
            "segments": [[s.kind, s.start_ms, s.end_ms, s.source, s.sure] for s in segments]}
        self.save(imdb)

    def season(self, imdb: str, season: int) -> dict:
        return self.title(imdb)["seasons"].setdefault(str(season), {})

    def reference(self, imdb: str, season: int, kind: str) -> Print | None:
        got = self.season(imdb, season).get(kind)
        return Print.unpack(got["print"]) if got and got.get("print") else None


def cpu_s() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


@dataclass
class Cost:
    """What a search for markers took: bytes read, seconds, ffmpeg's CPU seconds, and its steps."""
    read: int = 0
    seconds: float = 0.0
    cpu: float = 0.0
    steps: list[str] = field(default_factory=list)


# ---- finding a file's markers ----

CHAPTERED = re.compile(r"\b(?:NF|AMZN|ATVP|DSNP|HULU|HMAX|PCOK|PMTP|STAN|CRAV)\b", re.I)   # services' markers
OTHER_COPIES = 6            # other copies' headers read, at most, per episode (a header is a 256 KB read)
GROUP = re.compile(r"-([A-Za-z0-9]{2,20})(?:\.(?:mkv|mp4|avi|ts|m2ts))?$")
WEB_DL = re.compile(r"WEB-?DL", re.I)
SCAN_MS = 150_000           # the start of an episode is read this much at a time until the intro's sound is there
SAME_CUT_MS = 150           # two copies this close in length are the same cut (their times are the same)
AGREE_MS = 2_000            # two copies' chapters this close at both ends agree
PROBE_MS = 12_000           # read at a chapter's intro to hear whether the season's intro is there
SOURCES = ("chapters", "chapters of another copy", "sound")    # markers found in the file itself
KINDS = ("intro", "recap", "credits")


@dataclass
class Copy:
    """A file of the episode: its stream's name and size (the store's key), a reader of its link, its header."""
    filename: str
    size: int
    reader: Reader
    head: Header | None = None


def missing(segments: list[Segment]) -> set[str]:
    return {"intro", "credits"} - {s.kind for s in segments}


def _shifted(s: Segment, d: int, source: str, sure: bool = False) -> Segment:
    return Segment(s.kind, max(0, s.start_ms + d), s.end_ms + d, source, sure, True)


def group(filename: str) -> str:
    """A release's group ("FLUX", "NTb"): its releases share their chapters, right or wrong."""
    m = GROUP.search(filename)
    return m.group(1).lower() if m else filename.lower()


def _agree(a: Segment, b: Segment) -> bool:
    return abs(a.start_ms - b.start_ms) <= AGREE_MS and (a.kind == "credits" or abs(a.end_ms - b.end_ms) <= AGREE_MS)


def vote(mine: tuple[str, Segment] | None, theirs: list[tuple[str, Segment]]) -> tuple[Segment | None, bool]:
    """Of this file's chapter marker and the same-cut copies' (group, marker), the one most release groups agree on
    (this file's on a tie), and whether two groups or more do (sure). One group's chapters can be off: FLUX's
    Pluribus S1E3 intro is 55 s late and its credits 33 s early in all three of its releases, where two other
    groups and the sound agree."""
    every = ([mine] if mine else []) + theirs
    if not every:
        return None, False

    def groups(x: Segment) -> int:
        return len({g for g, y in every if _agree(x, y)})
    best = max(every, key=lambda gx: (groups(gx[1]), gx is mine))[1]
    return best, groups(best) >= 2


class Finder:
    """Finds and keeps files' intro, recap and credits markers (one search at a time, in the background).

    A marker is sure (skipped by itself with auto_skip, the credits counting down to the next episode with binge)
    when two copies' chapters agree on it or the season's sound is there; one copy's chapters alone only show the
    Skip intro button, and a skip when asked inside it."""

    def __init__(self, http: httpx.AsyncClient, store: Store | None = None,
                 pause: Callable[[], Awaitable[None]] | None = None):
        self.http = http
        self.store = store or Store()
        self.pause = pause
        self.lock = asyncio.Lock()
        self.last_cost: Cost | None = None
        self._chapters: dict[str, list[Segment]] = {}    # what files' own chapters name, while the rest is looked for

    def known(self, imdb: str, filename: str, size: int) -> list[Segment] | None:
        """A file's markers as far as they're known: all of them once looked for, else what its chapters name."""
        kept = self.store.file(imdb, filename, size)
        return kept if kept is not None else self._chapters.get(Store.key(filename, size))

    def copy(self, filename: str, size: int, url: str, headers: dict | None = None) -> Copy:
        return Copy(filename, size, Reader(self.http, url, headers, self.pause))

    async def chapters_of(self, imdb: str, show: str, f: Copy) -> list[Segment]:
        """What this file's own chapters name (not sure yet), or all that's kept about it: one read of its header."""
        if (kept := self.store.file(imdb, f.filename, f.size)) is not None:
            return kept
        f.head = f.head or await header(f.reader)
        found = from_chapters(f.head.chapters, f.head.duration_ms, show)
        if len(self._chapters) > 50:
            self._chapters.clear()
        self._chapters[Store.key(f.filename, f.size)] = found
        return found

    async def find(self, imdb: str, video: str, show: str, f: Copy,
                   copies: Callable[[str], Awaitable[list[tuple[str, int, Callable[[], Awaitable[Copy | None]]]]]],
                   neighbours: list[str], lang: str | None = None) -> list[Segment]:
        """This file's intro, recap and credits, kept: its chapters and the same-cut copies' compared, what it lacks
        from another cut's chapters moved onto it by sound, the intro checked or found by the season's sound.
        `copies(video)` lists an episode's other copies as (file name, size, open()), smallest first; `neighbours`
        are the episodes compared with this one when the season's sound isn't known yet."""
        if (kept := self.store.file(imdb, f.filename, f.size)) is not None:
            return kept
        async with self.lock:
            if (kept := self.store.file(imdb, f.filename, f.size)) is not None:
                return kept
            cost, t0, cpu0 = Cost(), time.perf_counter(), cpu_s()
            read0 = f.reader.read
            others: list[Copy] = []
            try:
                found = await self._find(imdb, video, show, f, copies, neighbours, lang, cost, others)
            finally:
                cost.read = f.reader.read - read0 + sum(c.reader.read for c in others)
                cost.seconds, cost.cpu = time.perf_counter() - t0, cpu_s() - cpu0
                self.last_cost = cost
                log.info("intros of %s: %s; read %.1f MB in %.1f s, ffmpeg %.1f s CPU", video,
                         "; ".join(cost.steps) or "nothing to do", cost.read / 1e6, cost.seconds, cost.cpu)
            self.store.keep_file(imdb, video, f.filename, f.size, f.head.duration_ms if f.head else 0, found,
                                 "; ".join(cost.steps))
            self._chapters.pop(Store.key(f.filename, f.size), None)
            return found

    async def _open(self, opener, others: list[Copy], cost: Cost, filename: str) -> Copy | None:
        try:
            c = await opener()
            if c is None:
                return None
            others.append(c)
            c.head = await header(c.reader)
            return c
        except (ThumbError, OSError) as e:
            cost.steps.append(f"{filename[:40]}: {e}")
            return None

    async def _find(self, imdb, video, show, f: Copy, copies, neighbours, lang, cost: Cost,
                    others: list[Copy]) -> list[Segment]:
        f.head = f.head or await header(f.reader)
        mine = {s.kind: (group(f.filename), s) for s in from_chapters(f.head.chapters, f.head.duration_ms, show)}
        cost.steps.append(f"chapters: {', '.join(mine) or 'none named'}")
        season = int(video.split(":")[1]) if video.count(":") == 2 else 0
        # Other copies' chapters: the same cut's to compare with this file's, another cut's moved onto it. One copy
        # per release group first; read until two groups agree on everything there is
        same: dict[str, list[tuple[str, Segment]]] = {k: [] for k in KINDS}
        moved: dict[str, Segment] = {}
        listed = sorted((x for x in await copies(video) if (x[0], x[1]) != (f.filename, f.size)),
                        key=lambda x: (not CHAPTERED.search(x[0]), not WEB_DL.search(x[0]), -(x[1] or 0)))
        firsts, seen = [], {group(f.filename)}
        for x in listed:
            if group(x[0]) not in seen:
                seen.add(group(x[0]))
                firsts.append(x)
        order = firsts + [x for x in listed if x not in firsts]

        def settled() -> bool:
            kinds = set(mine) | {k for k in KINDS if same[k]}
            return not missing([Segment(k, 0, 0, "") for k in kinds]) and all(
                vote(mine.get(k), same[k])[1] for k in kinds)
        reads = 0
        for filename, size, opener in order:
            if reads >= OTHER_COPIES or settled():
                break
            reads += 1
            if (c := await self._open(opener, others, cost, filename)) is None:
                continue
            theirs = from_chapters(c.head.chapters, c.head.duration_ms, show)
            if abs(f.head.duration_ms - c.head.duration_ms) <= SAME_CUT_MS:
                for t in theirs:
                    same[t.kind].append((group(filename), _shifted(t, 0, "chapters of another copy")))
            else:
                lacking = [t for t in theirs if t.kind not in mine and not same[t.kind] and t.kind not in moved]
                for t in await self._move(c, f, lacking, lang, cost):
                    moved[t.kind] = t
        found: dict[str, Segment] = {}
        for kind in KINDS:
            best, sure = vote(mine.get(kind), same[kind])
            if best is None and kind in moved:
                best, sure = moved[kind], False
            if best is not None:
                found[kind] = replace(best, sure=sure)
                cost.steps.append(f"{kind} at {best.start_ms / 1000:.1f} s: "
                                  + ("release groups agree" if sure else "one group's chapters"))
        # Services mark an intro wherever there's one to skip: their chapters with credits and no intro, none
        services = [c for c in [f, *others] if c.head and CHAPTERED.search(c.filename)
                    and abs(c.head.duration_ms - f.head.duration_ms) <= SAME_CUT_MS]
        no_intro = "intro" not in found and any(
            {"credits"} <= {x.kind for x in from_chapters(c.head.chapters, c.head.duration_ms, show)} for c in services)
        # The intro by the season's sound: checked where chapters put it, found where they don't
        if season and not no_intro and not (found.get("intro") and found["intro"].sure):
            got = await self._intro_by_sound(imdb, season, video, show, f, found.get("intro"), copies, neighbours,
                                             lang, cost, others)
            if got is False:
                found.pop("intro", None)
            elif got is not None:
                found["intro"] = got
        elif no_intro:
            cost.steps.append("no intro: the service's chapters mark the credits and no intro")
        if season and "credits" not in found:
            got = await self._by_sound(imdb, season, video, f, copies, neighbours, lang, cost, others, "credits")
            if got is not None:
                found["credits"] = got
        end = f.head.duration_ms
        return [replace(x, end_ms=end) if x.kind == "credits" or x.end_ms > end > 0 else x
                for x in (found[k] for k in KINDS if k in found)]

    async def _print(self, c: Copy, t0: int, t1: int, lang: str | None) -> Print:
        pcm, at = await sound(c.reader, c.head, max(0, t0), min(t1, c.head.duration_ms or t1), c.head.track(lang))
        return await asyncio.to_thread(fingerprint, pcm, at)       # ~0.3 s of numpy for 10 minutes: off the loop

    async def _move(self, c: Copy, f: Copy, segments: list[Segment], lang, cost: Cost) -> list[Segment]:
        """Another cut's markers on this file: where a few seconds of their sound are in it."""
        d_len = f.head.duration_ms - c.head.duration_ms
        out = []
        for s in segments:
            near = 12_000 if s.kind == "credits" else min(20_000, s.end_ms - s.start_ms)
            ref = await self._print(c, s.start_ms, s.start_ms + near, lang)
            lo, hi = min(0, d_len) - 4_000, max(0, d_len) + 4_000
            mine = await self._print(f, s.start_ms + lo, s.start_ms + near + hi, lang)
            ms = await asyncio.to_thread(shared, ref, mine, 4.0)
            if not ms:
                cost.steps.append(f"{s.kind} of {c.filename[:40]}: its sound isn't in this file")
                continue
            d = mine.ms(ms[0].b) - ref.ms(ms[0].a)
            cost.steps.append(f"{s.kind} of {c.filename[:40]} moved {d / 1000:+.2f} s by sound")
            t = _shifted(s, d, "chapters of another copy")
            out.append(replace(t, end_ms=f.head.duration_ms) if s.kind == "credits" else t)
        return out

    async def _cheapest(self, copies, video: str, f: Copy | None, others: list[Copy], cost: Cost) -> Copy | None:
        """The episode's copy that's cheapest to read (by its index's bitrate; the addons' sizes can be a season
        pack's): of the two smallest, and the file playing."""
        best = f if f is not None and f.head else None
        for filename, size, opener in [x for x in await copies(video) if f is None or (x[0], x[1]) != (f.filename, f.size)][:2]:
            c = next((o for o in others if (o.filename, o.size) == (filename, size) and o.head), None) \
                or await self._open(opener, others, cost, filename)
            if c is not None and (best is None or c.head.mbit < best.head.mbit):
                best = c
        return best

    async def _intro_by_sound(self, imdb, season, video, show, f: Copy, chapter: Segment | None, copies, neighbours,
                              lang, cost: Cost, others: list[Copy]) -> Segment | None | bool:
        """The intro checked by sound where one copy's chapters put it, else found by sound. Sure if a neighbouring
        episode's intro chapter sounds the same (the season's intro is learned from the two: a minute of sound), or
        the season's intro sound is there (a short read). False: the chapter is wrong and the sound isn't found."""
        ref = self.store.reference(imdb, season, "intro")
        if ref is None:
            if await self._learn_from_chapters(imdb, season, show, f, chapter, copies, neighbours, lang, cost,
                                               others):
                return replace(chapter, sure=True)
            ref = self.store.reference(imdb, season, "intro")
        if ref is not None and chapter is not None:
            probe = await self._print(f, chapter.start_ms - 3_000, chapter.start_ms + PROBE_MS, lang)
            ms = await asyncio.to_thread(shared, ref, probe, 4.0)
            begins = probe.ms(ms[0].b) - (ref.ms(ms[0].a) - ref.t0) if ms else None    # where ref's start is
            if begins is not None and abs(begins - chapter.start_ms) <= 6_000:
                cost.steps.append("intro: the season's sound is where the chapters say")
                return replace(chapter, sure=True)
            cost.steps.append("intro: the season's sound isn't where the chapters say")
        got = await self._by_sound(imdb, season, video, f, copies, neighbours, lang, cost, others, "intro")
        if got is not None and chapter is not None and abs(got.start_ms - chapter.start_ms) <= 6_000:
            return replace(chapter, sure=True)        # the chapter's times: a service's marks are where the theme is
        if got is None and chapter is not None and ref is not None:
            return False
        return got

    async def _learn_from_chapters(self, imdb, season, show, f: Copy, chapter: Segment | None, copies, neighbours,
                                   lang, cost: Cost, others: list[Copy]) -> bool:
        """The season's intro from intro chapters that sound the same: this file's and two neighbouring episodes'
        (a minute of sound or so). True if this file's chapter is one of them (it's right)."""
        prints: list[tuple[str, Print, float]] = []          # (episode, the chapter's sound, its length in ms)
        if chapter is not None:
            prints.append(("this episode", await self._print(f, chapter.start_ms - 2_000, chapter.end_ms + 2_000, lang),
                           chapter.end_ms - chapter.start_ms))
        for other in neighbours[:2]:
            listed = sorted(await copies(other), key=lambda x: (not CHAPTERED.search(x[0]), not WEB_DL.search(x[0]),
                                                                 -(x[1] or 0)))
            for filename, size, opener in listed[:2]:
                if (c := await self._open(opener, others, cost, filename)) is None:
                    continue
                theirs = next((x for x in from_chapters(c.head.chapters, c.head.duration_ms, show)
                               if x.kind == "intro"), None)
                if theirs is not None:
                    prints.append((other, await self._print(c, theirs.start_ms - 2_000, theirs.end_ms + 2_000, lang),
                                   theirs.end_ms - theirs.start_ms))
                    break
            for x in range(len(prints)):
                for y in range(x + 1, len(prints)):
                    (ea, a, la), (eb, b, lb) = prints[x], prints[y]
                    ms = await asyncio.to_thread(shared, a, b, MIN_INTRO_S)
                    if ms and ms[0].n * STEP_MS >= 0.5 * min(la, lb):
                        ref = a.part(a.ms(ms[0].a), a.ms(ms[0].a + ms[0].n))
                        self.store.season(imdb, season)["intro"] = {
                            "print": ref.pack(), "ms": int(len(ref) * STEP_MS), "from": [ea, eb, "chapters"],
                            "at": int(time.time())}
                        self.store.save(imdb)
                        cost.steps.append(f"intro: {ea}'s and {eb}'s intro chapters sound the same "
                                          f"({len(ref) * STEP_MS / 1000:.1f} s)")
                        return ea == "this episode"
        cost.steps.append(f"intro: {len(prints)} intro chapters, none sound the same")
        return False

    async def _by_sound(self, imdb, season, video, f: Copy, copies, neighbours, lang, cost: Cost,
                        others: list[Copy], kind: str) -> Segment | None:
        """The intro or the credits by the season's sound: found in the cheapest copy of this episode, then moved
        onto this file."""
        ref = self.store.reference(imdb, season, kind)
        c = await self._cheapest(copies, video, f, others, cost)
        if c is None:
            return None
        here, read_to = None, None
        if ref is None:
            ref, here, read_to = await self._learn(imdb, season, video, c, copies, neighbours, lang, cost, others,
                                                   kind)
            if ref is None:
                return None
        if here is None:
            here = await self._scan(c, ref, kind, lang, read_to)
        if here is None:
            cost.steps.append(f"{kind}: the season's sound isn't in this episode")
            return None
        start, end = here
        if c is not f and abs(f.head.duration_ms - c.head.duration_ms) > SAME_CUT_MS:
            d_len = f.head.duration_ms - c.head.duration_ms
            lo, hi = min(0, d_len) - 4_000, max(0, d_len) + 4_000
            mine = await self._print(f, start + lo, end + hi, lang)
            got = await asyncio.to_thread(locate, ref, mine, 0.5)
            if got is None:
                cost.steps.append(f"{kind}: found in {c.filename[:40]}, not in this file")
                return None
            start, end = got[0], got[1]
        if kind == "credits":
            end = f.head.duration_ms or end
        cost.steps.append(f"{kind} by sound {start / 1000:.1f}-{end / 1000:.1f} s")
        return Segment(kind, start, end, "sound", True, True)

    def _window(self, h: Header, kind: str) -> tuple[int, int]:
        if kind == "intro":
            return 0, min(int(h.duration_ms * INTRO_WINDOW[0]), INTRO_WINDOW[1])
        return max(0, h.duration_ms - CREDITS_WINDOW), h.duration_ms

    async def _scan(self, c: Copy, ref: Print, kind: str, lang, after: int | None = None) -> tuple[int, int] | None:
        """Where ref's sound is in the copy: its start read a part at a time (the intro is usually early), from
        `after` when what's before was read already."""
        a, b = self._window(c.head, kind)
        overlap = int(len(ref) * STEP_MS) + 5_000
        t = a if after is None else max(a, after - overlap)
        while t < b:
            p = await self._print(c, t, min(b, t + SCAN_MS + overlap), lang)
            if (got := await asyncio.to_thread(locate, ref, p, 0.5 if kind == "credits" else 0.6)) is not None:
                return got[0], got[1]
            t += SCAN_MS
        return None

    async def _learn(self, imdb, season, video, c: Copy, copies, neighbours, lang, cost: Cost, others: list[Copy],
                     kind: str) -> tuple[Print | None, tuple[int, int] | None, int | None]:
        """The season's intro (or credits) sound, from this episode and up to two neighbours: what they share. Their
        starts are read five minutes more at a time, until they share one or the window's end (a third of an
        episode, at most 15 minutes: Pluribus has a 10-minute cold open). (The sound, where it is in this episode,
        how far this episode was read.)"""
        data = self.store.season(imdb, season)
        if time.time() - data.get(f"{kind}_tried", 0) < 7 * 86400:
            return None, None, None             # looked for this week and not there (a show without an intro)
        eps = [c]
        for other in neighbours[:2]:
            if (o := await self._cheapest(copies, other, None, others, cost)) is not None:
                eps.append(o)
        if len(eps) < 2:
            return None, None, None
        windows = [self._window(e.head, kind) for e in eps]
        pcm: list[list[np.ndarray]] = [[] for _ in eps]
        begun: list[int | None] = [None] * len(eps)
        reached = [w[0] for w in windows]
        got, prints = None, []
        while True:
            step = LEARN_STEP if kind == "intro" else CREDITS_WINDOW
            more = False
            for k, e in enumerate(eps):
                to = min(windows[k][1], reached[k] + step)
                if to <= reached[k]:
                    continue
                part, at = await sound(e.reader, e.head, reached[k], to, e.head.track(lang))
                if begun[k] is None:
                    begun[k] = at
                pcm[k].append(part)
                reached[k], more = to, True
            if not more:
                break
            prints = await asyncio.to_thread(lambda: [fingerprint(np.concatenate(x), b) for x, b in zip(pcm, begun)])
            if kind == "intro":
                # all of them, else all but one (Community's bottle episode has none; Pluribus S1E4's comes at 10
                # minutes, so two that share it needn't wait for the third)
                got = await asyncio.to_thread(season_intro, prints)
                if got is None and len(eps) > 2:
                    got = await asyncio.to_thread(season_intro, prints, len(eps) - 1)
            else:
                got = await asyncio.to_thread(season_credits, prints, [e.head.duration_ms for e in eps])
            if got is not None:
                break
        if got is None:
            data[f"{kind}_tried"] = int(time.time())
            self.store.save(imdb)
            cost.steps.append(f"{kind}: {len(eps)} episodes share no {kind} sound")
            return None, None, None
        ref, _ = got
        data[kind] = {"print": ref.pack(), "ms": int(len(ref) * STEP_MS), "from": [video, *neighbours[:2]],
                      "at": int(time.time())}
        self.store.save(imdb)
        cost.steps.append(f"{kind}: learned from {len(eps)} episodes ({len(ref) * STEP_MS / 1000:.1f} s)")
        here = locate(ref, prints[0], 0.5 if kind == "credits" else 0.6)
        return ref, ((here[0], here[1]) if here else None), reached[0]
