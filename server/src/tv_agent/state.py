"""What the agent remembers between commands: the volume before mute, what it last played (for "next episode"
and progress sync), and per-stage timings for `tv-agent stats`."""
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class NowPlaying:
    imdb_id: str
    kind: str                 # "movie" or "series"
    video_id: str             # the movie id, or tt…:season:episode
    name: str
    player: str = "kodi"      # "tvplayer", "kodi" or "just"
    url: str = field(default="", repr=False)   # the debrid link (secret; memory only), to reopen it at a time
    runtime_ms: int = 0       # Cinemeta's runtime, for Just Player (its session doesn't give a duration)
    started: float = field(default_factory=time.time)
    filename: str = ""        # the file's name, size and hash: subtitle addons match on them
    size: int = 0
    video_hash: str = ""
    dolby_vision: bool = False      # Kodi jumps in it by reopening (agent.py, KODI_LOG's note)
    stopped_at: int | None = None   # the exact position when "stop" was said (polls are 10 s apart)
    stopped_mono: float = field(default=0.0, repr=False)    # and when (time.monotonic())


class Timings:
    def __init__(self, size=200):
        self._data: dict[str, deque] = defaultdict(lambda: deque(maxlen=size))

    def add(self, stage: str, ms: float):
        self._data[stage].append(ms)

    def summary(self) -> dict[str, dict[str, float]]:
        out = {}
        for stage, xs in self._data.items():
            s = sorted(xs)
            out[stage] = {"n": len(s), "p50": statistics.median(s), "p95": s[min(len(s) - 1, int(0.95 * len(s)))]}
        return out


@dataclass
class State:
    volume_before_mute: int | None = None
    volume_base: int | None = None     # the level before the pill lowered it (a volume command starts there)
    now_playing: NowPlaying | None = None
    timings: Timings = field(default_factory=Timings)
