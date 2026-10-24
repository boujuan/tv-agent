"""Preview pictures for TV Agent Player's held ←/→ (its PROTOCOL.md, `thumb_req` and `thumb`), made on this computer:
the TV decodes one stream above 1080p at a time, so it can't decode a frame of the film beside the film.

A picture is the keyframe at or before the asked time. The file's header and its seek index (a Matroska file's Cues:
every keyframe's place in the file) are read once per item; after that, each picture is one range read of just that
keyframe, on a connection kept open, handed to ffmpeg as a one-frame Matroska file on a pipe. ffmpeg decodes it,
scales it to the asked width, tone-maps HDR and Dolby Vision to SDR (libplacebo, which also applies a profile 5
file's reshaping; zscale and tonemap if libplacebo can't run) and writes a small JPEG. Pictures are kept per keyframe
for the item playing.

Only Matroska files have previews (debrid files nearly all are). The link travels only from here to its server: never
on ffmpeg's command line, in a log or in an error.
"""
import asyncio
import bisect
import logging
import resource
import time
from collections import OrderedDict

import httpx

from ..config import DATA_DIR

log = logging.getLogger(__name__)

HEAD = 256 << 10            # the first read: the header, its seek table, Info and Tracks are nearly always in it
CUES = 256 << 10            # the index's first read (a 3-hour film's is ~100 KB)
BLOCK = 512 << 10           # a keyframe's first read; the rest follows if it's bigger (a REMUX's can be 3 MB)
KEEP = 400                  # pictures kept for the item playing (~5 KB each)
FFMPEG_S = 6.0
# The index is read this long after a file opens, so a hold's first picture doesn't wait for it (measured on the TV:
# 2.6 s for the first, ~0.45 s after); not at once, while the TV reads the same file to start it
INDEX_AFTER_S = 10.0
# libplacebo's compiled shaders, kept between ffmpeg runs: a picture then takes ~0.3 s instead of ~0.7 s
SHADERS = DATA_DIR / "shaders"

EBML, SEGMENT, SEEKHEAD, SEEK, SEEK_ID, SEEK_POS = 0x1A45DFA3, 0x18538067, 0x114D9B74, 0x4DBB, 0x53AB, 0x53AC
INFO, TIMESCALE, TRACKS, ENTRY, NUMBER, TYPE = 0x1549A966, 0x2AD7B1, 0x1654AE6B, 0xAE, 0xD7, 0x83
VIDEO, COLOUR, TRANSFER, MAPPING, ADD_TYPE, ADD_DATA = 0xE0, 0x55B0, 0x55BA, 0x41E4, 0x41E7, 0x41ED
CUES_ID, CUE_POINT, CUE_TIME, CUE_POS, CUE_TRACK, CUE_CLUSTER, CUE_REL = 0x1C53BB6B, 0xBB, 0xB3, 0xB7, 0xF7, 0xF1, 0xF0
CLUSTER, TIMESTAMP, SIMPLE, GROUP, BLOCK_ID = 0x1F43B675, 0xE7, 0xA3, 0xA0, 0xA1
DOVI = (0x64766343, 0x64767643)         # BlockAddIDType 'dvcC' / 'dvvC': a Dolby Vision configuration record
LANGUAGE, LANGUAGE_BCP47, AUDIO_TRACK, TEXT_TRACK = 0x22B59C, 0x22B59D, 2, 17
UNKNOWN = b"\x01\xff\xff\xff\xff\xff\xff\xff"


class ThumbError(Exception):
    """Why there's no picture, as the player is told (never with the link)."""


class ReadError(ThumbError):
    """The file couldn't be read this time (it may work the next)."""


class Short(Exception):
    """The bytes read so far end inside the element."""


def vint(b: bytes, i: int, marker: bool = False) -> tuple[int, int]:
    """The EBML variable-length integer at b[i]: (value, its length). An element ID keeps its marker bit."""
    if i >= len(b):
        raise Short
    n = 9 - b[i].bit_length()
    if n > 8:
        raise ValueError("not EBML")
    if i + n > len(b):
        raise Short
    v = b[i] if marker else b[i] & (0xFF >> n)
    for x in b[i + 1:i + n]:
        v = v << 8 | x
    if not marker and v == (1 << 7 * n) - 1:
        v = -1                                    # unknown size
    return v, n


