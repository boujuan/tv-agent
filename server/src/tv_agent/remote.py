"""More of the local API, for the phone app (its listener passes these through, clients.py) and the panel: what's
playing in detail with its tracks and skippable parts, the remote's keys and typing, the TV's volume, the app
shortcuts, the lists to pick from, the activity history and its recordings, and the main settings.

    GET  /v1/now-playing                the title, episode, poster, position, Kodi's tracks, intro/recap/credits
    POST /v1/now-playing/track          {"audio": index} or {"subtitle": index | null} (Kodi)
    POST /v1/remote/key {"key", "long"} a key from KEYS (d-pad, OK, numbers…); "long" holds it
    POST /v1/remote/text {"text"}       typed into the TV's focused field (ASCII: the TV's input command)
    GET  /v1/tv/volume                  {"level"}
    GET  /v1/apps                       the apps the agent may open: [{"key", "label"}]
    GET  /v1/lists?list=recent|new|movies   the numbered list voice shows on the TV, here instead
    POST /v1/lists/pick {"list", "imdb", "video"}   play one of them
    GET  /v1/shown                      the numbered list on the TV now ("number 2" picks), or null
    POST /v1/shown/pick {"number", "id"}   a number of it, as saying it does (409: the TV shows another list now)
    GET  /v1/activity?limit=&before=    the history, newest first     POST /v1/activity/mark {"t", "wrong", "heard"}
    GET  /v1/recording?name=            a kept recording (WAV)
    GET  /v1/settings, POST /v1/settings {…}   the panel Settings tab's playback, language and update settings, live
                                        (audio_language: a language or "original"; subtitles: when they show)

Answers never carry the stream link (it has the debrid key) or a secret; track names are scrubbed of links.
"""
import asyncio
import re
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from . import actions as A
from . import activity
from . import config as config_mod
from . import stt as stt_mod
from .agent import LIST_TTL, Shown
from .decide.router import Decision
from .languages import name as language_name
from .redact import scrub
from .stremio import cinemeta
from .tv import media, tvplayer
from .tv import status as tv_status
from .tv.adb import TvUnreachable

KEYS = {**tv_status.KEYS, "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN", "left": "KEYCODE_DPAD_LEFT",
        "right": "KEYCODE_DPAD_RIGHT", "ok": "KEYCODE_DPAD_CENTER", "menu": "KEYCODE_MENU", "info": "KEYCODE_INFO",
        "guide": "KEYCODE_GUIDE", "search": "KEYCODE_SEARCH", "power": "KEYCODE_POWER", "enter": "KEYCODE_ENTER",
        "delete": "KEYCODE_DEL", "rewind": "KEYCODE_MEDIA_REWIND", "fast_forward": "KEYCODE_MEDIA_FAST_FORWARD",
        "next": "KEYCODE_MEDIA_NEXT", "previous": "KEYCODE_MEDIA_PREVIOUS", "channel_up": "KEYCODE_CHANNEL_UP",
        "channel_down": "KEYCODE_CHANNEL_DOWN", **{str(n): f"KEYCODE_{n}" for n in range(10)},
        # What the TCL remote's gear and input buttons send (its key layout, Vendor_0417_Product_0304.kl): the launcher
        # opens its settings menu and the inputs panel for them, and ignores MENU, INFO and SETTINGS (found 2026-10-23)
        "gear": "KEYCODE_NOTIFICATION", "input": "KEYCODE_TV_INPUT"}
LISTS = {"recent": ("Continue watching", "_recent_items"), "new": ("New episodes of your shows", "_new_episode_items"),
         "movies": ("New films", "_new_film_items")}
EPISODE = re.compile(r"^tt\d+:(\d+):(\d+)$")
LANG = re.compile(r"^[a-z]{3}$")
# The Settings tab's playback and language settings: each applies to the next command (agent.py reads cfg as it goes).
SETTINGS = ("player", "max_height", "max_mbps", "probe_streams", "binge", "auto_skip", "audio_language",
            "subtitle_languages", "subtitles", "native_language", "twitch_favorites", "log_transcripts", "updates",
            "check_episodes")
LANGUAGE_SETTINGS = {"audio_language", "subtitle_languages", "subtitles", "native_language"}
AUDIO = ("eng", "spa", "fre", "ita", "ger", "por", "jpn", "kor", "chi")
SUBTITLES = ("eng", "spa", "chs", "cht", "chi", "fre", "ita", "ger", "por", "jpn", "kor")


