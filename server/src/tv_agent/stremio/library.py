"""Your Stremio library: what's in Continue Watching, where you stopped, and the names to match speech against.
Cached for 10 minutes (and refreshed after every progress write)."""
import re
import time

from rapidfuzz import fuzz, process

from .account import Account
from .cinemeta import norm

ARTICLE = re.compile(r"^(?:the|a|an|el|la|los|las|un|una) ")


def _bare(name: str) -> str:
    """'The Office' -> 'office', 'Fundación' -> 'fundacion': what's left to compare."""
    return ARTICLE.sub("", norm(name))


def in_continue_watching(item: dict) -> bool:
    # stremio-core LibraryItem::is_in_continue_watching
    return (item.get("type") != "other" and (not item.get("removed") or item.get("temp"))
            and (item.get("state") or {}).get("timeOffset", 0) > 0)


class Library:
    def __init__(self, account: Account | None):
        self.account = account
        self._items: list[dict] = []
        self._at = 0.0

    async def items(self, fresh: bool = False) -> list[dict]:
        if self.account and (fresh or not self._items or time.monotonic() - self._at > 600):
            self._items = await self.account.library()
            self._at = time.monotonic()
        return self._items

    def invalidate(self):
        self._at = 0.0

    async def find(self, title: str, kind: str | None = None, cutoff: int = 88) -> dict | None:
        """The library item whose name matches what was said (movies and series you added or watched). The whole
        name has to match, give or take an article, accents or a slip ("dune parte dos" isn't your "Dune"), or
        the name before its subtitle ("el señor de los anillos" is your "…: La comunidad del anillo")."""
        items = [i for i in await self.items() if i.get("type") in ("movie", "series")
                 and (kind is None or i.get("type") == kind) and (not i.get("removed") or i.get("temp"))]
        if not items:
            return None
        said = _bare(title)
        for names in ([_bare(i.get("name", "")) for i in items],
                      [_bare(re.split(r":| - ", i.get("name", ""))[0]) for i in items]):
            hit = process.extractOne(said, names, scorer=fuzz.ratio, score_cutoff=cutoff)
            if hit:
                return items[hit[2]]
        return None

    async def continue_watching(self) -> list[dict]:
        """Most recently watched first, like Stremio's Continue Watching row."""
        items = [i for i in await self.items() if in_continue_watching(i)]
        return sorted(items, key=lambda i: (i.get("state") or {}).get("lastWatched") or "", reverse=True)

    async def get(self, imdb_id: str) -> dict | None:
        for i in await self.items():
            if i.get("_id") == imdb_id:
                return i
        return None
