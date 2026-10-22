"""Your Stremio account: installed addons and library, over Stremio's own API (api.strem.io, the calls
stremio-core makes; undocumented but stable). Login is Stremio's passwordless link flow: you approve a link in a
browser where you are signed in, and the agent receives an auth key. That key is full account access and lives
only in secrets.toml. Addon transport URLs embed addon config (debrid keys), so they stay in memory only."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

API = "https://api.strem.io/api/"
LINK = "https://link.stremio.com/api/v2/"


class StremioError(RuntimeError):
    pass


@dataclass(frozen=True)
class Addon:
    name: str
    transport_url: str   # secret: contains the addon's config
    manifest: dict

    @property
    def base(self) -> str:
        return self.transport_url.removesuffix("/manifest.json")

    def serves_streams(self, kind: str, video_id: str) -> bool:
        """Whether this addon answers /stream for this type and id (manifest resources, types, idPrefixes)."""
        m = self.manifest
        for res in m.get("resources", []):
            name = res if isinstance(res, str) else res.get("name")
            if name != "stream":
                continue
            types = m.get("types", []) if isinstance(res, str) else res.get("types", m.get("types", []))
            prefixes = m.get("idPrefixes") if isinstance(res, str) else res.get("idPrefixes", m.get("idPrefixes"))
            if kind in types and (not prefixes or any(video_id.startswith(p) for p in prefixes)):
                return True
        return False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Account:
    def __init__(self, http: httpx.AsyncClient, auth_key: str):
        self.http = http
        self._key = auth_key

    async def _call(self, method: str, body: dict):
        r = await self.http.post(API + method, json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise StremioError(f"{method}: {data['error'].get('message', 'error')}")
        return data.get("result")

    async def addons(self) -> list[Addon]:
        res = await self._call("addonCollectionGet",
                               {"type": "AddonCollectionGet", "authKey": self._key, "update": True})
        return [Addon(a.get("manifest", {}).get("name", "?"), a["transportUrl"], a.get("manifest", {}))
                for a in res.get("addons", []) if a.get("transportUrl")]

    async def library(self) -> list[dict]:
        return await self._call("datastoreGet",
                                {"authKey": self._key, "collection": "libraryItem", "ids": [], "all": True}) or []

    async def put(self, items: list[dict]):
        await self._call("datastorePut", {"authKey": self._key, "collection": "libraryItem", "changes": items})

    async def logout(self):
        await self._call("logout", {"type": "Logout", "authKey": self._key})


async def link_login(http: httpx.AsyncClient, show, timeout_s: int = 300) -> str:
    """Stremio's passwordless login: show(link) is called once; returns the auth key after you approve."""
    r = await http.get(LINK + "create", params={"type": "Create"}, timeout=10)
    r.raise_for_status()
    created = r.json().get("result") or {}
    code, link = created.get("code"), created.get("link")
    if not code or not link:
        raise StremioError("link.stremio.com did not return a login link")
    show(link)
    for _ in range(timeout_s // 2):
        await asyncio.sleep(2)
        r = await http.get(LINK + "read", params={"type": "Read", "code": code}, timeout=10)
        result = r.json().get("result") if r.status_code == 200 else None
        if result and result.get("authKey"):
            return result["authKey"]
    raise StremioError("login link expired before it was approved")
