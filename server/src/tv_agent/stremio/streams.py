"""Ask your Stremio addons for a title's streams and pick the best one the TV can start right away.

The ranking is plain code, not the model. Hard limits drop a stream: not a direct debrid link (torrents need Stremio
itself), not cached on the debrid service, a cam rip, above max_height, or a bitrate the TV's 100 Mbit/s port can't
carry. The rest are ordered by, in turn:

1. language: the audio the title should play in (languages.plan) is in the file, as AIOStreams read it from the
   file's own tracks or name, or as a name, description or filename says (flags, "Castellano", "Multi")
2. resolution, up to max_height
3. AIOStreams' score for the release (the ★ in its names: release group tier, source, HDR, audio…)
4. already in your debrid library, then the higher bitrate

AIOStreams describes each stream in machine-readable `streamData` when asked by an AIOStreams-compatible client
(the User-Agent below); other addons are read from their names and descriptions, keeping their own order.
"""
import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .. import __version__, tracing
from ..languages import code as language_code
from ..tv import thumbs
from .account import Addon

log = logging.getLogger(__name__)

# AIOStreams adds streamData for clients whose User-Agent starts with "AIO" (older versions: contains "AIOStreams/").
USER_AGENT = f"AIOStreams/compatible (tv-agent/{__version__})"
HEIGHT = [(re.compile(r"\b(2160p|4k|uhd)\b", re.I), 2160), (re.compile(r"\b1440p\b", re.I), 1440),
          (re.compile(r"\b1080p\b", re.I), 1080), (re.compile(r"\b720p\b", re.I), 720),
          (re.compile(r"\b(480p|576p|sd)\b", re.I), 480)]
CAM = re.compile(r"\b(cam|camrip|hdcam|ts|telesync|hdts|tc|telecine|scr|screener)\b", re.I)
CACHED = re.compile(r"⚡|\[(RD|AD|PM|TB|DL|ED|OC|PKP|EN|DB)\+\]|\binstant\b|(?<!un)\bcached\b", re.I)
UNCACHED = re.compile(r"⏳|\buncached\b|\bnot cached\b|\[(RD|AD|PM|TB|DL|ED|OC|PKP|EN|DB) download\]", re.I)
SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(TB|GB|GiB|MB|MiB)\b", re.I)
# Dubbed-only releases (one language, not the original): marked down unless the title is in that language.
DUB_ONLY = re.compile(r"\b(truefrench|french|vff|vfq|vf2|castellano|spanish|latino|italian|ita|german)\b", re.I)
MULTI = re.compile(r"\b(multi|dual)\b", re.I)
DOLBY_VISION = re.compile(r"\b(dv|dovi|dolby[ ._-]?vision)\b", re.I)
HDR10_PLUS = re.compile(r"hdr10(\+|plus|p\b)", re.I)
DV_REMUX = re.compile(r"(\bdv\b|dolby ?vision|\bdovi\b).*\bremux\b|\bremux\b.*(\bdv\b|dolby ?vision|\bdovi\b)", re.I)
UNIT = {"tb": 1e12, "gb": 1e9, "gib": 2**30, "mb": 1e6, "mib": 2**20}
CAM_QUALITIES = {"cam", "ts", "tc", "scr"}
# AIOStreams sends these when a link can't play (uncached, failed, 429…): a short video explaining it. Newer ones
# send /status/video/error.mp4 (seen 2026-10-24 after many links were opened in a few minutes).
PROBLEM_VIDEO = re.compile(r"/(?:static|status/video)/[\w-]+\.mp4$")
# What the pause screen shows that the file's header can't tell TV Agent Player (its PROTOCOL.md, `info`)
BADGES = [("atmos", re.compile(r"\batmos\b", re.I)), ("dtsx", re.compile(r"\bdts[ :._-]?x\b", re.I)),
          ("hdr10+", HDR10_PLUS), ("imax", re.compile(r"\bimax\b", re.I)), ("remux", re.compile(r"\bremux\b", re.I))]
LINK = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
GROUP = re.compile(r"-([A-Za-z0-9]{2,20})(?:\.(?:mkv|mp4|avi|ts|m2ts))?$")
# Debrid services by AIOStreams' id (streamData.service.id) and by the tag other addons put in a stream's name
DEBRID = {"realdebrid": "Real-Debrid", "alldebrid": "AllDebrid", "premiumize": "Premiumize", "torbox": "TorBox",
          "debridlink": "Debrid-Link", "easydebrid": "EasyDebrid", "offcloud": "Offcloud", "pikpak": "PikPak",
          "putio": "put.io", "RD": "Real-Debrid", "AD": "AllDebrid", "PM": "Premiumize", "TB": "TorBox",
          "DL": "Debrid-Link", "ED": "EasyDebrid", "OC": "Offcloud", "PKP": "PikPak"}
