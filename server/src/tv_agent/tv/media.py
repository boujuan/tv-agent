"""Transport and volume for whatever app is playing, through Android's media session service. Kodi playback is
controlled over its own API instead (kodi.py), which can seek to an exact time."""
import re

from .adb import Adb

KEYS = {"pause": "pause", "resume": "play", "stop": "stop", "forward": "fast-forward", "back": "rewind",
        "next": "next", "previous": "previous"}
MUSIC = "3"   # AudioManager.STREAM_MUSIC; on this TV its index runs 0..100


def dispatch(adb: Adb, op: str):
    adb.run(["cmd", "media_session", "dispatch", KEYS[op]])


def get_volume(adb: Adb) -> int | None:
    out = adb.run(["cmd", "media_session", "volume", "--stream", MUSIC, "--get"])
    m = re.search(r"volume is (\d+) in range", out)
    return int(m.group(1)) if m else None


def set_volume(adb: Adb, level: int):
    """Exact level with the TV's volume bar shown. Mute is level 0 plus a remembered level (state.py): the
    VOLUME_MUTE key only toggles, and KEYCODE_MUTE is the microphone's mute."""
    level = max(0, min(100, int(level)))
    adb.run(["cmd", "media_session", "volume", "--show", "--stream", MUSIC, "--set", str(level)])
