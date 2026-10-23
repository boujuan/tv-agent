"""Transport and volume for whatever app is playing, through Android's media session service. Kodi playback is
controlled over its own API instead (kodi.py), which can seek to an exact time."""
import re

from .adb import Adb

KEYS = {"pause": "pause", "resume": "play", "toggle": "play-pause", "stop": "stop", "forward": "fast-forward", "back": "rewind",
        "next": "next", "previous": "previous"}
MUSIC = "3"   # AudioManager.STREAM_MUSIC; on this TV its index runs 0..100


def dispatch(adb: Adb, op: str):
    adb.run(["cmd", "media_session", "dispatch", KEYS[op]])


def sessions(adb: Adb) -> list[tuple[str, bool, str]]:
    """(package, active, state) of each media session, e.g. ("tv.twitch.android.app", True, "PAUSED"). ~1 s."""
    out = adb.run(["dumpsys", "media_session"])
    found = []
    for block in re.split(r"\n(?=\s{4}\S.*\n\s{6}ownerPid)", out):
        pkg, act = re.search(r"package=(\S+)", block), re.search(r"active=(\w+)", block)
        st = re.search(r"state=PlaybackState \{state=(\w+)", block)
        if pkg:
            found.append((pkg.group(1), bool(act and act.group(1) == "true"), st.group(1) if st else ""))
    return found


def get_volume(adb: Adb) -> int | None:
    out = adb.run(["cmd", "media_session", "volume", "--stream", MUSIC, "--get"])
    m = re.search(r"volume is (\d+) in range", out)
    return int(m.group(1)) if m else None


def set_volume(adb: Adb, level: int, current: int | None = None):
    """Exact level, by pressing the volume keys the difference from now (one step each; the TV shows its own
    volume bar). Setting the level through Android's audio service is ignored on this TCL, at least while Kodi
    plays (found 2026-10-22). Mute is level 0 plus a remembered level (state.py): the VOLUME_MUTE key only
    toggles, reading the mute state takes a second, and KEYCODE_MUTE is the microphone's mute."""
    level = max(0, min(100, int(level)))
    if current is None:
        current = get_volume(adb)
    if current is None or current == level:
        return
    key = "KEYCODE_VOLUME_UP" if level > current else "KEYCODE_VOLUME_DOWN"
    adb.run(["cmd", "input", "keyevent", *([key] * abs(level - current))], timeout=20)
