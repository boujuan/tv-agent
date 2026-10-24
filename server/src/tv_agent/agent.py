"""Executes actions on the TV. One Agent per process: it owns the ADB connection, the HTTP client and the state."""
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from rapidfuzz import fuzz

from . import __version__, activity, languages, skips, twitch, youtube
from . import chain, episode_check, finding, subsync, tracing
from .actions import (Action, AutoSkip, Binge, Cancel, ContinueWatching, FindScene, Help, Home, Info, LibraryEdit,
                      NextEpisode, OpenApp,
                      OpenInStremio, Pick, PlayTitle, Recommend, Replay, Sequence, ShowList, Skip, Timer, Tracks,
                      Transport, Twitch, Unclear, Volume, Wrong, YouTube)
from .commands import LEGEND, SECTIONS, TV_HINT
from .config import CONFIG_DIR, Config, read_secret, set_value
from .decide import rules as rules_mod
from .decide import text as T
from .decide.d1 import D1
from .decide.llm import Llm
from .state import NowPlaying, State
from .stremio import cinemeta, progress, streams, subtitles, watched
from .stremio import search as title_search
from .stremio.account import Account, Addon, now_iso
from .stremio import library as library_mod
from .stremio.library import Library
from .tv import apps, justplayer, media, tvplayer
from .tv.adb import Adb
from .tv.kodi import Kodi, KodiError
from .tv.tvplayer import TvPlayer

log = logging.getLogger(__name__)
SAY_RECEIVER = "com.boujuan.tvagent/.SayReceiver"
CHOICES = "com.boujuan.tvagent/.ChoicesActivity"
# Words that say nothing about which episode it is
EPISODE_STOPWORDS = {"the", "and", "episode", "where", "when", "with", "that", "one", "gets", "get", "who", "what", "his",
                     "her", "their", "from", "into", "out", "about", "there", "this", "season", "part"}
OWN_APP = "com.boujuan.tvagent"          # the TV app: its pill covers the app the commands are for
KODI = "org.xbmc.kodi"
# Kodi's player can hang inside the TV's Dolby Vision decoder (MediaCodec) while opening a file, and then takes every
# later file without playing it until Kodi is restarted: its log stops mid-line (found 2026-10-23). A file that
# stops at 0:00 this soon after opening, with nothing in that log since, gets Kodi restarted and opened again, once.
KODI_LOG = "/sdcard/Android/data/org.xbmc.kodi/files/.kodi/temp/kodi.log"
STUCK_WITHIN = 45
KODI_FREE = 2.0             # seconds after Kodi's player is gone before the next file opens (its decoder is let go)
# A seek flushes the decoder, which this TV's Dolby Vision decoder can fail ("Reset: flush failed": black picture, or
# the time running to the end; TCL sets with Kodi 21 and 22, github.com/xbmc/xbmc/issues/25692). Jumps in a Dolby
# Vision file, or while Kodi is behind another screen (no window for the decoder), reopen it at the time instead:
# a new decoder holds nothing to flush. A picture lost anyway shows as Kodi logging decoder failures nonstop.
PICTURE_EVERY = 15
DECODER_FAILED = re.compile(r"dequeue(Output|Input)Buffer failed|Reset: flush failed")
DUCK = 0.4                  # TV Agent Player's own volume while the pill listens
SKIPS_WHERE = "Skipping works in TV Agent Player and Kodi, for what the agent started"
# "Skip the intro" with no times known for it (no TheIntroDB/IntroDB entry, no chapter): about an opening's length
# ahead, instead of nothing (3 of the 190 voice tests, 2026-10-24)
INTRO_GUESS_MS = 90_000
PRELOAD_BEFORE = 150_000    # the next episode opens in the background this long before the end (binge)
LIST_SIZE = 6                # rows that fit the TV app's list
PLOTS_AHEAD = 8             # the shows of Continue Watching whose episode plots are kept ahead (finding.py)
STARTS = {"play_title", "continue_watching", "next_episode", "pick"}     # actions that start playback
LIST_TTL = 300              # "number 2" refers to a list shown in the last 5 minutes
STREAMS_TTL = 1800          # an addon's answer for a video is reused for 30 minutes (links resolve when played)
NOW_PLAYING_FILE = CONFIG_DIR / "now-playing.json"
WARM_ITEMS = 2              # Continue Watching's first items: their streams looked up when the mic opens
EXPECT_AT_MOST = 2          # stream lookups started on a guess while a title is still being decided
# A leader this far ahead once popularity has been weighed, said exactly, is what d1 picked every time it was asked
# (2026-10-24: Oppenheimer 19.6 ahead of the 1980 series, d1 0.97; Shogun 18.2, d1 0.22, the same pick)
CERTAIN_GAP = 15
SUB_SYNC_MIN_MS = 100      # an addon's subtitles nearer the file's time than this are left as they are
CHECK_AFTER = 5.0           # the episode check waits this long after the file opens: the start has the network first
ERROR_SHOWN = 3.0           # a start that fails says why on the loading screen this long before going Home
HEARD: ContextVar[str] = ContextVar("heard", default="")    # what the command being run was, as transcribed
# What a start command did, for the activity history (execute() adds it to the outcome's details): its steps after
# the decision, in order (`trace`: finding the title, then starting it), the title picked, the stages' times and the
# stream played. Never a link.
START: ContextVar[dict | None] = ContextVar("start", default=None)
# A title d1 picked with less confidence than this, or that it couldn't be asked about (unreachable, off), isn't
# played: its candidates are listed to choose from by number. Every d1 title choice in the history before this was
# wrong (2026-10-24: Dune 2000 at 0.14 for "Dune episode 2", Dune: Prophecy 0.25, "Paul" 0, Can't Buy Me Love 0.27
# for "watch eBuy in 2023", The Moment 0.31), and with d1 unreachable the search's best match was wrong for 10 of 16
# misheard or vague names (Rebel Without a Cause for "rebel mung", Till Death Us Do Part for "do part two").
TITLE_SURE = 0.4
# While a title is found for a play: imdb id -> the candidates of a choice that wasn't sure enough (TITLE_SURE), and
# "by": how the title was picked in the end (resolve)
UNSURE: ContextVar[dict | None] = ContextVar("unsure", default=None)
FIRST_FRAME_WAIT = 10.0     # how long a start waits for TV Agent Player's first frame, to report it
ASKED_FOR = 12 * 3600       # audio or subtitles asked for a title by voice hold for it (its next episodes) this long
PROBE_AT_MOST = 4           # copies whose tracks are read at once when none says it has the language asked for


def _note_title(**fields):
    """Into the record of the start being made (START): the title it found, and how."""
    if (record := START.get()) is not None and "_t0" in record:
        record.setdefault("title", {}).update(fields)


def _step(stage: str, step: str, ms: float | None = None, done: float | None = None, **fields):
    """One step of the command, into its trace (tracing.py), as the panel draws it: what ran, what it found, how long
    it took (`ms`) and when it ended (`done`, if not now). A start made later in the background (the next episode,
    another copy) runs in the context of the command before, whose trace is recorded: nothing is added to it."""
    tracing.step(stage, step, ms=ms, done=done, **fields)


def _ms(since: float) -> int:
    return round((time.perf_counter() - since) * 1000)


@dataclass
class Outcome:
    ok: bool
    say: str
    details: dict | None = None     # how it went, for the activity history (never a link)


@dataclass(frozen=True)
class ListItem:
    imdb: str
    kind: str
    name: str
    label: str                # "The Bear · S3E2"
    detail: str               # "12 min left", "2 days ago · Doors"
    poster: str = ""
    video: str | None = None  # the episode to play; None: carry on where you were (or the film)
    ask: PlayTitle | None = None    # a title asked about (not sure which): played as that request asked
    at_ms: int = 0                  # where in it (a scene found in another episode than the one playing)
    then: tuple = ()                # a chain's steps after its play, done once this is picked


@dataclass(frozen=True)
class Shown:
    title: str
    items: list[ListItem]
    at: float


class DryAdb:
    """Stands in for Adb with --dry-run: prints each command instead of sending it."""
    host = "dry-run"

    def __init__(self):
        self.sent: list[list[str]] = []

    def run(self, argv, timeout=10.0):
        self.sent.append(list(argv))
        print("TV$", " ".join(argv))
        return ""

    def keepalive(self):
        pass

    def close(self):
        pass

    reconnect = close


def load_failed_text(msg: dict) -> str:
    """What to say on the TV when TV Agent Player can't read a file (its LOAD_FAILED event)."""
    cause, http = str(msg.get("cause") or ""), msg.get("http")
    if cause == "UnknownHostException":
        return "The TV can't look up this file's server: check its internet (DNS)"
    if http:
        return f"The file's server refused it (HTTP {http})"
    if "Timeout" in cause or "Connect" in cause:
        return "The TV can't reach this file's server"
    return "The TV is having trouble loading this file"


def _watched_some(item: dict) -> bool:
    """A library item you've watched (some of): not only added to your list."""
    st = item.get("state") or {}
    return bool(st.get("timeWatched") or st.get("timesWatched") or st.get("flaggedWatched") or st.get("video_id"))


def _clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _runtime_min(meta: dict) -> int | None:
    """Cinemeta's runtime ("99 min", "1h 39min", "2h") in minutes."""
    text = str(meta.get("runtime") or "")
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m(?:in)?", text)
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0) or None