def element(b: bytes, i: int) -> tuple[int, int, int]:
    """(ID, where its data starts, where it ends: -1 for an unknown size) of the element at b[i]."""
    eid, a = vint(b, i, marker=True)
    size, c = vint(b, i + a)
    start = i + a + c
    return eid, start, -1 if size < 0 else start + size


def children(b: bytes, start: int, end: int):
    """(ID, data start, data end, element start) of each element in b[start:end], all inside it."""
    i = start
    while i < end:
        eid, a, z = element(b, i)
        if z < 0 or z > end:
            raise ValueError("element runs past its parent")
        yield eid, a, z, i
        i = z


def uint(b: bytes) -> int:
    return int.from_bytes(b, "big")


def size_vint(n: int) -> bytes:
    """n as an 8-byte EBML size (always long enough)."""
    return bytes([0x01]) + n.to_bytes(7, "big")


class Mkv:
    """What a keyframe of a Matroska file needs, read once: the header to put before it (EBML header, Info, and
    Tracks with only the video track) and the video track's cue points."""

    def __init__(self):
        self.prefix = b""                      # replayed before each keyframe
        self.track = 0                         # the video track's number
        self.codec = ""
        self.dv = False                        # a Dolby Vision configuration record
        self.transfer = 0                      # Matroska's TransferCharacteristics (16 PQ, 18 HLG; 0 not said)
        self.scale = 1_000_000                 # ns per Matroska timestamp unit
        self.segment = 0                       # where the Segment's data starts (cue positions count from there)
        self.times: list[int] = []             # cue times, in ms
        self.places: list[tuple[int, int | None, int]] = []     # (cluster position, the block's place in it, time)

    @classmethod
    async def read(cls, fetch) -> "Mkv":
        """`fetch(start, length)` reads the file's bytes."""
        m = cls()
        head = await fetch(0, HEAD)
        try:
            eid, _, ebml_end = element(head, 0)
            if eid != EBML:
                raise ThumbError("not a Matroska file")
            eid, m.segment, _ = element(head, ebml_end)
            if eid != SEGMENT:
                raise ThumbError("not a Matroska file")
        except (Short, ValueError):
            raise ThumbError("not a Matroska file") from None
        found: dict[int, bytes] = {}
        seeks: dict[int, int] = {}
        i = m.segment
        try:
            while i < len(head):                   # the top level up to the first cluster
                eid, a, z = element(head, i)
                if eid == CLUSTER or z < 0 or z > len(head):
                    break
                if eid in (INFO, TRACKS):
                    found[eid] = head[i:z]
                elif eid == SEEKHEAD:
                    seeks |= m._seeks(head, a, z)
                i = z
        except (Short, ValueError):
            pass
        for eid in (INFO, TRACKS, CUES_ID):    # what wasn't in the head, where the seek table says
            if eid not in found and eid in seeks:
                found[eid] = await m._whole(fetch, m.segment + seeks[eid], CUES if eid == CUES_ID else 64 << 10)
        if TRACKS not in found or CUES_ID not in found:
            raise ThumbError("no seek index in the file")
        try:
            m._info(found.get(INFO, b""))
            m._tracks(found[TRACKS])
            m._cues(found[CUES_ID])
        except (Short, ValueError):
            raise ThumbError("a Matroska header I can't read") from None
        m.prefix = head[:ebml_end + 4] + UNKNOWN + found.get(INFO, b"") + m.prefix
        if not m.times:
            raise ThumbError("no seek index in the file")
        return m

    @staticmethod
    def _seeks(b: bytes, a: int, z: int) -> dict[int, int]:
        out = {}
        for eid, x, y, _ in children(b, a, z):
            if eid == SEEK:
                fields = {k: b[p:q] for k, p, q, _ in children(b, x, y)}
                if SEEK_ID in fields and SEEK_POS in fields:
                    out.setdefault(uint(fields[SEEK_ID]), uint(fields[SEEK_POS]))
        return out

    @staticmethod
    async def _whole(fetch, pos: int, first: int) -> bytes:
        """The whole element at pos, read in one go when `first` bytes hold it."""
        b = await fetch(pos, first)
        try:
            _, _, z = element(b, 0)
        except (Short, ValueError):
            raise ThumbError("a Matroska header I can't read") from None
        if z < 0:
            raise ThumbError("an index of unknown size")
        if z > len(b):
            b += await fetch(pos + len(b), z - len(b))
        return b[:z]

    def _info(self, info: bytes):
        if info:
            _, a, z = element(info, 0)
            for eid, x, y, _ in children(info, a, z):
                if eid == TIMESCALE:
                    self.scale = uint(info[x:y]) or self.scale

    def _tracks(self, tracks: bytes):
        _, a, z = element(tracks, 0)
        for eid, x, y, start in children(tracks, a, z):
            if eid != ENTRY:
                continue
            fields = {k: (p, q) for k, p, q, _ in children(tracks, x, y)}
            if TYPE not in fields or uint(tracks[slice(*fields[TYPE])]) != 1:
                continue
            self.track = uint(tracks[slice(*fields[NUMBER])])
            self.codec = tracks[slice(*fields[0x86])].decode(errors="replace") if 0x86 in fields else ""
            for k, p, q, _ in children(tracks, x, y):
                if k == MAPPING:
                    kinds = {kk: tracks[pp:qq] for kk, pp, qq, _ in children(tracks, p, q)}
                    self.dv |= uint(kinds.get(ADD_TYPE, b"")) in DOVI
                elif k == VIDEO:
                    for kk, pp, qq, _ in children(tracks, p, q):
                        if kk == COLOUR:
                            for c, s, e, _ in children(tracks, pp, qq):
                                if c == TRANSFER:
                                    self.transfer = uint(tracks[s:e])
            entry = tracks[start:y]
            self.prefix = tracks[:4] + size_vint(len(entry)) + entry
            return
        raise ThumbError("no video track")

    def _cues(self, cues: bytes):
        _, a, z = element(cues, 0)
        to_ms = self.scale / 1_000_000
        points = []
        for eid, x, y, _ in children(cues, a, z):
            if eid != CUE_POINT:
                continue
            t = None
            for k, p, q, _ in children(cues, x, y):
                if k == CUE_TIME:
                    t = uint(cues[p:q])
                elif k == CUE_POS and t is not None:
                    pos = {kk: uint(cues[pp:qq]) for kk, pp, qq, _ in children(cues, p, q)}
                    if pos.get(CUE_TRACK) == self.track and CUE_CLUSTER in pos:
                        points.append((t, pos[CUE_CLUSTER], pos.get(CUE_REL)))
                        break
        points.sort()
        self.times = [int(t * to_ms) for t, _, _ in points]
        self.places = [(c, r, t) for t, c, r in points]

    def cue(self, ms: int) -> int:
        """The index of the cue point at or before ms (the first one before the first)."""
        return max(0, bisect.bisect_right(self.times, ms) - 1)

    async def keyframe(self, fetch, i: int) -> bytes:
        """Cue point i's keyframe as a Matroska file of its own: the header, then a cluster with only that block."""
        cluster, rel, tick = self.places[i]
        pos = self.segment + cluster
        buf = bytearray(await fetch(pos, 12 + (rel or 0) + BLOCK))

        async def need(n: int):
            if n > len(buf):
                buf.extend(await fetch(pos + len(buf), max(n - len(buf), 64 << 10)))
                if n > len(buf):
                    raise ThumbError("the file ends early")

        try:
            eid, data, end = element(buf, 0)
        except Short:
            raise ThumbError("the file ends early") from None
        if eid != CLUSTER:
            raise ThumbError("the index points outside a cluster")
        stamp, j = 0, data + (rel or 0)
        while end < 0 or j < end:
            await need(j + 12)
            eid, a, z = element(buf, j)
            if eid == CLUSTER or z < 0:
                break
            if eid in (SIMPLE, GROUP):
                await need(z)
                if self._ours(buf, eid, a, z, None if rel is not None else tick - stamp):
                    return self.prefix + b"\x1f\x43\xb6\x75" + UNKNOWN + b"\xe7\x81\x00" + bytes(buf[j:z])
            elif eid == TIMESTAMP:
                await need(z)
                stamp = uint(buf[a:z])
            j = z
        raise ThumbError("no keyframe where the index says")

    def _ours(self, b, eid: int, a: int, z: int, at: int | None) -> bool:
        """Whether the block is the video track's keyframe (at `at` in the cluster's time, when that's known)."""
        if eid == GROUP:
            inner = next(((x, y) for k, x, y, _ in children(b, a, z) if k == BLOCK_ID), None)
            if inner is None:
                return False
            a = inner[0]
        track, n = vint(b, a)
        when = int.from_bytes(b[a + n:a + n + 2], "big", signed=True)
        key = eid == GROUP or bool(b[a + n + 2] & 0x80)
        return track == self.track and key and (at is None or when == at)


