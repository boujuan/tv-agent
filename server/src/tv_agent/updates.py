"""New releases on GitHub (the repos are public, so no login): this agent's, the panel's, the TV app's, TV Agent
Player's and the phone app's. Checked every few hours unless `updates = "off"`; the TV app's and the player's
releases can be installed from here over ADB, and `updates = "auto"` does that by itself when they're idle (the TV
app not listening, the player not playing). The phone app updates itself; the panel updates this computer's checkout.

    GET  /v1/updates            what's running and the latest release of each part (phones too)
    POST /v1/updates/check      ask GitHub now (at most once a minute)
    POST /v1/updates/tv         install the TV app's latest release (the panel only)
    POST /v1/updates/tvplayer   install TV Agent Player's latest release (the panel only)

An APK is installed only when its SHA-256 is the one GitHub gives for the release asset, and Android itself refuses
an update signed with another key than the installed app's.
"""
import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import __version__
from . import config as config_mod
from .install import install_app
from .tv import status as tv_status

log = logging.getLogger("tv_agent")
REPO, PHONE_REPO, PLAYER_REPO = "boujuan/tv-agent", "boujuan/tv-agent-phone", "boujuan/tv-agent-player"
# The apps on the TV this installs: the component's name, and its key in tv/status.py's packages()
TV_APPS = {"tv": ("The TV app", "tvagent"), "tvplayer": ("TV Agent Player", "tvplayer")}
RELEASES = "https://api.github.com/repos/{}/releases?per_page=30"
EVERY = 6 * 3600                # a check this often (updates = "notify" or "auto")
FIRST = 120                     # the first one this long after the agent starts
MIN_GAP = 60                    # "check now" more often than this answers with the last check
MAX_APK = 50_000_000
TAG = re.compile(r"^(?P<prefix>.*?v)(?P<version>\d+(?:\.\d+){1,3})$")


