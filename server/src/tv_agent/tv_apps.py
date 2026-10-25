"""The apps really on the TV: what its home screen lists (MAIN + LEANBACK_LAUNCHER), with their names and icons, for
the phone's remote and for voice ("open Netflix" for any app that's there, not only the rules' list).

Read from the TV app (AppsReceiver: Android's shell can't read app names or icons) and, from a TV app before it,
from `cmd package query-activities` with names from the rules' apps or the package. Kept in DATA_DIR/tv-apps/
(apps.json and one PNG per app), so it's there at once after a restart; read again at the start, every day, and
when a phone or the panel asks (POST /v1/apps/refresh).

The rules' apps stay as written (their names, aliases and hooks); an app on the TV that isn't one of them joins
the list voice and the models use (Rules.apps) under its own name, and leaves it when it's gone from the TV. The
rules file itself is never changed. Only apps the TV's home screen lists can be opened this way, by the activity
it gave.
"""
import asyncio
import json
import logging
import re
import time
import unicodedata
from pathlib import Path

from .config import DATA_DIR
from .decide import rules as R
from .tv.adb import TvUnreachable
from .tv.status import APP

log = logging.getLogger("tv_agent")

DIR = DATA_DIR / "tv-apps"
ON_TV = f"/sdcard/Android/data/{APP}/files/apps"
RECEIVER = f"{APP}/.AppsReceiver"
EVERY = 24 * 3600            # read again every day
FIRST = 20                   # seconds after the start (the TV connected, the first commands answered)
MAX_APPS = 80
MAX_ICON = 200_000           # bytes: a 96 px PNG is a few KB
PNG = b"\x89PNG\r\n\x1a\n"
QUERY = ["cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
         "-c", "android.intent.category.LEANBACK_LAUNCHER"]


def key_for(label: str, package: str, taken: set[str]) -> str:
    """A rules-style key from the app's name ("Movistar+" -> "movistar"), unique among `taken`."""
    plain = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "_", plain).strip("_")[:24] or re.sub(r"\W+", "_", package.split(".")[-1].lower())[:24]
    key, n = base or "app", 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


def aliases_for(label: str) -> tuple[str, ...]:
    """How it may be said: its name, and its name without punctuation or a trailing "TV" ("Pluto TV" -> "pluto")."""
    lower = label.strip().lower()
    plain = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", lower)).strip()
    short = re.sub(r"\s+(?:android\s+)?tv$", "", plain).strip()
    return tuple(dict.fromkeys(a for a in (lower, plain, short) if a))


def parse_query(out: str) -> list[tuple[str, str]]:
    """`cmd package query-activities --brief`'s lines ("  com.stremio.one/com.stremio.tv.MainActivity") as
    (package, activity), each package once, in the TV's order."""
    seen, apps = set(), []
    for line in out.splitlines():
        m = re.fullmatch(r"\s*([\w.]+)/([\w.$]+)\s*", line)
        if not m or m.group(1) in seen:
            continue
        package, activity = m.group(1), m.group(2)
        if activity.startswith("."):
            activity = package + activity
        if R.PACKAGE.match(package) and R.ACTIVITY.match(activity):
            seen.add(package)
            apps.append((package, activity))
    return apps