async def track_languages(fetch) -> list[tuple[int, str]]:
    """(type, language) of each audio (AUDIO_TRACK) and subtitle (TEXT_TRACK) track of a Matroska file, from its
    header, as the file names it ("spa", "es-ES"; Matroska's "eng" when it names none). `fetch(start, length)` reads
    the file's bytes; one read of its start nearly always holds them."""
    head = await fetch(0, HEAD)
    tracks, seeks = None, {}
    try:
        eid, _, ebml_end = element(head, 0)
        eid2, segment, _ = element(head, ebml_end)
        if eid != EBML or eid2 != SEGMENT:
            raise ThumbError("not a Matroska file")
    except (Short, ValueError):
        raise ThumbError("not a Matroska file") from None
    i = segment
    try:
        while i < len(head):
            eid, a, z = element(head, i)
            if eid == CLUSTER or z < 0 or z > len(head):
                break
            if eid == TRACKS:
                tracks = head[i:z]
                break
            if eid == SEEKHEAD:
                seeks |= Mkv._seeks(head, a, z)
            i = z
    except (Short, ValueError):
        pass
    if tracks is None:
        if TRACKS not in seeks:
            raise ThumbError("no tracks in the header")
        tracks = await Mkv._whole(fetch, segment + seeks[TRACKS], 64 << 10)
    out = []
    try:
        _, a, z = element(tracks, 0)
        for eid, x, y, _ in children(tracks, a, z):
            if eid != ENTRY:
                continue
            fields = {k: tracks[p:q] for k, p, q, _ in children(tracks, x, y)}
            kind = uint(fields.get(TYPE, b""))
            if kind in (AUDIO_TRACK, TEXT_TRACK):
                lang = fields.get(LANGUAGE_BCP47) or fields.get(LANGUAGE) or b"eng"
                out.append((kind, lang.decode(errors="replace").strip("\x00 ")))
    except (Short, ValueError):
        raise ThumbError("a Matroska header I can't read") from None
    return out