def version(text: str | None) -> tuple[int, ...] | None:
    """"0.8.0" → (0, 8, 0, 0, 1), comparable: four numbers ("1.2" is "1.2.0.0"), then 1 for a release and 0 for a
    test build ("0.9.0-dev", "0.2.0-test"), which comes just before its release: never replaced by an older one.
    None for anything else."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+){1,3})(-[\w.]+)?", (text or "").strip())
    if not m:
        return None
    numbers = [int(p) for p in m.group(1).split(".")]
    return (*numbers, *[0] * (4 - len(numbers)), 0 if m.group(2) else 1)


def newer(latest: str | None, current: str | None) -> bool:
    """Whether latest is a higher version than current (an unknown current counts as older; never a downgrade)."""
    lv, cv = version(latest), version(current)
    return lv is not None and (cv is None or lv > cv)


def latest(releases: list, prefix: str, asset_suffix: str = "") -> dict | None:
    """The highest published release whose tag is `prefix` + a version ("android-v", or "v" for the phone app's),
    with its asset ending in `asset_suffix` (its URL and SHA-256) when asked."""
    best = None
    for r in releases if isinstance(releases, list) else []:
        m = TAG.match(str(r.get("tag_name", "")))
        if not m or m["prefix"] != prefix or r.get("draft") or r.get("prerelease"):
            continue
        if best is None or version(m["version"]) > version(best["version"]):
            best = {"version": m["version"], "url": r.get("html_url", ""), "published": r.get("published_at"),
                    "notes": str(r.get("body") or "")[:4000], "assets": r.get("assets") or []}
    if best is None:
        return None
    assets = best.pop("assets")
    if asset_suffix:
        a = next((a for a in assets if str(a.get("name", "")).endswith(asset_suffix)), None)
        if a is None:
            return None
        digest = str(a.get("digest") or "")
        best |= {"apk": a.get("browser_download_url", ""), "size": a.get("size"),
                 "sha256": digest.removeprefix("sha256:") if digest.startswith("sha256:") else None}
    return best


class Updates:
    def __init__(self, svc):
        self.svc = svc
        self.found: dict[str, dict | None] = {}
        self.checked: float | None = None
        self.error: str | None = None
        self.versions: dict[str, str | None] = {}    # the TV apps' (TV_APPS), as of the last check or install
        self.installed: dict[str, bool] = {}         # and whether each is on the TV (unknown until the TV answers)
        self.installing = False
        self._lock = asyncio.Lock()

    @property
    def tv_version(self) -> str | None:
        return self.versions.get("tv")

    @property
    def http(self) -> httpx.AsyncClient:
        return self.svc.agent.http

    async def _releases(self, repo: str) -> list:
        r = await self.http.get(RELEASES.format(repo), timeout=15,
                                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        r.raise_for_status()
        return r.json()

    async def check(self, force: bool = False) -> None:
        async with self._lock:
            if not force and self.checked and time.time() - self.checked < MIN_GAP:
                return
            try:
                main, phone, player = await asyncio.gather(self._releases(REPO), self._releases(PHONE_REPO),
                                                           self._releases(PLAYER_REPO))
                self.found = {"server": latest(main, "server-v"), "panel": latest(main, "panel-v"),
                              "tv": latest(main, "android-v", ".apk"), "phone": latest(phone, "v", ".apk"),
                              "tvplayer": latest(player, "v", ".apk")}
                self.error = None
            except (httpx.HTTPError, ValueError) as e:
                self.error = f"GitHub: {type(e).__name__}"
                log.info("update check: %s", self.error)
            self.checked = time.time()
            await self._read_tv_version()

    async def _read_tv_version(self):
        try:
            pkgs = await asyncio.to_thread(tv_status.packages, self.svc.agent.adb)
            self.versions = {key: (pkgs.get(package) or {}).get("version") for key, (_, package) in TV_APPS.items()}
            self.installed = {key: bool((pkgs.get(package) or {}).get("installed", self.versions[key]))
                              for key, (_, package) in TV_APPS.items()}
        except Exception as e:                  # the TV off or asleep: the last known versions stay
            log.info("update check: the TV apps' versions: %s", type(e).__name__)

    def state(self) -> dict:
        def part(key: str, current: str | None = None, with_current: bool = True) -> dict:
            f = self.found.get(key) or {}
            out = {"latest": f.get("version"), "url": f.get("url"), "published": f.get("published"),
                   "notes": f.get("notes")}
            if with_current:                # "ahead": a test build newer than the latest release
                out |= {"current": current, "newer": newer(f.get("version"), current),
                        "ahead": current is not None and newer(current, f.get("version"))}
            if "apk" in f:
                out |= {"apk": f["apk"], "sha256": f.get("sha256"), "size": f.get("size")}
            return out
        return {"checked": self.checked, "error": self.error, "mode": self.svc.cfg.updates,
                "installing": self.installing,
                "components": {"server": part("server", __version__), "panel": part("panel", with_current=False),
                               "tv": part("tv", self.versions.get("tv")) | {"installed": self.installed.get("tv")},
                               "tvplayer": part("tvplayer", self.versions.get("tvplayer"))
                               | {"installed": self.installed.get("tvplayer")},
                               "phone": part("phone", with_current=False)}}

    async def install_tv(self, only_newer: bool = True) -> dict:
        return await self.install("tv", only_newer)

    async def install(self, key: str, only_newer: bool = True) -> dict:
        """An app's latest release on the TV ("tv": the TV app, "tvplayer": TV Agent Player), downloaded, checked
        against GitHub's SHA-256 and installed over ADB (paired again, as `tv-agent tv install` does). The player
        isn't replaced while it plays."""
        name = TV_APPS[key][0]
        if self.installing:
            raise RuntimeError("an update is being installed already")
        rel = self.found.get(key)
        if not rel or not rel.get("apk"):
            raise RuntimeError(self.error or f"no {name} release found on GitHub")
        if not rel.get("sha256"):
            raise RuntimeError("GitHub gave no SHA-256 for the APK, so it isn't installed")
        current = self.versions.get(key)
        if only_newer and not newer(rel["version"], current):
            return {"ok": True, "installed": False, "version": current, "say": f"{name} is up to date"}
        if key == "tvplayer" and self.svc.agent.tvplayer.now() is not None:
            raise RuntimeError("TV Agent Player is playing: it's updated once it stops")
        self.installing = True
        path = None
        try:
            data = await self._download(rel["apk"])
            if hashlib.sha256(data).hexdigest() != rel["sha256"]:
                raise RuntimeError("the downloaded APK's SHA-256 isn't the release's: not installed")
            fd, path = tempfile.mkstemp(prefix="tv-agent-", suffix=".apk", dir=config_mod.DATA_DIR)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                await asyncio.to_thread(install_app, self.svc.cfg, self.svc.agent.adb, path)
            except SystemExit as e:             # install_app's errors (pm install refused it, the app didn't pair)
                raise RuntimeError(str(e)) from None
            log.info("%s updated to %s", name, rel["version"])
            await self._read_tv_version()
            return {"ok": True, "installed": True, "version": self.versions.get(key) or rel["version"],
                    "say": f"{name} is now {rel['version']}"}
        finally:
            self.installing = False
            if path:
                os.unlink(path)

    async def _download(self, url: str) -> bytes:
        async with self.http.stream("GET", url, timeout=60) as r:
            r.raise_for_status()
            data = b""
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > MAX_APK:
                    raise RuntimeError("the APK is larger than expected: not installed")
        return data


async def loop(svc):
    """Checks every EVERY; with updates = "auto" a newer TV app or player is installed while it's idle (nobody
    talking to the TV app, nothing playing in the player), and only over an installed one."""
    await asyncio.sleep(FIRST)
    while True:
        if svc.cfg.updates != "off":
            await svc.updates.check(force=True)
            for key in TV_APPS:
                rel, current = svc.updates.found.get(key) or {}, svc.updates.versions.get(key)
                if svc.cfg.updates != "auto" or current is None or not newer(rel.get("version"), current) or \
                        svc._busy.locked() or (key == "tvplayer" and svc.agent.tvplayer.now() is not None):
                    continue
                try:
                    await svc.updates.install(key)
                except (RuntimeError, httpx.HTTPError, OSError) as e:
                    log.warning("%s update: %s", TV_APPS[key][0], e)
        await asyncio.sleep(EVERY)


def routes(svc) -> APIRouter:
    r = APIRouter()

    @r.get("/v1/updates")
    async def get_updates(request: Request):
        svc.check(request, lan=False)
        return svc.updates.state()

    @r.post("/v1/updates/check")
    async def check_now(request: Request):
        svc.check(request, lan=False)
        await svc.updates.check()
        return svc.updates.state()

    async def install(request: Request, key: str) -> dict:
        """The panel's: installing an app on the TV isn't for a phone to start."""
        from .clients import DEVICE
        svc.check(request, lan=False)
        if request.scope.get(DEVICE):
            raise HTTPException(403)
        if svc._busy.locked():
            raise HTTPException(409, "The TV app is listening: try again in a moment")
        await svc.updates.check()
        try:
            return await svc.updates.install(key)
        except (RuntimeError, httpx.HTTPError, OSError) as e:
            raise HTTPException(502 if isinstance(e, httpx.HTTPError) else 409, str(e) or type(e).__name__)

    @r.post("/v1/updates/tv")
    async def update_tv(request: Request):
        return await install(request, "tv")

    @r.post("/v1/updates/tvplayer")
    async def update_player(request: Request):
        return await install(request, "tvplayer")

    return r