def setting(key: str, value):
    """A setting's value checked (and made the type config.toml keeps), or HTTPException 422."""
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    match key:
        case "player" if value in config_mod.PLAYERS:
            return value
        case "max_height" if number and int(value) == value and 240 <= value <= 4320:
            return int(value)
        case "max_mbps" if number and 0 <= value <= 1000:
            return float(value)
        case "probe_streams" | "binge" | "auto_skip" | "log_transcripts" | "check_episodes" if isinstance(value, bool):
            return value
        case "updates" if value in config_mod.UPDATES:
            return value
        case "audio_language" | "native_language" if isinstance(value, str) and LANG.match(value):
            return value
        case "audio_language" if value == "original":
            return value
        case "subtitles" if value in config_mod.SUBTITLES_WHEN:
            return value
        case "subtitle_languages" if (isinstance(value, list) and 1 <= len(value) <= 10 and len(set(value)) == len(value)
                                      and all(isinstance(x, str) and LANG.match(x) for x in value)):
            return value
        case "twitch_favorites" if (isinstance(value, list) and len(value) <= 30 and all(
                isinstance(x, str) and re.fullmatch(r"\w{1,25}", x, re.ASCII) for x in value)):
            return value
    raise HTTPException(422, f"{key}: not a value it takes")


def tracks_json(t: dict) -> dict:
    """Kodi's audio and subtitle tracks, as the phone shows them (names without any link)."""
    def clean(s: dict) -> dict:
        lang = str(s.get("language") or "")
        return {"index": s.get("index"), "language": lang, "label": language_name(lang) if lang else "",
                "name": scrub(str(s.get("name") or ""))[:80]}
    on = bool(t.get("subtitleenabled"))
    return {"audio": [{**clean(s), "codec": s.get("codec") or "", "channels": s.get("channels")}
                      for s in t.get("audiostreams") or []],
            "audio_current": (t.get("currentaudiostream") or {}).get("index"),
            "subtitles": [{**clean(s), "forced": bool(s.get("isforced"))} for s in t.get("subtitles") or []],
            "subtitle_current": (t.get("currentsubtitle") or {}).get("index") if on else None, "subtitles_on": on}


