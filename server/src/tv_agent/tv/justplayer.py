"""Just Player (com.brouken.player, ExoPlayer/Media3) as the player instead of Kodi.

It has no remote API. The agent starts it with a VIEW intent (link, title, start position), reads where it is
from Android's media session, and moves it with media keys (+15 s forward, -5 s back per press, measured) or, for
long jumps, by reopening the same link at the target time. Audio follows the TV's language (English); subtitles
follow Android's system captions setting, which the agent sets per title before starting.
"""
import math
import re

from .adb import Adb

PACKAGE = "com.brouken.player"
ACTIVITY = f"{PACKAGE}/.PlayerActivity"
FORWARD_STEP_S, BACK_STEP_S = 15, 5
# The media session's position is as of its last update; nowELAPSED (same clock) brings it up to date.
STATUS = "dumpsys media_session; dumpsys alarm | grep -m1 -oE 'nowELAPSED=[0-9]+'"


def start(adb: Adb, url: str, title: str, position_ms: int = 0):
    """-S stops Just Player first: an open player ignores a new start position for the link it already has."""
    adb.run(["am", "start", "-S", "-a", "android.intent.action.VIEW", "-d", url, "-t", "video/*",
             "--ei", "position", str(max(0, int(position_ms))), "--es", "title", title, "-n", ACTIVITY])


def captions(adb: Adb, on: bool, language: str | None = None):
    """Android's system captions switch (Just Player shows subtitles in that language when it's on)."""
    adb.run(["settings", "put", "secure", "accessibility_captioning_enabled", "1" if on else "0"])
    if on and language:
        adb.run(["settings", "put", "secure", "accessibility_captioning_locale", language])


def captions_on(adb: Adb) -> bool:
    return adb.run(["settings", "get", "secure", "accessibility_captioning_enabled"]).strip() == "1"


def status(adb: Adb) -> tuple[str, int, float] | None:
    """(state, position ms now, speed) of Just Player's media session, or None if it isn't open."""
    out = adb.run(["sh", "-c", STATUS], timeout=8)
    parts = out.split(f"package={PACKAGE}")
    if len(parts) < 2:
        return None
    m = re.search(r"state=PlaybackState \{state=(\w+)\(\d+\), position=(\d+), buffered position=\d+, "
                  r"speed=([\d.]+), updated=(\d+)", parts[1])
    now = re.search(r"nowELAPSED=(\d+)", out)
    if not m:
        return None
    state, pos, speed, updated = m.group(1), int(m.group(2)), float(m.group(3)), int(m.group(4))
    if state == "PLAYING" and now:
        pos += int((int(now.group(1)) - updated) * speed)
    return state, pos, speed


def presses(seconds: int, forward: bool) -> int:
    return math.ceil(seconds / (FORWARD_STEP_S if forward else BACK_STEP_S))