def _ago(when: datetime, now: datetime) -> str:
    days = (now.date() - when.date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 14:
        return f"{days} days ago"
    return f"{days // 7} weeks ago" if days < 60 else when.strftime("%-d %b")


def _episode_label(name: str, v: dict) -> str:
    return f"{name} · S{v['season']}E{v.get('episode', 0)}"


def _stream_line(s: streams.Stream) -> dict:
    """A stream as the trace tells it: what it is and where from, never its link."""
    line = {"addon": s.addon, "name": " ".join(s.name.split())[:80], "file": s.filename or None,
            "height": s.height or None, "gb": round(s.size / 1e9, 1) if s.size else None,
            "release": streams.release(s) or None, "cached": s.cached, "in_library": s.library or None,
            "score": s.aio_score or None}
    return {k: v for k, v in line.items() if v is not None}


def _title_screen(name: str, meta: dict, video: str) -> dict:
    """What TV Agent Player's loading and pause screens say about a title (its PROTOCOL.md, `loading` and `info`):
    the name; year and runtime for a film, "S4E1 · <episode name>" for an episode; the plot; Cinemeta's artwork,
    which is public (metahub), never a stream link."""
    v = next((x for x in cinemeta.episodes(meta) if x["id"] == video), None)
    if v is not None:
        subtitle = " · ".join(x for x in (f"S{v['season']}E{v.get('episode', 0)}", v.get("name") or v.get("title"))
                              if x)
        plot = v.get("overview") or v.get("description") or meta.get("description")
    else:
        minutes = _runtime_min(meta)
        runtime = (f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min") if minutes else ""
        subtitle = " · ".join(x for x in (str(meta.get("year") or meta.get("releaseInfo") or ""), runtime) if x)
        plot = meta.get("description")
    return {"title": name, "subtitle": subtitle, "plot": plot or "", "poster": meta.get("poster") or "",
            "backdrop": meta.get("background") or "", "logo": meta.get("logo") or ""}


def _stream_screen(s: streams.Stream) -> dict:
    """What the pause screen says about the file: the addon that found it, its description without links, the
    debrid service, size, release group, and the badges the file's header can't tell the player."""
    return {"source": s.source or s.addon, "description": streams.description(s), "debrid": streams.debrid(s),
            "size": s.size, "release": streams.release(s), "badges": streams.badges(s)}


class Loading:
    """TV Agent Player's loading screen for one start: shown once the player's screen is up, then each step. A player
    without it (0.1.x) answers "unknown command" once and isn't sent it again (tvplayer.loading)."""

    def __init__(self, player: TvPlayer, video: str, screen: dict, ready: asyncio.Task, step: str):
        self.player, self.video, self.step, self.shown = player, video, step, False
        self.screen = {k: v for k, v in screen.items() if k != "plot"}
        self.then = f" · then {plan}" if (plan := chain.PLAN.get()) else ""     # a chain's next steps
        self.first = asyncio.create_task(self._show(ready))

    async def _show(self, ready: asyncio.Task):
        try:
            if await asyncio.shield(ready):
                self.shown = self.player.loading(self.video, **self.screen, step=self.step + self.then)
        except Exception as e:                  # the start goes on without it
            log.info("loading screen: %s", type(e).__name__)

    def at(self, step: str):
        if step == self.step:
            return
        self.step = step
        if self.shown:
            try:
                self.player.loading(self.video, **self.screen, step=step + self.then)
            except Exception as e:
                log.info("loading screen: %s", type(e).__name__)


class Agent:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.adb = DryAdb() if dry_run else Adb(cfg)
        self.state = State()
        self.http = httpx.AsyncClient(headers={"User-Agent": f"tv-agent/{__version__}"}, follow_redirects=True)
        key = cfg.secret("stremio_auth_key")
        self.account = Account(self.http, key) if key else None
        self.library = Library(self.account)
        self.kodi = Kodi(self.http, self.adb, cfg.kodi_port, cfg.kodi_user, cfg.secret("kodi_password"))
        # TV Agent Player connects to this agent (server.py listens for it); its token is read at each connection
        self.tvplayer = TvPlayer(lambda: read_secret("player_token"), self._tv_addresses, __version__)
        self.tvplayer.listeners.append(self._player_event)
        self.tvplayer.listeners.append(self._left_loading)
        self._left = asyncio.Event()             # the start's loading screen was closed (Back, Home)
        self.d1 = D1(self.http, cfg.d1_url, cfg.secret("liquid_api_key"), cfg.hosted_d1_model, cfg.d1_where,
                     cfg.secret("openrouter_api_key"))
        self.rules, self.rules_error = rules_mod.load_safe()
        if self.rules_error:
            log.error("rules: %s; using the defaults", self.rules_error)
        key = cfg.secret("openrouter_api_key")
        # Made whenever there's a key: whether it's used is the Decisions tab's on/off (cfg.decide_off), live
        self.llm = Llm(self.http, key, cfg.llm_model, self.rules, cfg.llm_reasoning, cfg.llm_private) if key else None
        self._addons: list[Addon] = []
        self._addons_at = 0.0
        self._progress: asyncio.Task | None = None
        self._follow_stop: asyncio.Event | None = None     # tells the follower something else is about to play
        self._tracks_task: asyncio.Task | None = None
        self._duration: asyncio.Task | None = None
        self._prefetch: asyncio.Task | None = None
        self._end_task: asyncio.Task | None = None
        self._next_up: tuple[str, str, streams.Stream, float] | None = None   # (video id, label, stream, when)
        self._after_this: str | None = None      # "stop" / "off" when what's playing ends (sleep timer)
        self._sleep: asyncio.Task | None = None   # "turn off in 30 minutes"
        self._shown: Shown | None = None         # the list on the TV, for "number 2"
        self._streams: dict[tuple[str, str], tuple[float, list[streams.Stream]]] = {}
        self._stream_jobs: dict[tuple[str, str], asyncio.Task] = {}
        self._segments: dict[tuple[str, int], list[skips.Segment]] = {}
        self._skip_task: asyncio.Task | None = None
        self._starting = asyncio.Lock()          # one start at a time replaces the previous session
        self._user_stopped = False               # a stop that was asked for isn't the end of an episode
        self._again: tuple[NowPlaying, float, object] | None = None    # the last Kodi start: (session, when, redo)
        self._reopen: tuple[NowPlaying, object] | None = None    # that start again at a time: (session, fn(ms))
        self._picture_task: asyncio.Task | None = None
        self.checks = episode_check.Checks()     # which files are which episode (episode_check.py)
        self._episode_cues: dict[tuple[str, str], list[tuple[int, str]]] = {}   # (video, lang): subtitles by id
        # (file name, size, the subtitle's link hashed): ms the addon's subtitles run later than that file, None when
        # it couldn't be told (subsync.py); kept so the next play of the file sends it with the subtitles at once
        self._sub_shifts: dict[tuple[str, int, str], int | None] = {}
        self._verify_task: asyncio.Task | None = None
        self._duck: asyncio.Task | None = None
        self._ducked: tuple[str, int] | None = None   # ("kodi", "tvplayer" or "tv", the volume before listening)
        self._preload_task: asyncio.Task | None = None
        self._preloaded: tuple[str, str, str] | None = None   # (video id, stream url, link) open in TV Agent Player
        self._fallback: tuple[NowPlaying, list[streams.Stream], tuple, float] | None = None   # another file, if it fails
        self._closing: asyncio.Task | None = None     # a failed start's loading screen, going Home in a moment
        self._asked: dict[str, tuple[float, dict]] = {}   # per title: (when, the audio / subtitles asked by voice)
        self._originals: dict[str, str] = {}     # per title: the language its files mark as the original audio
        self._probed: dict[tuple[str, int], tuple[set[str], set[str]] | None] = {}   # files' track languages
        self.chain: chain.Run | None = None           # the chain running, or the last one (chain.py)

    async def close(self):
        if self._progress and not self._progress.done():
            self._stop_following()                   # its last progress write, then it ends
            try:
                await asyncio.wait_for(asyncio.shield(self._progress), 5)
            except (asyncio.TimeoutError, Exception):
                self._progress.cancel()
        await self.http.aclose()
        await self.tvplayer.thumbs.close()
        self.adb.close()

    async def tv(self, fn, *args):
        """Run a blocking TV call (adb_shell is synchronous) off the event loop."""
        return await asyncio.to_thread(fn, self.adb, *args)

    def _tv_addresses(self) -> set[str]:
        return set(self.cfg.tv_hosts) | ({self.adb.host} if self.adb.host else set())

    def _ctl(self, pid: int | str) -> Kodi | TvPlayer:
        """The player a player id belongs to: TV Agent Player's (tvplayer.PID) or Kodi's (a number). Both have the
        same surface (kodi.py)."""
        return self.tvplayer if pid == tvplayer.PID else self.kodi

    def _player_event(self, msg: dict):
        """What TV Agent Player tells about the file this agent opened in it: the exact position it closed at (the
        progress write uses it), the remote's next/previous, and a file that fails soon after opening (another copy
        takes over)."""
        np, t = self.state.now_playing, msg.get("t")
        if not np or np.player != "tvplayer":
            return
        if t == "key" and msg.get("key") in ("next", "previous"):
            asyncio.create_task(self._next_episode(NextEpisode(offset=1 if msg["key"] == "next" else -1)))
            return
        if msg.get("item") != self.tvplayer.item:
            return
        if t == "closed":
            np.stopped_at, np.stopped_mono = int(msg.get("pos") or 0), time.monotonic()
        elif t == "error" and msg.get("fatal"):
            log.warning("TV Agent Player: %s%s playing %s", msg.get("code"), f" (HTTP {msg['http']})" if msg.get("http")
                        else "", np.video_id)
            asyncio.create_task(self._try_another(np, msg))
        elif t == "error" and msg.get("code") == "LOAD_FAILED":
            # The player retries for a while before giving up: say why now (2026-10-24: a DNS setting on the TV left
            # every name unresolvable, and the start just sat buffering).
            log.warning("TV Agent Player can't read %s: %s%s", np.video_id, msg.get("cause"),
                        f" (HTTP {msg['http']})" if msg.get("http") else "")
            asyncio.create_task(self.notify(load_failed_text(msg)))

    def _left_loading(self, msg: dict):
        """TV Agent Player 0.6.2 and later: Back or Home on a start's loading screen sends "closed" for the item it
        was loading, at 0 (PROTOCOL.md). That start stops at once (_unless_left)."""
        coming = getattr(self.tvplayer, "_coming", None)
        if msg.get("t") == "closed" and not msg.get("pos") and coming and msg.get("item") == coming[1]:
            self._left.set()

    async def _unless_left(self, start) -> Outcome:
        """A start, unless you leave its loading screen meanwhile: then it stops there, its stream search and all,
        instead of loading into a player that's gone."""
        self._left.clear()
        task = asyncio.ensure_future(start)
        left = asyncio.ensure_future(self._left.wait())
        try:
            await asyncio.wait({task, left}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            raise
        finally:
            left.cancel()
        if task.done():
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        log.info("start stopped: its loading screen was closed")
        return Outcome(False, "Stopped: the player was closed before it started")

    async def _wake(self):
        """End the screensaver. A command from the laptop (panel, MCP, CLI) presses no key, and an app started
        behind Aerial Views stays hidden and gets frozen by Android (Kodi's playback died that way)."""
        try:
            await self.tv(lambda adb: adb.run(["cmd", "input", "keyevent", "KEYCODE_WAKEUP"], timeout=4))
        except Exception as e:
            log.info("waking the TV: %s", type(e).__name__)

    async def notify(self, text: str):
        """A short message on the TV screen (the app's SayReceiver; ignored if the app isn't installed)."""
        try:
            await self.tv(lambda adb: adb.run(["am", "broadcast", "-n", SAY_RECEIVER, "--es", "text", text[:120]],
                                              timeout=4))
        except Exception as e:
            log.debug("notify failed: %s", e)

    def llm_on(self) -> Llm | None:
        """The LLM, when there's a key and its step is on (the Decisions tab)."""
        return self.llm if self.llm is not None and "llm" not in self.cfg.decide_off else None

    async def execute(self, action: Action, heard: str = "") -> Outcome:
        """`heard`: the command as transcribed, for the loading screen and the title choice."""
        handler = getattr(self, "_" + action.type.replace("none", "unclear"))
        HEARD.set(heard)
        record = {"_t0": time.perf_counter()} if action.type in STARTS else None
        START.set(record)
        if action.type in STARTS and self._closing is not None:
            self._closing.cancel()              # the screen stays up for this start
        if action.type not in ("volume", "none", "binge", "timer", "auto_skip", "wrong") and not self.dry_run:
            asyncio.create_task(self._wake())
        try:
            if action.type in STARTS and getattr(self.cfg, "player", "") == "tvplayer" and not self.dry_run:
                out = await self._unless_left(handler(action))
            else:
                out = await handler(action)
        except Exception as e:
            log.exception("action %s failed", action.type)
            out = Outcome(False, f"That didn't work ({type(e).__name__})")
        if record is not None:
            record.pop("_t0")
            out.details = {**record, **(out.details or {})}
        return out

    async def trace_title(self, a: PlayTitle, heard: str) -> tuple[tuple[str, str, str] | None, list[dict]]:
        """Finding the title of a play request as a start would (nothing is looked up for streams, nothing reaches
        the TV), with its trace: for the panel's Explain."""
        HEARD.set(heard)
        START.set({"_t0": time.perf_counter()})
        trace = tracing.Trace()
        token = tracing.TRACE.set(trace)
        try:
            found = await self.resolve(a.title, a.kind, a.season, exact=a.guess) if a.title else None
        finally:
            tracing.TRACE.reset(token)
        return found, trace.close()[0]

    # ---- Stremio: addons, titles, streams ----

    async def addons(self, max_age: float = 3600) -> list[Addon]:
        """Your installed Stremio addons, refreshed hourly. Their URLs (with addon config) stay in memory."""
        if not self.account:
            return []
        if not self._addons or time.monotonic() - self._addons_at > max_age:
            self._addons = await self.account.addons()
            self._addons_at = time.monotonic()
        return self._addons

    async def warm(self):
        """Refresh what a play needs before it's asked for (your addons, your library: ~0.4 s each when stale), and
        look up the streams of what Continue Watching would play first (3-6 s each, kept 30 minutes). Run when the mic
        opens and at startup."""
        try:
            await self.addons(max_age=3000)
            if time.monotonic() - self.library._at > 480:
                await self.library.items(fresh=True)
            watching = await self.library.continue_watching()
            for item in watching[:WARM_ITEMS]:
                target = await self._continue_target(item)
                if not isinstance(target, Outcome):
                    self.prefetch(item["type"], target[0])
            if not self.dry_run:                 # "the episode where…" of a show you're watching: its plots kept
                finding.prefetch_plots(self.http, [(i["_id"], i.get("name", "")) for i in watching
                                                   if i.get("type") == "series"][:PLOTS_AHEAD])
        except Exception as e:
            log.info("warming up: %s", type(e).__name__)

    def _fits(self, meta: dict | None) -> Callable[[list[streams.Stream]], bool]:
        """Whether an addon's answer is enough to start without the slower ones: a stream your rules keep, at the
        best resolution they allow (and in the right language, when the title is known)."""
        def fits(found: list[streams.Stream]) -> bool:
            plan = self.track_plan(meta) if meta else None
            ranked = streams.rank([replace(s) for s in found], self.cfg.max_height, self.cfg.max_mbps,
                                  _runtime_min(meta or {}), plan.audio if plan else "eng")
            return bool(ranked) and not ranked[0].why_dropped and ranked[0].height >= min(self.cfg.max_height, 2160) \
                and (plan is None or plan.audio is None or ranked[0].rank_key[1] == 2)
        return fits

    async def fetch_streams(self, kind: str, video_id: str, meta: dict | None = None) -> list[streams.Stream]:
        """Your addons' streams for a video (3-6 s from AIOStreams), reused for 30 minutes, with one lookup in
        flight per video: a lookup started early (prefetch) is the one the play then waits for. It doesn't wait for
        a slow addon once one has answered with a stream that fits (_fits)."""
        key = (kind, video_id)
        if (hit := self._streams.get(key)) and time.monotonic() - hit[0] < STREAMS_TTL:
            return [replace(s) for s in hit[1]]       # copies: ranking marks them
        job = self._stream_jobs.get(key)
        if job is None or (job.done() and (job.cancelled() or job.exception())):
            job = self._stream_jobs[key] = asyncio.create_task(
                streams.fetch(self.http, await self.addons(), kind, video_id, enough=self._fits(meta)))
        found = await asyncio.shield(job)                  # a replaced command doesn't waste the lookup
        self._stream_jobs.pop(key, None)
        if original := streams.original_language(found):
            self._originals[video_id.split(":")[0]] = original
        if found:
            if len(self._streams) > 40:
                self._streams.pop(min(self._streams, key=lambda k: self._streams[k][0]))
            self._streams[key] = (time.monotonic(), found)
        return [replace(s) for s in found]

    def prefetch(self, kind: str, video_id: str):
        """Start the stream lookup for a video about to be played, while the rest (metadata, Kodi) gets ready."""
        if not (self.account and not self.dry_run):
            return

        async def run():
            try:
                await self.fetch_streams(kind, video_id)
            except Exception as e:
                log.info("stream prefetch for %s: %s", video_id, type(e).__name__)
        asyncio.create_task(run())

    def track_plan(self, meta: dict) -> languages.TrackPlan:
        """Your language settings for this title, with the audio and subtitles asked for it by voice lately."""
        c, imdb = self.cfg, str(meta.get("id") or meta.get("imdb_id") or "")
        asked = self._asked_for(imdb)
        plan = languages.plan(meta, c.audio_language, c.subtitle_languages, c.native_language, c.subtitles,
                              self._originals.get(imdb), asked.get("audio"))
        if "subtitles" in asked:                # asked for: shown whatever the audio
            plan = replace(plan, subtitles=asked["subtitles"], unless=None)
        return plan

    def _asked_for(self, imdb: str) -> dict:
        hit = self._asked.get(imdb)
        return hit[1] if hit and time.time() - hit[0] < ASKED_FOR else {}

    def _ask(self, imdb: str, **fields):
        """Audio or subtitles asked for a title (its file now, its next episodes, a copy that replaces it)."""
        self._asked[imdb] = (time.time(), self._asked_for(imdb) | fields)
        if len(self._asked) > 50:
            self._asked.pop(min(self._asked, key=lambda k: self._asked[k][0]))

    async def streams_for(self, kind: str, video_id: str, meta: dict | None = None) -> list[streams.Stream]:
        meta = meta or {}
        # Files numbered as a neighbour that are this episode (episode_check): looked up at the same time
        named = self.checks.playing_as(video_id)[:4] if kind == "series" else []
        found, *others = await asyncio.gather(self.fetch_streams(kind, video_id, meta),
                                              *(self.fetch_streams(kind, n) for _, _, n in named),
                                              return_exceptions=True)
        if isinstance(found, BaseException):
            raise found
        for (filename, size, n), more in zip(named, others):
            if isinstance(more, BaseException):
                log.info("streams of %s (named %s): %s", video_id, n, type(more).__name__)
            else:
                found += [s for s in more if s.filename == filename]
        ranked = streams.rank(found, self.cfg.max_height, self.cfg.max_mbps, _runtime_min(meta),
                              self.track_plan(meta).audio)
        for s in ranked:
            if not s.why_dropped and (v := self.checks.get(s.filename, s.size, video_id)) and v.ok is False:
                s.why_dropped = f"it's {v.actual or 'another episode'} under this number"
        return ranked

    async def apply_tracks(self, plan: languages.TrackPlan, wait_s: float = 20.0,
                           addon_subs: asyncio.Task | None = None, settle_s: float = 2.5) -> str:
        """Once Kodi plays the file, switch audio and subtitles per the plan (subtitles by the audio it plays: shown
        only for audio that isn't yours, per your `subtitles`). Subtitle languages the file lacks come
        from your subtitle addons (addon_subs: the task fetching them). Kodi starts on the right languages itself
        (prefer() before opening); this refines that (Chinese script, no hearing-impaired or commentary tracks,
        addon subtitles) once playback has settled: for a few seconds after the picture starts moving Kodi applies
        its defaults, the settings it saved for a file played before and the jump to the resume point, which undo
        or drop anything set earlier. Returns a short note."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        last = None
        while loop.time() < deadline:
            pid = await self._kodi_player()
            if pid is not None:
                pos, _, speed = await self.kodi.position(pid)
                if last is not None and speed and pos > last:
                    break
                last = pos
            await asyncio.sleep(0.4)
        else:
            return ""
        await asyncio.sleep(settle_s)
        info = await self.kodi.tracks(pid)
        if not info.get("audiostreams"):
            return ""
        notes = []
        audio = None
        for _ in range(3):              # a track's language can show up a moment after the track itself
            audio = languages.pick_audio(info["audiostreams"], plan.audio) if plan.audio else None
            if audio or not plan.audio:
                break
            await asyncio.sleep(1)
            info = await self.kodi.tracks(pid) or info
        if audio and audio["index"] != (info.get("currentaudiostream") or {}).get("index"):
            await self.kodi.set_audio(pid, audio["index"])
        if audio is None and plan.audio:
            notes.append(f"no {plan.audio} audio")
        playing = (audio or info.get("currentaudiostream") or {}).get("language")
        langs = plan.subs_for(playing or plan.audio)
        if langs is None:
            if info.get("subtitleenabled"):
                await self.kodi.set_subtitle(pid, None)
            await self._recheck_tracks(pid, audio, None, False)
            return ", ".join(notes)
        inside = [t for t in info.get("subtitles") or [] if not t.get("isforced")]
        missing = [lang for lang in langs if not any(languages.matches(t, lang) for t in inside)]
        offered = []
        if missing and addon_subs is not None:
            try:
                offered = subtitles.choose(await asyncio.wait_for(asyncio.shield(addon_subs), 15), missing)
            except Exception as e:      # slow or failing addons: the file's own subtitles only
                log.info("addon subtitles: %s", type(e).__name__)
        # The language to show: the first of yours that the file or an addon has.
        first = next((lang for lang in langs
                      if lang not in missing or any(lang == want for want, _ in offered)), None)
        # Kodi shows the last subtitle it was given, so the one to show (if from an addon) goes last.
        offered.sort(key=lambda pair: pair[0] == first)
        added = []
        for lang, sub in offered:
            try:
                await self.kodi.add_subtitle(pid, sub.url)
                added.append((lang, sub))
            except KodiError as e:
                log.warning("adding %s subtitles from %s: %s", lang, sub.addon, e)
        target = None
        if added:
            await asyncio.sleep(1)      # Kodi loads them in the background and shows each one as it arrives
            listed = (await self.kodi.tracks(pid)).get("subtitles") or []
            log.info("added %s subtitles from %s", ", ".join(lang for lang, _ in added),
                     ", ".join(sorted({sub.addon for _, sub in added})))
            if first in missing:        # the one to show is the last added: the newest external track
                external = [t for t in listed if "external" in str(t.get("name") or "").lower()]
                target = max(external, key=lambda t: t["index"]) if external else None
        if first is not None and first not in missing:
            target = languages.pick_subtitle(inside, [first])
        if target:
            await self.kodi.set_subtitle(pid, target["index"])
            notes.append(f"{first} subs" + (f" from {added[-1][1].addon}" if first in missing else ""))
        else:
            notes.append("no subtitles found")
        await self._recheck_tracks(pid, audio, target, target is not None)
        return ", ".join(notes)

    async def _recheck_tracks(self, pid: int, audio: dict | None, sub: dict | None, subs_on: bool,
                              seconds: float = 8.0):
        """Put the choice back if Kodi changes it while playback settles (or loads an added subtitle late)."""
        loop = asyncio.get_running_loop()
        until, fixes = loop.time() + seconds, 0
        while loop.time() < until and fixes < 4:
            await asyncio.sleep(1)
            info = await self.kodi.tracks(pid)
            if not info.get("audiostreams"):
                return                  # stopped or something else is playing
            if audio and (info.get("currentaudiostream") or {}).get("index") != audio["index"]:
                log.info("Kodi changed the audio track; switching it back")
                await self.kodi.set_audio(pid, audio["index"])
                fixes += 1
            current = (info.get("currentsubtitle") or {}).get("index")
            if subs_on and sub and (not info.get("subtitleenabled") or current != sub["index"]):
                log.info("Kodi changed the subtitles; switching them back")
                await self.kodi.set_subtitle(pid, sub["index"])
                fixes += 1
            elif not subs_on and info.get("subtitleenabled"):
                await self.kodi.set_subtitle(pid, None)
                fixes += 1

    async def find_title(self, title: str, kind: str | None, season: int | None = None,
                         utterance: str | None = None, with_addons: bool = True,
                         expect: Callable[[str, str], None] | None = None) -> cinemeta.Meta | None:
        """Candidates of both kinds from Cinemeta and your metadata addons (search.py); d1 picks one unless a
        candidate clearly wins on its own. `expect` is told the leader while d1 decides (_expecting)."""
        if kind is None and season is not None:
            kind = "series"   # "the bear season 3" is the 2022 series, not the 1989 film
        hits = await self._title_hits(title, kind, with_addons)
        return await self._choose_title(title, kind, hits, utterance, expect) if hits else None

    async def _title_hits(self, title: str, kind: str | None, with_addons: bool = True,
                          enough: asyncio.Event | None = None) -> list[tuple[float, cinemeta.Meta]]:
        """The title search's candidates, best first ([] when nothing comes close). `enough`: see search.search."""
        t = time.perf_counter()
        try:
            addons = await self.addons() if with_addons else []
        except Exception as e:
            log.info("addons unavailable for the title search (%s); Cinemeta only", type(e).__name__)
            addons = []
        hits = cinemeta.ranked(title, await title_search.search(self.http, title, addons, enough=enough), kind)
        if kind:
            hits = [h for h in hits if h[1].type == kind] or hits
        weak = not hits or (min(hits[0][1].rank, hits[0][1].extra_rank) > 0
                            and cinemeta.similarity(title, hits[0][1]) < 70)
        _step("title", "search", _ms(t), query=title,
              sources="Cinemeta and your metadata addons" if title_search.search_catalogs(addons) else "Cinemeta",
              candidates=[f"{m.label} {score:.0f}" for score, m in hits[:4]], nothing_close=True if weak else None)
        return [] if weak else hits

    async def _choose_title(self, title: str, kind: str | None, hits: list[tuple[float, cinemeta.Meta]],
                            utterance: str | None = None,
                            expect: Callable[[str, str], None] | None = None) -> cinemeta.Meta:
        t = time.perf_counter()

        def chose(m: cinemeta.Meta, by: str, unsure: list[cinemeta.Meta] | None = None, **more) -> cinemeta.Meta:
            """The pick, into the start's trace with the candidates it was made from (scores after popularity).
            `unsure`: the candidates to list instead, if this ends up being what plays (UNSURE)."""
            _step("title", "choice", _ms(t), query=title, picked=m.label, by=by, **more,
                  candidates=[f"{c.label} {score:.0f}" for score, c in hits[:4]], sure=False if unsure else None,
                  label="Title chosen", method=tracing.method("d1", more.get("model")) if by == "d1" else
                  tracing.method("local", by), threshold=TITLE_SURE if by == "d1" else None,
                  verdict="listed (not sure)" if unsure else "accepted")
            if unsure and (asked := UNSURE.get()) is not None:
                asked[m.id] = unsure
            return m
        if self._decisive(title, hits):
            return chose(hits[0][1], "the search")
        # A close call: Cinemeta's popularity of the leading few tells a famous title from its namesakes.
        pops = await self._popularity([m for _, m in hits[:4]])
        hits = cinemeta.ranked(title, [m for _, m in hits], kind, pops)
        if self._decisive(title, hits) or (title_search.said_exactly(title, hits[0][1]) and len(hits) > 1
                                           and hits[0][0] - hits[1][0] >= CERTAIN_GAP):
            return chose(hits[0][1], "popularity")
        # Candidates: each source's top hit of each kind (Cinemeta's search knows alternate titles: "la casa de
        # papel" is Money Heist) plus the best name matches. Those top hits go first, since d1 leans to the first.
        firsts = [h for h in hits if h[1].rank == 0 or h[1].extra_rank == 0]
        top = (firsts + [h for h in hits if h not in firsts])[:self.rules.title_candidates]
        if "d1" in self.cfg.decide_off:
            return chose(top[0][1], "the search (d1 off)", [m for _, m in top])
        if expect:
            expect(top[0][1].id, top[0][1].type)
        t_d1 = time.perf_counter()
        try:
            pick = await self.d1.choice(utterance or f"play {title}", self.rules.title_question,
                                        {f"c{i}": m.label for i, (_, m) in enumerate(top)})
            log.info("d1 title choice %s (confidence %.2f)", pick.key, pick.confidence)
            ranked = sorted(range(len(top)), key=lambda i: -pick.probabilities.get(f"c{i}", 0))   # its likeliest first
            return chose(top[int(pick.key[1:])][1], "d1",
                         [top[i][1] for i in ranked] if pick.confidence < TITLE_SURE else None,
                         confidence=round(pick.confidence, 2), d1_ms=_ms(t_d1),
                         model=f"{self.d1.last_source}: {self.d1.hosted_model}" if self.d1.last_source == "hosted"
                         else "local d1", of=len(top))
        except (httpx.HTTPError, KeyError, ValueError, IndexError) as e:
            log.warning("d1 unavailable for title choice (%s); the candidates will be listed", type(e).__name__)
            return chose(top[0][1], f"the search (d1: {type(e).__name__})", [m for _, m in top])

    @staticmethod
    def _decisive(title: str, hits: list[tuple[float, cinemeta.Meta]]) -> bool:
        return len(hits) == 1 or (hits[0][0] - hits[1][0] >= 20 and title_search.said_exactly(title, hits[0][1]))

    async def _popularity(self, metas: list[cinemeta.Meta]) -> dict[str, float]:
        async def one(m: cinemeta.Meta) -> tuple[str, float]:
            try:
                found = await asyncio.wait_for(cinemeta.meta(self.http, m.type, m.id), 1.5)
                return m.id, float(found.get("popularity") or 0)
            except Exception:
                return m.id, 0.0
        return dict(await asyncio.gather(*(one(m) for m in metas)))

    async def resolve(self, title: str, kind: str | None, season: int | None, exact: bool = False,
                      expect: Callable[[str, str], None] | None = None) -> tuple[str, str, str] | None:
        """(imdb id, type, name): your library first (what you watch), then the title search. With the LLM on, it's
        asked at the same time for the real titles that sound like the name ("ceberance": Severance). Those are
        looked up as they come, side by side (your library, else Cinemeta by that name or by its part before a
        subtitle: "Rebel Moon" is "Rebel Moon - Part One: A Child of Fire"), and once one is found the search for
        what was heard stops waiting for its slow sources: a misheard name is a search nobody has made, which
        Cinemeta takes up to 8 s to answer. A title said exactly wins; then the LLM's names in its order (a series
        asked for by season has to have it), as the search for what was heard has it, else as looked up; else what
        that search found."""
        if kind is None and season is not None:
            kind = "series"
        t = time.perf_counter()

        def picked(got: tuple[str, str, str] | None, by: str) -> tuple[str, str, str] | None:
            if got:
                if (asked := UNSURE.get()) is not None:
                    asked["by"] = by
                _note_title(picked=got[2], imdb=got[0], by=by)
                _step("title", "picked", _ms(t), title=got[2], imdb=got[0], by=by)
            return got
        item = await self.library.find(title, kind)
        _note_title(said=title)
        _step("title", "library", _ms(t), query=title, found=item.get("name") if item else None,
              matched=bool(item))
        if item and (not exact or library_mod.bare(item.get("name", "")) == library_mod.bare(title)):
            return picked((item["_id"], item["type"], item.get("name", title)), "your library")
        utterance = HEARD.get() or None        # d1 sees the whole sentence ("…season four")
        if exact or not self.llm_on():         # exact: a sentence nothing understood; only a title said exactly
            m = await self.find_title(title, kind, season, utterance, expect=expect)
            return picked((m.id, m.type, m.name), "the search") \
                if m and (not exact or title_search.said_exactly(title, m)) else None
        found = asyncio.Event()
        fix = asyncio.create_task(self._sounds_like(title))
        heard = asyncio.create_task(self._title_hits(title, kind, enough=found))
        looks: dict[str, asyncio.Task] = {}
        how: dict[str, str] = {}                # where each sound-alike was found
        decided = False                        # once it has: nothing more is looked up

        async def look(name: str) -> tuple[str, str, str] | None:
            tl = time.perf_counter()
            item = await self.library.find(name, kind)
            _step("title", "library", _ms(tl), query=name, found=item.get("name") if item else None,
                  matched=bool(item))
            if item:
                got, how[name] = (item["_id"], item["type"], item.get("name", name)), "your library"
            elif (hit := await self.find_title(name, kind, season, utterance, with_addons=False)) \
                    and title_search.said_as(name, hit):
                got, how[name] = (hit.id, hit.type, hit.name), "Cinemeta"
            else:
                return None
            if not await self._has_season(got[0], got[1], season):
                _step("title", "season check", query=got[2], season=season, ok=False)
                return None
            found.set()
            if expect:
                expect(got[0], got[1])
            return got

        def looking(names: list[str]):
            for name in names:
                if name not in looks and not decided:
                    looks[name] = asyncio.create_task(look(name))

        def answered(task: asyncio.Task):       # looked up as soon as the LLM answers, while the search goes on
            if not task.cancelled() and not task.exception():
                _step("title", "sound-alikes", _ms(t), query=title, model=self.llm.model, names=task.result())
                looking(task.result())
        fix.add_done_callback(answered)
        m = choosing = None
        try:
            hits = await heard
            if any(title_search.said_exactly(title, h) for _, h in hits):
                m = await self._choose_title(title, kind, hits, utterance, expect)
                if title_search.said_exactly(title, m):
                    return picked((m.id, m.type, m.name), "said exactly")
            elif hits:                          # what the search found, in case no sound-alike is: chosen meanwhile
                choosing = asyncio.create_task(self._choose_title(title, kind, hits, utterance))
            try:
                names = await fix
            except Exception as e:
                log.info("LLM title fix: %s", type(e).__name__)
                names = []
            looking(names)
            for name in names:
                named = [h for _, h in hits if title_search.said_exactly(name, h)]
                got = (named[0].id, named[0].type, named[0].name) if len(named) == 1 else None
                if got and await self._has_season(got[0], got[1], season):
                    where = "the search for what was heard"
                else:
                    if got:
                        _step("title", "season check", query=got[2], season=season, ok=False)
                    got, where = await looks[name], how.get(name, "")
                if got:
                    log.info("title %r: sounds like %r (LLM): %s", title, name, got[2])
                    return picked(got, f"sounds like {name!r}, in {where}")
            if m is None and choosing is not None:
                m = await choosing
            return picked((m.id, m.type, m.name), "the search") if m else None
        finally:
            if not fix.done():                  # asked, but the title was known before it answered
                _step("title", "sound-alikes", _ms(t), query=title, model=self.llm.model, answered=False)
            for task in (fix, heard, choosing, *looks.values()):
                if task is not None and not task.done():
                    task.cancel()
            decided = True

    async def _has_season(self, imdb: str, kind: str, season: int | None) -> bool:
        """A sound-alike of a series asked for by season has that season ("paul the boys season 4" isn't Polo Boys,
        which has one). Cinemeta's episode list, usually fetched already; True when it can't say."""
        if season is None:
            return True
        if kind != "series":
            return False
        try:
            eps = cinemeta.episodes(await cinemeta.meta(self.http, kind, imdb))
        except (httpx.HTTPError, ValueError, AssertionError):
            return True
        return any(v["season"] == season for v in eps)

    async def _sounds_like(self, heard: str) -> list[str]:
        """The LLM's real titles for a misheard one, in its order, kept when they sound like it or were heard whole:
        in "paul the boys" ("Pon The Boys" misheard), "The Boys" is heard but sounds less like it than Polo Boys."""
        key = library_mod.sounds(heard)
        names = {i.get("name", "") for i in await self.library.items() if i.get("type") in ("movie", "series")}
        names = sorted(names - {""}, key=lambda n: -fuzz.ratio(library_mod.sounds(n), key))[:10]
        said = library_mod.bare(heard)

        def kept(t: str) -> bool:
            name = library_mod.bare(t)
            return name != said and (fuzz.ratio(library_mod.sounds(t), key) >= 65 or
                                     len(name) >= 3 and re.search(rf"\b{re.escape(name)}\b", said) is not None)
        return [t for t in await self.llm.titles(heard, HEARD.get() or f"play {heard}", names) if kept(t)][:2]

    async def episode_id(self, m: cinemeta.Meta, season: int | None, episode: int | None) -> str | None:
        """tt…:S:E for a series when a season (and maybe episode) was said, checked against Cinemeta's list."""
        if m.type != "series" or season is None:
            return None
        eps = cinemeta.episodes(await cinemeta.meta(self.http, "series", m.id))
        for v in eps:
            if v["season"] == season and (episode is None or v.get("episode") == episode):
                return v["id"]
        return None

    async def play(self, imdb: str, kind: str, name: str, video: str, label: str, meta: dict,
                   resume_ms: int = 0, prepared: streams.Stream | None = None, jump: bool = False) -> Outcome:
        """Pick a cached debrid stream from your addons, start it in the player (TV Agent Player, Kodi or Just
        Player), then follow its progress. `jump`: the same file reopened at resume_ms exactly (a jump in it), without
        a message."""
        if not self.account:
            return Outcome(False, "Stremio isn't signed in (tv-agent setup stremio)")
        t0 = time.perf_counter()
        done_at: dict[str, float] = {}           # when the player's screen was up and the link check done

        async def stamped(key: str, coro):
            try:
                return await coro
            finally:
                done_at[key] = time.perf_counter()
        if not jump and not chain.PLAN.get():      # a chain has said its plan instead
            asyncio.create_task(self.notify(f"Finding {label}…"))
        kodi_ready = player_ready = kodi_open = loading = first_frame = None
        if self.cfg.player == "kodi" and not self.dry_run:
            kodi_ready = asyncio.create_task(self._start_kodi())   # Kodi gets ready (or cold-starts) during the search
        elif self.cfg.player == "tvplayer" and not self.dry_run:
            # The player's screen comes up and connects during the search (~0.3 s), so the link goes to a ready player,
            # and shows the loading screen meanwhile. Whether Kodi plays too (it's stopped first: one player at a
            # time) is asked meanwhile as well: a Kodi frozen in the background answers only by its 1.5 s timeout.
            player_ready = asyncio.create_task(stamped("screen", self.tvplayer.ready(self.adb)))
            kodi_open = asyncio.create_task(self._kodi_player())
            if not jump:
                loading = Loading(self.tvplayer, video, _title_screen(name, meta, video) | {"heard": HEARD.get()},
                                  player_ready, "Starting…" if prepared is not None else "Finding streams…")
        hit = self._streams.get((kind, video))
        how = ("found while the last episode played" if prepared is not None else
               "kept from earlier" if hit and time.monotonic() - hit[0] < STREAMS_TTL else
               "already being looked up" if (kind, video) in self._stream_jobs else "looked up now")
        if prepared is not None:            # found and checked while the previous episode played
            found = [prepared]
        else:
            offered = await self.streams_for(kind, video, meta)
            found = [s for s in offered if not s.why_dropped]
            _step("start", "candidates", label="Streams ranked", method=tracing.method("local", "stream rules"),
                  offered=len(offered), kept=len(found),
                  dropped=dict(Counter(s.why_dropped for s in offered if s.why_dropped)) or None,
                  best=[_stream_line(s) for s in found[:5]] or None,
                  detail={"streams": [_stream_line(s) | {"dropped": s.why_dropped or None} for s in offered[:60]]})
        if self.cfg.player == "just":
            found = [s for s in found if not s.headers]     # Just Player can't send an addon's extra headers
        _step("start", "streams", _ms(t0), video=video, how=how, kept=len(found),
              best=" ".join(found[0].name.split()) if found else None)
        if not found:
            if kodi_ready is not None or player_ready is not None:
                self._not_started(f"No playable stream for {label}", kodi_ready or player_ready, loading)
            return Outcome(False, f"No playable stream for {label}")
        t_streams = time.perf_counter()
        # The next episode, already open in TV Agent Player (preload): its link as it was opened, nothing to check.
        preloaded = (self._preloaded[2] if self.cfg.player == "tvplayer" and prepared is not None and self._preloaded
                     and self._preloaded[:2] == (video, prepared.url) else None)
        chosen, link, checking = found[0], preloaded or found[0].url, None
        if self.cfg.probe_streams and not preloaded:      # runs while the player gets ready; finds where the link leads
            checking = asyncio.create_task(stamped("link", self._first_playable(found[:3])))
            if loading is not None:
                loading.at("Checking the link…")
        # A file in your debrid library, or one checked while the last episode played, is opened even if the
        # check fails now (it may be a passing hiccup); anything else needs an answer.
        trusted = prepared is not None or found[0].library

        async def checked() -> bool:
            nonlocal chosen, link
            if checking is None:
                return True
            got = await checking
            if got is not None:
                chosen, link = got
            _step("start", "link check", (done_at["link"] - t_streams) * 1000, done=done_at["link"], ok=got is not None,
                  of=len(found[:3]), trusted=True if got is None and trusted else None)
            return got is not None or trusted

        if self.dry_run:   # nothing reaches the player either
            if not await checked():
                return Outcome(False, f"The links for {label} didn't answer")
            return Outcome(True, f"(dry run) would play {label} in {self.cfg.player} from {chosen.addon}: "
                                 f"{chosen.filename or chosen.name}" + (f" from {_clock(resume_ms)}" if resume_ms else ""))
        runtime = (_runtime_min(meta) or 0) * 60_000
        plan = self.track_plan(meta)
        if self.cfg.player == "tvplayer":
            if not await checked():              # what was playing carries on, followed as before
                self._not_started(f"The links for {label} didn't answer", player_ready, loading)
                return Outcome(False, f"The links for {label} didn't answer")
            if not (player_ready is not None and await player_ready) and not await self.tvplayer.ready(self.adb):
                return Outcome(False, "TV Agent Player didn't open: is it installed and paired (tv-agent tv pair)?")
            t_ready = time.perf_counter()
            if "screen" in done_at:
                _step("start", "player", (done_at["screen"] - t0) * 1000, done=done_at["screen"],
                      app=f"TV Agent Player {self.tvplayer.app or ''}".strip())
            async with self._starting:
                if self.tvplayer.now() is not None:
                    await self._note_position(tvplayer.PID)    # a replay of this episode starts where it is
                resume_ms = self._replace_session(video, resume_ms, exact=jump)
                addon_subs = None
                if plan.subtitles is not None:   # asked now for the file chosen, added if the file lacks the language
                    addon_subs = asyncio.create_task(subtitles.fetch(self.http, await self.addons(), kind, video,
                                                                     chosen))
                # One player at a time: Kodi stopped if it plays. With the player's screen already up (binge, a jump,
                # the next title) Kodi can't be in front, so that's done after the file opens instead of before
                if kodi_open is not None and not self.tvplayer.up and (kodi := await kodi_open) is not None:
                    await self.kodi.stop(kodi)
                if loading is not None:
                    loading.at("Starting…")
                try:
                    await self.tvplayer.open(link, resume_ms, item=video, title=label,
                                             headers=chosen.headers or None, audio=plan.audio,
                                             subs=plan.subs_for(plan.audio),
                                             info=_title_screen(name, meta, video) | _stream_screen(chosen))
                except tvplayer.PlayerError:
                    if self.tvplayer.up:
                        raise
                    log.info("the player was closed before %s started", video)    # Back on its loading screen
                    return Outcome(False, f"Stopped: the player was closed before {label} started")
                if kodi_open is not None and not kodi_open.done():
                    asyncio.create_task(self._stop_kodi_when(kodi_open))
                t_open = time.perf_counter()
                _step("start", "load", (t_open - t_ready) * 1000, preloaded=True if preloaded else None,
                      resume=_clock(resume_ms) if resume_ms else None)
                self.state.now_playing = NowPlaying(imdb, kind, video, name, "tvplayer", chosen.url, runtime,
                                                    filename=chosen.filename, size=chosen.size,
                                                    video_hash=chosen.video_hash, dolby_vision=chosen.dolby_vision)
                self._fallback = (self.state.now_playing, [s for s in found if s is not chosen][:2],
                                  (imdb, kind, name, video, label, meta), time.monotonic())
                if kind == "series" and self.cfg.check_episodes:
                    self._verify_task = asyncio.create_task(self._switch_if_wrong(
                        self.state.now_playing, chosen, None, (imdb, kind, name, video, label, meta), link))
                self._tracks_task = asyncio.create_task(self._player_subs(plan, addon_subs, label,
                                                                          self.state.now_playing, link))
                if kind == "series" and self.cfg.auto_skip:
                    self._start_skip_watch(self.state.now_playing)
                self._started(imdb, name, kind, video, meta)
            # The first frame, as the player measured it from the load's arrival (~1.3 s; 0.1 s preloaded)
            item = self.tvplayer.item
            frame = self.tvplayer.last.get("first_frame") or await self.tvplayer.wait_for(
                lambda m: m.get("t") == "first_frame" and m.get("item") == item, FIRST_FRAME_WAIT)
            first_frame = frame.get("ms") if frame else None
            if first_frame is not None:
                _step("start", "first frame", first_frame, measured="by the player, from the load")
            stages = (("streams", t0, t_streams), ("player", t_streams, t_ready), ("open", t_ready, t_open))
            for stage, a, b in stages:
                self.state.timings.add(f"start:{stage}", (b - a) * 1000)
            if first_frame is not None:
                self.state.timings.add("start:first_frame", first_frame)
            log.info("start %s: streams %.0f ms, then the link checked and the player ready %.0f ms, open %.0f ms, "
                     "first frame %s ms%s", video, (t_streams - t0) * 1000, (t_ready - t_streams) * 1000,
                     (t_open - t_ready) * 1000, first_frame, " (preloaded)" if preloaded else "")
        elif self.cfg.player == "just":
            if not await checked():
                return Outcome(False, f"The links for {label} didn't answer")
            stages = (("streams", t0, t_streams),)
            async with self._starting:
                resume_ms = self._replace_session(video, resume_ms)
                subs = plan.subs_for(plan.audio)
                await self.tv(justplayer.captions, subs is not None, languages.android_locale(subs[0]) if subs else None)
                if (pid := await self._kodi_player()) is not None:
                    await self.kodi.stop(pid)                               # one player at a time
                t_load = time.perf_counter()
                await self.tv(justplayer.start, link, label, resume_ms)
                _step("start", "load", _ms(t_load), app="Just Player")
                self.state.now_playing = NowPlaying(imdb, kind, video, name, "just", chosen.url, runtime,
                                                    filename=chosen.filename, size=chosen.size,
                                                    video_hash=chosen.video_hash)
                if self.cfg.probe_streams:
                    self._duration = asyncio.create_task(self._probe_duration(self.state.now_playing))
                if kind == "series" and self.cfg.check_episodes:
                    self._verify_task = asyncio.create_task(self._switch_if_wrong(
                        self.state.now_playing, chosen, None, (imdb, kind, name, video, label, meta), link))
                self._started(imdb, name, kind, video, meta)
        else:
            await self.tv(lambda adb: adb.run(["am", "force-stop", justplayer.PACKAGE]))
            if not (kodi_ready is not None and await kodi_ready):
                await self.kodi.start()          # also brings a Kodi left in the background to the front
            try:
                subs = plan.subs_for(plan.audio)
                await self.kodi.prefer("original" if plan.audio is None
                                       else languages.KODI_NAMES.get(plan.audio, "mediadefault"),
                                       "none" if subs is None else languages.KODI_NAMES.get(subs[0], "original"))
                await self.kodi.allow_hdr(dolby_vision_only=chosen.dolby_vision and chosen.hdr10_plus)
            except KodiError as e:
                log.warning("Kodi language settings: %s", e)
            t_kodi = time.perf_counter()
            _step("start", "player", _ms(t0), app="Kodi")
            if not await checked():              # what was playing carries on, followed as before
                await self._home_unless_playing()
                return Outcome(False, f"The links for {label} didn't answer")
            t_check = time.perf_counter()
            async with self._starting:
                if (pid := await self._kodi_player()) is not None:
                    await self._note_position(pid)      # a replay of this episode starts where it is
                resume_ms = self._replace_session(video, resume_ms, exact=jump)
                addon_subs = None
                if plan.subtitles is not None:   # asked now for the file chosen, attached once Kodi has opened it
                    addon_subs = asyncio.create_task(subtitles.fetch(self.http, await self.addons(), kind, video,
                                                                     chosen))
                extra = "|" + urlencode(chosen.headers) if chosen.headers else ""
                if pid is not None:
                    await self._free_kodi(pid)
                await self.kodi.open(link + extra, resume_ms)
                t_open = time.perf_counter()
                _step("start", "load", (t_open - t_check) * 1000, app="Kodi")
                self.state.now_playing = NowPlaying(imdb, kind, video, name, "kodi", chosen.url + extra, runtime,
                                                    filename=chosen.filename, size=chosen.size,
                                                    video_hash=chosen.video_hash, dolby_vision=chosen.dolby_vision)
                self._reopen = (self.state.now_playing,
                                lambda ms: self.play(imdb, kind, name, video, label, meta, ms, prepared=chosen, jump=True))
                self._picture_task = asyncio.create_task(self._watch_picture(self.state.now_playing))
                if kind == "series" and self.cfg.check_episodes and not self.dry_run:
                    self._verify_task = asyncio.create_task(self._switch_if_wrong(
                        self.state.now_playing, chosen, None, (imdb, kind, name, video, label, meta), link))
                again = None if self._again and self._again[0].video_id == video and \
                    time.monotonic() - self._again[1] < STUCK_WITHIN * 2 else \
                    (lambda: self.play(imdb, kind, name, video, label, meta, resume_ms, prepared=chosen))
                self._again = (self.state.now_playing, time.monotonic(), again)     # a retry isn't retried
                self._tracks_task = asyncio.create_task(self._set_tracks(meta, label, addon_subs))
                if kind == "series" and self.cfg.auto_skip:
                    self._start_skip_watch(self.state.now_playing)
                self._started(imdb, name, kind, video, meta)
            stages = (("streams", t0, t_streams), ("kodi", t_streams, t_kodi), ("link", t_kodi, t_check),
                      ("open", t_check, t_open))
            for stage, a, b in stages:
                self.state.timings.add(f"start:{stage}", (b - a) * 1000)
            log.info("start %s: streams %.0f ms, then Kodi ready %.0f ms, link check %.0f ms more, open %.0f ms",
                     video, (t_streams - t0) * 1000, (t_kodi - t_streams) * 1000, (t_check - t_kodi) * 1000,
                     (t_open - t_check) * 1000)
        quality = f" · {'4K' if chosen.height >= 2160 else f'{chosen.height}p'}" if chosen.height else ""
        # The start's stages and the stream played, for the activity history (`tv-agent stats` has the stages too).
        # link_check: the check alone, from the streams; screen: the player's screen up and connected, from the start.
        timed = {f"{stage}_ms": round((b - a) * 1000) for stage, a, b in stages}
        if "link" in done_at:
            timed["link_check_ms"] = round((done_at["link"] - t_streams) * 1000)
        if "screen" in done_at:
            timed["screen_ms"] = round((done_at["screen"] - t0) * 1000)
        if first_frame is not None:
            timed["first_frame_ms"] = first_frame
        stream = {"addon": chosen.addon, "source": chosen.source, "name": " ".join(chosen.name.split()),
                  "filename": chosen.filename, "size": chosen.size or None, "height": chosen.height or None,
                  "release": streams.release(chosen), "debrid": streams.debrid(chosen), "cached": chosen.cached,
                  "in_debrid_library": chosen.library, "aio_score": chosen.aio_score, "of": len(found),
                  "preloaded": bool(preloaded)}
        details = {"stages": timed, "stream": {k: v for k, v in stream.items() if v not in (None, "")}}
        return Outcome(True, f"Playing {label}{quality}" + (f" · from {_clock(resume_ms)}" if resume_ms else ""),
                       details)

    def _replace_session(self, video: str, resume_ms: int, exact: bool = False) -> int:
        """Right before a new file opens, what belonged to the previous start stops: its follower (it would read
        the new file's position; its last progress write finishes on its own), its track fixes, intro watch and
        next-episode preparation, and an "after this one" timer. Returns where to start: a "stop" a moment ago
        on this same episode knows its position better than the library yet (not for a jump: `exact`)."""
        old = self.state.now_playing
        if (not exact and resume_ms > 0 and old and old.video_id == video and old.stopped_at
                and time.monotonic() - old.stopped_mono < 600):
            resume_ms = old.stopped_at
        self._stop_following()
        self._user_stopped, self._after_this = False, None
        for task in (self._prefetch, self._tracks_task, self._skip_task, self._picture_task, self._verify_task,
                     self._preload_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        return resume_ms

    def _started(self, imdb: str, name: str, kind: str, video: str, meta: dict):
        """The new session's own tasks: its follower, the restore file, the next episode's preparation."""
        self._follow(meta)
        self._save_now_playing()
        if kind == "series":
            self._prefetch = asyncio.create_task(self._prepare_next(imdb, name, video, meta))
        if not self.dry_run and (np := self.state.now_playing):     # a scene asked for later is found at once
            finding.prefetch_subs(self.http, self.addons, video, streams.Stream(
                "", np.url, "", "", np.filename, 0, size=np.size, video_hash=np.video_hash))

    def _not_started(self, why: str, ready: asyncio.Task, loading: Loading | None):
        """A start that didn't happen, with the player brought up for it. TV Agent Player's loading screen says why
        for a moment, then goes: the player stops (its screen closes) or, with something else playing on, the
        screen is cleared. Otherwise back to Home unless something plays. A new start meanwhile keeps the screen
        (execute() cancels this)."""
        if loading is not None:
            loading.at(why)

        async def close():
            if loading is not None:
                await asyncio.gather(loading.first, return_exceptions=True)
                if loading.shown and self.tvplayer.has_loading:
                    await asyncio.sleep(ERROR_SHOWN)
                    try:
                        if self.tvplayer.now() is not None:
                            await self.tvplayer.clear_loading()
                        else:
                            await self.tvplayer.stop(tvplayer.PID)
                        return
                    except (tvplayer.PlayerError, asyncio.TimeoutError, OSError) as e:
                        log.info("closing the loading screen: %s", type(e).__name__)
            await self._home_unless_playing(ready)
        self._closing = asyncio.create_task(close())

    async def _home_unless_playing(self, ready: asyncio.Task | None = None):
        """The player was brought up for a start that didn't happen: back to Home, unless it's playing something."""
        if ready is not None:
            await asyncio.gather(ready, return_exceptions=True)
        try:
            if await self._player_id() is None:
                await self.tv(apps.home)
        except Exception as e:
            log.info("back to Home: %s", type(e).__name__)

    async def _first_playable(self, top: list[streams.Stream]) -> tuple[streams.Stream, str] | None:
        """The best of these whose link answers, and where its link leads: all are checked at once, and the best
        one's answer is enough."""
        checks = [asyncio.create_task(streams.resolve(self.http, s)) for s in top]
        try:
            for s, check in zip(top, checks):
                if link := await check:
                    return s, link
            return None
        finally:
            for check in checks:
                check.cancel()

    async def _start_kodi(self) -> bool:
        """Kodi.start() that reports failure instead of raising (it runs ahead of time, before it's certain to be
        needed); the caller tries again where it matters."""
        try:
            await self.kodi.start()
            return True
        except (KodiError, httpx.HTTPError, OSError) as e:
            log.info("starting Kodi early: %s", e)
            return False

    async def _set_tracks(self, meta: dict, label: str, addon_subs: asyncio.Task | None = None):
        """Audio and subtitles once Kodi has opened the file (a few seconds for a 4K debrid link)."""
        try:
            note = await self.apply_tracks(self.track_plan(meta), addon_subs=addon_subs)
            log.info("tracks for %s: %s", label, note or "as the file has them")
            if "no " in note:
                await self.notify(note.capitalize())
        except Exception as e:
            log.warning("tracks for %s: %s", label, type(e).__name__)

    async def _probe_duration(self, np: NowPlaying):
        """The file's real length from its header (ffprobe on this laptop, ~1-2 s): Just Player's media session
        has no duration and Cinemeta's runtime is a per-show average, so "watched at 90%" needs this."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", np.url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
            np.runtime_ms = int(float(out.decode().strip()) * 1000)
            log.info("length of %s: %s", np.name, _clock(np.runtime_ms))
        except Exception as e:
            log.info("length of %s unknown (%s); using Cinemeta's runtime", np.name, type(e).__name__)

    def _save_now_playing(self):
        """Kept so a restart of the agent (settings changed in the panel, an update) picks the session back up.
        It holds the stream link, so the file is private like secrets.toml."""
        np = self.state.now_playing
        if self.dry_run or not np:
            return
        data = {k: getattr(np, k) for k in ("imdb_id", "kind", "video_id", "name", "player", "url", "runtime_ms",
                                            "started")}
        fd = os.open(NOW_PLAYING_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)

    async def restore(self):
        """After a restart: if the player is still on what the agent last started, follow it again."""
        try:
            data = json.loads(NOW_PLAYING_FILE.read_text())
        except (OSError, ValueError):
            return
        if time.time() - data.get("started", 0) > 6 * 3600:
            return
        np = NowPlaying(**{k: data[k] for k in ("imdb_id", "kind", "video_id", "name", "player", "url",
                                                 "runtime_ms", "started") if k in data})
        self.state.now_playing = np
        if np.player == "tvplayer":
            # It reconnects by itself (within ~10 s) and says what it has open: picked up then, without holding up
            # the start of the agent (the voice listeners start after this)
            self.tvplayer.adopt(np.video_id)
            asyncio.create_task(self._restore_player(np))
            return
        playing = (await self._just_status()) if np.player == "just" else (await self._kodi_player())
        if playing is None:
            self.state.now_playing = None
            return
        meta = await cinemeta.meta(self.http, np.kind, np.imdb_id)
        self._follow(meta)
        log.info("picked up %s (%s) again after a restart", np.video_id, np.player)

    async def _restore_player(self, np: NowPlaying):
        """restore() for TV Agent Player: followed again once it has reconnected with this file still open."""
        try:
            await asyncio.wait_for(self.tvplayer.connected.wait(), 20)
            await asyncio.sleep(1.5)            # its state, position and tracks come right after it connects
        except asyncio.TimeoutError:
            pass
        if self.state.now_playing is not np:
            return                              # something else has started meanwhile
        if self.tvplayer.now() is None:
            self.state.now_playing = None
            return
        try:
            self._follow(await cinemeta.meta(self.http, np.kind, np.imdb_id))
            log.info("picked up %s (%s) again after a restart", np.video_id, np.player)
        except Exception as e:
            log.info("following %s again: %s", np.video_id, type(e).__name__)

    def _stop_following(self):
        """The previous session's follower polls no more (it would read the new file's position); its last
        progress write, with the position it had, finishes on its own."""
        if self._progress is asyncio.current_task():
            return                              # called from the follower itself (its end started the next one)
        if self._follow_stop:
            self._follow_stop.set()

    def _follow(self, meta: dict):
        np = self.state.now_playing
        if not (self.account and np):
            return

        async def kodi_poll():
            pid = await self.kodi.player()
            return None if pid is None else await self.kodi.position(pid)

        async def just_poll():
            st = await self.tv(justplayer.status)
            if st is None or st[0] in ("STOPPED", "NONE", "ERROR"):
                return None
            return st[1], np.runtime_ms, st[2]

        async def player_poll():                 # what TV Agent Player last said: no TV round trip
            return self.tvplayer.now()

        self._stop_following()                   # never two followers
        stop = self._follow_stop = asyncio.Event()

        async def follow():
            try:
                videos = await self.stremio_videos(np.imdb_id) if np.kind == "series" else None
                poll = {"just": just_poll, "tvplayer": player_poll}.get(np.player, kodi_poll)
                how, last, duration = await progress.track(poll,
                                                           self.account, self.library, np, meta,
                                                           videos=videos, stop=stop)
                log.info("%s %s at %s", np.video_id, how, _clock(last or 0))
                if how != "replaced":
                    # Its own task: starting the next episode ends this follower.
                    self._end_task = asyncio.create_task(self._ended(np, how, last, duration))
            finally:
                if np.player == "just":   # system captions were turned on for Just Player only: off again
                    try:
                        await self.tv(justplayer.captions, False)
                    except Exception:
                        pass

        self._progress = asyncio.create_task(follow())

    async def _just_status(self) -> tuple[str, int, float] | None:
        """Just Player's state when the agent last started something in it and it's still open."""
        np = self.state.now_playing
        if self.dry_run or not np or np.player != "just":
            return None
        try:
            st = await self.tv(justplayer.status)
        except Exception:
            return None
        return st if st and st[0] in ("PLAYING", "PAUSED", "BUFFERING") else None

    async def _player_id(self) -> int | str | None:
        """The player with something open, to pass to _ctl(): TV Agent Player (tvplayer.PID, known from what it
        reports, without asking the TV) or Kodi (its video player id); None when neither plays."""
        if self.tvplayer.now() is not None:
            return tvplayer.PID
        return await self._kodi_player()

    async def _kodi_player(self) -> int | None:
        """Kodi's video player id when Kodi is playing something, else None (Kodi off or not set up)."""
        if not self.kodi.auth or self.dry_run:
            return None
        try:
            return await asyncio.wait_for(self.kodi.player(), 1.5)
        except Exception:
            return None

    # ---- while you speak: the TV quieter ----

    async def duck(self):
        """Half the volume while the pill listens (the TV's sound reaches the remote's microphone): the player's own
        volume when TV Agent Player or Kodi plays (no volume bar on screen), else the TV's while another app plays
        something."""
        if self.dry_run or self._ducked:
            return
        try:
            if self.tvplayer.now() is not None:
                await self.tvplayer.volume(DUCK)
                self._ducked = ("tvplayer", 1)
            elif (pid := await self._kodi_player()) is not None:
                vol = (await self.kodi.call("Application.GetProperties", {"properties": ["volume"]}))["volume"]
                await self.kodi.call("Application.SetVolume", {"volume": max(1, vol // 2)})
                self._ducked = ("kodi", vol)
            elif any(act and st == "PLAYING" for _, act, st in await self.tv(media.sessions)):
                current = await self.tv(media.get_volume)
                if current:
                    await self.tv(media.set_volume, max(1, current // 2), current)
                    self._ducked = ("tv", current)
        except Exception as e:
            log.info("lowering the volume: %s", type(e).__name__)

    def start_duck(self):
        self._duck = asyncio.create_task(self.duck())

    async def unduck(self, action: Action | None = None):
        """The volume from before listening comes back; a volume command acts on that level instead."""
        if self._duck and not self._duck.done():
            await asyncio.gather(self._duck, return_exceptions=True)
        ducked, self._ducked = self._ducked, None
        if not ducked:
            return
        where, level = ducked
        try:
            if where == "tvplayer":
                await self.tvplayer.volume(1.0)
            elif where == "kodi":
                await self.kodi.call("Application.SetVolume", {"volume": level})
            elif action is not None and action.type == "volume":
                self.state.volume_base = level
            else:
                await self.tv(media.set_volume, level)
        except Exception as e:
            log.info("restoring the volume: %s", type(e).__name__)

    # ---- the end of an episode: binge mode, sleep timer, back Home ----

    async def _prepare_next(self, imdb: str, name: str, video: str, meta: dict):
        """While an episode plays, find the next one and its stream and check the link, so binge mode and
        "next episode" start it at once."""
        try:
            await asyncio.sleep(20)             # after this one has settled (Kodi, its tracks, its link)
            eps = cinemeta.episodes(meta)
            ids = [v["id"] for v in eps]
            if video not in ids or ids.index(video) + 1 >= len(eps):
                self._next_up = None
                return
            nxt = eps[ids.index(video) + 1]
            found = [s for s in await self.streams_for("series", nxt["id"], meta) if not s.why_dropped]
            if self.cfg.player == "just":
                found = [s for s in found if not s.headers]
            top = found[:3]
            ok = (await asyncio.gather(*(streams.playable(self.http, s) for s in top)) if self.cfg.probe_streams
                  else [True] * len(top))
            chosen = None
            for s, good in zip(top, ok):            # the first that answers and is that episode
                if good and (not self.cfg.check_episodes or (await self._verify(s, nxt["id"], meta)).ok is not False):
                    chosen = s
                    break
            self._next_up = (nxt["id"], _episode_label(name, nxt), chosen, time.monotonic()) if chosen else None
            if chosen:
                log.info("next up: %s (%s)", self._next_up[1], chosen.filename or chosen.name)
                np = self.state.now_playing
                if np and np.player == "tvplayer" and np.video_id == video and self.cfg.binge:
                    self._preload_task = asyncio.create_task(self._preload_next(np, nxt["id"], chosen, meta))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("getting the next episode ready: %s", type(e).__name__)

    async def _preload_next(self, np: NowPlaying, video: str, s: streams.Stream, meta: dict):
        """Binge mode in TV Agent Player: the next episode opened in the background (its header, seek index and first
        seconds) PRELOAD_BEFORE the end of this one, so it starts at once (0.1 s measured against ~1.3 s)."""
        try:
            while True:
                await asyncio.sleep(10)
                now = self.tvplayer.now()
                if self.state.now_playing is not np or now is None:
                    return
                if now[1] and now[0] >= now[1] - PRELOAD_BEFORE:
                    break
            link = await streams.resolve(self.http, s) or s.url
            plan = self.track_plan(meta)
            await self.tvplayer.preload(link, item=video, headers=s.headers or None, audio=plan.audio,
                                        subs=plan.subs_for(plan.audio),
                                        info=_title_screen(np.name, meta, video) | _stream_screen(s))
            self._preloaded = (video, s.url, link)
            log.info("next episode %s preloaded in the player", video)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("preloading the next episode: %s", type(e).__name__)

    async def _player_subs(self, plan: languages.TrackPlan, addon_subs: asyncio.Task | None, label: str,
                           np: NowPlaying | None = None, link: str | None = None):
        """TV Agent Player chose the file's own audio and subtitles by the plan before its first frame. What's left:
        subtitles by the audio it chose, when that isn't the audio planned (they show only for audio that isn't
        yours: the file lacked the planned language), and subtitles from your addons when the first of your
        languages that the file or an addon has isn't in the file, timed to the file (_sync_subtitle)."""
        if plan.subtitles is None:
            return
        try:
            item = self.tvplayer.item
            if "tracks" not in self.tvplayer.last and await self.tvplayer.wait_for(
                    lambda m: m.get("t") == "tracks" and m.get("item") == item, 30) is None:
                return
            info = await self.tvplayer.tracks()
            playing = (info.get("currentaudiostream") or {}).get("language")
            langs = plan.subs_for(playing or plan.audio)
            inside = [t for t in info["subtitles"] if not t.get("isforced") and "(external)" not in t.get("name", "")]
            if langs is None:                        # the player was asked for subtitles: this audio is yours
                if info.get("subtitleenabled"):
                    await self.tvplayer.set_subtitle(tvplayer.PID, None)
                return
            if plan.subs_for(plan.audio) is None and not info.get("subtitleenabled") and (
                    sub := languages.pick_subtitle(inside, langs)) is not None:
                await self.tvplayer.set_subtitle(tvplayer.PID, sub["index"])    # asked for none: yours was expected
            have = next((i for i, lang in enumerate(langs) if any(languages.matches(t, lang) for t in inside)),
                        len(langs))
            wanted = langs[:have]                    # only languages you put before the file's own first one
            if not wanted or addon_subs is None:
                return
            offered = subtitles.choose(await asyncio.wait_for(asyncio.shield(addon_subs), 15), wanted)
            if not offered:
                if have == len(langs):
                    await self.notify("No subtitles found")
                return
            lang, sub = offered[0]
            key = (np.filename, np.size, hashlib.sha1(sub.url.encode()).hexdigest()[:16]) if np else None
            known = self._sub_shifts.get(key) if key else None
            index = await self.tvplayer.add_subtitle(
                tvplayer.PID, sub.url, lang=lang, label=f"{languages.name(lang)} ({sub.addon})",
                delay_ms=-known if known and abs(known) >= SUB_SYNC_MIN_MS else None)
            log.info("added %s subtitles from %s for %s%s", lang, sub.addon, label,
                     f", synced {-known / 1000:+.1f} s" if known and abs(known) >= SUB_SYNC_MIN_MS else "")
            if key and link and key not in self._sub_shifts:
                await self._sync_subtitle(np, index, sub, lang, link, key)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # slow or failing addons, a file that won't parse: the file's own subtitles only
            log.info("addon subtitles for %s: %s", label, type(e).__name__)

    async def _sync_subtitle(self, np: NowPlaying, index: int, sub: subtitles.Subtitle, lang: str, link: str,
                             key: tuple[str, int, str]):
        """The addon's subtitles timed to this file (subsync.py): its own lines, read once the start has had the
        network (or kept from the episode check), lined up with the addon's by when lines start, and the player
        told that track's delay. In the file's own language the words are compared too, and the two must agree.
        Nothing is sent for a file without text subtitles of its own, or when the lines don't clearly meet."""
        await asyncio.sleep(CHECK_AFTER)
        got = await asyncio.wait_for(episode_check.file_cues(link), 45)
        ms = None
        if got and len(got[1]) >= subsync.MIN_LINES:
            file_lang, found = got
            theirs = subtitles.cues(await subtitles.download(self.http, sub.url))
            ms = subsync.shift(found, theirs)
            if file_lang[:2].lower() == lang[:2].lower():
                share, by_words = episode_check.compare(found, theirs)
                if share >= episode_check.MATCH and by_words is not None:
                    words_ms = round(by_words * 1000)
                    ms = words_ms if ms is None or abs(ms - words_ms) <= 500 else None
        self._sub_shifts[key] = ms
        if len(self._sub_shifts) > 500:
            self._sub_shifts.pop(next(iter(self._sub_shifts)))
        if ms is None:
            log.info("subtitles from %s left as they are: %s", sub.addon,
                     "the file has too few lines of its own to compare" if not got or len(got[1]) < subsync.MIN_LINES
                     else "their lines don't clearly meet the file's")
        elif abs(ms) < SUB_SYNC_MIN_MS:
            log.info("subtitles from %s already in time with this file (%+d ms)", sub.addon, ms)
        elif self.state.now_playing is np:
            await self.tvplayer.subtitle_delay(index, -ms)
            log.info("subtitles from %s synced to this file: %+.1f s", sub.addon, -ms / 1000)

    async def _stop_kodi_when(self, kodi_open: asyncio.Task):
        """Kodi stopped once the check started with the search says it plays (TV Agent Player has the screen)."""
        try:
            if (kodi := await kodi_open) is not None:
                await self.kodi.stop(kodi)
        except Exception as e:
            log.info("stopping Kodi: %s", type(e).__name__)

    async def _try_another(self, np: NowPlaying, error: dict):
        """TV Agent Player couldn't play a file soon after it opened (a dead link, something it can't read): the next
        copy from your addons takes over where it was, up to twice."""
        fb, self._fallback = self._fallback, None
        if not fb or fb[0] is not np or self.state.now_playing is not np or not fb[1] or time.monotonic() - fb[3] > 120:
            await self.notify("Couldn't play this file")
            return
        alts, what = fb[1], fb[2]
        imdb, kind, name, video, label, meta = what
        log.info("%s didn't play (%s): trying %s", video, error.get("code"), alts[0].filename or alts[0].name)
        await self.notify("That file didn't play: trying another copy")
        out = await self.play(imdb, kind, name, video, label, meta, np.stopped_at or 0, prepared=alts[0], jump=True)
        if out.ok and len(alts) > 1 and (now := self.state.now_playing) is not np:
            self._fallback = (now, alts[1:], what, time.monotonic())

    async def _ended(self, np: NowPlaying, how: str, last: int | None, duration: int):
        """Playback stopped: at the very end of an episode the next one starts (binge mode, unless a sleep timer
        says otherwise); otherwise back to Home rather than the player's menus."""
        if self._user_stopped or how != "stopped" or self.dry_run or self.state.now_playing is not np:
            return                              # asked to stop, or something else has started meanwhile
        if not last and await self._restart_stuck_kodi(np):
            return
        at_end = bool(duration and last is not None and duration - last <= 8000)
        after, self._after_this = self._after_this, None
        HEARD.set("")                           # what starts now wasn't said (this runs in that start's context)
        START.set(None)                         # nor is it that command's start
        try:
            if at_end and after == "off":
                await self._turn_off()
                return
            if at_end and after is None and np.kind == "series" and self.cfg.binge:
                out = await self._next_episode(NextEpisode(offset=1))
                await self.notify(out.say)
                if out.ok:
                    return
            await self.tv(apps.home)
        except asyncio.CancelledError:
            raise                               # a new command took over
        except Exception as e:
            log.warning("after %s ended: %s", np.video_id, type(e).__name__)
            await self.notify("Couldn't start the next episode")
            await self._home_unless_playing()

    async def _watch_picture(self, np: NowPlaying):
        """While Kodi plays this file: a decoder that lost its picture (Kodi logging its failures nonstop, sound
        going on) gets Kodi restarted and the file reopened where it was, once."""
        try:
            while True:
                await asyncio.sleep(PICTURE_EVERY)
                if self.state.now_playing is not np or (pid := await self._kodi_player()) is None:
                    return
                tail = await self.tv(lambda adb: adb.run(["tail", "-c", "4000", KODI_LOG], timeout=5))
                if len(DECODER_FAILED.findall(tail)) < 8:
                    continue
                at = (await self.kodi.position(pid))[0]
                log.warning("Kodi lost the picture of %s at %s (its decoder failing): restarting it", np.video_id,
                            _clock(at))
                await self._recover_kodi(np, at, "The picture was lost: restarting Kodi")
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("watching Kodi's picture: %s", type(e).__name__)

    async def _recover_kodi(self, np: NowPlaying, at_ms: int, say: str) -> bool:
        """Kodi force-stopped (its decoder is let go with it), started again, and this file reopened at at_ms."""
        if not self._reopen or self._reopen[0] is not np:
            return False
        reopen = self._reopen[1]
        await self.notify(say)
        try:
            await self.tv(lambda adb: adb.run(["am", "force-stop", KODI]))
            await self.kodi.start()
            out = await reopen(at_ms)
        except Exception as e:
            log.warning("restarting Kodi: %s", type(e).__name__)
            await self.notify("Kodi didn't start again")
            await self._home_unless_playing()
            return True
        await self.notify(out.say)
        return True

    async def _jump_by_reopening(self, pid: int, target_ms: int) -> Outcome | None:
        """A jump as a fresh start of the same file at the time (PICTURE_EVERY's note), or None when the file isn't
        ours to reopen."""
        np = self.state.now_playing
        if not self._reopen or self._reopen[0] is not np or np.player != "kodi":
            return None
        out = await self._reopen[1](max(0, target_ms))
        return Outcome(out.ok, f"To {_clock(max(0, target_ms))}" if out.ok else out.say)

    async def episode_subs(self, video: str, lang: str = "eng") -> list[tuple[int, str]]:
        """An episode's subtitles from your subtitle addons, asked by its id (not by a file): kept in memory for
        the check that a file is this episode and for jumps to a moment."""
        key = (video, lang)
        if key not in self._episode_cues:
            kind = "series" if re.match(r"^tt\d+:\d+:\d+$", video) else "movie"
            asking = [asyncio.create_task(subtitles.fetch(self.http, [addon], kind, video, None, timeout=6))
                      for addon in await self.addons()]
            cues: list[tuple[int, str]] = []
            try:
                for answer in asyncio.as_completed(asking):     # the first addon with a whole file is enough
                    for _, sub in (subtitles.choose(await answer, [lang]) or [])[:2]:
                        try:
                            cues = subtitles.cues(await subtitles.download(self.http, sub.url))
                        except (httpx.HTTPError, OSError, ValueError) as e:
                            log.info("subtitles of %s from %s: %s", video, sub.addon, type(e).__name__)
                            continue
                        if len(cues) >= 50:
                            break
                    if len(cues) >= 50:
                        break
            finally:
                for task in asking:
                    task.cancel()
            if len(self._episode_cues) >= 60:
                self._episode_cues.pop(next(iter(self._episode_cues)))
            self._episode_cues[key] = cues
        return self._episode_cues[key]

    def _verify(self, s: streams.Stream, video: str, meta: dict, link: str | None = None) -> asyncio.Task:
        """Whether this file is the episode (episode_check.py): known already, or checked now in the background.
        `link`: where its link was found to lead, if it was."""
        async def run() -> episode_check.Verdict:
            if known := self.checks.get(s.filename, s.size, video):
                return known
            ids = [v["id"] for v in cinemeta.episodes(meta)]
            try:
                # Resolved once: each read would follow the addon's redirect
                resolved = link if link and link != s.url else await streams.resolve(self.http, s) or s.url
                verdict = await asyncio.wait_for(episode_check.check(resolved, video, self.episode_subs, ids), 45)
            except (asyncio.TimeoutError, OSError, ValueError, httpx.HTTPError) as e:
                log.info("checking %s is %s: %s", s.filename, video, type(e).__name__)
                return episode_check.Verdict(video, None, checked=time.time())
            log.info("%s is %s: %s (%.2f%s)", s.filename or s.name, video,
                     {True: "yes", False: f"no, {verdict.actual or 'another episode'}", None: "can't tell"}[verdict.ok],
                     verdict.share, f", subtitles {verdict.shift:+.1f} s" if verdict.shift is not None else "")
            if verdict.ok is not None:
                self.checks.keep(s.filename, s.size, verdict)
            return verdict
        return asyncio.create_task(run())

    async def _switch_if_wrong(self, np: NowPlaying, chosen: streams.Stream, verifying: asyncio.Task | None,
                               what: tuple, link: str | None = None):
        """Once this file is known not to be the episode asked for, a right one takes over at the same moment. A
        check started here waits CHECK_AFTER first: it reads the file too, and the player's start comes first."""
        imdb, kind, name, video, label, meta = what
        try:
            if verifying is None and not self.checks.get(chosen.filename, chosen.size, video):
                await asyncio.sleep(CHECK_AFTER)
            verdict = await (verifying or self._verify(chosen, video, meta, link))
            if verdict.ok is not False or self.state.now_playing is not np:
                return
            alt = next((s for s in await self.streams_for(kind, video, meta) if not s.why_dropped and
                        s.filename != chosen.filename and not (self.cfg.player == "just" and s.headers)), None)
            if alt is None:
                await self.notify(f"This file is {verdict.actual or 'another episode'} under this number, and "
                                  "there's no other copy")
                return
            at = await self._where(np)
            log.info("%s is %s, not %s: switching to %s at %s", chosen.filename, verdict.actual, video,
                     alt.filename or alt.name, _clock(at))
            await self.notify(f"That file was another episode under this number: switching to {label}")
            await self.play(imdb, kind, name, video, label, meta, at, prepared=alt, jump=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("checking the episode: %s", type(e).__name__)

    async def _free_kodi(self, pid: int):
        """What Kodi plays is stopped, and its player gone, before another file opens. Opened over a playing file,
        this TV's video decoder isn't free yet ("InstanceGuard locked" in Kodi's log): the new file plays without a
        picture, a seek in it runs to the end, or Kodi hangs (found 2026-10-23)."""
        try:
            await self.kodi.stop(pid)
        except (KodiError, httpx.HTTPError) as e:
            log.info("stopping Kodi before the next file: %s", e)
        for _ in range(20):
            if await self._kodi_player() is None:
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(KODI_FREE)

    async def _restart_stuck_kodi(self, np: NowPlaying) -> bool:
        """A file that stopped at 0:00 right after opening: when Kodi's log has nothing since the open, Kodi is
        stuck (KODI_LOG's note), so it's restarted and the file opened again. True when it was."""
        if not self._again or self._again[0] is not np or self._again[2] is None:
            return False
        _, opened, again = self._again
        since = time.monotonic() - opened
        if since > STUCK_WITHIN:
            return False
        try:
            now, written = (await self.tv(lambda adb: adb.run(["sh", "-c", f"date +%s; stat -c %Y {KODI_LOG}"]))).split()
            quiet = int(now) - int(written)
        except Exception as e:
            log.info("is Kodi stuck: %s", type(e).__name__)
            return False
        if quiet < since - 1:                   # Kodi wrote about this file: it failed on its own (e.g. the link)
            return False
        log.warning("Kodi is stuck (nothing in its log for %d s, the file opened %.0f s ago): restarting it", quiet,
                    since)
        await self.notify("Kodi was stuck: restarting it")
        try:
            await self.tv(lambda adb: adb.run(["am", "force-stop", KODI]))
            await self.kodi.start()
            out = await again()
        except Exception as e:
            log.warning("restarting Kodi: %s", type(e).__name__)
            await self.notify("Kodi was stuck and didn't start again")
            await self._home_unless_playing()
            return True
        await self.notify(out.say)
        return True

    async def _note_position(self, pid: int | str):
        """Where the player is, read just before stopping it: the progress write and a "continue" right after use it."""
        np = self.state.now_playing
        if np and np.player in ("kodi", "tvplayer") and self._progress and not self._progress.done():   # our file is on
            try:
                np.stopped_at = (await self._ctl(pid).position(pid))[0]
                np.stopped_mono = time.monotonic()
            except Exception as e:
                log.info("position before stopping: %s", type(e).__name__)

    async def _turn_off(self):
        await self.tv(lambda adb: adb.run(["cmd", "input", "keyevent", "KEYCODE_SLEEP"]))

    async def _binge(self, a: Binge) -> Outcome:
        set_value(self.cfg, "binge", a.on)
        if a.on:
            self._after_this = None
        return Outcome(True, "Binge mode on: the next episode starts by itself" if a.on else "Binge mode off")

    async def _timer(self, a: Timer) -> Outcome:
        if a.cancel:
            had = self._after_this is not None or bool(self._sleep and not self._sleep.done())
            self._after_this = None
            if self._sleep:
                self._sleep.cancel()
            return Outcome(True, "Sleep timer off" if had else "No sleep timer was set")
        if a.minutes is None:
            self._after_this = a.then
            return Outcome(True, "Turning the TV off after this one" if a.then == "off" else "Stopping after this one")
        if self._sleep:
            self._sleep.cancel()
        self._sleep = asyncio.create_task(self._sleep_in(a.minutes * 60, a.then))
        return Outcome(True, f"{'Turning the TV off' if a.then == 'off' else 'Stopping'} in {a.minutes} min")

    async def _sleep_in(self, seconds: float, then: str):
        await asyncio.sleep(seconds)
        self._user_stopped = True
        try:
            if (pid := await self._player_id()) is not None:
                await self._note_position(pid)
                await self._ctl(pid).stop(pid)
            elif await self._just_status() is not None:
                await self.tv(media.dispatch, "stop")
        except Exception as e:                  # the TV goes off (or Home) all the same
            log.warning("sleep timer, stopping the player: %s", type(e).__name__)
        if then == "off":
            await self._turn_off()
        else:
            await self.tv(apps.home)

    # ---- subtitles and audio on demand ----

    async def _tracks(self, a: Tracks, remember: bool = True) -> Outcome:
        """Audio and subtitles of what's playing. Audio (or subtitles) in a language the file lacks: another copy of
        what the agent started that has it takes over where it is (_other_copy). What's asked holds for the title
        (its next episodes, a copy switched to: _ask) unless `remember` is off; "from now on" (`keep`) changes your
        settings instead (_language_settings). With your subtitles shown only for audio that isn't yours, a switch
        of audio turns them on or off to match."""
        if a.keep or a.subtitles == "foreign":
            return await self._language_settings(a)
        pid = await self._player_id()
        ctl = self._ctl(pid)
        if a.subtitles == "toggle":           # "subtitles": off if they're on, else on
            if pid is not None:
                on = bool((await ctl.tracks(pid)).get("subtitleenabled"))
            else:
                on = await self._just_status() is not None and await self.tv(justplayer.captions_on)
            a = a.model_copy(update={"subtitles": "off" if on else "on"})
        if pid is None:
            if await self._just_status() is not None and a.subtitles and not a.audio:
                on = a.subtitles != "off"
                lang = a.subtitles if a.subtitles not in ("on", "off") else (self.cfg.subtitle_languages or ["eng"])[0]
                await self.tv(justplayer.captions, on, languages.android_locale(lang) if on else None)
                return Outcome(True, "Subtitles on (from the next start)" if on else "Subtitles off")
            return Outcome(False, "Subtitles and audio language can be changed in TV Agent Player and Kodi")
        info = await ctl.tracks(pid)
        np = self._session(pid)
        meta = await self._meta_of(np) if np and a.audio else {}
        before = self._asked.get(np.imdb_id) if np else None
        if np and remember:
            asked = {}
            if a.audio:
                asked["audio"] = a.audio
            if a.subtitles:
                langs = self.cfg.subtitle_languages
                asked["subtitles"] = (None if a.subtitles == "off" else list(langs) if a.subtitles == "on"
                                      else [a.subtitles, *(x for x in langs if x != a.subtitles)])
            self._ask(np.imdb_id, **asked)
        said = []
        if a.audio:
            tracks = info.get("audiostreams") or []
            want = (languages.made_in(meta, self._originals.get(np.imdb_id)) if np else None) \
                if a.audio == "original" else a.audio
            track = languages.pick_audio(tracks, want) if want else None
            if a.audio == "original" and track is None and not (want and np):
                track = (next((t for t in tracks if t.get("isoriginal")), None)
                         or next((t for t in tracks if t.get("isdefault")), None) or (tracks[0] if tracks else None))
            if track is None:
                if np and want and self.account:
                    return self._unless_failed(await self._other_copy(np, meta, audio=want), np, before)
                return Outcome(False, f"No {languages.name(want or a.audio)} audio in this file")
            if track["index"] != (info.get("currentaudiostream") or {}).get("index"):
                await ctl.set_audio(pid, track["index"])
            said.append(f"{languages.name(track.get('language')) or 'original'} audio")
            plan = self.track_plan(meta) if np else None
            if not a.subtitles and plan and plan.unless:    # your subtitles follow the audio
                if plan.subs_for(track.get("language")) is None:
                    if info.get("subtitleenabled"):
                        await ctl.set_subtitle(pid, None)
                        said.append("subtitles off")
                elif not info.get("subtitleenabled") and (got := await self._show_subtitles(
                        pid, ctl, info, plan.subtitles, np)):
                    said.append(got)
        if a.subtitles == "off":
            await ctl.set_subtitle(pid, None)
            said.append("subtitles off")
        elif a.subtitles:
            langs = self.cfg.subtitle_languages if a.subtitles == "on" else [a.subtitles]
            got = await self._show_subtitles(pid, ctl, info, langs, np)
            if got is None:
                if np and a.subtitles != "on" and self.account and not said:
                    return self._unless_failed(await self._other_copy(np, meta or await self._meta_of(np),
                                                                      subtitle=a.subtitles), np, before)
                return Outcome(False, "No subtitles in that language")
            said.append(got)
        text = ", ".join(said)
        return Outcome(True, text[:1].upper() + text[1:] if text else "OK")

    def _unless_failed(self, out: Outcome, np: NowPlaying, before: tuple[float, dict] | None) -> Outcome:
        """A copy switch that didn't happen leaves what was asked for the title as it was."""
        if not out.ok:
            if before is None:
                self._asked.pop(np.imdb_id, None)
            else:
                self._asked[np.imdb_id] = before
        return out

    async def _show_subtitles(self, pid: int | str, ctl, info: dict, langs: list[str],
                              np: NowPlaying | None) -> str | None:
        """The first of these languages the file has shown, else one from your subtitle addons (for what the agent
        started); what's shown, or None when neither has any."""
        inside = [t for t in info.get("subtitles") or [] if not t.get("isforced")]
        if sub := languages.pick_subtitle(inside, langs):
            await ctl.set_subtitle(pid, sub["index"])
            return f"{languages.name(sub.get('language'))} subtitles"
        offered = []
        if np:
            stream = streams.Stream("", np.url, "", "", np.filename, 0, size=np.size, video_hash=np.video_hash)
            offered = subtitles.choose(
                await subtitles.fetch(self.http, await self.addons(), np.kind, np.video_id, stream), langs)
        if not offered:
            return None
        lang, best = offered[0]
        await ctl.add_subtitle(pid, best.url, **({"lang": lang, "label": f"{languages.name(lang)} ({best.addon})"}
                                                 if pid == tvplayer.PID else {}))
        return f"{languages.name(lang)} subtitles from {best.addon}"

    async def _meta_of(self, np: NowPlaying) -> dict:
        try:
            return await cinemeta.meta(self.http, np.kind, np.imdb_id)
        except (httpx.HTTPError, ValueError) as e:
            log.info("details of %s: %s", np.imdb_id, type(e).__name__)
            return {}

    def _session(self, pid: int | str | None) -> NowPlaying | None:
        """What the agent started, while that player plays it."""
        np = self.state.now_playing
        if np is None or pid is None:
            return None
        if np.player == "tvplayer":
            return np if pid == tvplayer.PID else None
        return np if np.player == "kodi" and pid != tvplayer.PID and self._progress and not self._progress.done() \
            else None

    async def _where(self, np: NowPlaying) -> int:
        """Where the player is in what the agent started (ms)."""
        if np.player in ("kodi", "tvplayer") and (pid := await self._player_id()) is not None:
            return (await self._ctl(pid).position(pid))[0]
        if (st := await self._just_status()) is not None:
            return st[1]
        return 0

    async def _other_copy(self, np: NowPlaying, meta: dict, audio: str | None = None,
                          subtitle: str | None = None) -> Outcome:
        """What's playing from another copy that has audio (or subtitles) in this language, from where it is: the
        best such copy by your stream rules (a wrong episode's switch, _switch_if_wrong). The TV says it's looking;
        when no copy has it, what's playing goes on as it was."""
        lang = audio or subtitle
        what = f"{languages.name(lang)} {'audio' if audio else 'subtitles'}"
        await self.notify(f"No {what} in this file: looking for a copy with it…")
        try:
            alt = await self._copy_with(np, meta, audio, subtitle)
        except (httpx.HTTPError, OSError) as e:
            log.info("copies with %s: %s", what, type(e).__name__)
            alt = None
        if self.state.now_playing is not np:
            return Outcome(False, "Something else started meanwhile")
        if alt is None:
            return Outcome(False, f"No copy of {np.name} has {what}: it goes on as it was")
        at = await self._where(np)
        v = next((x for x in cinemeta.episodes(meta) if x["id"] == np.video_id), None)
        label = _episode_label(np.name, v) if v else np.name
        log.info("%s for %s: switching to %s at %s", what, np.video_id, alt.filename or alt.name, _clock(at))
        await self.notify(f"Switching to a copy with {what}…")
        out = await self.play(np.imdb_id, np.kind, np.name, np.video_id, label, meta, at, prepared=alt, jump=True)
        if not out.ok:
            return Outcome(False, f"The copy with {what} didn't start ({out.say})")
        quality = f" · {'4K' if alt.height >= 2160 else f'{alt.height}p'}" if alt.height else ""
        return Outcome(True, f"{what[:1].upper() + what[1:]}: another copy{quality}, from {_clock(at)}", out.details)

    async def _copy_with(self, np: NowPlaying, meta: dict, audio: str | None = None,
                         subtitle: str | None = None) -> streams.Stream | None:
        """The best copy but the one playing (your stream rules, with the language asked for the title) that has
        audio or subtitles in this language and whose link answers. Copies your addons say have it come first; the
        best few that say nothing, then those whose name (not their tracks) says it lacks it, are read too (their
        tracks, from the file's header), all at once. Without link checks (probe_streams off) only what the addons
        say counts."""
        def other(s: streams.Stream) -> bool:
            return not s.why_dropped and s.url != np.url.split("|")[0] and not (
                s.filename and s.filename == np.filename and s.size == np.size) and not (
                self.cfg.player == "just" and s.headers)
        found = [s for s in await self.streams_for(np.kind, np.video_id, meta) if other(s)]
        has = (lambda s: streams.has_audio(s, audio)) if audio else (lambda s: streams.has_subtitles(s, subtitle))
        said = [s for s in found if has(s)][:3]
        if not self.cfg.probe_streams:
            return said[0] if said else None
        unsure = [s for s in found if has(s) is None] + [s for s in found if has(s) is False and not s.tracks_read]
        maybe = said + unsure[:PROBE_AT_MOST]
        reads = [asyncio.create_task(self._probe(s)) for s in maybe]
        try:
            for s, read in zip(maybe, reads):
                got = await read
                if got is False:                # the link doesn't answer
                    continue
                if got is None and s in said:   # unreadable: what the addon says goes
                    return s
                if got and (audio in got[0] if audio else languages.code(subtitle) in got[1]):
                    return s
            return None
        finally:
            for read in reads:
                read.cancel()

    async def _probe(self, s: streams.Stream) -> tuple[set[str], set[str]] | None | bool:
        """(audio, subtitle) languages of a copy from its file's header, kept; None when it can't be read, False when
        its link doesn't answer."""
        key = (s.filename or s.url, s.size)
        if key not in self._probed:
            link = await streams.resolve(self.http, s)
            if link is None:
                return False
            if len(self._probed) > 200:
                self._probed.pop(next(iter(self._probed)))
            self._probed[key] = await streams.probe(self.http, link, s.headers)
        return self._probed[key]

    async def _language_settings(self, a: Tracks) -> Outcome:
        """"From now on, audio in Spanish", "a partir de ahora sin subtítulos": your settings (as the panel and the
        phone change them), and what's playing follows. What was asked for single titles is forgotten. Subtitles in
        a language turn on (for audio that isn't yours) if they were off; just "on" is always."""
        c, said = self.cfg, []
        if a.audio:
            set_value(c, "audio_language", a.audio)
            said.append("original audio" if a.audio == "original" else f"{languages.name(a.audio)} audio")
        if a.subtitles == "off":
            set_value(c, "subtitles", "off")
            said.append("no subtitles")
        elif a.subtitles in ("on", "toggle"):
            set_value(c, "subtitles", "always")
            said.append("subtitles always")
        elif a.subtitles == "foreign":
            set_value(c, "subtitles", "foreign")
            said.append(f"subtitles only when the audio isn't {languages.name(c.native_language)}")
        elif a.subtitles:
            set_value(c, "subtitle_languages", [a.subtitles, *(x for x in c.subtitle_languages if x != a.subtitles)])
            if c.subtitles == "off":
                set_value(c, "subtitles", "foreign")
            said.append(f"{languages.name(a.subtitles)} subtitles")
        self._asked.clear()
        text = "From now on: " + ", ".join(said)
        pid = await self._player_id()
        if not said or (np := self._session(pid)) is None:
            return Outcome(True, text)
        if a.subtitles == "foreign":        # what's playing: subtitles as its audio wants them
            info = await self._ctl(pid).tracks(pid)
            playing = (info.get("currentaudiostream") or {}).get("language")
            on = self.track_plan(await self._meta_of(np)).subs_for(playing) is not None
            now = await self._tracks(Tracks(subtitles="on" if on else "off"), remember=False)
        else:
            now = await self._tracks(Tracks(audio=a.audio, subtitles={"toggle": "on"}.get(a.subtitles, a.subtitles)),
                                     remember=False)
        return Outcome(True, f"{text} · {now.say}" if now.ok else f"{text} ({now.say})", now.details)

    async def _replay(self, a: Replay) -> Outcome:
        pid = await self._player_id()
        if pid is None:
            return await self._transport(Transport(op="back", seconds=a.seconds))
        ctl = self._ctl(pid)
        was_on = bool((await ctl.tracks(pid)).get("subtitleenabled"))
        await ctl.seek_by(pid, -a.seconds)
        if not was_on and (await self._tracks(Tracks(subtitles="on"), remember=False)).ok:
            asyncio.create_task(self._subtitles_off_later(pid, a.seconds + 8))
        return Outcome(True, f"Back {a.seconds} s" + ("" if was_on else " with subtitles for a moment"))

    async def _subtitles_off_later(self, pid: int | str, seconds: float):
        await asyncio.sleep(seconds)
        try:
            if await self._player_id() == pid:
                await self._ctl(pid).set_subtitle(pid, None)
        except Exception:
            pass

    # ---- executors: movies and series in Kodi ----

    async def current_series(self) -> tuple[str, str, str | None] | None:
        """(imdb id, name, episode id) of the series on now: the one this agent started, else the series most
        recently in your Continue Watching (the progress sync keeps that current)."""
        np = self.state.now_playing
        if np and np.kind == "series":
            return np.imdb_id, np.name, np.video_id
        row = [i for i in await self.library.continue_watching() if i.get("type") == "series"]
        if not row:
            return None
        return row[0]["_id"], row[0].get("name", ""), (row[0].get("state") or {}).get("video_id")

    async def _play_title(self, a: PlayTitle) -> Outcome:
        playing_video = None
        if a.title is None:                      # "go to season 3 episode 4": the show that's on
            current = await self.current_series()
            if not current:
                return Outcome(False, "Which show? Say its name too")
            imdb, name, playing_video = current
            kind = "series"
        else:
            t = time.perf_counter()
            unsure = UNSURE.set({})
            try:
                found = await self.resolve(a.title, a.kind, a.season, exact=a.guess, expect=self._expecting(a))
                asked = UNSURE.get()
            finally:
                UNSURE.reset(unsure)
            ms = _ms(t)
            self.state.timings.add("start:title", ms)
            _note_title(ms=ms)
            log.info("title %r: %s (%d ms)", a.title, found[2] if found else "not found", ms)
            if not found:
                return Outcome(False, f"Didn't get that: “{a.title}”" if a.guess else f"Couldn't find “{a.title}”")
            imdb, kind, name = found
            if imdb in asked and asked.get("by") != "your library":
                return await self._ask_title(a, asked[imdb])
        return await self._play_found(a, imdb, kind, name, playing_video)

    async def _ask_title(self, a: PlayTitle, metas: list[cinemeta.Meta]) -> Outcome:
        """A title d1 wasn't sure of (TITLE_SURE): its candidates listed on the TV, its likeliest first, instead of a
        guess played; "number 2" plays that one as asked (the season, episode or episode said)."""
        async def poster(m: cinemeta.Meta) -> str:
            try:
                return (await asyncio.wait_for(cinemeta.meta(self.http, m.type, m.id), 2)).get("poster") or ""
            except Exception:
                return ""
        posters = await asyncio.gather(*(poster(m) for m in metas))
        items = [ListItem(m.id, m.type, m.name, m.label, "", art, ask=a.model_copy(update={"title": m.name[:120]}))
                 for m, art in zip(metas, posters)]
        es = T.spanish(HEARD.get() or a.title or "")
        self._shown = Shown(f"¿Cuál es “{a.title}”?" if es else f"Which “{a.title}”?", items, time.monotonic())
        await self._show_on_tv(self._shown)
        _note_title(asked=[m.label for m in metas])
        log.info("title %r: not sure which, %d listed", a.title, len(items))
        # In a chain the steps after the play can't run on a list: it stops there, saying why
        return Outcome(not chain.PLAN.get(), "No estoy seguro de cuál: di un número" if es else
                       "Not sure which one: say a number")

    def _expecting(self, a: PlayTitle) -> Callable[[str, str], None]:
        """Called with the title the search or the LLM leans to while the rest decides (d1, the other sound-alikes):
        its stream lookup, the longest wait of a start (3-6 s), begins at once, since it's usually what plays. At
        most EXPECT_AT_MOST guesses per command."""
        tried: set[str] = set()

        def expect(imdb: str, kind: str):
            if imdb not in tried and len(tried) < EXPECT_AT_MOST and not a.episode_name and self.account \
                    and not self.dry_run:
                tried.add(imdb)
                asyncio.create_task(self._expect(a, imdb, kind))
        return expect

    async def _expect(self, a: PlayTitle, imdb: str, kind: str):
        try:
            if kind == "movie":
                self.prefetch(kind, imdb)
                return
            state = ((await self.library.get(imdb)) or {}).get("state") or {}
            eps = cinemeta.episodes(await cinemeta.meta(self.http, kind, imdb))
            if eps and isinstance(v := self._episode_for(a, "", eps, state), dict):
                self.prefetch(kind, v["id"])
        except Exception as e:
            log.info("streams ahead for %s: %s", imdb, type(e).__name__)

    async def _play_found(self, a: PlayTitle, imdb: str, kind: str, name: str,
                          playing_video: str | None = None) -> Outcome:
        """Play a title found: a film, or a series at the season/episode asked, else where you were, else its first
        episode."""
        state = ((await self.library.get(imdb)) or {}).get("state") or {}
        # The stream lookup (3-6 s) starts now when the video is already known; the metadata comes meanwhile.
        if kind == "movie":
            self.prefetch(kind, imdb)
        elif a.season is not None and a.episode is not None:
            self.prefetch(kind, f"{imdb}:{a.season}:{a.episode}")
        elif a.season is None and a.episode is None and not (a.restart or a.latest) and state.get("video_id"):
            self.prefetch(kind, playing_video or state["video_id"])
        meta = await cinemeta.meta(self.http, kind, imdb)
        video, label = imdb, name
        if kind == "series":
            eps = cinemeta.episodes(meta)
            if not eps:
                return Outcome(False, f"No episodes listed for {name}")
            if a.episode_name and (a.season is None or a.episode is None):
                found = await self.find_episode(imdb, name, eps, a.episode_name)
                if found.episode is None:
                    return await self._ask_episode(a, imdb, name, found)
                v = found.episode
                resume = state.get("timeOffset", 0) if state.get("video_id") == v["id"] and not a.restart else 0
                self.prefetch(kind, v["id"])
                return await self.play(imdb, kind, name, v["id"], _episode_label(name, v), meta, resume)
            v = self._episode_for(a, name, eps, state, playing_video)
            if isinstance(v, str):
                return Outcome(False, v)
            video, label = v["id"], _episode_label(name, v)
        resume = state.get("timeOffset", 0) if state.get("video_id") == video and not a.restart else 0
        return await self.play(imdb, kind, name, video, label, meta, resume)

    @staticmethod
    def _episode_for(a: PlayTitle, name: str, eps: list[dict], state: dict,
                     playing_video: str | None = None) -> dict | str:
        """The episode a request for a series plays (eps: Cinemeta's, in order), or why there's none."""
        season = a.season
        if season is None and a.episode is not None:
            # "episode 3 of the bear": the season you're on, else the first
            on = playing_video or state.get("video_id")
            current = next((x for x in eps if x["id"] == on), None)
            season = current["season"] if current else eps[0]["season"]
        if season is not None:
            match = [v for v in eps if v["season"] == season and (a.episode is None or v.get("episode") == a.episode)]
            if not match:
                return f"{name} has no season {season}" + (f" episode {a.episode}" if a.episode else "")
            return match[0]
        if a.latest:
            now = datetime.now(timezone.utc)
            return ([x for x in eps if (when := cinemeta.released(x)) and when <= now] or eps)[-1]
        if state.get("video_id") and not a.restart:
            return next((x for x in eps if x["id"] == state["video_id"]), eps[0])   # "play the bear": where you were
        return eps[0]

    async def find_episode(self, imdb: str, name: str, eps: list[dict], wanted: str) -> finding.EpisodeFind:
        """An episode by its name ("Advanced Dungeons & Dragons") or by what happens in it ("Mr. Burns gets shot"):
        the one, or the ones to pick from (finding.py)."""
        es = T.spanish(HEARD.get() or wanted)
        asyncio.create_task(self.notify(f"Buscando ese capítulo de {name}…" if es else
                                        f"Looking for that episode of {name}…"))
        t = time.perf_counter()
        found = await finding.episode(self, imdb, name, eps, wanted)
        _step("title", "episode", _ms(t), wanted=wanted, episode=found.episode["id"] if found.episode else None,
              choices=[v["id"] for v in found.choices] or None, by=found.why)
        return found

    async def _ask_episode(self, a: PlayTitle, imdb: str, name: str, found: finding.EpisodeFind) -> Outcome:
        """Not sure which episode was meant, or none fits: the likeliest listed on the TV instead of a guess played;
        "number 2" plays that one (and a chain goes on from there)."""
        es = T.spanish(HEARD.get() or a.episode_name or "")
        if not found.choices:
            return Outcome(False, (f"No encontré ese capítulo de {name}: “{a.episode_name}”" if es else
                                   f"Couldn't find that episode of {name}: “{a.episode_name}”")
                           + (f" ({found.why})" if found.why and not es else ""))
        then = tuple(chain.REST.get())
        items = [ListItem(imdb, "series", name, _episode_label(name, v),
                          (v.get("overview") or v.get("description") or "")[:90], v.get("thumbnail") or "",
                          video=v["id"], then=then) for v in found.choices]
        none = found.why in ("none fits", "found in the subtitles")
        title = ((f"No encontré ese capítulo de {name}. ¿Alguno de estos?" if es else
                  f"Couldn't find that episode of {name}. One of these?") if none else
                 (f"¿Qué capítulo de {name}?" if es else f"Which episode of {name}?"))
        self._shown = Shown(title, items, time.monotonic())
        await self._show_on_tv(self._shown)
        log.info("episode %r of %s: %s, %d listed", a.episode_name, name, found.why, len(items))
        say = "di un número" if es else "say a number"
        # In a chain the steps after the play wait for the pick (`then`): the chain stops here, saying why
        return Outcome(not chain.PLAN.get(), (f"No estoy seguro de qué capítulo: {say}" if es else
                                              f"Not sure which episode: {say}") if not none else
                       (f"No lo encontré: {say} si es uno de estos" if es else f"Couldn't find it: {say} if it's one"))

    async def _continue_watching(self, a: ContinueWatching) -> Outcome:
        if not a.title and (pid := await self._player_id()) is not None:
            await self._ctl(pid).play(pid, True)  # "carry on with the show" while it's open: just resume
            return Outcome(True, "Resume")
        if not a.title and await self._just_status():
            await self.tv(media.dispatch, "resume")
            return Outcome(True, "Resume")
        if a.title:
            item = await self.library.find(a.title)
        else:
            row = await self.library.continue_watching()
            item = row[0] if row else None
        if not item:
            return Outcome(False, f"Nothing to continue{' for ' + a.title if a.title else ''}")
        return await self._continue_item(item)

    async def _continue_item(self, item: dict) -> Outcome:
        """Where you stopped in this library item, or the next episode when you'd finished that one."""
        st = item.get("state") or {}
        if st.get("timeOffset") and st.get("duration") and st["timeOffset"] < progress.WATCHED_AT * st["duration"]:
            self.prefetch(item["type"], st.get("video_id") or item["_id"])   # where you stopped: its streams meanwhile
        target = await self._continue_target(item)
        if isinstance(target, Outcome):
            return target
        video, offset, label, meta = target
        return await self.play(item["_id"], item["type"], item.get("name", ""), video, label, meta, offset)

    async def _continue_target(self, item: dict) -> tuple[str, int, str, dict] | Outcome:
        """(video, where in it, label, meta) that carrying on with this library item plays; warm() looks its
        streams up before it's asked for."""
        imdb, kind, name = item["_id"], item["type"], item.get("name", "")
        st = item.get("state") or {}
        meta = await cinemeta.meta(self.http, kind, imdb)
        video, offset, label = st.get("video_id") or imdb, st.get("timeOffset", 0), name
        if kind == "series":
            eps = cinemeta.episodes(meta)
            ids = [v["id"] for v in eps]
            if video not in ids:
                video, offset = (ids[0] if ids else imdb), 0
            elif not offset or (st.get("duration") and offset >= progress.WATCHED_AT * st["duration"]):
                i = ids.index(video) + 1                 # finished that one: go on to the next episode
                if i >= len(ids):
                    return Outcome(False, f"You've seen all of {name}")
                video, offset = ids[i], 0
            v = eps[ids.index(video)] if video in ids else None
            label = _episode_label(name, v) if v else name
        return video, offset, label, meta

    async def _next_episode(self, a: NextEpisode) -> Outcome:
        current = await self.current_series()
        if not current:
            return Outcome(False, "No series in progress")
        imdb, name, on = current
        meta = await cinemeta.meta(self.http, "series", imdb)
        eps = cinemeta.episodes(meta)
        ids = [v["id"] for v in eps]
        i = ids.index(on) + a.offset if on in ids else 0
        if i >= len(eps):
            return Outcome(False, f"That was the last episode of {name}")
        if i < 0:
            return Outcome(False, f"That's the first episode of {name}")
        prepared = None
        if self._next_up and self._next_up[0] == eps[i]["id"] and time.monotonic() - self._next_up[3] < 3 * 3600:
            prepared = self._next_up[2]
        self._next_up = None
        return await self.play(imdb, "series", name, eps[i]["id"], _episode_label(name, eps[i]), meta,
                               prepared=prepared)

    async def llm_context(self, said: str) -> dict:
        """What the LLM step gets besides what you said: what's playing, the apps, and up to 10 of your library's
        titles that sound like it (llm.py)."""
        np = self.state.now_playing
        try:
            names = list({i.get("name", "") for i in await self.library.items()
                          if i.get("type") in ("movie", "series") and (not i.get("removed") or i.get("temp"))} - {""})
        except Exception:
            names = []
        names.sort(key=lambda n: -fuzz.partial_ratio(n.lower(), said.lower()))
        return {"playing": f"{np.name} ({np.kind}, {np.video_id.split(':', 1)[-1] if np.kind == 'series' else ''})"
                if np else None,
                "apps": sorted(a.label for a in self.rules.apps.values()), "candidates": names[:10]}

    # ---- lists on the TV: what you were watching, what's new; "number 2" ----

    async def _show_list(self, a: ShowList) -> Outcome:
        if a.list == "recent":
            title, items = "Continue watching", await self._recent_items()
        elif a.list == "new":
            title, items = "New episodes of your shows", await self._new_episode_items()
        else:
            title, items = "New films", await self._new_film_items()
        if not items:
            return Outcome(False, {"recent": "Nothing to continue", "new": "No new episodes of your shows lately",
                                   "movies": "None of your addons has a list of new films"}[a.list])
        self._shown = Shown(title, items, time.monotonic())
        await self._show_on_tv(self._shown)
        return Outcome(True, f"{title}: say a number")

    async def _find_scene(self, a: FindScene) -> Outcome:
        """"Go to the part where Mr. Burns gets shot": found in the subtitles of what's playing (finding.py), the line
        it was found by shown on the TV, then the jump. Found in the episode before or after instead: offered."""
        np = self.state.now_playing
        if np is None:
            return Outcome(False, "Nothing of yours is playing to search")
        if self.llm is None:
            return Outcome(False, "Finding a moment needs the LLM (Settings › API keys: OpenRouter)")
        if chain.cut_off(a.what) or chain.cut_off(HEARD.get()):          # "… where Paul rides the"
            return Outcome(False, f"“{a.what}” sounds cut off: say it again")
        m = re.match(r"^tt\d+:(\d+):(\d+)$", np.video_id)
        title = np.name + (f" S{m.group(1)}E{m.group(2)}" if m else "")
        stream = streams.Stream("", np.url, "", "", np.filename, 0, size=np.size, video_hash=np.video_hash)
        found = await finding.scene(self, np.video_id, title, a.what, HEARD.get(), stream)
        if found.ms is None:
            return Outcome(False, found.why if found.why.startswith(("No subtitles", "Couldn't", "Finding")) else
                           f"Not sure where “{a.what}” is: {found.why}")
        if found.video and found.video != np.video_id:
            return await self._offer_scene(np, found)
        if found.others:                                     # two answers, two places: which?
            return await self._which_moment(np, found)
        shift = self._subtitle_shift(np, found.cues)
        secs = max(0, round((found.ms + shift) / 1000))
        _step("scene", "time shift", label="Matched to the file", method=tracing.method("local", "lines in common"),
              shift_ms=shift, why=None if shift else "no measure of the file's subtitles, or too few lines in common")
        await self.notify(f"{_clock(secs * 1000)}: “{found.shown}”")   # the line it's placed by, shown before the jump
        await asyncio.sleep(chain.SCENE_SHOWN)
        target = max(0, secs - chain.SCENE_BEFORE)
        t = time.perf_counter()
        out = await self._transport(Transport(op="seek_to", seconds=target))
        _step("scene", "jump", _ms(t), label="Jump", method=tracing.method("player", self.cfg.player),
              to=_clock(target * 1000), verdict="done" if out.ok else "failed", why=None if out.ok else out.say)
        if not out.ok:
            return out
        return Outcome(True, f"To {_clock(secs * 1000)}: “{found.shown}”", {"scene_ms": target * 1000})

    def _subtitle_shift(self, np: NowPlaying, cues: list[tuple[int, str]]) -> int:
        """ms to add to a time in these subtitles for the file playing. The episode check measured how far its own
        subtitles of the episode (English, by its id) are from the file's; these are matched to those by their
        lines. 0 when there's no measure, or too few lines in common (another language)."""
        v = self.checks.get(np.filename, np.size, np.video_id)
        kept = self._episode_cues.get((np.video_id, "eng"))
        if not (v and v.ok and v.shift and kept and cues):
            return 0
        at = {f: ms for ms, w in kept if len(f := finding.fold(w)) > 12}
        deltas = sorted(at[f] - ms for ms, w in cues if (f := finding.fold(w)) in at)
        if len(deltas) < 5:
            return 0
        return deltas[len(deltas) // 2] - round(v.shift * 1000)

    async def _which_moment(self, np: NowPlaying, found: finding.SceneFind) -> Outcome:
        """The moment placed in more than one place (or once, and not at all the other time): the places listed, the
        jump made by the pick ("number 2", "yes" for the first)."""
        es = T.spanish(HEARD.get())
        shift = self._subtitle_shift(np, found.cues)
        places = [f for f in (found, *found.others) if f.ms is not None]
        items = [ListItem(np.imdb_id, np.kind, np.name, _clock(max(0, f.ms + shift)), f"“{f.shown}”",
                          video=np.video_id, at_ms=max(0, f.ms + shift)) for f in places]
        self._shown = Shown("¿Qué momento?" if es else "Which moment?", items, time.monotonic())
        await self._show_on_tv(self._shown)
        log.info("moment of %s: not sure, %s listed", np.video_id, [i.label for i in items])
        if len(items) == 1:
            return Outcome(False, "¿Es este? di «sí» para ir" if es else "Is it this one? Say yes to go there")
        return Outcome(False, "No estoy seguro de qué momento: di un número" if es else
                       "Not sure which moment: say a number")

    async def _offer_scene(self, np: NowPlaying, found: finding.SceneFind) -> Outcome:
        """A moment found in the episode before or after the one playing: offered, not played (a list of one: "yes"
        or "number 1" goes there)."""
        es = T.spanish(HEARD.get())
        m = re.match(r"^tt\d+:(\d+):(\d+)$", found.video)
        where = f"S{m.group(1)}E{m.group(2)}" if m else found.video
        item = ListItem(np.imdb_id, np.kind, np.name, f"{np.name} · {where} · {_clock(found.ms)}", f"“{found.shown}”",
                        video=found.video, at_ms=found.ms)
        self._shown = Shown("¿Ir ahí?" if es else "Go there?", [item], time.monotonic())
        await self._show_on_tv(self._shown)
        return Outcome(False, f"Está en el {where}, en {_clock(found.ms)}: di «sí» para ir" if es else
                       f"It's in {where} at {_clock(found.ms)}: say yes to go there")

    async def _recommend(self, a: Recommend) -> Outcome:
        """Suggestions for a request ("something like Dark", "a short comedy"): the LLM proposes titles, with your
        recent viewing as taste; each one must be found by the title search (a real title, with its poster) before
        it's listed on the TV. "Number 2" then plays it."""
        es = T.spanish(a.query)
        if self.llm is None:
            return Outcome(False, "Las recomendaciones necesitan la clave de OpenRouter" if es else
                           "Recommendations need the OpenRouter key (Settings › API keys)")
        items = sorted([i for i in await self.library.items() if i.get("type") in ("movie", "series")],
                       key=lambda i: (i.get("state") or {}).get("lastWatched") or "", reverse=True)
        taste = [i.get("name", "") for i in items if _watched_some(i)][:30]
        seen = [i.get("name", "") for i in items if _watched_some(i)][:150]
        try:
            ideas, limit = await self.llm.recommend(a.query, a.kind, taste, seen, "Spanish" if es else "English")
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            log.info("recommendations unavailable (%s)", type(e).__name__)
            return Outcome(False, "No hay recomendaciones ahora mismo" if es else "No recommendations right now")
        watched = {i.get("_id") for i in items if _watched_some(i)}      # already seen or being watched
        try:
            addons = await self.addons()         # your metadata addon knows titles by their Spanish names too
        except Exception as e:
            log.info("addons unavailable for suggestions (%s); Cinemeta only", type(e).__name__)
            addons = []
        picked, ids, tried = [], set(), set()
        for round_ in (1, 2):                    # a second round when too few fit (a length limit leaves out many)
            tried |= {x["title"] for x in ideas}
            found = await asyncio.gather(*(self._recommended(x, es, addons, limit) for x in ideas))
            for it in found:
                if it and it.imdb not in ids and it.imdb not in watched:
                    ids.add(it.imdb)
                    picked.append(it)
            if len(picked) >= 3 or round_ == 2:
                break
            try:
                ideas, limit = await self.llm.recommend(a.query, a.kind, taste, seen + sorted(tried),
                                                        "Spanish" if es else "English")
            except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError):
                break
        if not picked:
            return Outcome(False, "No encontré nada que encaje" if es else "Couldn't find anything that fits")
        request = a.query if len(a.query) <= 40 else a.query[:39] + "…"
        self._shown = Shown(f"Para “{request}”" if es else f"For “{request}”", picked[:8], time.monotonic())
        await self._show_on_tv(self._shown)
        return Outcome(True, "Recomendaciones: di un número" if es else "Suggestions: say a number")

    async def _recommended(self, idea: dict, es: bool, addons: list, limit: int | None) -> ListItem | None:
        """A suggested title as a list item, if the title search knows it by that name (and about that year); a film
        longer than the request's limit is left out."""
        try:
            hits = cinemeta.ranked(idea["title"], await asyncio.wait_for(
                title_search.search(self.http, idea["title"], addons), 6), idea["kind"])
            year = idea.get("year")
            match = next((m for _, m in hits if m.type == idea["kind"] and title_search.said_exactly(idea["title"], m)
                          and (not year or not m.year[:4].isdigit() or abs(int(m.year[:4]) - year) <= 1)), None)
            if match is None:
                log.info("suggestion %r (%s) not found by that name", idea["title"], year)
                return None
            meta = await asyncio.wait_for(cinemeta.meta(self.http, match.type, match.id), 4)
        except Exception as e:               # one suggestion that can't be checked is left out
            log.info("suggestion %r: %s", idea.get("title"), type(e).__name__)
            return None
        if limit and match.type == "movie" and (minutes := _runtime_min(meta)) and minutes > limit:
            log.info("suggestion %r runs %d min, over %d", idea["title"], minutes, limit)
            return None
        rating = meta.get("imdbRating")
        detail = " · ".join(x for x in (idea.get("why", ""), f"★ {rating}" if rating not in (None, "", "N/A") else "",
                                         ("serie" if es else "series") if match.type == "series" else "") if x)
        year = match.year[:4] if match.year else ""
        label = f"{match.name} ({year})" if year and year not in match.name else match.name
        return ListItem(match.id, match.type, match.name, label, detail, meta.get("poster") or "")

    async def _show_on_tv(self, shown: Shown):
        """The TV app's numbered list (ChoicesActivity); the remote's OK or number keys pick too."""
        if not await self._screen({"title": shown.title, "items": [{"label": i.label, "detail": i.detail,
                                                                    "poster": i.poster} for i in shown.items]}):
            await self.notify("  ".join(f"{n}. {i.label}" for n, i in enumerate(shown.items, 1)))

    async def _screen(self, payload: dict) -> bool:
        """A list screen in the TV app (ChoicesActivity); False for a TV app from before it had one."""
        try:
            out = await self.tv(lambda adb: adb.run(["am", "start", "-n", CHOICES, "--es", "list", json.dumps(payload)],
                                                    timeout=6))
        except Exception as e:
            out = f"Error: {type(e).__name__}"
        if "Error" in out:
            log.info("list on the TV: %s", out.strip()[:120])
            return False
        return True

    async def _help(self, a: Help) -> Outcome:
        """"What can you do?": every command, by section, on the TV (commands.py), in the language it was asked in."""
        lang = a.lang or "en"
        items = []
        for section in SECTIONS:
            items.append({"label": section.name(lang), "header": True})
            for c in section.commands:
                items.append({"label": c.say(lang), "detail": c.does(lang)})
        title = "Lo que puedes decir" if lang == "es" else "What you can say"
        if not await self._screen({"title": title, "mode": "help", "items": items, "legend": LEGEND[lang],
                                   "hint": TV_HINT[lang]}):
            return Outcome(False, "Update the TV app to see the list (android/build-and-install.sh)")
        return Outcome(True, title)

    async def _info(self, a: Info) -> Outcome:
        """"What am I watching?": what's playing and when it ends. "Where am I in Severance?": your last episode of
        it and how far in, from your Stremio library."""
        if a.title:
            found = await self.resolve(a.title, None, None)
            if not found:
                return Outcome(False, f"Couldn't find “{a.title}”")
            imdb, kind, name = found
            st = ((await self.library.get(imdb)) or {}).get("state") or {}
            if not st.get("video_id") and not st.get("timeOffset"):
                return Outcome(True, f"You haven't watched {name}")
            label = name
            if m := re.match(r"^tt\d+:(\d+):(\d+)$", st.get("video_id") or ""):
                label += f" · S{m.group(1)}E{m.group(2)}"
            off, total = st.get("timeOffset") or 0, st.get("duration") or 0
            how = ("finished" if total and off >= progress.WATCHED_AT * total else
                   f"{_clock(off)} of {_clock(total)}" if off and total else f"at {_clock(off)}" if off else "not started")
            when = ""
            if st.get("lastWatched"):
                try:
                    when = " · " + _ago(datetime.fromisoformat(st["lastWatched"].replace("Z", "+00:00")),
                                        datetime.now(timezone.utc))
                except ValueError:
                    pass
            return Outcome(True, f"{label}: {how}{when}")
        np = self.state.now_playing
        pid = await self._player_id()
        if pid is not None:
            pos, total, _ = await self._ctl(pid).position(pid)
            if np:
                label = np.name + (f" · S{np.video_id.split(':')[1]}E{np.video_id.split(':')[2]}"
                                   if np.kind == "series" and np.video_id.count(":") == 2 else "")
            else:
                label = (await self.kodi.info("VideoPlayer.Title")).get("VideoPlayer.Title") or "Kodi"
            if not total:
                return Outcome(True, label)
            ends = (datetime.now() + timedelta(milliseconds=total - pos)).strftime("%H:%M")
            return Outcome(True, f"{label} · {_clock(pos)} of {_clock(total)} · ends at {ends}")
        if np and (st := await self._just_status()) is not None:
            return Outcome(True, f"{np.name} · {_clock(st[1])}" + (f" of {_clock(np.runtime_ms)}" if np.runtime_ms else ""))
        return Outcome(True, "Nothing's playing")

    async def play_link(self, url: str) -> Outcome:
        """Any video link on the TV (the panel's "send a link"), in the configured player."""
        if not re.match(r"^https?://\S+$", url):
            return Outcome(False, "Only http(s) links")
        self._stop_following()                       # what was followed isn't what plays now
        self.state.now_playing = None                # nor its progress, binge or restore after a restart
        NOW_PLAYING_FILE.unlink(missing_ok=True)
        if self.cfg.player == "tvplayer":
            if not await self.tvplayer.ready(self.adb):
                return Outcome(False, "TV Agent Player didn't open: is it installed and paired (tv-agent tv pair)?")
            await self.tvplayer.open(url, item="link", title="Link from the panel")
        elif self.cfg.player == "kodi":
            await self.kodi.start()
            await self.kodi.open(url)
        else:
            await self.tv(justplayer.start, url, "Link from the panel")
        return Outcome(True, "Playing the link")

    def now_playing_link(self) -> dict | None:
        """What the agent is playing, with its stream link (the panel only: the link carries the debrid key)."""
        np = self.state.now_playing
        if not np:
            return None
        return {"name": np.name, "video_id": np.video_id, "kind": np.kind, "player": np.player, "url": np.url,
                "filename": np.filename}

    async def _sequence(self, a: Sequence) -> Outcome:
        """A chain: a play, then one or two steps on what it plays, each once the one before is done (chain.py)."""
        ok, say, details = await chain.run(self, a, HEARD.get())
        return Outcome(ok, say, details)

    async def _cancel(self, a: Cancel) -> Outcome:
        """"Cancel": what's left of a chain, or a start on its way, stops (chain.cancel)."""
        ok, say = await chain.cancel(self)
        return Outcome(ok, say)

    async def _skip_when_it_comes(self, what: str) -> Outcome:
        """The intro or recap of what just started, skipped as it comes (it may be minutes in, or already past)."""
        pid, np = await self._player_id(), self.state.now_playing
        if pid is None or np is None or np.player not in ("kodi", "tvplayer"):
            return Outcome(False, SKIPS_WHERE)
        pos, total, _ = await self._ctl(pid).position(pid)
        found = [seg for seg in await self.segments(np, total) if seg.kind == what]
        if not found:
            return Outcome(False, f"No {what} timestamps for this episode")
        if all(pos >= seg.end_ms - 3_000 for seg in found):
            return Outcome(True, f"Already past the {what}")
        self._start_skip_watch(np, also=what)
        return Outcome(True, f"Skipping the {what} when it comes")

    async def _recent_items(self) -> list[ListItem]:
        out = []
        for item in (await self.library.continue_watching())[:LIST_SIZE]:
            st = item.get("state") or {}
            label = item.get("name", "")
            if item.get("type") == "series" and (m := re.match(r"^tt\d+:(\d+):(\d+)$", st.get("video_id") or "")):
                label += f" · S{m.group(1)}E{m.group(2)}"
            left = (st.get("duration") or 0) - (st.get("timeOffset") or 0)
            detail = f"{round(left / 60000)} min left" if left >= 60000 else "Nearly done"
            out.append(ListItem(item["_id"], item.get("type", "movie"), item.get("name", ""), label, detail,
                                item.get("poster") or ""))
        return out

    async def _new_episode_items(self, days: int = 120) -> list[ListItem]:
        """The newest episode out of each show in your library, newest first, unless you've got to it already."""
        shows = {i["_id"]: i for i in await self.library.items() if i.get("type") == "series"
                 and cinemeta.IMDB_ID.match(i.get("_id", "")) and (not i.get("removed") or i.get("temp"))}
        now = datetime.now(timezone.utc)
        rows = []
        for m in await cinemeta.last_videos(self.http, list(shows)):
            item = shows.get(m.get("id"))
            out = [(when, v) for v in m.get("videos") or [] if v.get("season") and cinemeta.VIDEO_ID.match(v.get("id", ""))
                   and (when := cinemeta.released(v)) and when <= now]
            if not item or not out:
                continue
            when, v = max(out, key=lambda x: (x[0], x[1]["season"], x[1].get("episode") or 0))
            on = re.match(r"^tt\d+:(\d+):(\d+)$", (item.get("state") or {}).get("video_id") or "")
            if (now - when).days > days or (on and (int(on.group(1)), int(on.group(2))) >= (v["season"], v.get("episode") or 0)):
                continue
            name = item.get("name") or m.get("name", "")
            detail = _ago(when, now) + (f" · {v['name']}" if v.get("name") and v["name"] != "TBA" else "")
            rows.append((when, ListItem(item["_id"], "series", name, _episode_label(name, v), detail,
                                        m.get("poster") or item.get("poster") or "", v["id"])))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [item for _, item in rows[:LIST_SIZE]]

    async def _new_film_items(self) -> list[ListItem]:
        """The first film catalog of your addons named like "Latest", else "Trending" / "Popular"."""
        found = []
        for a in await self.addons():
            for c in a.manifest.get("catalogs", []):
                name = (c.get("name") or "").lower()
                if c.get("type") != "movie" or any(e.get("isRequired") for e in c.get("extra", [])):
                    continue
                pref = 0 if re.search(r"\b(?:latest|new|recent)\b", name) else (
                    1 if re.search(r"\b(?:trending|popular)\b", name) else None)
                if pref is not None:
                    found.append((pref, a, c["id"]))
        if not found:
            return []
        _, addon, catalog = min(found, key=lambda f: f[0])
        try:
            r = await self.http.get(f"{addon.base}/catalog/movie/{catalog}.json", timeout=10)
            r.raise_for_status()
            metas = r.json().get("metas") or []
        except (httpx.HTTPError, ValueError) as e:
            log.info("new films from %s: %s", addon.name, type(e).__name__)
            return []
        out = []
        for m in metas:
            mid = m.get("imdb_id") or m.get("id") or ""
            if not cinemeta.IMDB_ID.match(mid):
                continue
            year = str(m.get("releaseInfo") or m.get("year") or "")[:4]
            genres = ", ".join((m.get("genres") or m.get("genre") or [])[:2])
            rating = f"★ {m['imdbRating']}" if m.get("imdbRating") not in (None, "", "N/A") else ""
            out.append(ListItem(mid, "movie", m.get("name", ""), f"{m.get('name', '')} ({year})" if year else
                                m.get("name", ""), " · ".join(x for x in (rating, genres) if x), m.get("poster") or ""))
            if len(out) == LIST_SIZE:
                break
        return out

    async def _pick(self, a: Pick) -> Outcome:
        shown = self._shown
        if not shown or time.monotonic() - shown.at > LIST_TTL:
            return Outcome(False, "No list on screen")
        if a.number > len(shown.items):
            return Outcome(False, f"The list goes up to {len(shown.items)}")
        return await self.play_item(shown.items[a.number - 1])

    async def play_item(self, it: ListItem) -> Outcome:
        """One item of a list (on the TV, or on a phone): where you were, a series' first episode, or the film; a
        title asked about, as its request said."""
        if it.ask is not None:
            return await self._play_found(it.ask, it.imdb, it.kind, it.name)
        if it.at_ms and (np := self.state.now_playing) and it.video == np.video_id \
                and await self._player_id() is not None:                          # a moment of what's playing
            return await self._transport(Transport(op="seek_to", seconds=max(0, it.at_ms // 1000 - chain.SCENE_BEFORE)))
        if it.then and it.video and (m := re.match(r"^tt\d+:(\d+):(\d+)$", it.video)):   # a chain goes on from it
            first = PlayTitle(title=it.name[:120], season=int(m.group(1)), episode=int(m.group(2)), kind="series")
            return await self._sequence(Sequence(steps=[first, *it.then]))
        if it.video is None and (item := await self.library.get(it.imdb)) and library_mod.in_continue_watching(item):
            return await self._continue_item(item)
        if it.video is None and it.kind == "series":        # a recommended series: where you were, or episode 1
            return await self._play_found(PlayTitle(title=it.name[:120], kind="series"), it.imdb, "series", it.name)
        self.prefetch(it.kind, it.video or it.imdb)
        meta = await cinemeta.meta(self.http, it.kind, it.imdb)
        state = ((await self.library.get(it.imdb)) or {}).get("state") or {}
        video = it.video or it.imdb
        resume = state.get("timeOffset", 0) if state.get("video_id") == video else 0
        if it.at_ms:                                         # a scene found there: a moment before its line
            resume = max(0, it.at_ms - chain.SCENE_BEFORE * 1000)
        label = _episode_label(it.name, {"season": int(m.group(1)), "episode": int(m.group(2)), "name": ""}) \
            if it.at_ms and (m := re.match(r"^tt\d+:(\d+):(\d+)$", video)) else it.label
        return await self.play(it.imdb, it.kind, it.name, video, label, meta, resume)

    # ---- skipping intros, recaps and credits (skips.py) ----

    async def segments(self, np: NowPlaying, total_ms: int) -> list[skips.Segment]:
        """The episode's intro, recap and credits timestamps for this file's length (the last episode's are kept)."""
        m = re.match(r"^(tt\d+):(\d+):(\d+)$", np.video_id)
        if np.kind != "series" or not m or not total_ms:
            return []
        key = (np.video_id, total_ms // 1000)
        if key not in self._segments:
            self._segments = {key: await skips.fetch(self.http, m.group(1), int(m.group(2)), int(m.group(3)), total_ms)}
        return self._segments[key]

    async def _skip(self, a: Skip) -> Outcome:
        pid = await self._player_id()
        np = self.state.now_playing
        if pid is None or np is None or np.player not in ("kodi", "tvplayer"):
            return Outcome(False, SKIPS_WHERE)
        ctl = self._ctl(pid)
        pos, total, _ = await ctl.position(pid)
        what = "intro" if a.what == "credits" and total and pos < total / 4 else a.what   # "the opening credits"
        found = await self.segments(np, total)
        if what == "credits":
            if not (skips.around(found, "credits", pos, lead_ms=30_000) or (total and pos >= 0.85 * total)):
                return Outcome(False, "The credits haven't started")
            if np.kind == "series" and self.cfg.binge:
                return await self._next_episode(NextEpisode(offset=1))
            self._user_stopped = True
            await self._note_position(pid)
            await ctl.stop(pid)
            await self.tv(apps.home)
            return Outcome(True, "Credits skipped")
        if seg := skips.around(found, what, pos):
            await ctl.seek_to(pid, seg.end_ms)
            log.info("skip: %s %s %s-%s (%s)", np.video_id, what, _clock(seg.start_ms), _clock(seg.end_ms), seg.source)
            return Outcome(True, f"Skipped the {what}")
        if pid != tvplayer.PID and await self._skip_chapter(what):
            return Outcome(True, f"Skipped the {what} (chapter)")
        if a.what == "credits":
            return Outcome(False, "The credits haven't started")
        if what == "intro":
            to = pos + INTRO_GUESS_MS
            if total and to >= total - 5_000:
                return Outcome(False, "No intro times for this one, and it's nearly over")
            await ctl.seek_to(pid, to)
            log.info("skip: %s intro by guess, %s to %s", np.video_id, _clock(pos), _clock(to))
            return Outcome(True, f"No intro times known: {INTRO_GUESS_MS // 1000} s ahead")
        return Outcome(False, f"No {what} timestamps for this part of the episode; “skip 90 seconds” jumps ahead")

    async def _skip_chapter(self, what: str) -> bool:
        """A chapter named like the part to skip (anime and some releases name them): on to the next chapter."""
        try:
            name = str((await self.kodi.info("Player.ChapterName")).get("Player.ChapterName") or "").lower()
        except Exception:
            return False
        names = {"intro": r"\b(?:intro|opening|op|title sequence|theme)\b", "recap": r"\b(?:recap|previously)\b"}
        if not re.search(names[what], name):
            return False
        await self.kodi.action("chapterorbigstepforward")
        return True

    async def _wrong(self, a: Wrong) -> Outcome:
        """"That's wrong": the last command is marked in the history, to be taught in the panel."""
        if not self.cfg.log_transcripts:
            return Outcome(False, "The history is off, so there's nothing to mark (panel: Settings)")
        e = await asyncio.to_thread(activity.mark_last_wrong)
        if not e:
            return Outcome(False, "Nothing to mark yet")
        return Outcome(True, f"Noted: “{e.get('heard', '')}”. Teach it in the panel (Activity)")

    async def _auto_skip(self, a: AutoSkip) -> Outcome:
        set_value(self.cfg, "auto_skip", a.on)
        if a.on and (np := self.state.now_playing) and np.kind == "series" and np.player in ("kodi", "tvplayer"):
            self._start_skip_watch(np)
        return Outcome(True, "Auto skip on: intros and recaps go by themselves when their timestamps are sure"
                       if a.on else "Auto skip off")

    def _start_skip_watch(self, np: NowPlaying, also: str | None = None):
        if self._skip_task and not self._skip_task.done():
            self._skip_task.cancel()
        self._skip_task = asyncio.create_task(self._skip_watch(np, also=also))

    async def _skip_watch(self, np: NowPlaying, sleep=asyncio.sleep, also: str | None = None):
        """auto_skip: the episode's intro and recap skipped as they come, when their timestamps are sure. `also`:
        that part too, sure or not, because it was asked for ("play The Bear and skip the intro")."""
        try:
            await sleep(4)                               # the file is open and moving
            if (pid := await self._player_id()) is None:
                return
            _, total, _ = await self._ctl(pid).position(pid)
            auto = ("intro", "recap") if self.cfg.auto_skip else ()
            todo = sorted((s for s in await self.segments(np, total)
                           if (s.sure and s.kind in auto) or s.kind == also), key=lambda s: s.start_ms)
            for seg in todo:
                while True:
                    if self.state.now_playing is not np or (pid := await self._player_id()) is None:
                        return
                    pos, _, speed = await self._ctl(pid).position(pid)
                    if pos >= seg.end_ms - 3_000:
                        break                            # past it already (resumed later on, or skipped by hand)
                    if pos >= seg.start_ms:
                        await self._ctl(pid).seek_to(pid, seg.end_ms)
                        await self.notify(f"Skipped the {seg.kind}")
                        log.info("auto skip: %s %s %s-%s", np.video_id, seg.kind, _clock(seg.start_ms),
                                 _clock(seg.end_ms))
                        break
                    ahead = (seg.start_ms - pos) / 1000 / (speed if speed > 0 else 1)
                    await sleep(min(max(ahead - 0.3, 0.25), 10) if speed else 5)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("auto skip: %s", type(e).__name__)

    # ---- your Stremio library: add, remove, mark watched ----

    async def stremio_videos(self, imdb: str) -> list[dict] | None:
        """The show's videos as your Stremio gets them (its first addon with metadata for the title, else
        Cinemeta); watched.stremio_order puts them in the order the marks use. None when it can't be had."""
        try:
            for a in await self.addons():
                if a.serves("meta", "series", imdb):
                    r = await self.http.get(f"{a.base}/meta/series/{imdb}.json", timeout=8, follow_redirects=True)
                    r.raise_for_status()
                    return [v for v in (r.json().get("meta") or {}).get("videos") or []
                            if isinstance(v, dict) and v.get("id")] or None
            return (await cinemeta.meta(self.http, "series", imdb)).get("videos") or None
        except Exception as e:
            log.info("episode list for watched marks: %s", type(e).__name__)
            return None

    async def _subject(self, a: LibraryEdit) -> tuple[str, str, str, int | None, int | None] | None:
        """(imdb id, kind, name, season, episode) a library command is about: the title said, else what's
        playing, else what you watched last. No season: the whole title."""
        if a.title:
            found = await self.resolve(a.title, a.kind, a.season)
            return (*found, a.season, a.episode) if found else None
        np = self.state.now_playing
        if np:
            m = re.match(r"^tt\d+:(\d+):(\d+)$", np.video_id)
            return np.imdb_id, np.kind, np.name, *((int(m.group(1)), int(m.group(2))) if m else (None, None))
        row = await self.library.continue_watching()
        if not row:
            return None
        m = re.match(r"^tt\d+:(\d+):(\d+)$", (row[0].get("state") or {}).get("video_id") or "")
        return (row[0]["_id"], row[0]["type"], row[0].get("name", ""),
                *((int(m.group(1)), int(m.group(2))) if m else (None, None)))

    async def _library(self, a: LibraryEdit) -> Outcome:
        if not self.account:
            return Outcome(False, "Not signed in to Stremio (tv-agent setup stremio)")
        found = await self._subject(a)
        if not found:
            return Outcome(False, f"Couldn't find “{a.title}”" if a.title else "Nothing playing or watched lately")
        imdb, kind, name, season, episode = found
        now = now_iso()
        # A fresh copy: another Stremio may have changed the item since the cached one (it's written back whole).
        item = next((i for i in await self.library.items(fresh=True) if i.get("_id") == imdb), None)
        if item is None:
            if a.op in ("remove", "unwatched"):
                return Outcome(False, f"{name} isn't in your library")
            meta = await cinemeta.meta(self.http, kind, imdb)
            item = {"_id": imdb, "name": name, "type": kind, "poster": meta.get("poster"), "posterShape": "poster",
                    "removed": True, "temp": True, "_ctime": now, "behaviorHints": {}, "state": {}}
        item = {**item, "state": dict(item.get("state") or {}), "_mtime": now}
        st, on = item["state"], a.op == "watched"
        what = name
        if a.op == "add":
            item.update(removed=False, temp=False)
            say = f"Added {name} to your library"
        elif a.op == "remove":
            item.update(removed=True, temp=False)
            say = f"Removed {name} from your library"
        elif kind == "movie":
            st.update(flaggedWatched=int(on), timesWatched=max(1, st.get("timesWatched") or 0) if on else 0)
            if on:
                st.update(timeOffset=0, lastWatched=now)
            say = f"{name}: marked as {'watched' if on else 'not watched'}"
        else:
            videos = await self.stremio_videos(imdb)
            if not videos:
                return Outcome(False, "Couldn't get the episode list; nothing changed")
            if season is not None and episode is not None:
                chosen = [f"{imdb}:{season}:{episode}"]
                what = f"{name} S{season}E{episode}"
            else:                              # a season, or the whole show: the episodes out so far
                today = datetime.now(timezone.utc)
                chosen = [v["id"] for v in videos if v.get("season") and (season is None or v["season"] == season)
                          and (when := cinemeta.released(v)) and when <= today]
                what = f"{name} season {season}" if season is not None else name
            marks = watched.mark(st.get("watched"), videos, chosen, on)
            if not chosen or marks is None:
                return Outcome(False, f"Couldn't line up Stremio's watched marks for {what}; nothing changed")
            st["watched"] = marks
            if on and st.get("video_id") in chosen:
                st["timeOffset"] = 0
            say = f"{what}: marked as {'watched' if on else 'not watched'}"
        await self.account.put([item])
        self.library.invalidate()
        return Outcome(True, say)

    # ---- executors: apps, links, transport, volume ----

    async def _open_app(self, a: OpenApp) -> Outcome:
        app = self.rules.apps.get(a.app)
        if not app:
            return Outcome(False, f"No app called {a.app} in the app list")
        await self.tv(apps.open_app, app)
        return Outcome(True, f"Opening {app.label}")

    async def _home(self, a: Home) -> Outcome:
        await self.tv(apps.home)
        return Outcome(True, "Home")

    async def _youtube(self, a: YouTube) -> Outcome:
        hit = await asyncio.to_thread(youtube.first_video, a.query)
        if not hit:
            return Outcome(False, f"Nothing on YouTube for “{a.query}”")
        vid, name = hit
        await self.tv(apps.view, *self.rules.link("youtube", id=vid))
        return Outcome(True, f"YouTube: {name}")

    async def _twitch(self, a: Twitch) -> Outcome:
        login = twitch.resolve(a.channel, self.cfg.twitch_favorites)
        if not login:
            return Outcome(False, f"Which Twitch channel is “{a.channel}”?")
        await self.tv(apps.view, *self.rules.link("twitch", channel=login))
        return Outcome(True, f"Twitch: {login}")

    async def _open_in_stremio(self, a: OpenInStremio) -> Outcome:
        m = await self.find_title(a.title, a.kind, a.season)
        if not m:
            return Outcome(False, f"Couldn't find “{a.title}”")
        video = await self.episode_id(m, a.season, a.episode)
        app, uri = self.rules.link("stremio", type=m.type, id=m.id, video=video or "")
        await self.tv(apps.view, app, uri, True)
        return Outcome(True, f"Stremio: {m.label}")

    async def front_app(self) -> str | None:
        """The app on screen under the TV app's pill: its package, "home" for the launcher; None if unknown. The
        activity manager lists the tasks top first."""
        try:
            out = await self.tv(lambda adb: adb.run(["sh", "-c", "dumpsys activity activities | grep -m12 -E "
                                                                  "'^ +\\* Task\\{'"], timeout=4))
        except Exception as e:
            log.info("app in front: %s", type(e).__name__)
            return None
        for line in out.splitlines():
            m = re.search(r"A=\d+:([\w.]+)", line)
            if not m or m.group(1) == OWN_APP:
                continue
            return "home" if "type=home" in line else m.group(1)
        return None

    async def _transport(self, a: Transport) -> Outcome:
        if self.tvplayer.now() is not None:
            # TV Agent Player has a file open, so it's on screen (it closes when another app comes to the front):
            # no need to ask the TV which app is in front (~1 s)
            return await self._controlled(a, tvplayer.PID)
        front, pid = await asyncio.gather(self.front_app(), self._kodi_player())
        other = front not in (None, "home", KODI, justplayer.PACKAGE, tvplayer.PACKAGE)   # another app gets the keys
        if other:
            log.info("%s for %s, the app in front", a.op, front)
            if a.op == "seek_to":
                return Outcome(False, "Jumping to a time works in TV Agent Player, Kodi and Just Player only")
            await self.tv(media.dispatch, a.op)
            if a.op == "stop":                   # stop goes back to Home, in any app ("stop and go to the menu")
                await self.tv(apps.home)
            return Outcome(True, "Play/pause" if a.op == "toggle" else a.op.capitalize())
        if pid is not None and a.op in ("seek_to", "forward", "back") and (
                (np := self.state.now_playing) and np.dolby_vision or front not in (KODI, None)):
            now, total, _ = await self.kodi.position(pid)
            if a.op == "seek_to":
                target = int(total * a.percent / 100) if a.percent is not None else (a.seconds or 0) * 1000
            else:
                step = (a.seconds or (30 if a.op == "forward" else 10)) * 1000
                target = now + step if a.op == "forward" else now - step
            if (out := await self._jump_by_reopening(pid, target)) is not None:
                return out
        if pid is not None:
            return await self._controlled(a, pid)
        if (st := await self._just_status()) is not None:
            return await self._just_transport(a, st)
        if a.op == "seek_to":
            return Outcome(False, "Jumping to a time works in TV Agent Player, Kodi and Just Player only")
        if a.op == "resume":
            sessions = [x for x in await self.tv(media.sessions) if x[1] and x[2] in ("PAUSED", "PLAYING")]
            if any(pkg == KODI for pkg, _, _ in sessions):
                await self.kodi.start()                  # Kodi paused in the background: front, then play
                if (pid := await self._kodi_player()) is not None:
                    await self.kodi.play(pid, True)
                    return Outcome(True, "Resume")
            if not sessions:                             # nothing paused anywhere: what you watched last
                return await self._continue_watching(ContinueWatching(title=None))
        await self.tv(media.dispatch, a.op)
        return Outcome(True, "Play/pause" if a.op == "toggle" else a.op.capitalize())

    async def _controlled(self, a: Transport, pid: int | str) -> Outcome:
        """Transport in a player the agent drives (TV Agent Player or Kodi): exact, in place."""
        ctl = self._ctl(pid)
        if a.op in ("pause", "resume", "toggle"):
            await ctl.play(pid, "toggle" if a.op == "toggle" else a.op == "resume")
        elif a.op == "stop":
            self._user_stopped = True
            await self._note_position(pid)
            await ctl.stop(pid)
            await self.tv(apps.home)         # not Kodi's own menus, nor whatever was behind the player
        elif a.op in ("forward", "back"):
            step = a.seconds or (30 if a.op == "forward" else 10)
            await ctl.seek_by(pid, step if a.op == "forward" else -step)
        elif a.op == "seek_to" and a.percent is not None:
            await ctl.seek_percent(pid, a.percent)
        elif a.op == "seek_to":
            await ctl.seek_to(pid, (a.seconds or 0) * 1000)
        elif a.op in ("next", "previous"):
            return await self._next_episode(NextEpisode(offset=1 if a.op == "next" else -1))
        else:
            await ctl.seek_to(pid, 0)
        if a.op == "seek_to":
            return Outcome(True, f"To {a.percent}%" if a.percent is not None else f"To {_clock((a.seconds or 0) * 1000)}")
        return Outcome(True, "Play/pause" if a.op == "toggle" else a.op.replace("_", " ").capitalize())

    async def _just_transport(self, a: Transport, st: tuple[str, int, float]) -> Outcome:
        """Just Player: media keys for pause/resume/stop and short skips (+15 s / -5 s a press); a longer jump
        reopens the same link at the target time (a few seconds of buffering)."""
        np = self.state.now_playing
        if a.op in ("next", "previous"):
            return await self._next_episode(NextEpisode(offset=1 if a.op == "next" else -1))
        if a.op in ("pause", "resume", "toggle", "stop"):
            await self.tv(media.dispatch, a.op)
            if a.op == "stop":
                await self.tv(apps.home)
            return Outcome(True, "Play/pause" if a.op == "toggle" else a.op.capitalize())
        pos = st[1]
        if a.op in ("forward", "back"):
            step = a.seconds or (30 if a.op == "forward" else 10)
            if step <= 120:
                for _ in range(min(24, justplayer.presses(step, a.op == "forward"))):
                    await self.tv(media.dispatch, a.op)
                return Outcome(True, a.op.capitalize())
            target = pos + step * 1000 * (1 if a.op == "forward" else -1)
        elif a.percent is not None:
            if not np.runtime_ms:
                return Outcome(False, "Don't know this one's length")
            target = np.runtime_ms * a.percent // 100
        else:
            target = (a.seconds or 0) * 1000
        await self.tv(justplayer.start, np.url, np.name, max(0, target))
        return Outcome(True, f"Jumping to {_clock(max(0, target))}")

    async def _volume(self, a: Volume) -> Outcome:
        s = self.state
        base, s.volume_base = s.volume_base, None        # the level before the pill lowered it
        if a.op == "mute":
            current = await self.tv(media.get_volume)
            if base:
                await self.tv(media.set_volume, base, current)   # back where it was, then mute from there
                current = base
            if current:
                s.volume_before_mute = current
            await self.tv(media.set_volume, 0, current)
            return Outcome(True, "Muted")
        if a.op == "unmute":
            await self.tv(media.set_volume, s.volume_before_mute or 12)
            s.volume_before_mute = None
            return Outcome(True, "Sound on")
        current = await self.tv(media.get_volume)
        if base:
            await self.tv(media.set_volume, base, current)
            current = base
        if a.op == "set":
            level = a.amount if a.amount is not None else 12
        else:
            base = current if current is not None else (s.volume_before_mute or 12)
            step = a.amount if a.amount is not None else 5
            level = base + step if a.op == "up" else base - step
        level = max(0, min(100, level))
        await self.tv(media.set_volume, level, current)
        return Outcome(True, f"Volume {level}")

    async def _unclear(self, a: Unclear) -> Outcome:
        return Outcome(False, f"Didn't get that: “{a.heard}”" if a.heard else "Didn't get that")
