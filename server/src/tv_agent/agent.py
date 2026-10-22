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
from .actions import (Action, Binge, ContinueWatching, Home, NextEpisode, OpenApp, OpenInStremio, PlayTitle,
                      Replay, Timer, Tracks, Transport, Twitch, Unclear, Volume, YouTube)
from .config import CONFIG_DIR, Config, set_value
from .decide import rules as rules_mod
from .decide.d1 import D1
from .state import NowPlaying, State
from .stremio import cinemeta, progress, streams, subtitles
from .stremio.account import Account, Addon
from .stremio.library import Library
from .tv import apps, justplayer, media
from .tv.adb import Adb
from .tv.kodi import Kodi, KodiError

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
        self._tracks_task: asyncio.Task | None = None
        self._duration: asyncio.Task | None = None
        self._prefetch: asyncio.Task | None = None
        self._end_task: asyncio.Task | None = None
        self._next_up: tuple[str, str, streams.Stream, float] | None = None   # (video id, label, stream, when)
        self._after_this: str | None = None      # "stop" / "off" when what's playing ends (sleep timer)
        self._sleep: asyncio.Task | None = None   # "turn off in 30 minutes"
        self._user_stopped = False               # a stop that was asked for isn't the end of an episode
        self._duck: asyncio.Task | None = None
        self._ducked: tuple[str, int] | None = None   # ("kodi" or "tv", the volume before listening)

    async def close(self):
        if self._progress:
            self._progress.cancel()
            await asyncio.gather(self._progress, return_exceptions=True)
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

    async def execute(self, action: Action) -> Outcome:
        handler = getattr(self, "_" + action.type.replace("none", "unclear"))
        if action.type not in ("volume", "none", "binge", "timer") and not self.dry_run:
            asyncio.create_task(self._wake())
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
                   resume_ms: int = 0, prepared: streams.Stream | None = None) -> Outcome:
        """Pick a cached debrid stream from your addons, start it in the player (Kodi or Just Player), then follow
        its progress."""
        if not self.account:
            return Outcome(False, "Stremio isn't signed in (tv-agent setup stremio)")
        await self.notify(f"Finding {label}…")
        kodi_ready = None
        if self.cfg.player == "kodi" and not self.dry_run and not await self.kodi.up():
            kodi_ready = asyncio.create_task(self._start_kodi())   # Kodi's cold start overlaps the stream search
        if prepared is not None:            # found and checked while the previous episode played
            found = [prepared]
        else:
            found = [s for s in await self.streams_for(kind, video, meta) if not s.why_dropped]
        if self.cfg.player == "just":
            found = [s for s in found if not s.headers]     # Just Player can't send an addon's extra headers
        if not found:
            return Outcome(False, f"No playable stream for {label}")
        chosen = found[0]
        if self.cfg.probe_streams and prepared is None:
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
        self._user_stopped, self._after_this = False, None
        if self._prefetch and not self._prefetch.done():
            self._prefetch.cancel()
        if self.cfg.player == "just":
            plan = self.track_plan(meta)
            subs = languages.android_locale(plan.subtitles[0]) if plan.subtitles else None
            await self.tv(justplayer.captions, plan.subtitles is not None, subs)
            if (pid := await self._kodi_player()) is not None:
                await self.kodi.stop(pid)                               # one player at a time
            await self.tv(justplayer.start, chosen.url, label, resume_ms)
            self.state.now_playing = NowPlaying(imdb, kind, video, name, "just", chosen.url, runtime,
                                                filename=chosen.filename, size=chosen.size,
                                                video_hash=chosen.video_hash)
            if self.cfg.probe_streams:
                self._duration = asyncio.create_task(self._probe_duration(self.state.now_playing))
        else:
            plan = self.track_plan(meta)
            addon_subs = None
            if plan.subtitles is not None:   # asked now, attached once Kodi has opened the file
                addon_subs = asyncio.create_task(subtitles.fetch(self.http, await self.addons(), kind, video, chosen))
            await self.tv(lambda adb: adb.run(["am", "force-stop", justplayer.PACKAGE]))
            if not (kodi_ready is not None and await kodi_ready):
                await self.kodi.start()          # also brings a Kodi left in the background to the front
            try:
                await self.kodi.prefer(languages.KODI_NAMES.get(plan.audio or "", "mediadefault"),
                                       "none" if plan.subtitles is None
                                       else languages.KODI_NAMES.get(plan.subtitles[0], "original"))
            except KodiError as e:
                log.warning("Kodi language settings: %s", e)
            url = chosen.url + ("|" + urlencode(chosen.headers) if chosen.headers else "")
            await self.kodi.open(url, resume_ms)
            self.state.now_playing = NowPlaying(imdb, kind, video, name, "kodi", url, runtime,
                                                filename=chosen.filename, size=chosen.size,
                                                video_hash=chosen.video_hash)
            self._tracks_task = asyncio.create_task(self._set_tracks(meta, label, addon_subs))
        self._follow(meta)
        self._save_now_playing()
        if kind == "series":
            self._prefetch = asyncio.create_task(self._prepare_next(imdb, name, video, meta))
        quality = f" · {'4K' if chosen.height >= 2160 else f'{chosen.height}p'}" if chosen.height else ""
        return Outcome(True, f"Playing {label}{quality}" + (f" · from {_clock(resume_ms)}" if resume_ms else ""))

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

    async def _stop_following(self):
        if self._progress is asyncio.current_task():
            return                              # called from the follower itself (its end started the next one)
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
                how, last, duration = await progress.track(just_poll if np.player == "just" else kodi_poll,
                                                           self.account, self.library, np, meta)
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
        if self._user_stopped or how != "stopped" or self.dry_run:
            return
        at_end = bool(duration and last is not None and duration - last <= 8000)
        after, self._after_this = self._after_this, None
        if at_end and after == "off":
            await self._turn_off()
            return
        if at_end and after is None and np.kind == "series" and self.cfg.binge:
            out = await self._next_episode(NextEpisode(offset=1))
            await self.notify(out.say)
            if out.ok:
                return
        await self.tv(apps.home)

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
        if (pid := await self._kodi_player()) is not None:
            await self.kodi.stop(pid)
        elif await self._just_status() is not None:
            await self.tv(media.dispatch, "stop")
        if then == "off":
            await self._turn_off()
        else:
            await self.tv(apps.home)

    # ---- subtitles and audio on demand ----

    async def _tracks(self, a: Tracks) -> Outcome:
        pid = await self._kodi_player()
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
        prepared = None
        if self._next_up and self._next_up[0] == eps[i]["id"] and time.monotonic() - self._next_up[3] < 3 * 3600:
            prepared = self._next_up[2]
        self._next_up = None
        return await self.play(imdb, "series", name, eps[i]["id"], _episode_label(name, eps[i]), meta,
                               prepared=prepared)

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