DEBRID_TAG = re.compile(r"\[(RD|AD|PM|TB|DL|ED|OC|PKP)[+\] ]")
# Languages a stream's text names, for addons without streamData: flags (Torrentio shows the non-English ones) and
# words in the filename ("Castellano", "SPA-ENG", "iTA")
FLAGS = {"🇬🇧": "English", "🇺🇸": "English", "🇪🇸": "Spanish", "🇲🇽": "Latino", "🇫🇷": "French", "🇮🇹": "Italian",
         "🇩🇪": "German", "🇵🇹": "Portuguese", "🇧🇷": "Portuguese", "🇯🇵": "Japanese", "🇰🇷": "Korean",
         "🇨🇳": "Chinese", "🇹🇼": "Chinese", "🇷🇺": "Russian"}
LANGUAGE_WORDS = [(re.compile(rx, re.I), name) for rx, name in (
    (r"\b(?:english|eng)\b", "English"), (r"\b(?:castellano|spanish|espa[nñ]ol|spa|esp)\b", "Spanish"),
    (r"\blatino\b", "Latino"), (r"\b(?:truefrench|french|vff|vfq|vf2|fre|fra)\b", "French"),
    (r"\b(?:italian|ita)\b", "Italian"), (r"\b(?:german|ger|deu)\b", "German"), (r"\b(?:japanese|jpn)\b", "Japanese"),
    (r"\b(?:korean|kor)\b", "Korean"), (r"\b(?:multi|dual)\b", "Multi"))]


@dataclass
class Stream:
    addon: str
    url: str = field(repr=False)     # secret: debrid links carry account keys
    name: str
    description: str
    filename: str
    order: int
    video_hash: str = ""            # OpenSubtitles hash, when the addon knows it (subtitles match on it)
    height: int = 0
    size: int = 0
    duration_ms: int = 0             # from AIOStreams; else the title's runtime is used for the bitrate
    cached: bool | None = None
    library: bool = False            # already in your debrid account's library
    quality: str = ""                # "WEB-DL", "BluRay REMUX"… (AIOStreams)
    visual: tuple[str, ...] = ()     # "DV", "HDR10"…
    audio: tuple[str, ...] = ()      # "DD+", "Atmos"…
    languages: tuple[str, ...] = ()  # audio languages by name: "English", "Spanish", "Latino", "Multi"…
    subtitles: tuple[str, ...] = ()  # subtitle languages by name, when the addon says
    tracks_read: bool = False        # languages and subtitles are the file's own tracks (AIOStreams read them)
    original: str = ""               # the audio track the file marks as the original language, by name
    group: str = ""                  # release group
    source: str = ""                 # the addon AIOStreams found it with ("Torrentio"); else the addon itself
    service: str = ""                # the debrid service's id in AIOStreams ("realdebrid")
    aio_score: int | None = None     # AIOStreams' ranked-expression score (its ★)
    matched: tuple[str, ...] = ()    # AIOStreams' ranked expressions that matched ("Web T1", "4K", "DV"…)
    headers: dict = field(default_factory=dict, repr=False)
    score: float = 0.0
    why_dropped: str = ""
    rank_key: tuple = field(default=(), repr=False)

    @property
    def dolby_vision(self) -> bool:
        return "DV" in self.visual or bool(DOLBY_VISION.search(self.filename or self.name))

    @property
    def hdr10_plus(self) -> bool:
        return "HDR10+" in self.visual or bool(HDR10_PLUS.search(self.filename or self.name))

    def mbps(self, runtime_min: int | None) -> float | None:
        seconds = self.duration_ms / 1000 if self.duration_ms else (runtime_min or 0) * 60
        return self.size * 8 / seconds / 1e6 if self.size and seconds else None


def _height(resolution: str) -> int:
    m = re.match(r"(\d{3,4})p", resolution or "")
    return int(m.group(1)) if m else (2160 if (resolution or "").lower() in ("4k", "uhd") else 0)


