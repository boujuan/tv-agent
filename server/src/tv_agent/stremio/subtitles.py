"""Subtitles from your Stremio subtitle addons (SubSense, OpenSubtitles…), asked the way Stremio asks: by IMDb id
plus the chosen file's hash, size and name, so the subtitles that best match that release come first. Kodi loads
them by link (Player.AddSubtitle); Just Player can't be handed a list of links over ADB. `download()` and `cues()`
read one as timed lines (the scene search: "go to the part where…").
"""
import asyncio
import gzip
import html
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

import httpx

from ..languages import ALIASES, CHINESE, matches
from .account import Addon
from .streams import Stream

log = logging.getLogger(__name__)

# AIOMetadata's subtitles route is a watch-tracking trigger that always answers with nothing.
SKIP = re.compile(r"aiometadata", re.I)
IMPAIRED = re.compile(r"\bHI\b|\bSDH\b|hearing", re.I)
KNOWN = ("eng", "spa", "fre", "ita", "ger", "por", "jpn", "kor")


@dataclass(frozen=True)
class Subtitle:
    url: str = field(repr=False)
    language: str            # eng, spa, chi… (as languages.ALIASES), or the addon's own text
    name: str                # what the addon calls it, for logs
    addon: str
    impaired: bool = False   # hearing-impaired / SDH
    ai: bool = False         # machine-translated


def language(lang: str, code: str = "") -> str:
    """'eng', 'en', 'English' -> 'eng'; 'chi', 'zh', 'Chinese bilingual' (OpenSubtitles' 'ze') -> 'chi'."""
    words = {lang.strip().lower(), code.strip().lower()} - {""}
    for key in KNOWN:
        if words & ALIASES[key]:
            return key
    if words & CHINESE or code.lower() == "ze" or any("chinese" in w for w in words):
        return "chi"
    return next(iter(sorted(words)), "")


def parse(addon: str, raw: dict) -> Subtitle | None:
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    label = " ".join(str(raw.get(k) or "") for k in ("label", "title", "lang")).strip()
    return Subtitle(url=url, language=language(str(raw.get("lang") or ""), str(raw.get("lang_code") or "")),
                    name=label or str(raw.get("id") or ""), addon=addon, impaired=bool(IMPAIRED.search(label)),
                    ai=bool(raw.get("ai_translated")))


def query_path(kind: str, video_id: str, stream: Stream | None) -> str:
    """/subtitles/{type}/{id}[/videoHash=…&videoSize=…&filename=…].json, the extras Stremio sends."""
    extras = {}
    if stream:
        if stream.video_hash:
            extras["videoHash"] = stream.video_hash
        if stream.size:
            extras["videoSize"] = str(stream.size)
        if stream.filename:
            extras["filename"] = stream.filename
    tail = f"/{urlencode(extras, quote_via=quote)}" if extras else ""
    return f"/subtitles/{kind}/{video_id}{tail}.json"


async def fetch(http: httpx.AsyncClient, addons: list[Addon], kind: str, video_id: str, stream: Stream | None,
                timeout: float = 8.0) -> list[Subtitle]:
    """Every subtitle your subtitle addons offer for this video, best match first within each addon. The client's
    User-Agent must not mention Stremio: SubSense then rewrites links to Stremio's own local server."""
    path = query_path(kind, video_id, stream)

    async def one(addon: Addon) -> list[Subtitle]:
        try:
            r = await http.get(f"{addon.base}{path}", timeout=timeout)   # OpenSubtitles PRO answers with a redirect
            r.raise_for_status()
            return [s for raw in r.json().get("subtitles") or [] if (s := parse(addon.name, raw))]
        except (httpx.HTTPError, ValueError) as e:
            log.warning("subtitles from %s: %s", addon.name, type(e).__name__)
            return []

    serving = [a for a in addons if a.serves("subtitles", kind, video_id) and not SKIP.search(a.name)]
    found = await asyncio.gather(*(one(a) for a in serving))
    return [s for subs in found for s in subs]


def choose(subs: list[Subtitle], langs: list[str]) -> list[tuple[str, Subtitle]]:
    """The best subtitle for each wanted language that has one, in your order: full before hearing-impaired,
    human before machine-translated, then the addon's own ranking. Chinese of an unknown script counts once."""
    out, used = [], set()
    for lang in langs:
        found = [s for s in subs if s.url not in used and matches({"language": s.language, "name": s.name}, lang)]
        if not found:
            continue
        best = sorted(found, key=lambda s: (s.impaired, s.ai))[0]
        used.add(best.url)
        out.append((lang, best))
    return out


CUE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->")
TAG = re.compile(r"<[^>]+>|\{[^}]*\}")


async def download(http: httpx.AsyncClient, url: str, timeout: float = 10.0) -> str:
    """A subtitle file's text (SRT or WebVTT; gzipped or not; UTF-8, else Windows-1252)."""
    r = await http.get(url, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    data = r.content[:3_000_000]
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)[:3_000_000]
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def cues(text: str) -> list[tuple[int, str]]:
    """(start in ms, the words) of each cue, tags and position codes removed."""
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        m = CUE.search(block)
        if not m:
            continue
        h, mi, se = (int(x or 0) for x in m.groups()[:3])
        start = ((h * 60 + mi) * 60 + se) * 1000 + int(m.group(4).ljust(3, "0"))    # ",5" is 500 ms, ",040" 40
        words = " ".join(html.unescape(TAG.sub("", line)).strip() for line in block[m.end():].splitlines()[1:])
        words = " ".join(words.split())
        if words:
            out.append((start, words))
    return out
