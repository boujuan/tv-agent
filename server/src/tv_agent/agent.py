"""Executes actions on the TV. One Agent per process: it owns the ADB connection, the HTTP client and the state."""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from rapidfuzz import fuzz

from . import __version__, activity, languages, skips, twitch, youtube
from .actions import (Action, AutoSkip, Binge, ContinueWatching, Help, Home, Info, LibraryEdit, NextEpisode, OpenApp,
                      OpenInStremio, Pick, PlayTitle, Replay, Sequence, ShowList, Skip, Timer, Tracks, Transport,
                      Twitch, Unclear, Volume, Wrong, YouTube)
from .commands import LEGEND, SECTIONS, TV_HINT
from .config import CONFIG_DIR, Config, set_value
from .decide import rules as rules_mod
from .decide.d1 import D1
from .decide.llm import Llm
from .state import NowPlaying, State
from .stremio import cinemeta, progress, streams, subtitles, watched
from .stremio import search as title_search
from .stremio.account import Account, Addon, now_iso
from .stremio import library as library_mod
from .stremio.library import Library
from .tv import apps, justplayer, media
from .tv.adb import Adb
from .tv.kodi import Kodi, KodiError

log = logging.getLogger(__name__)
SAY_RECEIVER = "com.boujuan.tvagent/.SayReceiver"
CHOICES = "com.boujuan.tvagent/.ChoicesActivity"
LIST_SIZE = 6                # rows that fit the TV app's list
STARTS = {"play_title", "continue_watching", "next_episode", "pick"}     # actions that start playback
LIST_TTL = 300              # "number 2" refers to a list shown in the last 5 minutes
STREAMS_TTL = 1800          # an addon's answer for a video is reused for 30 minutes (links resolve when played)
NOW_PLAYING_FILE = CONFIG_DIR / "now-playing.json"


@dataclass
class Outcome:
    ok: bool
    say: str


@dataclass(frozen=True)
class ListItem:
    imdb: str
    kind: str
    name: str
    label: str                # "The Bear · S3E2"
    detail: str               # "12 min left", "2 days ago · Doors"
    poster: str = ""
    video: str | None = None  # the episode to play; None: carry on where you were (or the film)


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


