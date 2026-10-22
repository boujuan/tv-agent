"""Cinemeta, Stremio's public metadata addon: title search and episode lists (IMDb ids, `tt…:season:episode`)."""
import asyncio
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

import httpx
from rapidfuzz import fuzz

BASE = "https://v3-cinemeta.strem.io"
IMDB_ID = re.compile(r"^tt\d{1,10}$")
VIDEO_ID = re.compile(r"^tt\d{1,10}:\d{1,3}:\d{1,4}$")
NOT_RANKED = 99


def norm(name: str) -> str:
    """'Dune: Parte dos' -> 'dune parte dos'; accents and punctuation don't count."""
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


@dataclass(frozen=True)
class Meta:
    id: str
    type: str          # "movie" or "series"
    name: str
    year: str
    rank: int          # position in Cinemeta's (popularity-ordered) results; NOT_RANKED if it didn't list it
    extra_rank: int = NOT_RANKED            # best position in your metadata addons' results (search.py)
    names: tuple[str, ...] = field(default=())   # every name it's known by ("The Platform", "El hoyo")

    def __post_init__(self):
        if not self.names:
            object.__setattr__(self, "names", (self.name,))

    @property
    def label(self) -> str:
        other = next((n for n in self.names if norm(n) != norm(self.name)), None)
        name = f"{self.name} / {other}" if other else self.name
        return f"{name} ({self.year}, {self.type})" if self.year else f"{name} ({self.type})"


async def search(client: httpx.AsyncClient, query: str) -> list[Meta]:
    async def one(kind: str) -> list[Meta]:
        for attempt in (1, 2):   # Cinemeta occasionally stalls; one retry, then give up on that kind
            try:
                r = await client.get(f"{BASE}/catalog/{kind}/top/search={quote(query)}.json", timeout=4)
                r.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 2:
                    return []
        out = []
        for i, m in enumerate(r.json().get("metas", [])[:10]):
            if IMDB_ID.match(m.get("id", "")):
                out.append(Meta(m["id"], kind, m.get("name", ""), str(m.get("releaseInfo") or m.get("year") or ""), i))
        return out
    movies, series = await asyncio.gather(one("movie"), one("series"))
    return movies + series


RANK_BONUS = (40, 25, 15, 8, 4)


def _bonus(rank: int) -> int:
    return RANK_BONUS[rank] if rank < len(RANK_BONUS) else 0


def similarity(query: str, m: Meta) -> float:
    q = norm(query)
    return max(fuzz.WRatio(q, norm(n)) for n in m.names)


def ranked(query: str, metas: list[Meta], kind: str | None = None,
           popularity: dict[str, float] | None = None) -> list[tuple[float, Meta]]:
    """Best match first. Cinemeta's own order counts most: its search knows other titles ("la casa de papel"
    finds Money Heist first). Then your metadata addons' order (names in your language, no obscure namesakes),
    similarity to any of a title's names (a bonus when one is exactly what was said), the kind the user implied,
    and a nudge towards recent titles for movie/series ties ("the bear": the 2022 series over the 1989 film).
    `popularity` (Cinemeta's, fetched for close calls) settles films against series of the same name: "regreso al
    futuro" is the 1985 film (2.3) rather than the 1991 cartoon (0.4)."""
    q = norm(query)
    popularity = popularity or {}

    def score(m: Meta) -> float:
        s = 0.5 * similarity(query, m)
        if any(norm(n) == q for n in m.names):
            s += 20
        s += _bonus(m.rank) + 0.6 * _bonus(m.extra_rank)
        if kind and m.type == kind:
            s += 15
        year = re.match(r"\d{4}", m.year)
        if year:
            s += max(0.0, min(5.0, (int(year.group()) - 1990) / 7))
        if pop := popularity.get(m.id):
            s += min(30.0, 15 * math.log10(1 + 10 * pop))
        return s
    return sorted(((score(m), m) for m in metas), key=lambda x: -x[0])


_metas: dict[tuple[str, str], tuple[float, dict]] = {}
META_TTL = 300


async def meta(client: httpx.AsyncClient, kind: str, imdb_id: str) -> dict:
    """The title's details and episode list; kept 5 minutes (a play asks for it more than once). Titles Cinemeta
    hasn't cached are redirected to its live service (cinemeta-live.strem.io)."""
    assert IMDB_ID.match(imdb_id), imdb_id
    hit = _metas.get((kind, imdb_id))
    if hit and time.monotonic() - hit[0] < META_TTL:
        return hit[1]
    r = await client.get(f"{BASE}/meta/{kind}/{imdb_id}.json", timeout=6, follow_redirects=True)
    r.raise_for_status()
    found = r.json().get("meta") or {}
    if len(_metas) > 200:
        _metas.clear()
    _metas[(kind, imdb_id)] = (time.monotonic(), found)
    return found


async def last_videos(client: httpx.AsyncClient, imdb_ids: list[str]) -> list[dict]:
    """The newest episodes of these series, with release dates (what Stremio's new-episode notices use)."""
    ids = [i for i in imdb_ids if IMDB_ID.match(i)][:100]
    if not ids:
        return []
    r = await client.get(f"{BASE}/catalog/series/last-videos/lastVideosIds={','.join(ids)}.json", timeout=8,
                         follow_redirects=True)
    r.raise_for_status()
    return r.json().get("metasDetailed") or r.json().get("metas") or []


def released(v: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v.get("released") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def episodes(m: dict) -> list[dict]:
    """Episodes in watch order, specials (season 0) left out."""
    vids = [v for v in m.get("videos", []) if v.get("season") and VIDEO_ID.match(v.get("id", ""))]
    return sorted(vids, key=lambda v: (v["season"], v.get("episode") or 0))
