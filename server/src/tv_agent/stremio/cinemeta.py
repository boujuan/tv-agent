"""Cinemeta, Stremio's public metadata addon: title search and episode lists (IMDb ids, `tt…:season:episode`)."""
import asyncio
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from rapidfuzz import fuzz

BASE = "https://v3-cinemeta.strem.io"
IMDB_ID = re.compile(r"^tt\d{1,10}$")
VIDEO_ID = re.compile(r"^tt\d{1,10}:\d{1,3}:\d{1,4}$")


@dataclass(frozen=True)
class Meta:
    id: str
    type: str          # "movie" or "series"
    name: str
    year: str
    rank: int          # position in Cinemeta's (popularity-ordered) results

    @property
    def label(self) -> str:
        return f"{self.name} ({self.year}, {self.type})" if self.year else f"{self.name} ({self.type})"


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


def ranked(query: str, metas: list[Meta], kind: str | None = None) -> list[tuple[float, Meta]]:
    """Best match first. Cinemeta's own order counts most: its search knows other titles ("la casa de papel"
    finds Money Heist first). Then name similarity, the kind the user implied, and a nudge towards recent
    titles for movie/series ties ("the bear": the 2022 series over the 1989 film)."""
    def score(m: Meta) -> float:
        s = 0.5 * fuzz.WRatio(query.lower(), m.name.lower())
        s += RANK_BONUS[m.rank] if m.rank < len(RANK_BONUS) else 0
        if kind and m.type == kind:
            s += 15
        year = re.match(r"\d{4}", m.year)
        if year:
            s += max(0.0, min(5.0, (int(year.group()) - 1990) / 7))
        return s
    return sorted(((score(m), m) for m in metas), key=lambda x: -x[0])


async def meta(client: httpx.AsyncClient, kind: str, imdb_id: str) -> dict:
    assert IMDB_ID.match(imdb_id), imdb_id
    r = await client.get(f"{BASE}/meta/{kind}/{imdb_id}.json", timeout=6)
    r.raise_for_status()
    return r.json().get("meta") or {}


def episodes(m: dict) -> list[dict]:
    """Episodes in watch order, specials (season 0) left out."""
    vids = [v for v in m.get("videos", []) if v.get("season") and VIDEO_ID.match(v.get("id", ""))]
    return sorted(vids, key=lambda v: (v["season"], v.get("episode") or 0))
