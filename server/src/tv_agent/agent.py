"""Executes actions on the TV. One Agent per process: it owns the ADB connection, the HTTP client and the state."""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from rapidfuzz import fuzz

from . import __version__, languages, twitch, youtube
from .actions import (Action, ContinueWatching, Home, NextEpisode, OpenApp, OpenInStremio, PlayTitle, Transport,
                      Twitch, Unclear, Volume, YouTube)
from .config import CONFIG_DIR, Config
from .decide import rules as rules_mod
from .decide.d1 import D1
from .state import NowPlaying, State
from .stremio import cinemeta, progress, streams
from .stremio.account import Account, Addon
from .stremio.library import Library
from .tv import apps, justplayer, media
from .tv.adb import Adb
from .tv.kodi import Kodi

log = logging.getLogger(__name__)
SAY_RECEIVER = "com.boujuan.tvagent/.SayReceiver"
NOW_PLAYING_FILE = CONFIG_DIR / "now-playing.json"


@dataclass
class Outcome:
    ok: bool
    say: str


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
        self.d1 = D1(self.http, cfg.d1_url)
        self.rules, self.rules_error = rules_mod.load_safe()
        if self.rules_error:
            log.error("rules: %s; using the defaults", self.rules_error)
        self._addons: list[Addon] = []
        self._addons_at = 0.0
        self._progress: asyncio.Task | None = None
        self._tracks: asyncio.Task | None = None
        self._duration: asyncio.Task | None = None

    async def close(self):
        if self._progress:
            self._progress.cancel()
            await asyncio.gather(self._progress, return_exceptions=True)
        await self.http.aclose()
        self.adb.close()

    async def tv(self, fn, *args):
        """Run a blocking TV call (adb_shell is synchronous) off the event loop."""
        return await asyncio.to_thread(fn, self.adb, *args)

    async def notify(self, text: str):
        """A short message on the TV screen (the app's SayReceiver; ignored if the app isn't installed)."""
        try:
            await self.tv(lambda adb: adb.run(["am", "broadcast", "-n", SAY_RECEIVER, "--es", "text", text[:120]],
                                              timeout=4))
        except Exception as e:
            log.debug("notify failed: %s", e)

    async def execute(self, action: Action) -> Outcome:
        handler = getattr(self, "_" + action.type.replace("none", "unclear"))
        try:
            return await handler(action)
        except Exception as e:
            log.exception("action %s failed", action.type)
            return Outcome(False, f"That didn't work ({type(e).__name__})")

    # ---- Stremio: addons, titles, streams ----

    async def addons(self) -> list[Addon]:
        """Your installed Stremio addons, refreshed hourly. Their URLs (with addon config) stay in memory."""
        if not self.account:
            return []
        if not self._addons or time.monotonic() - self._addons_at > 3600:
            self._addons = await self.account.addons()
            self._addons_at = time.monotonic()
        return self._addons

    def track_plan(self, meta: dict) -> languages.TrackPlan:
        c = self.cfg
        return languages.plan(meta, c.audio_language, c.subtitle_languages, c.native_language)

    async def streams_for(self, kind: str, video_id: str, meta: dict | None = None) -> list[streams.Stream]:
        meta = meta or {}
        found = await streams.fetch(self.http, await self.addons(), kind, video_id)
        plan = self.track_plan(meta)
        return streams.rank(found, self.cfg.max_height, self.cfg.max_mbps, _runtime_min(meta),
                            plan.subtitles is None, plan.audio)

    async def apply_tracks(self, plan: languages.TrackPlan, wait_s: float = 12.0) -> str:
        """Once Kodi knows the file's tracks, switch audio and subtitles per the plan. Returns a short note."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while loop.time() < deadline:
            pid = await self._kodi_player()
            info = await self.kodi.tracks(pid) if pid is not None else {}
            if info.get("audiostreams"):
                break
            await asyncio.sleep(0.5)
        else:
            return ""
        notes = []
        audio = languages.pick_audio(info["audiostreams"], plan.audio) if plan.audio else None
        if audio and audio["index"] != (info.get("currentaudiostream") or {}).get("index"):
            await self.kodi.set_audio(pid, audio["index"])
        if audio is None and plan.audio:
            notes.append(f"no {plan.audio} audio")
        if plan.subtitles is None:
            if info.get("subtitleenabled"):
                await self.kodi.set_subtitle(pid, None)
        else:
            sub = languages.pick_subtitle(info.get("subtitles") or [], plan.subtitles)
            if sub:
                await self.kodi.set_subtitle(pid, sub["index"])
                notes.append(f"{sub.get('language')} subs")
            else:
                notes.append("no subtitles in this file")
        return ", ".join(notes)

    async def find_title(self, title: str, kind: str | None, season: int | None = None,
                         utterance: str | None = None) -> cinemeta.Meta | None:
        """Cinemeta's candidates of both kinds; d1 picks one unless a candidate clearly wins on its own."""
        if kind is None and season is not None:
            kind = "series"   # "the bear season 3" is the 2022 series, not the 1989 film
        hits = cinemeta.ranked(title, await cinemeta.search(self.http, title), kind)
        if kind:
            hits = [h for h in hits if h[1].type == kind] or hits
        if not hits or (hits[0][1].rank > 0 and fuzz.WRatio(title.lower(), hits[0][1].name.lower()) < 70):
            return None
        if len(hits) == 1 or (hits[0][0] - hits[1][0] >= 20 and hits[0][1].name.lower() == title.lower()):
            return hits[0][1]
        # Candidates: Cinemeta's own top hit of each kind (its search knows alternate titles: "la casa de papel"
        # is Money Heist) plus the best name matches. Those top hits go first, since d1 leans to the first option.
        firsts = [h for h in hits if h[1].rank == 0]
        top = (firsts + [h for h in hits if h not in firsts])[:self.rules.title_candidates]
        try:
            pick = await self.d1.choice(utterance or f"play {title}", self.rules.title_question,
                                        {f"c{i}": m.label for i, (_, m) in enumerate(top)})
            log.info("d1 title choice %s (confidence %.2f)", pick.key, pick.confidence)
            return top[int(pick.key[1:])][1]
        except (httpx.HTTPError, KeyError, ValueError, IndexError) as e:
            log.warning("d1 unavailable for title choice (%s); using the best match", type(e).__name__)
            return top[0][1]

    async def resolve(self, title: str, kind: str | None, season: int | None) -> tuple[str, str, str] | None:
        """(imdb id, type, name): your library first (what you watch), then Cinemeta."""
        if kind is None and season is not None:
            kind = "series"
        item = await self.library.find(title, kind)
        if item:
            return item["_id"], item["type"], item.get("name", title)
        m = await self.find_title(title, kind, season)
        return (m.id, m.type, m.name) if m else None

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
                   resume_ms: int = 0) -> Outcome:
        """Pick a cached debrid stream from your addons, start it in the player (Kodi or Just Player), then follow
        its progress."""
        if not self.account:
            return Outcome(False, "Stremio isn't signed in (tv-agent setup stremio)")
        await self.notify(f"Finding {label}…")
        found = [s for s in await self.streams_for(kind, video, meta) if not s.why_dropped]
        if self.cfg.player == "just":
            found = [s for s in found if not s.headers]     # Just Player can't send an addon's extra headers
        if not found:
            return Outcome(False, f"No playable stream for {label}")
        chosen = found[0]
        if self.cfg.probe_streams:
            top = found[:3]
            ok = await asyncio.gather(*(streams.playable(self.http, s) for s in top))
            chosen = next((s for s, good in zip(top, ok) if good), None)
            if chosen is None:
                return Outcome(False, f"The links for {label} didn't answer")
        if self.dry_run:   # nothing reaches the player either
            return Outcome(True, f"(dry run) would play {label} in {self.cfg.player} from {chosen.addon}: "
                                 f"{chosen.filename or chosen.name}" + (f" from {_clock(resume_ms)}" if resume_ms else ""))
        runtime = (_runtime_min(meta) or 0) * 60_000
        await self._stop_following()      # the previous session's last progress write and cleanup come first
        if self.cfg.player == "just":
            plan = self.track_plan(meta)
            subs = languages.android_locale(plan.subtitles[0]) if plan.subtitles else None
            await self.tv(justplayer.captions, plan.subtitles is not None, subs)
            if (pid := await self._kodi_player()) is not None:
                await self.kodi.stop(pid)                               # one player at a time
            await self.tv(justplayer.start, chosen.url, label, resume_ms)
            self.state.now_playing = NowPlaying(imdb, kind, video, name, "just", chosen.url, runtime)
            if self.cfg.probe_streams:
                self._duration = asyncio.create_task(self._probe_duration(self.state.now_playing))
        else:
            await self.tv(lambda adb: adb.run(["am", "force-stop", justplayer.PACKAGE]))
            if not await self.kodi.up():
                await self.kodi.start()
            url = chosen.url + ("|" + urlencode(chosen.headers) if chosen.headers else "")
            await self.kodi.open(url, resume_ms)
            self.state.now_playing = NowPlaying(imdb, kind, video, name, "kodi", url, runtime)
            self._tracks = asyncio.create_task(self._set_tracks(meta, label))
        self._follow(meta)
        self._save_now_playing()
        quality = f" · {'4K' if chosen.height >= 2160 else f'{chosen.height}p'}" if chosen.height else ""
        return Outcome(True, f"Playing {label}{quality}" + (f" · from {_clock(resume_ms)}" if resume_ms else ""))

    async def _set_tracks(self, meta: dict, label: str):
        """Audio and subtitles once Kodi has opened the file (a few seconds for a 4K debrid link)."""
        try:
            note = await self.apply_tracks(self.track_plan(meta))
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

    async def _stop_following(self):
        if self._progress and not self._progress.done():
            self._progress.cancel()
            await asyncio.gather(self._progress, return_exceptions=True)

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

        async def follow():
            try:
                await progress.track(just_poll if np.player == "just" else kodi_poll,
                                     self.account, self.library, np, meta)
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
        meta = await cinemeta.meta(self.http, kind, imdb)
        state = ((await self.library.get(imdb)) or {}).get("state") or {}
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
        return await self.play(imdb, "series", name, eps[i]["id"], _episode_label(name, eps[i]), meta)

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
                await self.kodi.stop(pid)
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
            return Outcome(True, a.op.replace("_", " ").capitalize())
        if (st := await self._just_status()) is not None:
            return await self._just_transport(a, st)
        if a.op == "seek_to":
            return Outcome(False, "Jumping to a time works in Kodi and Just Player only")
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
        if a.op == "mute":
            current = await self.tv(media.get_volume)
            if current:
                s.volume_before_mute = current
            await self.tv(media.set_volume, 0)
            return Outcome(True, "Muted")
        if a.op == "unmute":
            await self.tv(media.set_volume, s.volume_before_mute or 12)
            s.volume_before_mute = None
            return Outcome(True, "Sound on")
        if a.op == "set":
            level = a.amount if a.amount is not None else 12
        else:
            current = await self.tv(media.get_volume)
            if current is None:
                current = s.volume_before_mute or 12
            step = a.amount if a.amount is not None else 5
            level = current + step if a.op == "up" else current - step
        level = max(0, min(100, level))
        await self.tv(media.set_volume, level)
        return Outcome(True, f"Volume {level}")

    async def _unclear(self, a: Unclear) -> Outcome:
        return Outcome(False, f"Didn't get that: “{a.heard}”" if a.heard else "Didn't get that")