def chain(m: Mkv, w: int, placebo: bool = True) -> str:
    """ffmpeg's filters: scaled to w wide, in SDR BT.709 full range for a JPEG. libplacebo for Dolby Vision and
    HDR, and for HEVC that doesn't say (its own header may: libplacebo goes by the frame's); zscale and tonemap
    when libplacebo can't run (a profile 5 file's colours are then wrong)."""
    hdr = m.dv or m.transfer in (16, 18)
    if placebo and (hdr or m.transfer == 0 and "HEVC" in m.codec):
        cache = f":shader_cache={SHADERS}/" if SHADERS.is_dir() and not set(str(SHADERS)) & set(":,;'\\[]=") else ""
        return (f"libplacebo=w={w}:h=-2:format=yuv420p:colorspace=bt709:color_primaries=bt709:color_trc=bt709"
                f":range=pc:peak_detect=false{cache}")
    if hdr:
        return (f"zscale=w={w}:h=-2:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=pc,format=yuvj420p")
    return f"scale={w}:-2,format=yuvj420p"


async def jpeg(mkv: bytes, filters: str, quality: int = 5) -> bytes:
    """ffmpeg: the one-frame file in on a pipe, a JPEG out."""
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-thread_type", "slice", "-f", "matroska",
        "-i", "pipe:0", "-map", "0:v:0", "-frames:v", "1", "-vf", filters, "-f", "image2pipe", "-c:v", "mjpeg",
        "-q:v", str(quality), "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(mkv), FFMPEG_S)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        raise ThumbError("ffmpeg took too long") from None
    if p.returncode or out[:2] != b"\xff\xd8":
        lines = err.decode(errors="replace").strip().splitlines()
        raise ThumbError(f"ffmpeg: {lines[-1][:160] if lines else p.returncode}")
    return out