class TvApps:
    def __init__(self, svc, folder: Path | None = None):
        self.svc = svc
        self.dir = folder or DIR
        self.apps: list[dict] = []          # [{"package", "activity", "label", "icon": bool}], the TV's order
        self.checked: float | None = None   # when they were read (time.time()); None: never
        self.source = ""                    # "tv app" (names and icons) or "adb" (packages only)
        self.error: str | None = None
        self._merged: dict[str, str] = {}   # key -> package added to the rules' apps (taken out before a merge)
        self._lock = asyncio.Lock()
        self._load()

    # ---- the cache ----

    def _load(self):
        try:
            data = json.loads((self.dir / "apps.json").read_text())
            self.apps = [a for a in data.get("apps", []) if isinstance(a, dict) and a.get("package")]
            self.checked, self.source = data.get("checked"), data.get("source", "")
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "apps.json.tmp"
        tmp.write_text(json.dumps({"apps": self.apps, "checked": self.checked, "source": self.source}))
        tmp.replace(self.dir / "apps.json")

    def icon(self, package: str) -> bytes | None:
        if not R.PACKAGE.match(package or ""):
            return None
        try:
            data = (self.dir / f"{package}.png").read_bytes()
        except OSError:
            return None
        return data if data.startswith(PNG) else None

    def installed(self) -> set[str] | None:
        """The packages on the TV; None while they've never been read (then nothing is left out)."""
        return {a["package"] for a in self.apps} if self.checked else None

    # ---- reading the TV ----

    async def refresh(self) -> dict:
        """Read the TV's apps now: from the TV app (names, icons), else over ADB (packages only)."""
        async with self._lock:
            agent = self.svc.agent
            try:
                out = await agent.tv(lambda adb: adb.run(["am", "broadcast", "-n", RECEIVER], timeout=30))
                if "result=-1" in out:
                    apps = await self._from_tv_app()
                    self.source = "tv app"
                else:                                    # a TV app from before AppsReceiver
                    apps = await self._from_adb()
                    self.source = "adb"
            except (TvUnreachable, OSError, RuntimeError, ValueError) as e:
                self.error = f"{type(e).__name__}: {e}"[:200]
                log.info("TV apps: %s", self.error)
                return self.state()
            self.apps, self.checked, self.error = apps[:MAX_APPS], time.time(), None
            self._save()
            self.merge(agent.rules)
            log.info("TV apps: %d (%s)", len(self.apps), self.source)
            return self.state()

    async def _from_tv_app(self) -> list[dict]:
        agent = self.svc.agent
        raw = json.loads(await agent.tv(lambda adb: adb.run(["cat", f"{ON_TV}/apps.json"], timeout=10)))
        apps = []
        self.dir.mkdir(parents=True, exist_ok=True)
        for a in raw if isinstance(raw, list) else []:
            package, activity, label = str(a.get("package", "")), str(a.get("activity", "")), str(a.get("label", ""))
            if not (R.PACKAGE.match(package) and R.ACTIVITY.match(activity)):
                continue
            has_icon = False
            if a.get("icon") == f"{package}.png":
                data = await agent.tv(lambda adb, p=package: adb.run_bytes(["cat", f"{ON_TV}/{p}.png"], timeout=10))
                if data.startswith(PNG) and len(data) <= MAX_ICON:
                    (self.dir / f"{package}.png").write_bytes(data)
                    has_icon = True
            apps.append({"package": package, "activity": activity, "label": label.strip()[:60] or package,
                         "icon": has_icon})
        return apps

    async def _from_adb(self) -> list[dict]:
        out = await self.svc.agent.tv(lambda adb: adb.run(QUERY, timeout=15))
        known = {a.package: a.label for a in self.svc.agent.rules.apps.values()}
        before = {a["package"]: a for a in self.apps}
        return [{"package": p, "activity": act,
                 "label": known.get(p) or before.get(p, {}).get("label") or p.split(".")[-1].capitalize(),
                 "icon": (self.dir / f"{p}.png").is_file()} for p, act in parse_query(out)]

    # ---- the list voice and the models use ----

    def merge(self, rules) -> None:
        """The TV's apps that aren't among the rules' (by package) join rules.apps; those added before and gone from
        the TV leave. The agent's own listening app isn't one to open."""
        for key, package in self._merged.items():
            if (app := rules.apps.get(key)) is not None and app.package == package:     # never one the rules have
                del rules.apps[key]
        self._merged = {}
        if not self.checked:
            return
        packages = {a.package for a in rules.apps.values()}
        for a in self.apps:
            if a["package"] in packages or a["package"] == APP:
                continue
            aliases = aliases_for(a["label"])
            if not aliases:
                continue
            key = key_for(a["label"], a["package"], set(rules.apps))
            rules.apps[key] = R.App(key, a["label"], a["package"], a["activity"], aliases)
            self._merged[key] = a["package"]

    def state(self) -> dict:
        return {"checked": self.checked, "source": self.source, "error": self.error, "count": len(self.apps)}


async def loop(svc):
    """Read at the start, then every day."""
    await asyncio.sleep(FIRST)
    while True:
        await svc.tv_apps.refresh()
        await asyncio.sleep(EVERY)