def parse(addon: str, raw: dict, order: int) -> Stream | None:
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None   # torrents (infoHash), YouTube ids, external links and AIOStreams' error entries need Stremio
    hints = raw.get("behaviorHints") or {}
    s = Stream(addon=addon, url=url, name=str(raw.get("name") or ""),
               description=str(raw.get("description") or raw.get("title") or ""),
               filename=str(hints.get("filename") or ""), order=order, video_hash=str(hints.get("videoHash") or ""),
               headers=((hints.get("proxyHeaders") or {}).get("request") or {}))
    data = raw.get("streamData")
    if isinstance(data, dict) and data.get("type") not in ("error", "statistic"):
        pf = data.get("parsedFile") or {}
        s.height = _height(str(pf.get("resolution") or ""))
        s.size = int(data.get("size") or hints.get("videoSize") or 0)
        s.duration_ms = int(data.get("duration") or 0)
        service = data.get("service") or {}
        s.cached = service.get("cached") if isinstance(service.get("cached"), bool) else None
        s.service = str(service.get("id") or "")
        s.source = str(data.get("addon") or "")
        s.library = bool(data.get("library"))
        s.quality = str(pf.get("quality") or "")
        s.visual = tuple(pf.get("visualTags") or ())
        s.audio = tuple(pf.get("audioTags") or ())
        s.languages = tuple(pf.get("languages") or ())
        s.subtitles = tuple(pf.get("subtitles") or ())
        tracks = [t for t in pf.get("audioTracks") or [] if isinstance(t, dict)]
        if tracks and all(t.get("lang") for t in tracks):
            s.languages = tuple(dict.fromkeys(str(t["lang"]) for t in tracks))
            s.tracks_read = pf.get("mediaInfoQuality") == "probe" or bool(pf.get("subtitleTracks"))
        originals = {str(t.get("lang")) for t in tracks if t.get("original")}
        s.original = originals.pop() if len(originals) == 1 else ""
        s.group = str(pf.get("releaseGroup") or "")
        if isinstance(data.get("streamExpressionScore"), (int, float)):
            s.aio_score = int(data["streamExpressionScore"])
        s.matched = tuple(data.get("rankedStreamExpressionsMatched") or ())
        s.filename = s.filename or str(data.get("filename") or "")
        return s
    text = f"{s.name}\n{s.description}\n{s.filename}"
    s.height = next((h for rx, h in HEIGHT if rx.search(text)), 0)
    if isinstance(hints.get("videoSize"), int):
        s.size = hints["videoSize"]
    elif m := SIZE.search(text):
        s.size = int(float(m.group(1).replace(",", ".")) * UNIT[m.group(2).lower()])
    s.cached = False if UNCACHED.search(text) else (True if CACHED.search(text) else None)
    s.languages = tuple(dict.fromkeys([*(name for flag, name in FLAGS.items() if flag in text),
                                       *(name for rx, name in LANGUAGE_WORDS if rx.search(s.filename))]))
    return s


def badges(s: Stream) -> list[str]:
    """atmos, dtsx, hdr10+, imax, remux: from AIOStreams' tags and the stream's name, description and filename."""
    text = " ".join([s.name, s.description, s.filename, s.quality, *s.visual, *s.audio])
    return [b for b, rx in BADGES if rx.search(text)]


def description(s: Stream) -> str:
    """The addon's text about the stream as the pause screen shows it: links (they can carry an account key) and
    invisible characters removed, blank lines dropped."""
    text = LINK.sub("", s.description).replace("\u200d", "").replace("\u200b", "")
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())[:600]


def debrid(s: Stream) -> str:
    """The debrid service the link plays from ("Real-Debrid"), if the addon says."""
    if s.service:
        return DEBRID.get(s.service, s.service.title())
    m = DEBRID_TAG.search(f"{s.name}\n{s.description}")
    return DEBRID[m.group(1)] if m else ""


def release(s: Stream) -> str:
    """The release group: AIOStreams' parse, else the filename's "-GROUP" ending."""
    m = GROUP.search(s.filename)
    return s.group or (m.group(1) if m else "")


def has_audio(s: Stream, lang: str) -> bool | None:
    """Whether the file has audio in this language: True, False, or None when nothing says. Latin American Spanish
    counts as Spanish. A filename's dub marker without "multi" or "dual" is the only language."""
    names = {x.lower() for x in s.languages}
    if any(language_code(x) == lang for x in names):
        return True
    if names & {"multi", "dual audio"}:
        return None
    if s.languages and (s.service or s.tracks_read):      # AIOStreams named them all
        return False
    if DUB_ONLY.search(s.filename) and not MULTI.search(s.filename):
        return False
    return None


def has_subtitles(s: Stream, lang: str) -> bool | None:
    """Whether the file has subtitles in this language, when the addon says which it has."""
    if not s.subtitles:
        return None
    return any(language_code(x) == language_code(lang) for x in s.subtitles)


