"""Ask your Stremio addons for a title's streams and pick one Kodi can start right away.

The ranking is plain code, not the model. Your addon (AIOStreams) already sorts by your own preferences, so its
order is kept and only hard limits are applied: playable debrid links (direct https `url`, not torrents), cached
on the debrid service, no larger than max_height, a bitrate the TV's 100 Mbit/s port can carry, and no cam rips.
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from .account import Addon

log = logging.getLogger(__name__)

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
    cached: bool | None = None
    headers: dict = field(default_factory=dict, repr=False)
    score: float = 0.0
    why_dropped: str = ""


def parse(addon: str, raw: dict, order: int) -> Stream | None:
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None   # torrents (infoHash), YouTube ids and external links need Stremio itself
    hints = raw.get("behaviorHints") or {}
    s = Stream(addon=addon, url=url, name=str(raw.get("name") or ""),
               description=str(raw.get("description") or raw.get("title") or ""),
               filename=str(hints.get("filename") or ""), order=order,
               headers=((hints.get("proxyHeaders") or {}).get("request") or {}))
    text = f"{s.name}\n{s.description}\n{s.filename}"
    s.height = next((h for rx, h in HEIGHT if rx.search(text)), 0)
    if isinstance(hints.get("videoSize"), int):
        s.size = hints["videoSize"]
    elif m := SIZE.search(text):
        s.size = int(float(m.group(1).replace(",", ".")) * UNIT[m.group(2).lower()])
    s.cached = False if UNCACHED.search(text) else (True if CACHED.search(text) else None)
    return s


def rank(streams: list[Stream], max_height: int, max_mbps: float, runtime_min: int | None,
         native_original: bool = False) -> list[Stream]:
    """Best first. Streams that break a hard limit get why_dropped set and sink to the end. For a title not
    made in your language, dubbed-only releases are marked down (a French-only file of an American show)."""
    for s in streams:
        text = f"{s.name}\n{s.description}\n{s.filename}"
        s.score = 100.0 - s.order          # keep the addon's own order
        if not native_original and DUB_ONLY.search(s.filename) and not MULTI.search(s.filename):
            s.score -= 40
        if s.cached is False:
            s.why_dropped = "not cached"
        elif CAM.search(text):
            s.why_dropped = "cam/ts"
        elif s.height > max_height:
            s.why_dropped = f"{s.height}p > {max_height}p"
        elif runtime_min and s.size and s.size * 8 / (runtime_min * 60) / 1e6 > max_mbps:
            s.why_dropped = f"{s.size * 8 / (runtime_min * 60) / 1e6:.0f} Mbit/s"
        if s.cached:
            s.score += 20
        if DV_REMUX.search(text):
            s.score -= 30                  # Dolby Vision profile 7 remuxes often fail outside dedicated players
        if s.why_dropped:
            s.score -= 1000
    return sorted(streams, key=lambda s: -s.score)


async def fetch(http: httpx.AsyncClient, addons: list[Addon], kind: str, video_id: str,
                timeout: float = 8.0) -> list[Stream]:
    """All playable streams from every addon that serves this id, in addon order."""
    async def one(addon: Addon) -> list[dict]:
        try:
            r = await http.get(f"{addon.base}/stream/{kind}/{video_id}.json", timeout=timeout)
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
    """A one-byte range request: the debrid link answers with file bytes, not an error page. Debrid services label
    files loosely (Premiumize: application/force-download), so anything but a web page or JSON counts."""
    try:
        async with http.stream("GET", s.url, headers={"Range": "bytes=0-0", **s.headers}, timeout=5) as r:
            ctype = r.headers.get("content-type", "").lower()
            return r.status_code in (200, 206) and not ctype.startswith(("text/", "application/json"))
    except httpx.HTTPError:
        return False