def _clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _runtime_min(meta: dict) -> int | None:
    m = re.search(r"(\d+)\s*min", str(meta.get("runtime") or ""))
    return int(m.group(1)) if m else None


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
        self.d1 = D1(self.http, cfg.d1_url, cfg.secret("liquid_api_key"), cfg.hosted_d1_model, cfg.d1_where)
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
        self._duck: asyncio.Task | None = None
        self._ducked: tuple[str, int] | None = None   # ("kodi" or "tv", the volume before listening)

    async def close(self):
        if self._progress and not self._progress.done():
            self._stop_following()                   # its last progress write, then it ends
            try:
                await asyncio.wait_for(asyncio.shield(self._progress), 5)
            except (asyncio.TimeoutError, Exception):
                self._progress.cancel()
        await self.http.aclose()
        self.adb.close()

    async def tv(self, fn, *args):
        """Run a blocking TV call (adb_shell is synchronous) off the event loop."""
        return await asyncio.to_thread(fn, self.adb, *args)

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

    async def execute(self, action: Action) -> Outcome:
        handler = getattr(self, "_" + action.type.replace("none", "unclear"))
        if action.type not in ("volume", "none", "binge", "timer", "auto_skip", "wrong") and not self.dry_run:
            asyncio.create_task(self._wake())
        try:
            return await handler(action)
        except Exception as e:
            log.exception("action %s failed", action.type)
            return Outcome(False, f"That didn't work ({type(e).__name__})")

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
        """Refresh what a play needs before it's asked for (your addons, your library: ~0.4 s each when stale).
        Run when the mic opens and at startup."""
        try:
            await self.addons(max_age=3000)
            if time.monotonic() - self.library._at > 480:
                await self.library.items(fresh=True)
        except Exception as e:
            log.info("warming up: %s", type(e).__name__)

    async def fetch_streams(self, kind: str, video_id: str) -> list[streams.Stream]:
        """Your addons' streams for a video (3-6 s from AIOStreams), reused for 30 minutes, with one lookup in
        flight per video: a lookup started early (prefetch) is the one the play then waits for."""
        key = (kind, video_id)
        if (hit := self._streams.get(key)) and time.monotonic() - hit[0] < STREAMS_TTL:
            return [replace(s) for s in hit[1]]       # copies: ranking marks them
        job = self._stream_jobs.get(key)
        if job is None or (job.done() and (job.cancelled() or job.exception())):
            job = self._stream_jobs[key] = asyncio.create_task(
                streams.fetch(self.http, await self.addons(), kind, video_id))
        found = await asyncio.shield(job)                  # a replaced command doesn't waste the lookup
        self._stream_jobs.pop(key, None)
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
        c = self.cfg
        return languages.plan(meta, c.audio_language, c.subtitle_languages, c.native_language)

    async def streams_for(self, kind: str, video_id: str, meta: dict | None = None) -> list[streams.Stream]:
        meta = meta or {}
        found = await self.fetch_streams(kind, video_id)
        plan = self.track_plan(meta)
        return streams.rank(found, self.cfg.max_height, self.cfg.max_mbps, _runtime_min(meta),
                            plan.subtitles is None, plan.audio)

    async def apply_tracks(self, plan: languages.TrackPlan, wait_s: float = 20.0,
                           addon_subs: asyncio.Task | None = None, settle_s: float = 2.5) -> str:
        """Once Kodi plays the file, switch audio and subtitles per the plan. Subtitle languages the file lacks come
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
        if plan.subtitles is None:
            if info.get("subtitleenabled"):
                await self.kodi.set_subtitle(pid, None)
            await self._recheck_tracks(pid, audio, None, False)
            return ", ".join(notes)
        inside = [t for t in info.get("subtitles") or [] if not t.get("isforced")]
        missing = [lang for lang in plan.subtitles if not any(languages.matches(t, lang) for t in inside)]
        offered = []
        if missing and addon_subs is not None:
            try:
                offered = subtitles.choose(await asyncio.wait_for(asyncio.shield(addon_subs), 15), missing)
            except Exception as e:      # slow or failing addons: the file's own subtitles only
                log.info("addon subtitles: %s", type(e).__name__)
        # The language to show: the first of yours that the file or an addon has.
        first = next((lang for lang in plan.subtitles
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
                         utterance: str | None = None, with_addons: bool = True) -> cinemeta.Meta | None:
        """Candidates of both kinds from Cinemeta and your metadata addons (search.py); d1 picks one unless a
        candidate clearly wins on its own."""
        if kind is None and season is not None:
            kind = "series"   # "the bear season 3" is the 2022 series, not the 1989 film
        try:
            addons = await self.addons() if with_addons else []
        except Exception as e:
            log.info("addons unavailable for the title search (%s); Cinemeta only", type(e).__name__)
            addons = []
        hits = cinemeta.ranked(title, await title_search.search(self.http, title, addons), kind)
        if kind:
            hits = [h for h in hits if h[1].type == kind] or hits
        if not hits or (min(hits[0][1].rank, hits[0][1].extra_rank) > 0 and cinemeta.similarity(title, hits[0][1]) < 70):
            return None
        if self._decisive(title, hits):
            return hits[0][1]
        # A close call: Cinemeta's popularity of the leading few tells a famous title from its namesakes.
        pops = await self._popularity([m for _, m in hits[:4]])
        hits = cinemeta.ranked(title, [m for _, m in hits], kind, pops)
        if self._decisive(title, hits):
            return hits[0][1]
        # Candidates: each source's top hit of each kind (Cinemeta's search knows alternate titles: "la casa de
        # papel" is Money Heist) plus the best name matches. Those top hits go first, since d1 leans to the first.
        firsts = [h for h in hits if h[1].rank == 0 or h[1].extra_rank == 0]
        top = (firsts + [h for h in hits if h not in firsts])[:self.rules.title_candidates]
        if "d1" in self.cfg.decide_off:
            return top[0][1]
        try:
            pick = await self.d1.choice(utterance or f"play {title}", self.rules.title_question,
                                        {f"c{i}": m.label for i, (_, m) in enumerate(top)})
            log.info("d1 title choice %s (confidence %.2f)", pick.key, pick.confidence)
            return top[int(pick.key[1:])][1]
        except (httpx.HTTPError, KeyError, ValueError, IndexError) as e:
            log.warning("d1 unavailable for title choice (%s); using the best match", type(e).__name__)
            return top[0][1]

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

    async def resolve(self, title: str, kind: str | None, season: int | None) -> tuple[str, str, str] | None:
        """(imdb id, type, name): your library first (what you watch), then Cinemeta. A name the search doesn't find
        exactly also goes to the LLM, meanwhile, for the real titles it sounds like ("ceberance": Severance); the
        closest-sounding one that the library or the search has is used."""
        if kind is None and season is not None:
            kind = "series"
        item = await self.library.find(title, kind)
        if item:
            return item["_id"], item["type"], item.get("name", title)
        fix = asyncio.create_task(self._sounds_like(title)) if self.llm_on() else None
        try:
            m = await self.find_title(title, kind, season)
            if m and title_search.said_exactly(title, m) or fix is None:
                return (m.id, m.type, m.name) if m else None
            candidates = await fix
        finally:
            if fix and not fix.done():
                fix.cancel()
        for name in candidates:
            if m and title_search.said_exactly(name, m):
                return m.id, m.type, m.name                  # the search had found it
            log.info("title %r: sounds like %r (LLM)", title, name)
            if item := await self.library.find(name, kind):
                return item["_id"], item["type"], item.get("name", name)
            better = await self.find_title(name, kind, season, with_addons=False)     # a real title: Cinemeta has it
            if better and title_search.said_exactly(name, better):
                return better.id, better.type, better.name
        return (m.id, m.type, m.name) if m else None

    async def _sounds_like(self, heard: str) -> list[str]:
        """The LLM's real titles for a misheard one, kept when they sound like it, closest first."""
        key = library_mod.sounds(heard)
        names = {i.get("name", "") for i in await self.library.items() if i.get("type") in ("movie", "series")}
        names = sorted(names - {""}, key=lambda n: -fuzz.ratio(library_mod.sounds(n), key))[:10]
        scored = [(fuzz.ratio(library_mod.sounds(t), key), t) for t in await self.llm.titles(heard, f"play {heard}",
                                                                                            names)]
        said = library_mod.bare(heard)
        return [t for score, t in sorted(scored, key=lambda x: -x[0])          # stable: the LLM's order on a tie
                if score >= 65 and library_mod.bare(t) != said][:2]

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
                   resume_ms: int = 0, prepared: streams.Stream | None = None) -> Outcome:
        """Pick a cached debrid stream from your addons, start it in the player (Kodi or Just Player), then follow
        its progress."""
        if not self.account:
            return Outcome(False, "Stremio isn't signed in (tv-agent setup stremio)")
        t0 = time.perf_counter()
        asyncio.create_task(self.notify(f"Finding {label}…"))
        kodi_ready = None
        if self.cfg.player == "kodi" and not self.dry_run:
            kodi_ready = asyncio.create_task(self._start_kodi())   # Kodi gets ready (or cold-starts) during the search
        if prepared is not None:            # found and checked while the previous episode played
            found = [prepared]
        else:
            found = [s for s in await self.streams_for(kind, video, meta) if not s.why_dropped]
        if self.cfg.player == "just":
            found = [s for s in found if not s.headers]     # Just Player can't send an addon's extra headers
        if not found:
            if kodi_ready is not None:
                asyncio.create_task(self._home_unless_playing(kodi_ready))
            return Outcome(False, f"No playable stream for {label}")
        t_streams = time.perf_counter()
        chosen, link, checking = found[0], found[0].url, None
        if self.cfg.probe_streams:      # runs while Kodi gets ready; also finds where the link leads
            checking = asyncio.create_task(self._first_playable(found[:3]))
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
            return got is not None or trusted

        if self.dry_run:   # nothing reaches the player either
            if not await checked():
                return Outcome(False, f"The links for {label} didn't answer")
            return Outcome(True, f"(dry run) would play {label} in {self.cfg.player} from {chosen.addon}: "
                                 f"{chosen.filename or chosen.name}" + (f" from {_clock(resume_ms)}" if resume_ms else ""))
        runtime = (_runtime_min(meta) or 0) * 60_000
        plan = self.track_plan(meta)
        if self.cfg.player == "just":
            if not await checked():
                return Outcome(False, f"The links for {label} didn't answer")
            async with self._starting:
                resume_ms = self._replace_session(video, resume_ms)
                subs = languages.android_locale(plan.subtitles[0]) if plan.subtitles else None
                await self.tv(justplayer.captions, plan.subtitles is not None, subs)
                if (pid := await self._kodi_player()) is not None:
                    await self.kodi.stop(pid)                               # one player at a time
                await self.tv(justplayer.start, link, label, resume_ms)
                self.state.now_playing = NowPlaying(imdb, kind, video, name, "just", chosen.url, runtime,
                                                    filename=chosen.filename, size=chosen.size,
                                                    video_hash=chosen.video_hash)
                if self.cfg.probe_streams:
                    self._duration = asyncio.create_task(self._probe_duration(self.state.now_playing))
                self._started(imdb, name, kind, video, meta)
        else:
            await self.tv(lambda adb: adb.run(["am", "force-stop", justplayer.PACKAGE]))
            if not (kodi_ready is not None and await kodi_ready):
                await self.kodi.start()          # also brings a Kodi left in the background to the front
            try:
                await self.kodi.prefer(languages.KODI_NAMES.get(plan.audio or "", "mediadefault"),
                                       "none" if plan.subtitles is None
                                       else languages.KODI_NAMES.get(plan.subtitles[0], "original"))
            except KodiError as e:
                log.warning("Kodi language settings: %s", e)
            t_kodi = time.perf_counter()
            if not await checked():              # what was playing carries on, followed as before
                await self._home_unless_playing()
                return Outcome(False, f"The links for {label} didn't answer")
            t_check = time.perf_counter()
            async with self._starting:
                resume_ms = self._replace_session(video, resume_ms)
                addon_subs = None
                if plan.subtitles is not None:   # asked now for the file chosen, attached once Kodi has opened it
                    addon_subs = asyncio.create_task(subtitles.fetch(self.http, await self.addons(), kind, video,
                                                                     chosen))
                extra = "|" + urlencode(chosen.headers) if chosen.headers else ""
                await self.kodi.open(link + extra, resume_ms)
                t_open = time.perf_counter()
                self.state.now_playing = NowPlaying(imdb, kind, video, name, "kodi", chosen.url + extra, runtime,
                                                    filename=chosen.filename, size=chosen.size,
                                                    video_hash=chosen.video_hash)
                self._tracks_task = asyncio.create_task(self._set_tracks(meta, label, addon_subs))
                if kind == "series" and self.cfg.auto_skip:
                    self._start_skip_watch(self.state.now_playing)
                self._started(imdb, name, kind, video, meta)
            for stage, a, b in (("streams", t0, t_streams), ("kodi", t_streams, t_kodi), ("link", t_kodi, t_check),
                                ("open", t_check, t_open)):
                self.state.timings.add(f"start:{stage}", (b - a) * 1000)
            log.info("start %s: streams %.0f ms, then Kodi ready %.0f ms, link check %.0f ms more, open %.0f ms",
                     video, (t_streams - t0) * 1000, (t_kodi - t_streams) * 1000, (t_check - t_kodi) * 1000,
                     (t_open - t_check) * 1000)
        quality = f" · {'4K' if chosen.height >= 2160 else f'{chosen.height}p'}" if chosen.height else ""
        return Outcome(True, f"Playing {label}{quality}" + (f" · from {_clock(resume_ms)}" if resume_ms else ""))

    def _replace_session(self, video: str, resume_ms: int) -> int:
        """Right before a new file opens, what belonged to the previous start stops: its follower (it would read
        the new file's position; its last progress write finishes on its own), its track fixes, intro watch and
        next-episode preparation, and an "after this one" timer. Returns where to start: a "stop" a moment ago
        on this same episode knows its position better than the library yet."""
        old = self.state.now_playing
        if (resume_ms > 0 and old and old.video_id == video and old.stopped_at
                and time.monotonic() - old.stopped_mono < 600):
            resume_ms = old.stopped_at
        self._stop_following()
        self._user_stopped, self._after_this = False, None
        for task in (self._prefetch, self._tracks_task, self._skip_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        return resume_ms

    def _started(self, imdb: str, name: str, kind: str, video: str, meta: dict):
        """The new session's own tasks: its follower, the restore file, the next episode's preparation."""
        self._follow(meta)
        self._save_now_playing()
        if kind == "series":
            self._prefetch = asyncio.create_task(self._prepare_next(imdb, name, video, meta))

    async def _home_unless_playing(self, kodi_ready: asyncio.Task | None = None):
        """Kodi was brought up for a start that didn't happen: back to Home, unless it's playing something."""
        if kodi_ready is not None:
            await asyncio.gather(kodi_ready, return_exceptions=True)
        try:
            if await self._kodi_player() is None:
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
        playing = (await self._just_status()) if np.player == "just" else (await self._kodi_player())
        if playing is None:
            self.state.now_playing = None
            return
        meta = await cinemeta.meta(self.http, np.kind, np.imdb_id)
        self._follow(meta)
        log.info("picked up %s (%s) again after a restart", np.video_id, np.player)

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

        self._stop_following()                   # never two followers
        stop = self._follow_stop = asyncio.Event()

        async def follow():
            try:
                videos = await self.stremio_videos(np.imdb_id) if np.kind == "series" else None
                how, last, duration = await progress.track(just_poll if np.player == "just" else kodi_poll,
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
        """Half the volume while the pill listens (the TV's sound reaches the remote's microphone): Kodi's own
        volume when it plays (no volume bar on screen), else the TV's while another app plays something."""
        if self.dry_run or self._ducked:
            return
        try:
            if (pid := await self._kodi_player()) is not None:
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
            if where == "kodi":
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
            chosen = next((s for s, good in zip(top, ok) if good), None)
            self._next_up = (nxt["id"], _episode_label(name, nxt), chosen, time.monotonic()) if chosen else None
            if chosen:
                log.info("next up: %s (%s)", self._next_up[1], chosen.filename or chosen.name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("getting the next episode ready: %s", type(e).__name__)

    async def _ended(self, np: NowPlaying, how: str, last: int | None, duration: int):
        """Playback stopped: at the very end of an episode the next one starts (binge mode, unless a sleep timer
        says otherwise); otherwise back to Home rather than the player's menus."""
        if self._user_stopped or how != "stopped" or self.dry_run or self.state.now_playing is not np:
            return                              # asked to stop, or something else has started meanwhile
        at_end = bool(duration and last is not None and duration - last <= 8000)
        after, self._after_this = self._after_this, None
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

    async def _note_position(self, pid: int):
        """Where Kodi is, read just before stopping it: the progress write and a "continue" right after use it."""
        np = self.state.now_playing
        if np and np.player == "kodi" and self._progress and not self._progress.done():   # our file is on
            try:
                np.stopped_at = (await self.kodi.position(pid))[0]
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
            if (pid := await self._kodi_player()) is not None:
                await self._note_position(pid)
                await self.kodi.stop(pid)
            elif await self._just_status() is not None:
                await self.tv(media.dispatch, "stop")
        except Exception as e:                  # the TV goes off (or Home) all the same
            log.warning("sleep timer, stopping the player: %s", type(e).__name__)
        if then == "off":
            await self._turn_off()
        else:
            await self.tv(apps.home)

    # ---- subtitles and audio on demand ----

    async def _tracks(self, a: Tracks) -> Outcome:
        pid = await self._kodi_player()
        if a.subtitles == "toggle":           # "subtitles": off if they're on, else on
            if pid is not None:
                on = bool((await self.kodi.tracks(pid)).get("subtitleenabled"))
            else:
                on = await self._just_status() is not None and await self.tv(justplayer.captions_on)
            a = a.model_copy(update={"subtitles": "off" if on else "on"})
        if pid is None:
            if await self._just_status() is not None and a.subtitles and not a.audio:
                on = a.subtitles != "off"
                lang = a.subtitles if a.subtitles not in ("on", "off") else (self.cfg.subtitle_languages or ["eng"])[0]
                await self.tv(justplayer.captions, on, languages.android_locale(lang) if on else None)
                return Outcome(True, "Subtitles on (from the next start)" if on else "Subtitles off")
            return Outcome(False, "Subtitles and audio language can be changed in Kodi")
        info = await self.kodi.tracks(pid)
        said = []
        if a.audio:
            tracks = info.get("audiostreams") or []
            if a.audio == "original":
                track = next((t for t in tracks if t.get("isoriginal")), None) or (tracks[0] if tracks else None)
            else:
                track = languages.pick_audio(tracks, a.audio)
            if track is None:
                return Outcome(False, f"No {languages.KODI_NAMES.get(a.audio, a.audio)} audio in this file")
            await self.kodi.set_audio(pid, track["index"])
            said.append(f"{languages.name(track.get('language')) or 'original'} audio")
        if a.subtitles == "off":
            await self.kodi.set_subtitle(pid, None)
            said.append("subtitles off")
        elif a.subtitles:
            langs = self.cfg.subtitle_languages if a.subtitles == "on" else [a.subtitles]
            inside = [t for t in info.get("subtitles") or [] if not t.get("isforced")]
            sub = languages.pick_subtitle(inside, langs)
            if sub:
                await self.kodi.set_subtitle(pid, sub["index"])
                said.append(f"{languages.name(sub.get('language'))} subtitles")
            else:
                np = self.state.now_playing
                offered = []
                if np:
                    stream = streams.Stream("", np.url, "", "", np.filename, 0, size=np.size, video_hash=np.video_hash)
                    offered = subtitles.choose(
                        await subtitles.fetch(self.http, await self.addons(), np.kind, np.video_id, stream), langs)
                if not offered:
                    return Outcome(False, "No subtitles in that language")
                lang, best = offered[0]
                await self.kodi.add_subtitle(pid, best.url)
                said.append(f"{languages.name(lang)} subtitles from {best.addon}")
        text = ", ".join(said)
        return Outcome(True, text[:1].upper() + text[1:] if text else "OK")

    async def _replay(self, a: Replay) -> Outcome:
        pid = await self._kodi_player()
        if pid is None:
            return await self._transport(Transport(op="back", seconds=a.seconds))
        was_on = bool((await self.kodi.tracks(pid)).get("subtitleenabled"))
        await self.kodi.seek_by(pid, -a.seconds)
        if not was_on and (await self._tracks(Tracks(subtitles="on"))).ok:
            asyncio.create_task(self._subtitles_off_later(pid, a.seconds + 8))
        return Outcome(True, f"Back {a.seconds} s" + ("" if was_on else " with subtitles for a moment"))

    async def _subtitles_off_later(self, pid: int, seconds: float):
        await asyncio.sleep(seconds)
        try:
            if await self._kodi_player() == pid:
                await self.kodi.set_subtitle(pid, None)
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
            found = await self.resolve(a.title, a.kind, a.season)
            if not found:
                return Outcome(False, f"Couldn't find “{a.title}”")
            imdb, kind, name = found
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
            season = a.season
            if season is None and a.episode is not None:
                # "episode 3 of the bear": the season you're on, else the first
                on = playing_video or state.get("video_id")
                current = next((x for x in eps if x["id"] == on), None)
                season = current["season"] if current else eps[0]["season"]
            if season is not None:
                match = [v for v in eps if v["season"] == season and (a.episode is None or v.get("episode") == a.episode)]
                if not match:
                    return Outcome(False, f"{name} has no season {season}" + (f" episode {a.episode}" if a.episode else ""))
                v = match[0]
            elif a.latest:
                now = datetime.now(timezone.utc)
                v = ([x for x in eps if (when := cinemeta.released(x)) and when <= now] or eps)[-1]
            elif state.get("video_id") and not a.restart:
                v = next((x for x in eps if x["id"] == state["video_id"]), eps[0])   # "play the bear": where you were
            else:
                v = eps[0]
            video, label = v["id"], _episode_label(name, v)
        resume = state.get("timeOffset", 0) if state.get("video_id") == video and not a.restart else 0
        return await self.play(imdb, kind, name, video, label, meta, resume)

    async def _continue_watching(self, a: ContinueWatching) -> Outcome:
        if not a.title and (pid := await self._kodi_player()) is not None:
            await self.kodi.play(pid, True)      # "carry on with the show" while Kodi has it open: just resume
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
        imdb, kind, name = item["_id"], item["type"], item.get("name", "")
        st = item.get("state") or {}
        if st.get("timeOffset") and st.get("duration") and st["timeOffset"] < progress.WATCHED_AT * st["duration"]:
            self.prefetch(kind, st.get("video_id") or imdb)     # where you stopped: its streams meanwhile
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
        return await self.play(imdb, kind, name, video, label, meta, offset)

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
        pid = await self._kodi_player()
        if pid is not None:
            pos, total, _ = await self.kodi.position(pid)
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

    async def _sequence(self, a: Sequence) -> Outcome:
        """Two commands said together, one after the other. After one that starts something, the next waits until
        it plays (a jump or a subtitle change needs the player), and "skip the intro" waits for the intro."""
        said = []
        for i, step in enumerate(a.steps):
            started = i and a.steps[i - 1].type in STARTS
            if started:
                await self._until_playing()
            if started and step.type == "skip" and step.what != "credits":
                out = await self._skip_when_it_comes(step.what)
            else:
                out = await self.execute(step)
            said.append(out.say)
            if not out.ok:
                return Outcome(False, " · ".join(said))
        return Outcome(True, " · ".join(said))

    async def _skip_when_it_comes(self, what: str) -> Outcome:
        """The intro or recap of what just started, skipped as it comes (it may be minutes in, or already past)."""
        pid, np = await self._kodi_player(), self.state.now_playing
        if pid is None or np is None or np.player != "kodi":
            return Outcome(False, "Skipping works in Kodi, for what the agent started")
        pos, total, _ = await self.kodi.position(pid)
        found = [seg for seg in await self.segments(np, total) if seg.kind == what]
        if not found:
            return Outcome(False, f"No {what} timestamps for this episode")
        if all(pos >= seg.end_ms - 3_000 for seg in found):
            return Outcome(True, f"Already past the {what}")
        self._start_skip_watch(np, also=what)
        return Outcome(True, f"Skipping the {what} when it comes")

    async def _until_playing(self, wait_s: float = 20.0):
        loop = asyncio.get_running_loop()
        until = loop.time() + wait_s
        while loop.time() < until:
            if (pid := await self._kodi_player()) is not None and (await self.kodi.position(pid))[1] > 0:
                await asyncio.sleep(1.5)        # past Kodi's own jump to where you stopped
                return
            if await self._just_status() is not None:
                return
            await asyncio.sleep(0.5)

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
        it = shown.items[a.number - 1]
        if it.video is None and (item := await self.library.get(it.imdb)) and library_mod.in_continue_watching(item):
            return await self._continue_item(item)
        self.prefetch(it.kind, it.video or it.imdb)
        meta = await cinemeta.meta(self.http, it.kind, it.imdb)
        state = ((await self.library.get(it.imdb)) or {}).get("state") or {}
        video = it.video or it.imdb
        resume = state.get("timeOffset", 0) if state.get("video_id") == video else 0
        return await self.play(it.imdb, it.kind, it.name, video, it.label, meta, resume)

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
        pid = await self._kodi_player()
        np = self.state.now_playing
        if pid is None or np is None or np.player != "kodi":
            return Outcome(False, "Skipping works in Kodi, for what the agent started")
        pos, total, _ = await self.kodi.position(pid)
        what = "intro" if a.what == "credits" and total and pos < total / 4 else a.what   # "the opening credits"
        found = await self.segments(np, total)
        if what == "credits":
            if not (skips.around(found, "credits", pos, lead_ms=30_000) or (total and pos >= 0.85 * total)):
                return Outcome(False, "The credits haven't started")
            if np.kind == "series" and self.cfg.binge:
                return await self._next_episode(NextEpisode(offset=1))
            self._user_stopped = True
            await self._note_position(pid)
            await self.kodi.stop(pid)
            await self.tv(apps.home)
            return Outcome(True, "Credits skipped")
        if seg := skips.around(found, what, pos):
            await self.kodi.seek_to(pid, seg.end_ms)
            log.info("skip: %s %s %s-%s (%s)", np.video_id, what, _clock(seg.start_ms), _clock(seg.end_ms), seg.source)
            return Outcome(True, f"Skipped the {what}")
        if await self._skip_chapter(what):
            return Outcome(True, f"Skipped the {what} (chapter)")
        if a.what == "credits":
            return Outcome(False, "The credits haven't started")
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
        if a.on and (np := self.state.now_playing) and np.kind == "series" and np.player == "kodi":
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
            if (pid := await self._kodi_player()) is None:
                return
            _, total, _ = await self.kodi.position(pid)
            auto = ("intro", "recap") if self.cfg.auto_skip else ()
            todo = sorted((s for s in await self.segments(np, total)
                           if (s.sure and s.kind in auto) or s.kind == also), key=lambda s: s.start_ms)
            for seg in todo:
                while True:
                    if self.state.now_playing is not np or (pid := await self._kodi_player()) is None:
                        return
                    pos, _, speed = await self.kodi.position(pid)
                    if pos >= seg.end_ms - 3_000:
                        break                            # past it already (resumed later on, or skipped by hand)
                    if pos >= seg.start_ms:
                        await self.kodi.seek_to(pid, seg.end_ms)
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

    async def _transport(self, a: Transport) -> Outcome:
        pid = await self._kodi_player()
        if pid is not None:
            if a.op in ("pause", "resume"):
                await self.kodi.play(pid, a.op == "resume")
            elif a.op == "stop":
                self._user_stopped = True
                await self._note_position(pid)
                await self.kodi.stop(pid)
                await self.tv(apps.home)         # not Kodi's own menus
            elif a.op in ("forward", "back"):
                step = a.seconds or (30 if a.op == "forward" else 10)
                await self.kodi.seek_by(pid, step if a.op == "forward" else -step)
            elif a.op == "seek_to" and a.percent is not None:
                await self.kodi.seek_percent(pid, a.percent)
            elif a.op == "seek_to":
                await self.kodi.seek_to(pid, (a.seconds or 0) * 1000)
            elif a.op in ("next", "previous"):
                return await self._next_episode(NextEpisode(offset=1 if a.op == "next" else -1))
            else:
                await self.kodi.seek_to(pid, 0)
            if a.op == "seek_to":
                return Outcome(True, f"To {a.percent}%" if a.percent is not None else f"To {_clock((a.seconds or 0) * 1000)}")
            return Outcome(True, a.op.replace("_", " ").capitalize())
        if (st := await self._just_status()) is not None:
            return await self._just_transport(a, st)
        if a.op == "seek_to":
            return Outcome(False, "Jumping to a time works in Kodi and Just Player only")
        if a.op == "resume":
            sessions = [x for x in await self.tv(media.sessions) if x[1] and x[2] in ("PAUSED", "PLAYING")]
            if any(pkg == "org.xbmc.kodi" for pkg, _, _ in sessions):
                await self.kodi.start()                  # Kodi paused in the background: front, then play
                if (pid := await self._kodi_player()) is not None:
                    await self.kodi.play(pid, True)
                    return Outcome(True, "Resume")
            if not sessions:                             # nothing paused anywhere: what you watched last
                return await self._continue_watching(ContinueWatching(title=None))
        await self.tv(media.dispatch, a.op)
        return Outcome(True, a.op.capitalize())

    async def _just_transport(self, a: Transport, st: tuple[str, int, float]) -> Outcome:
        """Just Player: media keys for pause/resume/stop and short skips (+15 s / -5 s a press); a longer jump
        reopens the same link at the target time (a few seconds of buffering)."""
        np = self.state.now_playing
        if a.op in ("next", "previous"):
            return await self._next_episode(NextEpisode(offset=1 if a.op == "next" else -1))
        if a.op in ("pause", "resume", "stop"):
            await self.tv(media.dispatch, a.op)
            return Outcome(True, a.op.capitalize())
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