def _language_fit(s: Stream, want: str | None) -> float:
    """2: the wanted audio language is in the file; 1.5: only Latin American Spanish for Spanish; 1: unknown or a
    multi-language release; 0: dubbed without it."""
    if not want:
        return 1
    has = has_audio(s, want)
    if has and want == "spa" and {x.lower() for x in s.languages} & {"latino", "latin american"} and not any(
            x.lower() in ("spanish", "castellano", "español", "spa") for x in s.languages):
        return 1.5
    return 2 if has else 1 if has is None else 0


def original_language(found: list[Stream]) -> str | None:
    """The language the files mark as the original audio, when most of those that mark one agree."""
    votes = Counter(c for s in found if s.original and (c := language_code(s.original)))
    if not votes:
        return None
    lang, n = votes.most_common(1)[0]
    return lang if n * 2 > sum(votes.values()) else None


async def probe(http: httpx.AsyncClient, link: str, headers: dict | None = None) -> tuple[set[str], set[str]] | None:
    """(audio languages, subtitle languages) of the file at link (where its addon link leads), as codes, from its
    Matroska header: one range read; None when it can't be read (another container, an error)."""
    async def fetch(start: int, length: int) -> bytes:
        r = await http.get(link, headers={**(headers or {}), "Range": f"bytes={start}-{start + length - 1}"}, timeout=8)
        if r.status_code != 206:
            raise thumbs.ReadError(f"HTTP {r.status_code}")
        return r.content
    try:
        found = await thumbs.track_languages(fetch)
    except (thumbs.ThumbError, httpx.HTTPError) as e:
        log.info("tracks of a file: %s", e if isinstance(e, thumbs.ThumbError) else type(e).__name__)
        return None
    langs = lambda kind: {c for k, x in found if k == kind and (c := language_code(x.split("-")[0]))}   # noqa: E731
    return langs(thumbs.AUDIO_TRACK), langs(thumbs.TEXT_TRACK)


def rank(streams: list[Stream], max_height: int, max_mbps: float, runtime_min: int | None,
         audio: str | None = "eng") -> list[Stream]:
    """Best first (see the module docstring). Streams that break a hard limit get why_dropped set and sink to the
    end. audio is the language the title should play in (None: no preference)."""
    want = audio
    for s in streams:
        text = f"{s.name}\n{s.description}\n{s.filename}"
        rate = s.mbps(runtime_min)
        if s.cached is False:
            s.why_dropped = "not cached"
        elif s.quality.lower() in CAM_QUALITIES or (not s.quality and CAM.search(text)):
            s.why_dropped = "cam/ts"
        elif s.height > max_height:
            s.why_dropped = f"{s.height}p > {max_height}p"
        elif max_mbps and rate and rate > max_mbps:     # 0: no limit
            s.why_dropped = f"{rate:.0f} Mbit/s"
        dv_remux = ("DV" in s.visual and "remux" in s.quality.lower()) or bool(DV_REMUX.search(text))
        s.score = (s.aio_score if s.aio_score is not None else 100.0 - s.order) + (20 if s.cached else 0)
        s.rank_key = (not s.why_dropped, _language_fit(s, want),
                      not dv_remux,                # Dolby Vision profile 7 remuxes often fail outside dedicated players
                      s.height, s.score, s.library, rate or 0, -s.order)
    return sorted(streams, key=lambda s: s.rank_key, reverse=True)