def routes(svc) -> APIRouter:
    r = APIRouter()

    async def body_of(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "a JSON object") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "a JSON object")
        return body

    # ---- what's playing ----

    @r.get("/v1/now-playing")
    async def now_playing(request: Request):
        svc.check(request, lan=False)
        agent = svc.agent
        np = agent.state.now_playing
        out: dict = {"now_playing": None, "player": None, "tracks": None, "segments": []}
        total_ms = 0
        if (pid := await agent._player_id()) is not None:
            ctl = agent._ctl(pid)
            pos, total_ms, speed = await ctl.position(pid)
            out["player"] = {"app": "tvplayer" if pid == tvplayer.PID else "kodi", "position_s": pos // 1000,
                             "duration_s": total_ms // 1000, "playing": speed != 0}
            try:
                out["tracks"] = tracks_json(await ctl.tracks(pid) or {})
            except (httpx.HTTPError, ValueError, tvplayer.PlayerError):
                pass
        elif (st := await agent._just_status()) is not None:
            out["player"] = {"app": "just", "position_s": st[1] // 1000,
                             "duration_s": np.runtime_ms // 1000 if np else 0, "playing": st[0] == "PLAYING"}
        if np is None:
            return out
        item = {k: getattr(np, k) for k in ("imdb_id", "kind", "video_id", "name", "player")}
        ep = EPISODE.match(np.video_id)
        if ep:
            item.update(season=int(ep.group(1)), episode=int(ep.group(2)))
        try:
            meta = await cinemeta.meta(agent.http, np.kind, np.imdb_id)
            item.update(poster=meta.get("poster"), background=meta.get("background"))
            video = next((v for v in meta.get("videos") or [] if ep and v.get("season") == item["season"]
                          and (v.get("episode") or v.get("number")) == item["episode"]), None)
            if video:
                item.update(episode_name=video.get("name") or video.get("title"), thumbnail=video.get("thumbnail"))
        except (httpx.HTTPError, ValueError, AssertionError):
            pass
        out["now_playing"] = item
        if pid is not None and np.player in ("kodi", "tvplayer") and total_ms:
            try:
                out["segments"] = [{"what": s.kind, "start_s": s.start_ms // 1000, "end_s": s.end_ms // 1000}
                                   for s in await agent.segments(np, total_ms)]
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        return out

    @r.post("/v1/now-playing/track")
    async def track(request: Request):
        """One of the player's tracks by its index (the phone's track lists); "subtitle": null hides subtitles."""
        svc.check(request, lan=False)
        body = await body_of(request)
        pid = await svc.agent._player_id()
        if pid is None:
            raise HTTPException(409, "Choosing a track works in TV Agent Player and Kodi")
        ctl = svc.agent._ctl(pid)
        try:
            if "audio" in body:
                await ctl.set_audio(pid, int(body["audio"]))
            if "subtitle" in body:
                await ctl.set_subtitle(pid, None if body["subtitle"] is None else int(body["subtitle"]))
        except (TypeError, ValueError):
            raise HTTPException(422, "audio / subtitle: a track's index") from None
        except tvplayer.PlayerError as e:
            raise HTTPException(409, str(e)) from None
        return {"ok": True}

    # ---- the remote ----

    @r.post("/v1/remote/key")
    async def key(request: Request):
        svc.check(request, lan=False)
        body = await body_of(request)
        code = KEYS.get(str(body.get("key", "")))
        if not code:
            raise HTTPException(422, f"key: one of {', '.join(KEYS)}")
        argv = ["input", "keyevent", *(["--longpress"] if body.get("long") is True else []), code]
        await svc.agent.tv(lambda adb: adb.run(argv, timeout=5))
        return {"ok": True}

    @r.post("/v1/remote/text")
    async def type_text(request: Request):
        """Typed into the field the TV has focused (a search box). Android's input command only types ASCII."""
        svc.check(request, lan=False)
        text = str((await body_of(request)).get("text", ""))
        if not 0 < len(text) <= 200 or not text.isascii() or not text.isprintable():
            raise HTTPException(422, "text: 1-200 letters, digits, spaces and punctuation without accents (the TV's "
                                     "input command types no others)")
        await svc.agent.tv(lambda adb: adb.run(["input", "text", text.replace(" ", "%s")], timeout=10))
        return {"ok": True}

    @r.get("/v1/tv/volume")
    async def volume(request: Request):
        svc.check(request, lan=False)
        try:
            return {"level": await asyncio.to_thread(media.get_volume, svc.panel_adb)}
        except TvUnreachable as e:
            raise HTTPException(503, str(e)) from None

    @r.get("/v1/apps")
    async def apps(request: Request):
        svc.check(request, lan=False)
        return {"apps": [{"key": a.key, "label": a.label} for a in svc.agent.rules.apps.values()]}

    # ---- lists ----

    phone_lists: dict[str, Shown] = {}       # the lists phones show: apart from the one on the TV ("number 2")

    async def show(name: str) -> Shown:
        """The list as voice would show it, for the phone. It doesn't replace the list on the TV, which "number 2"
        and the remote's keys pick from."""
        title, items = LISTS[name]
        phone_lists[name] = Shown(title, await getattr(svc.agent, items)(), time.monotonic())
        return phone_lists[name]

    @r.get("/v1/lists")
    async def lists(request: Request):
        svc.check(request, lan=False)
        name = request.query_params.get("list", "recent")
        if name not in LISTS:
            raise HTTPException(422, f"list: one of {', '.join(LISTS)}")
        shown = await show(name)
        return {"list": name, "title": shown.title,
                "items": [{"number": n, "imdb": i.imdb, "kind": i.kind, "name": i.name, "label": i.label,
                           "detail": i.detail, "poster": i.poster, "video": i.video}
                          for n, i in enumerate(shown.items, 1)]}

    @r.post("/v1/lists/pick")
    async def pick(request: Request):
        """Play an item of a list the phone shows: found again by its title (the list may have changed meanwhile)."""
        svc.check(request, lan=False)
        body = await body_of(request)
        name, imdb, video = body.get("list"), str(body.get("imdb", "")), body.get("video")
        if name not in LISTS:
            raise HTTPException(422, f"list: one of {', '.join(LISTS)}")
        shown = phone_lists.get(name)
        if not shown or time.monotonic() - shown.at > LIST_TTL - 30:
            shown = await show(name)
        number = next((n for n, i in enumerate(shown.items, 1) if i.imdb == imdb and (video is None or i.video == video)),
                      None)
        if number is None:
            raise HTTPException(404, "Not in that list any more")
        item = shown.items[number - 1]
        t = time.perf_counter()
        out = await svc.agent.play_item(item)
        svc.record("phone", item.label, Decision(A.Pick(number=number), "remote", 1.0, "list"), out.ok, out.say,
                   (time.perf_counter() - t) * 1000)
        return {"ok": out.ok, "say": out.say}

    # ---- the numbered list on the TV ----

    def current_list():
        """The list on the TV that "number 2" would pick from now (agent.LIST_TTL), or None."""
        shown = svc.agent._shown
        return shown if shown and time.monotonic() - shown.at <= LIST_TTL else None

    def list_id(shown) -> int:
        return int(shown.at * 1000)

    @r.get("/v1/shown")
    async def shown_now(request: Request):
        """The numbered list on the TV now (suggestions, a title to make sure of, episodes or moments found, "go
        there?"), for a phone to show it and pick with a tap; {"shown": null} when there's none."""
        svc.check(request, lan=False)
        shown = current_list()
        if shown is None:
            return {"shown": None}
        return {"shown": {"id": list_id(shown), "title": shown.title, "age_s": round(time.monotonic() - shown.at),
                          "items": [{"number": n, "label": i.label, "detail": i.detail, "poster": i.poster or None}
                                    for n, i in enumerate(shown.items, 1)]}}

    @r.post("/v1/shown/pick")
    async def shown_pick(request: Request):
        """A number of the list on the TV, as saying it does; with the id the phone saw, refused when the TV shows
        another list by now."""
        svc.check(request, lan=False)
        body = await body_of(request)
        number, seen = body.get("number"), body.get("id")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise HTTPException(422, "number: 1 or more")
        shown = current_list()
        if shown is None:
            raise HTTPException(404, "No list on screen")
        if seen is not None and seen != list_id(shown):
            raise HTTPException(409, "The TV shows another list now")
        if number > len(shown.items):
            raise HTTPException(422, f"The list goes up to {len(shown.items)}")
        item = shown.items[number - 1]
        t = time.perf_counter()
        out = await svc.agent.play_item(item)
        svc.record("phone", item.label, Decision(A.Pick(number=number), "remote", 1.0, "shown"), out.ok, out.say,
                   (time.perf_counter() - t) * 1000)
        return {"ok": out.ok, "say": out.say}

    # ---- activity ----

    @r.get("/v1/activity")
    async def activity_list(request: Request):
        svc.check(request, lan=False)
        q = request.query_params
        try:
            limit = max(1, min(500, int(q.get("limit", 100))))
            before = float(q["before"]) if q.get("before") else None
        except ValueError:
            raise HTTPException(422, "limit: a number; before: a time") from None
        rows = await asyncio.to_thread(activity.read)
        if before is not None:
            rows = [e for e in rows if e["t"] < before]
        return {"entries": rows[:limit], "more": len(rows) > limit, "enabled": svc.cfg.log_transcripts}

    @r.post("/v1/activity/mark")
    async def mark(request: Request):
        """Mark a command wrong (to teach later), or not."""
        svc.check(request, lan=False)
        body = await body_of(request)
        try:
            t = float(body["t"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "t: the entry's time") from None
        heard = body.get("heard")
        e = await asyncio.to_thread(activity.mark, t, bool(body.get("wrong", True)),
                                    heard=str(heard) if heard is not None else None)
        if e is None:
            raise HTTPException(404, "no such entry")
        return {"ok": True, "entry": e}

    @r.get("/v1/recording")
    async def recording(request: Request):
        svc.check(request, lan=False)
        path = stt_mod.recording(request.query_params.get("name", ""))
        if path is None:
            raise HTTPException(404, "no such recording")
        return Response(await asyncio.to_thread(path.read_bytes), media_type="audio/wav")

    # ---- settings ----

    def settings() -> dict:
        """The values, and the choices each takes: `audio` is the languages (native_language's too),
        `audio_language` adds "original", `subtitles_when` is when subtitles show."""
        native = language_name(svc.cfg.native_language)
        audio = [{"code": c, "label": language_name(c)} for c in AUDIO]
        return {"settings": {k: getattr(svc.cfg, k) for k in SETTINGS},
                "choices": {"player": list(config_mod.PLAYERS), "max_height": [2160, 1440, 1080, 720],
                            "audio": audio,
                            "audio_language": [{"code": "original", "label": "Original language"}, *audio],
                            "subtitles": [{"code": c, "label": language_name(c) + {"chs": " (Simplified)",
                                           "cht": " (Traditional)"}.get(c, "")} for c in SUBTITLES],
                            "subtitles_when": [{"code": "foreign", "label": f"Only when the audio isn't {native}"},
                                               {"code": "always", "label": "Always"},
                                               {"code": "off", "label": "Off"}]}}

    @r.get("/v1/settings")
    async def get_settings(request: Request):
        svc.check(request, lan=False)
        return settings()

    @r.post("/v1/settings")
    async def set_settings(request: Request):
        """Some of SETTINGS, all checked first; saved in config.toml and used from the next command."""
        svc.check(request, lan=False)
        body = await body_of(request)
        unknown = set(body) - set(SETTINGS)
        if unknown:
            raise HTTPException(422, f"not settings this changes: {', '.join(sorted(unknown))}")
        new = {k: setting(k, v) for k, v in body.items()}
        for k, v in new.items():
            config_mod.set_value(svc.cfg, k, v)
        if LANGUAGE_SETTINGS & set(new):
            svc.agent._asked.clear()            # what was asked by voice for single titles gives way
        return settings()

    return r

