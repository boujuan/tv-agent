"""The apps the agent may open, with the activities the TV resolved for them (cmd package resolve-activity,
2026-10-22). Only these packages ever appear in an `am start` the agent sends."""
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from .adb import Adb


@dataclass(frozen=True)
class App:
    key: str
    label: str
    package: str
    activity: str
    aliases: tuple[str, ...]

    @property
    def component(self) -> str:
        return f"{self.package}/{self.activity}"


APPS = {a.key: a for a in (
    App("tizentube", "TizenTube", "io.gh.reisxd.tizentube.cobalt", "dev.cobalt.app.MainActivity",
        ("youtube", "you tube", "tizentube", "tizen tube")),
    App("stremio", "Stremio", "com.stremio.one", "com.stremio.tv.MainActivity", ("stremio", "stream io")),
    App("kodi", "Kodi", "org.xbmc.kodi", ".Splash", ("kodi", "codey", "cody")),
    App("twitch", "Twitch", "tv.twitch.android.app", "tv.twitch.starshot64.app.StarshotActivity", ("twitch",)),
    App("movistar", "Movistar+", "com.movistarplus.androidtv", ".MainActivity",
        ("movistar", "movistar plus", "movistar tv")),
    App("prime", "Prime Video", "com.amazon.amazonvideo.livingroom", "com.amazon.ignition.IgnitionActivity",
        ("prime", "prime video", "amazon", "amazon prime")),
)}
PACKAGES = {a.package for a in APPS.values()}

# Intent flags: NEW_TASK | CLEAR_TOP (0x14000000), so a deep link replaces a stale screen instead of reusing it.
FRESH = "0x14000000"


def find(spoken: str) -> App | None:
    names = {alias: app for app in APPS.values() for alias in app.aliases}
    hit = process.extractOne(spoken.lower(), list(names), scorer=fuzz.ratio, score_cutoff=78)
    return names[hit[0]] if hit else None


def open_app(adb: Adb, app: App):
    adb.run(["am", "start", "-n", app.component, "-a", "android.intent.action.MAIN",
             "-c", "android.intent.category.LEANBACK_LAUNCHER"])


def view(adb: Adb, app: App, uri: str, fresh: bool = False):
    """Open a deep link in one allowlisted app."""
    argv = ["am", "start", "-a", "android.intent.action.VIEW", "-d", uri, "-n", app.component]
    if fresh:
        argv[2:2] = ["-f", FRESH]
    adb.run(argv)


def home(adb: Adb):
    adb.run(["input", "keyevent", "KEYCODE_HOME"])


def foreground_package(adb: Adb) -> str | None:
    out = adb.run(["dumpsys", "activity", "activities"], timeout=5)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(("topResumedActivity=", "mResumedActivity:", "ResumedActivity:")):
            for word in line.split():
                if "/" in word:
                    return word.split("/")[0].lstrip("{")
    return None