async def fetch(http: httpx.AsyncClient, addons: list[Addon], kind: str, video_id: str, timeout: float = 8.0,
                enough: Callable[[list[Stream]], bool] | None = None, grace: float = 0.5) -> list[Stream]:
    """All playable streams from every addon that serves this id, in addon order. With `enough`: once the answers in
    have a stream it takes, the addons still asking get `grace` seconds more, then are left out (one that hangs
    would hold the start for its 8 s timeout, twice). Each addon's answer and time go into the command's trace."""
    t0 = time.perf_counter()
    answers: dict[str, dict] = {}

    async def one(addon: Addon) -> list[dict]:
        t = time.perf_counter()
        for attempt in (1, 2):
            try:
                r = await http.get(f"{addon.base}/stream/{kind}/{video_id}.json", timeout=timeout,
                                   headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                got = r.json().get("streams") or []
                answers[addon.name] = {"addon": addon.name, "ms": round(tracing.since(t)), "streams": len(got),
                                       **({"tries": 2} if attempt == 2 else {})}
                return got
            except httpx.HTTPStatusError as e:
                # One retry a second later when the addon has a hiccup (5xx, rate limit)
                code = e.response.status_code
                log.warning("addon %s: HTTP %d%s", addon.name, code, ", trying again" if attempt == 1 else "")
                answers[addon.name] = {"addon": addon.name, "ms": round(tracing.since(t)), "error": f"HTTP {code}"}
                if attempt == 2 or not (code >= 500 or code == 429):
                    return []
                await asyncio.sleep(1.0)
            except httpx.TransportError as e:
                # Timed out or cut off: asked again at once. AIOStreams keeps working on an answer it was too slow
                # with (a title nobody asked it for lately: 7-9 s, more when it's busy), so the second ask is
                # shorter (2-5 s); a reused connection the server had just closed fails the same way
                log.warning("addon %s: %s%s", addon.name, type(e).__name__, ", trying again" if attempt == 1 else "")
                answers[addon.name] = {"addon": addon.name, "ms": round(tracing.since(t)), "error": type(e).__name__}
                if attempt == 2:
                    return []
            except (httpx.HTTPError, ValueError) as e:
                log.warning("addon %s: %s", addon.name, type(e).__name__)
                answers[addon.name] = {"addon": addon.name, "ms": round(tracing.since(t)), "error": type(e).__name__}
                return []
        return []

    def parsed() -> list[Stream]:
        out, order = [], 0
        for addon, task in zip(serving, tasks):
            for raw in task.result() if task.done() and not task.cancelled() and not task.exception() else []:
                if s := parse(addon.name, raw, order):
                    out.append(s)
                order += 1
        return out

    serving = [a for a in addons if a.serves_streams(kind, video_id)]
    tasks = [asyncio.create_task(one(a)) for a in serving]
    pending = set(tasks)
    try:
        while pending:
            _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if pending and enough is not None and enough(parsed()):
                _, pending = await asyncio.wait(pending, timeout=grace)
                if pending:
                    log.info("streams for %s: going on without %s", video_id,
                             ", ".join(a.name for a, t in zip(serving, tasks) if t in pending))
                break
    finally:
        for task in pending:
            task.cancel()
    found = parsed()
    left = [a.name for a, t in zip(serving, tasks) if t in pending]
    tracing.step("start", "addons", label="Addons asked", ms=tracing.since(t0), method=tracing.method("addon"),
                 video=video_id, playable=len(found), left_out=left or None,
                 addons=[answers.get(a.name) or {"addon": a.name, "ms": round(tracing.since(t0)), "left_out": True}
                         for a in serving])
    return found


async def resolve(http: httpx.AsyncClient, s: Stream, hops: list[dict] | None = None) -> str | None:
    """Where the link leads when it plays, else None. A one-byte range request: the link answers with file bytes,
    not an error page. Redirects are followed by hand, because AIOStreams redirects a link that can't play (not
    cached after all, failed, rate-limited) to a short explanatory video that would otherwise look fine. Debrid
    services label files loosely (Premiumize: application/force-download), so anything but a web page or JSON
    counts. The player is given the final address: each of its range requests (header, index, the resume point)
    then skips the addon's redirect. `hops`: each request's host, answer and time are added to it (no paths: they
    carry keys)."""
    url = s.url
    try:
        for _ in range(4):
            t = time.perf_counter()
            async with http.stream("GET", url, headers={"Range": "bytes=0-0", **s.headers}, timeout=5,
                                   follow_redirects=False) as r:
                if hops is not None:
                    hops.append({"host": r.url.host, "status": r.status_code, "ms": round(tracing.since(t))})
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = str(r.url.join(r.headers["location"]))
                    if PROBLEM_VIDEO.search(httpx.URL(url).path):
                        log.info("%s: the addon says it can't play (%s)", s.filename or s.name,
                                 httpx.URL(url).path.rsplit("/", 1)[-1])
                        return None
                    continue
                return url if _file_bytes(r) else None
    except httpx.HTTPError:
        return None
    return None


async def recheck(http: httpx.AsyncClient, link: str, headers: dict | None = None) -> bool:
    """Whether a link resolve() found a while ago still answers with file bytes: one range request to the file's own
    address (~0.2 s), not the addon's redirect again (0.5-1.5 s: its debrid service is asked for the file anew)."""
    try:
        async with http.stream("GET", link, headers={"Range": "bytes=0-0", **(headers or {})}, timeout=4,
                               follow_redirects=False) as r:
            return _file_bytes(r)
    except httpx.HTTPError:
        return False


def _file_bytes(r: httpx.Response) -> bool:
    ctype = r.headers.get("content-type", "").lower()
    return r.status_code in (200, 206) and not ctype.startswith(("text/", "application/json"))


async def playable(http: httpx.AsyncClient, s: Stream) -> bool:
    return await resolve(http, s) is not None