class Thumbs:
    """The pictures for one player: the item playing, its file's index once read, the pictures made (by keyframe).
    One picture is made at a time; a request that waited while a newer one came is dropped."""

    def __init__(self):
        self._http: httpx.AsyncClient | None = None
        self._item: str | None = None
        self._link: tuple[str, dict] | None = None
        self._mkv: asyncio.Task | None = None
        self._soon: asyncio.Task | None = None
        self._pics: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self._lock = asyncio.Lock()
        self._latest = 0
        self._placebo = True                     # off once libplacebo has failed (no Vulkan device, say)
        self.made: list[tuple[float, float]] = []    # (ms, ffmpeg CPU ms) of each picture made for this item

    def follow(self, item: str, url: str, headers: dict | None):
        """What the player plays now: its pictures and index from here on."""
        if item != self._item:
            for task in (self._mkv, self._soon):
                if task:
                    task.cancel()
            self._item, self._link, self._mkv, self._pics, self.made = item, (url, dict(headers or {})), None, \
                OrderedDict(), []
            self._soon = asyncio.create_task(self._index_soon(item))

    async def _index_soon(self, item: str):
        await asyncio.sleep(INDEX_AFTER_S)
        try:
            await self._index(item)
        except ThumbError as e:
            log.info("previews for %s: %s", item, e)

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _fetch(self, start: int, length: int) -> bytes:
        url, headers = self._link
        if self._http is None:
            self._http = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(6.0, connect=4.0),
                                           limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=60))
        try:
            r = await self._http.get(url, headers={**headers, "Range": f"bytes={start}-{start + length - 1}"})
        except httpx.HTTPError as e:
            raise ReadError(f"reading the file: {type(e).__name__}") from None
        if r.status_code != 206:
            raise ReadError(f"reading the file: HTTP {r.status_code}")
        return r.content

    async def _index(self, item: str) -> Mkv:
        if self._mkv is None:
            SHADERS.mkdir(parents=True, exist_ok=True)

            async def read():
                t0 = time.perf_counter()
                m = await Mkv.read(self._fetch)
                log.info("previews for %s: %s%s, %d keyframes indexed, read in %.0f ms", item, m.codec,
                         " DV" if m.dv else {16: " PQ", 18: " HLG"}.get(m.transfer, ""), len(m.times),
                         (time.perf_counter() - t0) * 1000)
                return m
            self._mkv = asyncio.create_task(read())
        task = self._mkv
        try:
            return await asyncio.shield(task)
        except ReadError:
            if self._mkv is task:
                self._mkv = None                 # tried again at the next request
            raise
        except asyncio.CancelledError:
            raise ThumbError("another file is playing") from None

    async def picture(self, item: str, ms: int, w: int = 320) -> tuple[int, bytes] | None:
        """(the keyframe's time in ms, a JPEG w px wide) for the keyframe at or before ms of `item`; None when a
        newer request came while this one waited."""
        if item != self._item or self._link is None:
            raise ThumbError("not the file playing")
        w = max(64, min(int(w) // 2 * 2, 640))
        self._latest = ask = self._latest + 1
        async with self._lock:
            if ask != self._latest:
                return None
            t0 = time.perf_counter()
            m = await self._index(item)
            i = m.cue(ms)
            if (i, w) in self._pics:
                self._pics.move_to_end((i, w))
                return m.times[i], self._pics[(i, w)]
            one = await m.keyframe(self._fetch, i)
            t_read = time.perf_counter()
            cpu0 = _children_cpu()
            try:
                pic = await jpeg(one, chain(m, w, self._placebo))
            except ThumbError as e:
                if isinstance(e, ReadError) or not (self._placebo and "libplacebo" in chain(m, w)):
                    raise
                log.warning("previews: libplacebo failed (%s); zscale from now on", e)
                self._placebo = False
                pic = await jpeg(one, chain(m, w, False))
            cpu = (_children_cpu() - cpu0) * 1000
            took = (time.perf_counter() - t0) * 1000
            self.made.append((took, cpu))
            log.debug("preview at %d ms: keyframe %d ms, %d KB read in %.0f ms, picture %.0f ms (ffmpeg CPU %.0f ms), "
                      "%d B", ms, m.times[i], len(one) >> 10, (t_read - t0) * 1000, took, cpu, len(pic))
            if item == self._item:
                self._pics[(i, w)] = pic
                while len(self._pics) > KEEP:
                    self._pics.popitem(last=False)
            return m.times[i], pic


def _children_cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime
