"""Title search: Cinemeta plus the search catalogs of your metadata addons (AIOMetadata…). Those know titles by their
names in your language ("el hoyo" is The Platform, "padre de familia" Family Guy) and leave out obscure namesakes
that Cinemeta ranks first. Results are merged by IMDb id, keeping every name a title is known by.

A Cinemeta search nobody has made lately can take 4-8 s (its cache misses), so the searches run side by side and
the agent goes on as soon as one of them has the title by exactly the name you said."""
import asyncio
from dataclasses import replace
from urllib.parse import quote

import httpx

from . import cinemeta
from .account import Addon
from .cinemeta import IMDB_ID, Meta, norm

ALIAS_LANGUAGES = ("eng", "spa")
MAX_CATALOGS = 4


def said_exactly(query: str, m: Meta) -> bool:
    q = norm(query)
    return any(norm(n) == q for n in m.names)


def search_catalogs(addons: list[Addon]) -> list[tuple[Addon, str, str]]:
    """(addon, type, catalog id) for each movie or series catalog that takes a search; Cinemeta is asked directly."""
    out = []
    for a in addons:
        if a.manifest.get("id") == "com.linvo.cinemeta":
            continue
        for c in a.manifest.get("catalogs", []):
            if c.get("type") in ("movie", "series") and any(e.get("name") == "search" for e in c.get("extra", [])):
                out.append((a, c["type"], c["id"]))
    return out[:MAX_CATALOGS]


async def addon_search(client: httpx.AsyncClient, addon: Addon, kind: str, catalog: str, query: str) -> list[Meta]:
    try:
        r = await client.get(f"{addon.base}/catalog/{kind}/{catalog}/search={quote(query)}.json", timeout=6)
        r.raise_for_status()
        metas = r.json().get("metas") or []
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for i, m in enumerate(metas[:10]):
        mid = m.get("imdb_id") or m.get("id") or ""
        if not IMDB_ID.match(mid):
            continue                      # tvdb:/tmdb: ids: no streams or episode list by IMDb id
        names = [m.get("name") or ""] + [a.get("name") or "" for a in m.get("aliases") or []
                                         if isinstance(a, dict) and a.get("language") in ALIAS_LANGUAGES]
        year = str(m.get("releaseInfo") or m.get("year") or "")
        out.append(Meta(mid, kind, m.get("name") or "", year, cinemeta.NOT_RANKED, extra_rank=i,
                        names=tuple(dict.fromkeys(n for n in names if n))))
    return out


def merge(results: list[list[Meta]]) -> list[Meta]:
    """One Meta per IMDb id: the first source's name (Cinemeta's, English) leads; every name and rank is kept."""
    by_id: dict[str, Meta] = {}
    for metas in results:
        for m in metas:
            old = by_id.get(m.id)
            if old is None:
                by_id[m.id] = m
            elif old.type == m.type:
                by_id[m.id] = replace(old, names=tuple(dict.fromkeys(old.names + m.names)),
                                      rank=min(old.rank, m.rank), extra_rank=min(old.extra_rank, m.extra_rank))
    return list(by_id.values())


async def search(client: httpx.AsyncClient, query: str, addons: list[Addon], patience: float = 1.5) -> list[Meta]:
    """Cinemeta and your metadata addons side by side. After `patience` seconds, an exact name match from those
    that have answered is enough; otherwise the slower ones get a few more seconds."""
    tasks = [asyncio.create_task(cinemeta.search(client, query))]
    tasks += [asyncio.create_task(addon_search(client, a, kind, cat, query)) for a, kind, cat in search_catalogs(addons)]
    done, pending = await asyncio.wait(tasks, timeout=patience)
    if pending and not any(said_exactly(query, m) for t in done if not t.exception() for m in t.result()):
        _, pending = await asyncio.wait(pending, timeout=7)
    for t in pending:
        t.cancel()
    return merge([t.result() for t in tasks if t.done() and not t.cancelled() and not t.exception()])
