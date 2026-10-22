"""Ask your Stremio addons for a title's streams and pick the best one the TV can start right away.

The ranking is plain code, not the model. Hard limits drop a stream: not a direct debrid link (torrents need Stremio
itself), not cached on the debrid service, a cam rip, above max_height, or a bitrate the TV's 100 Mbit/s port can't
carry. The rest are ordered by, in turn:

1. language: the audio the title should play in (English, or Spanish for Spanish-made titles) is in the file
2. resolution, up to max_height
3. AIOStreams' score for the release (the ★ in its names: release group tier, source, HDR, audio…)
4. already in your debrid library, then the higher bitrate

AIOStreams describes each stream in machine-readable `streamData` when asked by an AIOStreams-compatible client
(the User-Agent below); other addons are read from their names and descriptions, keeping their own order.
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from .. import __version__
from ..languages import ALIASES
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
DV_REMUX = re.compile(r"(\bdv\b|dolby ?vision|\bdovi\b).*\bremux\b|\bremux\b.*(\bdv\b|dolby ?vision|\bdovi\b)", re.I)
UNIT = {"tb": 1e12, "gb": 1e9, "gib": 2**30, "mb": 1e6, "mib": 2**20}
CAM_QUALITIES = {"cam", "ts", "tc", "scr"}
# AIOStreams sends these when a link can't play (uncached, failed, 429…): a short video explaining it.
PROBLEM_VIDEO = re.compile(r"/static/[\w-]+\.mp4$")


@dataclass
class Stream:
    addon: str
    url: str = field(repr=False)     # secret: debrid links carry account keys
    name: str
    description: str
    filename: str
    order: int
    height: int = 0
    size: int = 0
    duration_ms: int = 0             # from AIOStreams; else the title's runtime is used for the bitrate
    cached: bool | None = None
    library: bool = False            # already in your debrid account's library
    quality: str = ""                # "WEB-DL", "BluRay REMUX"… (AIOStreams)
    visual: tuple[str, ...] = ()     # "DV", "HDR10"…
    audio: tuple[str, ...] = ()      # "DD+", "Atmos"…
    languages: tuple[str, ...] = ()  # audio languages by name: "English", "Spanish", "Latino", "Multi"…
    group: str = ""                  # release group
    aio_score: int | None = None     # AIOStreams' ranked-expression score (its ★)
    matched: tuple[str, ...] = ()    # AIOStreams' ranked expressions that matched ("Web T1", "4K", "DV"…)
    headers: dict = field(default_factory=dict, repr=False)
    score: float = 0.0
    why_dropped: str = ""
    rank_key: tuple = field(default=(), repr=False)

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
               filename=str(hints.get("filename") or ""), order=order,
               headers=((hints.get("proxyHeaders") or {}).get("request") or {}))
    data = raw.get("streamData")
    if isinstance(data, dict) and data.get("type") not in ("error", "statistic"):
        pf = data.get("parsedFile") or {}
        s.height = _height(str(pf.get("resolution") or ""))
        s.size = int(data.get("size") or hints.get("videoSize") or 0)
        s.duration_ms = int(data.get("duration") or 0)
        service = data.get("service") or {}
        s.cached = service.get("cached") if isinstance(service.get("cached"), bool) else None
        s.library = bool(data.get("library"))
        s.quality = str(pf.get("quality") or "")
        s.visual = tuple(pf.get("visualTags") or ())
        s.audio = tuple(pf.get("audioTags") or ())
        s.languages = tuple(pf.get("languages") or ())
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
    return s


def _language_fit(s: Stream, want: str | None) -> float:
    """2: the wanted audio language is in the file; 1: unknown or a multi-language release; 0: dubbed without it.
    Latin American Spanish counts, a little below Castilian."""
    if not want:
        return 1
    if s.languages:
        names = {x.lower() for x in s.languages}
        if names & ALIASES.get(want, {want}):
            return 2
        if want == "spa" and "latino" in names:
            return 1.5
        return 1 if names & {"multi", "dual audio"} else 0
    if DUB_ONLY.search(s.filename) and not MULTI.search(s.filename):   # streams without streamData: the filename
        return 0
    return 1


def rank(streams: list[Stream], max_height: int, max_mbps: float, runtime_min: int | None,
         native_original: bool = False, audio: str | None = None) -> list[Stream]:
    """Best first (see the module docstring). Streams that break a hard limit get why_dropped set and sink to the
    end. audio is the language the title should play in; without it (older callers), a dub-only release of a title
    not made in your language is marked down, as before."""
    want = audio if audio else (None if native_original else "eng")
    for s in streams:
        text = f"{s.name}\n{s.description}\n{s.filename}"
        rate = s.mbps(runtime_min)
        if s.cached is False:
            s.why_dropped = "not cached"
        elif s.quality.lower() in CAM_QUALITIES or (not s.quality and CAM.search(text)):
            s.why_dropped = "cam/ts"
        elif s.height > max_height:
            s.why_dropped = f"{s.height}p > {max_height}p"
        elif rate and rate > max_mbps:
            s.why_dropped = f"{rate:.0f} Mbit/s"
        dv_remux = ("DV" in s.visual and "remux" in s.quality.lower()) or bool(DV_REMUX.search(text))
        s.score = (s.aio_score if s.aio_score is not None else 100.0 - s.order) + (20 if s.cached else 0)
        s.rank_key = (not s.why_dropped, _language_fit(s, want),
                      not dv_remux,                # Dolby Vision profile 7 remuxes often fail outside dedicated players
                      s.height, s.score, s.library, rate or 0, -s.order)
    return sorted(streams, key=lambda s: s.rank_key, reverse=True)


async def fetch(http: httpx.AsyncClient, addons: list[Addon], kind: str, video_id: str,
                timeout: float = 8.0) -> list[Stream]:
    """All playable streams from every addon that serves this id, in addon order."""
    async def one(addon: Addon) -> list[dict]:
        try:
            r = await http.get(f"{addon.base}/stream/{kind}/{video_id}.json", timeout=timeout,
                               headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json().get("streams") or []
        except (httpx.HTTPError, ValueError) as e:
            log.warning("addon %s: %s", addon.name, type(e).__name__)
            return []

    serving = [a for a in addons if a.serves_streams(kind, video_id)]
    results = await asyncio.gather(*(one(a) for a in serving))
    out, order = [], 0
    for addon, raws in zip(serving, results):
        for raw in raws:
            if s := parse(addon.name, raw, order):
                out.append(s)
            order += 1
    return out


async def playable(http: httpx.AsyncClient, s: Stream) -> bool:
    """A one-byte range request: the link answers with file bytes, not an error page. Redirects are followed by
    hand, because AIOStreams redirects a link that can't play (not cached after all, failed, rate-limited) to a
    short explanatory video that would otherwise look fine. Debrid services label files loosely (Premiumize:
    application/force-download), so anything but a web page or JSON counts."""
    url = s.url
    try:
        for _ in range(4):
            async with http.stream("GET", url, headers={"Range": "bytes=0-0", **s.headers}, timeout=5,
                                   follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = str(r.url.join(r.headers["location"]))
                    if PROBLEM_VIDEO.search(httpx.URL(url).path):
                        log.info("%s: the addon says it can't play (%s)", s.filename or s.name,
                                 httpx.URL(url).path.rsplit("/", 1)[-1])
                        return False
                    continue
                ctype = r.headers.get("content-type", "").lower()
                return r.status_code in (200, 206) and not ctype.startswith(("text/", "application/json"))
    except httpx.HTTPError:
        return False
    return False
