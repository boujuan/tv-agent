"""Your Stremio library: what's in Continue Watching, where you stopped, and the names to match speech against.
Cached for 10 minutes (and refreshed after every progress write)."""
import time

from rapidfuzz import fuzz, process

from .account import Account


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

    async def find(self, title: str, kind: str | None = None, cutoff: int = 90) -> dict | None:
        """The library item whose name best matches what was said (movies and series you added or watched)."""
        items = [i for i in await self.items() if i.get("type") in ("movie", "series")
                 and (kind is None or i.get("type") == kind) and (not i.get("removed") or i.get("temp"))]
        if not items:
            return None
        hit = process.extractOne(title.lower(), [i.get("name", "").lower() for i in items],
                                 scorer=fuzz.WRatio, score_cutoff=cutoff)
        return items[hit[2]] if hit else None

    async def continue_watching(self) -> list[dict]:
        """Most recently watched first, like Stremio's Continue Watching row."""
        items = [i for i in await self.items() if in_continue_watching(i)]
        return sorted(items, key=lambda i: (i.get("state") or {}).get("lastWatched") or "", reverse=True)

    async def get(self, imdb_id: str) -> dict | None:
        for i in await self.items():
            if i.get("_id") == imdb_id:
                return i
        return None
