"""The daemon: two HTTP listeners and the TV app's audio stream, in one process.

LAN, on this computer's address on the TV's network only (lan_address(), port 8765), for the TV app:
    POST /v1/listen/start   the mic pill opened: warm the TV connection while you speak
    POST /v1/utterance      raw 16 kHz mono PCM16 -> transcript, decision, a short reply; the action runs after
    GET  /v1/health
  Requests must come from the TV's addresses and carry the agent token; bodies over 400 KB are refused; one
  utterance at a time (a new one cancels whatever the previous one is still doing).

LAN, port 8767 (stream_port): the same, streamed while you speak, so it's decided in your pause (listen_stream.py).
  app -> laptop  a JSON line {"token": "…", "rate": 16000}, then frames: a 2-byte big-endian length and PCM16 LE
                 audio; a frame of length 0 ends the audio (mic pressed again, the 10 s cap, the app's own limit)
  laptop -> app  JSON lines: {"heard": "…"} as it understands, {"stop": true} when it has decided (stop recording),
                 then {"transcript", "say", "ok"} as /v1/utterance answers; then it closes
  Same address check and token; at most 12 s of audio; one at a time.

Localhost (127.0.0.1:8766), for the panel, the CLI and the MCP server, same token:
    POST /v1/text {"text"}      decide and do it            POST /v1/audio  PCM16 from the laptop's mic
    POST /v1/decide {"text"}    decide only, with the path it took (the panel's diagram)
    POST /v1/action {action}    GET /v1/state    GET /v1/stats
    GET  /v1/rules              POST /v1/rules {rules}  (checked, saved, live)   POST /v1/rules/reset|reload
    POST /v1/eval [{"rules"}]   score the labelled sentences, optionally with rules not saved yet
    GET  /v1/pipeline           the decision steps (order, on/off, which d1, the LLM's model) and what's available
    POST /v1/pipeline {…}       change them: saved in config.toml and live at once
"""
import asyncio
import hmac
import json
import logging
import os
import platform
import re
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from . import __version__
from . import actions as A
from . import activity
from .agent import Agent
from . import config as config_mod
from .config import Config, lan_address
from .decide import evaluate, learned
from .decide import rules as R
from .decide.d1 import WHERE
from .decide.router import STEPS, Decision, Router, normalize
from .languages import name as language_name
from .listen_stream import StreamSession
from . import stt as stt_mod
from .stt import Stt
from .tv import status as tv_status
from .tv.adb import Adb, TvUnreachable
from .stt import model_dir as stt_model_dir

log = logging.getLogger("tv_agent")
MAX_BODY = 400_000          # 12 s of 16 kHz PCM16
SOURCES = ("tv", "laptop", "text", "mcp")


def describe(a: A.Action) -> str:
    """What the TV pill shows right after you stop talking."""
    match a:
        case A.PlayTitle():
            ep = (f"S{a.season}" if a.season is not None else "") + (f"E{a.episode}" if a.episode is not None else "")
            ep = f" {ep}" if ep else ""
            if a.title is None:
                parts = ([f"season {a.season}"] if a.season is not None else []) + \
                        ([f"episode {a.episode}"] if a.episode is not None else [])
                return f"Going to {' '.join(parts)}…"
            if a.latest:
                return f"Finding the latest {a.title.title()}…"
            return f"Finding {a.title.title()}{ep}…"
        case A.ContinueWatching():
            return f"Continuing {a.title.title()}…" if a.title else "Continuing…"
        case A.NextEpisode():
            return "Next episode…" if a.offset > 0 else "Previous episode…"
        case A.OpenInStremio():
            return f"{a.title.title()} in Stremio"
        case A.YouTube():
            return f"YouTube: {a.query}"
        case A.Twitch():
            return f"Twitch: {a.channel}"
        case A.OpenApp():
            return f"Opening {a.app.title()}"
        case A.Home():
            return "Home"
        case A.Transport() if a.op == "seek_to" and a.percent == 50:
            return "To the middle"
        case A.Transport() if a.op == "seek_to" and (a.seconds or 0) == 0 and a.percent is None:
            return "From the start"
        case A.Transport():
            return a.op.replace("_", " ").capitalize()
        case A.Volume():
            return {"mute": "Mute", "unmute": "Sound on", "set": f"Volume {a.amount}"}.get(a.op, f"Volume {a.op}")
        case A.Binge():
            return "Binge mode on" if a.on else "Binge mode off"
        case A.Timer():
            if a.cancel:
                return "Sleep timer off"
            what = "Turning off" if a.then == "off" else "Stopping"
            return f"{what} in {a.minutes} min" if a.minutes else f"{what} after this one"
        case A.Tracks():
            parts = []
            if a.audio:
                parts.append("Original audio" if a.audio == "original" else f"{language_name(a.audio)} audio")
            if a.subtitles:
                on_off = {"on": "Subtitles on", "off": "Subtitles off", "toggle": "Subtitles"}
                parts.append(on_off.get(a.subtitles) or f"{language_name(a.subtitles)} subtitles")
            return ", ".join(parts)
        case A.Replay():
            return f"Back {a.seconds} s"
        case A.ShowList():
            return {"recent": "What you were watching…", "new": "New episodes…", "movies": "New films…"}[a.list]
        case A.Pick():
            return f"Number {a.number}…"
        case A.Skip():
            return f"Skipping the {a.what}"
        case A.AutoSkip():
            return "Auto skip on" if a.on else "Auto skip off"
        case A.Wrong():
            return "Noted"
        case A.Help():
            return "Lo que puedes decir…" if a.lang == "es" else "What you can say…"
        case A.Info():
            return f"{a.title.title()}…" if a.title else "What's on…"
        case A.Sequence():
            return " · ".join(describe(x) for x in a.steps)
        case A.LibraryEdit():
            what = a.title.title() if a.title else "this"
            return {"add": f"Adding {what} to your library", "remove": f"Removing {what} from your library",
                    "watched": f"Marking {what} as watched", "unwatched": f"Marking {what} as not watched"}[a.op]
        case _:
            return "Didn't get that"


STT_ORDER = {"local": ("local",), "hosted": ("hosted",), "local-first": ("local", "hosted"),
             "hosted-first": ("hosted", "local")}
HALF_SECOND = 16000             # bytes of 16 kHz 16-bit mono audio
STARTED = time.time()


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.agent = Agent(cfg)
        self.router = Router(self.agent.d1, self.agent.rules, self.agent.llm, self.agent.llm_context, cfg.decide_steps,
                             cfg.decide_off)
        self._learned_mtime = 0.0
        self._learn(self.agent.rules)
        self.stt: Stt | None = None             # the local speech model, loaded when stt_where needs it
        self._stt_name = ""                     # which one (stt_model may change while it loads)
        self._stt_gen = 0                       # bumped by unload_stt, so a load finishing after it isn't kept
        self._stt_loading = asyncio.Lock()
        self._panel_adb: Adb | None = None
        self.token = cfg.secret("agent_token") or ""
        self._busy = asyncio.Lock()
        self._running: asyncio.Task | None = None
        self._rules_mtime = self._mtime()

    # ---- rules, live ----

    @staticmethod
    def _mtime() -> float:
        try:
            return R.USER_FILE.stat().st_mtime
        except OSError:
            return 0.0

    def _learn(self, rules: R.Rules):
        """The sentences you taught (learned.json) on top of the rules' fixed phrases."""
        for x in learned.load():
            rules.phrases[normalize(x["say"], rules)] = (x["do"], "learned")
        self._learned_mtime = learned.mtime()

    def use_rules(self, rules: R.Rules, error: str | None = None):
        self._learn(rules)
        self.agent.rules = self.router.rules = rules
        if self.agent.llm:
            self.agent.llm.rules = rules
        self.agent.rules_error = error
        self._rules_mtime = self._mtime()
        log.info("rules: %s (%s)", rules.source, ", ".join(f"{v} {k}" for k, v in rules.counts.items()))

    def maybe_reload(self):
        """Pick up a hand-edited rules.json (or newly taught sentences); a broken one is reported and the current
        rules stay."""
        if self._mtime() == self._rules_mtime and learned.mtime() == self._learned_mtime:
            return
        try:
            self.use_rules(R.load())
        except (R.RulesError, ValueError, OSError) as e:
            self.agent.rules_error = f"{R.USER_FILE.name}: {e}"
            self._rules_mtime = self._mtime()
            log.error("rules not reloaded: %s", e)

    # ---- requests ----

    def check(self, request: Request, lan: bool):
        allowed = set(self.cfg.tv_hosts) | ({self.agent.adb.host} if self.agent.adb.host else set())
        if lan and (request.client is None or request.client.host not in allowed):
            raise HTTPException(403)
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not self.token or not hmac.compare_digest(given.encode(), self.token.encode()):
            raise HTTPException(401)

    def record(self, source: str, heard: str, d: Decision, ok, say: str, ms, extra: dict | None = None):
        if self.cfg.log_transcripts:
            try:
                activity.append(activity.entry(source, heard, d, ok, say, ms, extra))
            except OSError as e:
                log.warning("activity log: %s", e)

    async def execute(self, d: Decision, heard: str, source: str, extra: dict | None = None):
        t = time.perf_counter()
        out = await self.agent.execute(d.action)
        ms = (time.perf_counter() - t) * 1000
        self.agent.state.timings.add(f"execute:{d.action.type}", ms)
        log.info("%s -> %s (%.0f ms)", d.action.type, out.say, ms)
        self.record(source, heard, d, out.ok, out.say, ms, extra)
        return out

    async def run(self, d: Decision, heard: str, source: str, extra: dict | None = None):
        """Execute in the background so the pill can close; the result arrives on the TV as a toast."""
        out = await self.execute(d, heard, source, extra)
        if d.action.type in ("play_title", "continue_watching", "next_episode", "youtube", "twitch", "pick",
                             "library", "skip", "wrong", "info", "sequence") or not out.ok:
            await self.agent.notify(out.say)

    def start(self, d: Decision, heard: str, source: str, extra: dict | None = None):
        if d.action.type not in ("volume", "binge", "timer", "auto_skip", "info", "help"):
            # A new request replaces one still starting something (a stream search, a binge start); a volume or
            # mode change, a question or the help on the way doesn't.
            for task in (self._running, self.agent._end_task):
                if task and not task.done():
                    task.cancel()
        self._running = asyncio.create_task(self.run(d, heard, source, extra))

    async def decide(self, text: str) -> Decision:
        self.maybe_reload()
        t = time.perf_counter()
        d = await self.router.decide(text)
        d.ms = (time.perf_counter() - t) * 1000
        self.agent.state.timings.add(f"decide:{d.via}", d.ms)
        if self.cfg.log_transcripts:
            log.info("heard %r -> %s via %s (%.2f)", text, d.action.type, d.via, d.confidence)
        return d

    async def hear(self, request: Request) -> tuple[str, dict]:
        """A whole recording's text, and how it was heard (for the history)."""
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            raise HTTPException(413)
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413)
        text, info = await self.transcribe(body)
        info["audio_s"] = round(len(body) / 32000, 1)
        info["recording"] = await self.keep_recording(body)
        return text, info

    async def keep_recording(self, pcm: bytes) -> str | None:
        """Kept for the Activity tab when keep_recordings is on (and the history too); a failed save loses only
        the recording, never the command."""
        if not (self.cfg.keep_recordings and self.cfg.log_transcripts):
            return None
        try:
            return await asyncio.to_thread(stt_mod.save_recording, pcm)
        except OSError as e:
            log.warning("recording not kept: %s", type(e).__name__)
            return None

    @property
    def panel_adb(self) -> Adb:
        """The panel's own ADB connection (TV status, speed test, screenshot), so its slower reads never hold up
        a voice command on the agent's connection."""
        if self._panel_adb is None:
            self._panel_adb = self.agent.adb if not isinstance(self.agent.adb, Adb) else Adb(self.cfg)
        return self._panel_adb

    # ---- speech to text: hosted or on this laptop ----

    async def local_stt(self, keep: bool = True) -> Stt | None:
        """The local speech model (stt_model), loaded on first need (~2-5 s); None if it can't be. One that was
        changed or unloaded while it loaded isn't kept (the caller still gets it). keep=False: not kept either."""
        async with self._stt_loading:
            while self.stt is None or self._stt_name != self.cfg.stt_model:
                name, gen = self.cfg.stt_model, self._stt_gen
                log.info("loading speech model %s", name)
                try:
                    stt = await asyncio.to_thread(Stt, stt_model_dir(name))
                except Exception as e:
                    log.error("speech model %s: %s", name, e)
                    return None
                if gen != self._stt_gen or not keep:
                    return stt
                self.stt, self._stt_name = stt, name
            return self.stt

    def unload_stt(self):
        self.stt, self._stt_name = None, ""
        self._stt_gen += 1

    async def transcribe(self, pcm: bytes, where: str | None = None, model: str | None = None,
                         fallback: bool = True) -> tuple[str, dict]:
        """The text of some audio, where stt_where says: "local-first" / "hosted-first" try the other when the
        first fails (or this laptop's model hears nothing in half a second or more of audio); "hosted" falls back
        to the local model when it's loaded. fallback=False: the first only (the panel comparing models). Also
        how it went: where, how long, what it cost, and what failed before."""
        where = where or self.cfg.stt_where
        order = list(STT_ORDER.get(where, ("local",)))
        if where == "hosted" and self.stt is not None:
            order.append("local")
        if not fallback:
            order = order[:1]
        key = self.cfg.secret("openrouter_api_key")
        model = model or self.cfg.stt_hosted_model
        failed: dict = {}
        for i, how in enumerate(order):
            t = time.perf_counter()
            if how == "hosted":
                if not key:
                    failed = {"stt_error": "no OpenRouter key"}
                    continue
                try:
                    text, cost = await stt_mod.hosted_transcribe(self.agent.http, key, model, pcm)
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    log.warning("hosted speech-to-text (%s) didn't answer: %s", model, type(e).__name__)
                    failed = {"stt_error": f"{model}: {type(e).__name__}",
                              "stt_failed_ms": round((time.perf_counter() - t) * 1000)}
                    continue
                info = {"stt": model, "stt_cost": cost}
            else:
                # the panel asking for this laptop's text while hosted only: loaded for this once, not kept
                local = await self.local_stt(keep=fallback or self.cfg.stt_where != "hosted")
                if local is None:
                    failed = {"stt_error": "no local speech model"}
                    continue
                text = await asyncio.to_thread(local.transcribe, pcm)
                info = {"stt": "local"}
                if not text.strip() and i < len(order) - 1 and len(pcm) >= HALF_SECOND:
                    failed = {"stt_error": "this laptop heard nothing",
                              "stt_failed_ms": round((time.perf_counter() - t) * 1000)}
                    continue
            ms = (time.perf_counter() - t) * 1000
            self.agent.state.timings.add("stt", ms)
            return text, {**failed, **info, "stt_ms": round(ms)}
        return "", {"stt": model if order[0] == "hosted" else "local", **failed}


async def stream_client(svc: "Service", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """One mic press, streamed (protocol in the module docstring)."""
    peer = (writer.get_extra_info("peername") or ("",))[0]

    async def send(msg: dict):
        writer.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
        await writer.drain()

    try:
        allowed = set(svc.cfg.tv_hosts) | ({svc.agent.adb.host} if svc.agent.adb.host else set())
        if peer not in allowed:
            return
        hello = json.loads(await asyncio.wait_for(reader.readline(), 3) or b"{}")
        given = str(hello.get("token") or "")
        if not svc.token or not hmac.compare_digest(given.encode(), svc.token.encode()):
            await send({"error": "unauthorized"})
            return
        if svc._busy.locked():
            await send({"error": "busy"})
            return
        async with svc._busy:
            log.info("TV app streaming (from %s)", peer)
            # Pressed on the screensaver, the app opens behind it, hidden and without the microphone: wake the TV.
            asyncio.create_task(svc.agent._wake())
            asyncio.create_task(svc.agent.warm())    # addons and library fresh before a play is asked for
            svc.agent.start_duck()              # half volume while you speak (the remote's mic hears the TV)
            try:
                await stream_session(svc, reader, send)
            finally:
                await svc.agent.unduck()        # no-op once the session restored it
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError) as e:
        log.info("audio stream from %s ended: %s", peer, type(e).__name__)
    finally:
        writer.close()


async def stream_session(svc: "Service", reader: asyncio.StreamReader, send):
    svc.maybe_reload()
    rules = svc.agent.rules
    heard: dict = {}                    # how the last transcription went, and what they all cost (for the history)

    async def transcribe(audio: bytes) -> str:
        text, info = await svc.transcribe(audio)
        cost = (heard.get("stt_cost") or 0) + (info.get("stt_cost") or 0)     # each checkpoint is billed
        calls = heard.get("stt_calls", 0) + 1
        heard.clear()
        heard.update(info, stt_calls=calls)
        if cost:
            heard["stt_cost"] = cost
        return text

    sess = StreamSession(transcribe, svc.decide, lambda text: normalize(text, rules), send)
    total, ended = 0, False
    while not sess.committed.is_set():
        try:
            n = int.from_bytes(await asyncio.wait_for(reader.readexactly(2), 3), "big")
            if n == 0:
                ended = True
                break
            if n > 8192 or total + n > MAX_BODY:
                break
            data = await asyncio.wait_for(reader.readexactly(n), 3)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            ended = True            # the app went quiet or away: decide on what arrived
            break
        total += n
        sess.feed(data)
    d, text = await sess.finish() or (Decision(A.Unclear(heard=""), "none", 0.0, "", []), "")
    await send({"stop": True})
    await svc.agent.unduck(d.action)
    extra = {**heard, "audio_s": round(len(sess.pcm) / 32000, 1)}
    if sess.endpoint_ms is not None:
        svc.agent.state.timings.add("endpoint", sess.endpoint_ms)
        extra["after_speech_ms"] = round(sess.endpoint_ms)
        log.info("decided %.0f ms after the last word", sess.endpoint_ms)
    extra["recording"] = await svc.keep_recording(bytes(sess.pcm))
    if d.action.type != "none":
        svc.start(d, text, "tv", extra)
    else:
        svc.record("tv", text, d, False, unclear_say(text), None, extra)
    await send({"transcript": text, "say": describe(d.action) if d.action.type != "none" else unclear_say(text),
                "ok": d.action.type != "none"})
    # Read (and drop) what the app sent meanwhile until its end marker: closing with unread audio would reset the
    # connection, and the app could lose the reply above and send the recording again.
    try:
        while not ended and (n := int.from_bytes(await asyncio.wait_for(reader.readexactly(2), 2), "big")):
            await asyncio.wait_for(reader.readexactly(n), 2)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        pass


def unclear_say(text: str) -> str:
    return f"Didn't get that: “{text}”" if text else "Didn't hear anything"


def lan_app(svc: Service) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/v1/health")
    async def health(request: Request):
        svc.check(request, lan=True)
        return {"ok": True}

    @app.post("/v1/listen/start")
    async def listen_start(request: Request):
        svc.check(request, lan=True)
        log.info("TV app listening (from %s)", request.client.host)
        asyncio.create_task(svc.agent._wake())     # the screensaver would hide the pill (see stream_client)
        asyncio.create_task(svc.agent.warm())
        svc.agent.start_duck()
        return {"ok": True}

    @app.post("/v1/choose")
    async def choose(request: Request):
        """An item of the TV app's list, picked with the remote (OK or a number key)."""
        svc.check(request, lan=True)
        try:
            a = A.Pick(number=int((await request.json()).get("number", 0)))
        except (ValueError, TypeError, AttributeError, ValidationError):
            raise HTTPException(422) from None
        svc.start(Decision(a, "remote", 1.0, "list"), f"number {a.number}", "tv")
        return {"ok": True}

    @app.post("/v1/utterance")
    async def utterance(request: Request):
        svc.check(request, lan=True)
        if svc._busy.locked():
            raise HTTPException(429)
        async with svc._busy:
            text, extra = await svc.hear(request)
            d = await svc.decide(text)
            await svc.agent.unduck(d.action)
            if d.action.type != "none":
                svc.start(d, text, "tv", extra)
            else:
                svc.record("tv", text, d, False, unclear_say(text), None, extra)
            return {"transcript": text, "say": describe(d.action) if d.action.type != "none" else unclear_say(text),
                    "ok": d.action.type != "none"}

    return app


def decision_json(d: Decision) -> dict:
    return {"action": d.action.model_dump(), "via": d.via, "rule": d.rule, "confidence": round(d.confidence, 3),
            "trace": d.trace, "describe": describe(d.action)}


def local_app(svc: Service) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/text")
    async def text(request: Request):
        svc.check(request, lan=False)
        body = await request.json()
        heard = str(body.get("text", ""))[:300]
        source = body.get("source") if body.get("source") in SOURCES else "text"
        d = await svc.decide(heard)
        out = await svc.execute(d, heard, source)
        return {"heard": heard, **decision_json(d), "ok": out.ok, "say": out.say}

    @app.post("/v1/audio")
    async def audio(request: Request):
        """The panel's microphone: same as the TV's, but waits for the result to show it."""
        svc.check(request, lan=False)
        if svc._busy.locked():
            raise HTTPException(429)
        async with svc._busy:
            heard, extra = await svc.hear(request)
            d = await svc.decide(heard)
            if d.action.type == "none":
                svc.record("laptop", heard, d, False, unclear_say(heard), None, extra)
                return {"heard": heard, **decision_json(d), "ok": False, "say": unclear_say(heard)}
        out = await svc.execute(d, heard, "laptop", extra)
        return {"heard": heard, **decision_json(d), "ok": out.ok, "say": out.say}

    @app.post("/v1/decide")
    async def decide(request: Request):
        svc.check(request, lan=False)
        svc.maybe_reload()
        return decision_json(await svc.router.decide(str((await request.json()).get("text", ""))[:300]))

    @app.post("/v1/action")
    async def action(request: Request):
        svc.check(request, lan=False)
        try:
            a = A.parse(await request.body())
        except ValidationError as e:
            raise HTTPException(422, str(e.errors()[:2])) from None
        out = await svc.agent.execute(a)
        return {"action": a.model_dump(), "ok": out.ok, "say": out.say}

    @app.get("/v1/state")
    async def state(request: Request):
        svc.check(request, lan=False)
        np = svc.agent.state.now_playing
        player = None
        if (pid := await svc.agent._kodi_player()) is not None:
            pos, total, speed = await svc.agent.kodi.position(pid)
            player = {"app": "kodi", "position_s": pos // 1000, "duration_s": total // 1000, "playing": speed != 0}
        elif (st := await svc.agent._just_status()) is not None:
            player = {"app": "just", "position_s": st[1] // 1000, "duration_s": np.runtime_ms // 1000,
                      "playing": st[0] == "PLAYING"}
        # Never the stream URL: it carries the debrid key.
        shown = {k: getattr(np, k) for k in ("imdb_id", "kind", "video_id", "name", "player")} if np else None
        return {"now_playing": shown, "player": player, "tv": svc.agent.adb.host, "configured_player": svc.cfg.player,
                "rules": {"source": svc.agent.rules.source, "error": svc.agent.rules_error}}

    @app.get("/v1/stats")
    async def stats(request: Request):
        svc.check(request, lan=False)
        return svc.agent.state.timings.summary()

    @app.get("/v1/rules")
    async def get_rules(request: Request):
        svc.check(request, lan=False)
        svc.maybe_reload()
        r = svc.agent.rules
        return {"rules": r.raw, "source": r.source, "counts": r.counts, "error": svc.agent.rules_error,
                "defaults": R.defaults_raw(), "converters": R.CONVERTER_HELP, "builtin_intents": R.BUILTIN_INTENTS,
                "fields": {k: [f for f in m.model_fields if f != "type"] for k, m in A.ACTION_TYPES.items()}}

    def rules_reply(fn):
        try:
            rules = fn()
        except (R.RulesError, ValueError, OSError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
        svc.use_rules(rules)
        return {"ok": True, "source": rules.source, "counts": rules.counts}

    @app.post("/v1/rules")
    async def put_rules(request: Request):
        svc.check(request, lan=False)
        raw = await request.json()
        return rules_reply(lambda: R.save(raw))

    @app.get("/v1/learned")
    async def learned_list(request: Request):
        svc.check(request, lan=False)
        return {"learned": learned.load()}

    @app.post("/v1/learn")
    async def learn(request: Request):
        """Teach one sentence its action (the panel's Activity tab); `t` marks that history entry as taught."""
        svc.check(request, lan=False)
        body = await request.json()
        try:
            item = learned.add(str(body.get("say", ""))[:300], body.get("do") or {})
        except (ValidationError, ValueError) as e:
            raise HTTPException(422, str(e)[:200]) from None
        if body.get("t"):
            activity.mark(float(body["t"]), False, heard=body.get("heard"), taught=item["do"]["type"])
        svc.use_rules(*R.load_safe())
        return {"ok": True, "learned": item}

    @app.post("/v1/learned/remove")
    async def learned_remove(request: Request):
        svc.check(request, lan=False)
        removed = learned.remove(str((await request.json()).get("say", "")))
        svc.use_rules(*R.load_safe())
        return {"ok": removed}

    @app.post("/v1/rules/reset")
    async def reset_rules(request: Request):
        svc.check(request, lan=False)
        return rules_reply(R.reset)

    @app.post("/v1/rules/reload")
    async def reload_rules(request: Request):
        svc.check(request, lan=False)
        return rules_reply(R.load)

    def pipeline() -> dict:
        c, d1 = svc.cfg, svc.agent.d1
        return {"steps": svc.router.steps, "off": sorted(svc.router.off), "d1_where": d1.where,
                "hosted_d1_model": d1.hosted_model, "llm_model": c.llm_model, "llm_reasoning": c.llm_reasoning,
                "llm_private": c.llm_private, "fallback": svc.agent.rules.fallback_intent,
                "keys": {"liquid": bool(c.secret("liquid_api_key")), "openrouter": bool(c.secret("openrouter_api_key")),
                         "hosted_d1": d1.hosted() is not None},
                "counts": svc.agent.rules.counts, "learned": len(learned.load()),
                "stt_where": c.stt_where, "stt_hosted_model": c.stt_hosted_model, "stt_model": c.stt_model,
                "stt_loaded": svc.stt is not None, "keep_recordings": c.keep_recordings, "d1_model": str(c.d1_model),
                "llama_server": c.llama_server}

    @app.get("/v1/pipeline")
    async def get_pipeline(request: Request):
        svc.check(request, lan=False)
        return pipeline()

    @app.post("/v1/pipeline")
    async def set_pipeline(request: Request):
        """The Decisions tab's steps: order, on/off, which d1, the LLM's model. Saved and used at once."""
        svc.check(request, lan=False)
        body = await request.json()
        new = {}
        if "steps" in body:
            steps = [str(x) for x in body["steps"]]
            if sorted(steps) != sorted(STEPS):
                raise HTTPException(422, f"steps: {', '.join(STEPS)}, each once")
            new["decide_steps"] = steps
        if "off" in body:
            off = sorted({str(x) for x in body["off"]})
            if not set(off) <= set(STEPS):
                raise HTTPException(422, f"off: some of {', '.join(STEPS)}")
            new["decide_off"] = off
        if "d1_where" in body:
            if body["d1_where"] not in WHERE:
                raise HTTPException(422, f"d1_where: {', '.join(WHERE)}")
            new["d1_where"] = body["d1_where"]
        if "hosted_d1_model" in body:
            hosted = str(body["hosted_d1_model"]).strip()
            if hosted not in ("d1", "d1-ultra") and not re.fullmatch(r"[\w.~-]+/[\w.:-]{1,80}", hosted):
                raise HTTPException(422, "hosted_d1_model: d1, d1-ultra or an OpenRouter decision model id")
            new["hosted_d1_model"] = hosted
        if "llm_model" in body:
            model = str(body["llm_model"]).strip()
            if not re.fullmatch(r"[\w.~:/-]{3,100}", model):
                raise HTTPException(422, "llm_model: an OpenRouter model id")
            new["llm_model"] = model
        if "llm_reasoning" in body:
            if body["llm_reasoning"] not in ("", "none", "minimal", "low", "medium", "high"):
                raise HTTPException(422, "llm_reasoning: none, minimal, low, medium, high or \"\"")
            new["llm_reasoning"] = body["llm_reasoning"]
        if "llm_private" in body:
            new["llm_private"] = bool(body["llm_private"])
        if "stt_where" in body:
            if body["stt_where"] not in STT_ORDER:
                raise HTTPException(422, f"stt_where: one of {', '.join(STT_ORDER)}")
            new["stt_where"] = body["stt_where"]
        if "stt_hosted_model" in body:
            model = str(body["stt_hosted_model"]).strip()
            if not re.fullmatch(r"[\w.~-]+/[\w.:-]{1,80}", model):
                raise HTTPException(422, "stt_hosted_model: an OpenRouter transcription model id")
            new["stt_hosted_model"] = model
        if "stt_model" in body:
            choice = str(body["stt_model"]).strip()
            if not stt_mod.installed(choice):
                raise HTTPException(422, f"speech model {choice}: not downloaded (or not a model folder)")
            new["stt_model"] = choice
        for key in ("keep_recordings", "llm_private"):
            if key in body and not isinstance(body[key], bool):
                raise HTTPException(422, f"{key}: true or false")
        if "keep_recordings" in body:
            new["keep_recordings"] = body["keep_recordings"]
        if "d1_model" in body:
            path = Path(str(body["d1_model"])).expanduser()
            if path.suffix != ".gguf" or not path.is_file():
                raise HTTPException(422, "d1_model: a .gguf file on this computer")
            if "\n" in str(path) or "\r" in str(path):
                raise HTTPException(422, "d1_model: a path without line breaks")
            new["d1_model"] = str(path)
        if new and config_mod.written(config_mod.LEGACY):      # the settings these replaced: their meaning kept
            for key in ("decide_steps", "decide_off", "d1_where"):
                new.setdefault(key, getattr(svc.cfg, key))
        for key, value in new.items():
            config_mod.set_value(svc.cfg, key, value)
        if new:
            config_mod.unset(config_mod.LEGACY)
        svc.router.steps, svc.router.off = list(svc.cfg.decide_steps), set(svc.cfg.decide_off)
        svc.agent.d1.where, svc.agent.d1.hosted_model = svc.cfg.d1_where, svc.cfg.hosted_d1_model
        if svc.agent.llm:
            llm = svc.agent.llm
            llm.model, llm.reasoning, llm.private = svc.cfg.llm_model, svc.cfg.llm_reasoning, svc.cfg.llm_private
        if "stt_model" in new or body.get("stt_loaded") is False:
            svc.unload_stt()                      # ~1 GB of memory back; the new one loads next (unless hosted only)
        if svc.stt is None and (body.get("stt_loaded") is True or (
                ("stt_model" in new or "stt_where" in new) and svc.cfg.stt_where != "hosted")):
            await svc.local_stt()
        if new:
            log.info("decision steps: %s", ", ".join(f"{k} = {v}" for k, v in new.items()))
        return pipeline()

    @app.post("/v1/transcribe")
    async def transcribe_again(request: Request):
        """A kept recording through a speech model again (the Activity tab compares them): where local or hosted,
        model an OpenRouter transcription model."""
        svc.check(request, lan=False)
        body = await request.json()
        pcm = await asyncio.to_thread(stt_mod.read_recording, str(body.get("recording", "")))
        if pcm is None:
            raise HTTPException(404, "no such recording")
        where = body.get("where", "local")
        model = body.get("model") or None
        if where == "hosted" and not svc.cfg.secret("openrouter_api_key"):
            raise HTTPException(422, "hosted speech-to-text needs the OpenRouter key")
        text, info = await svc.transcribe(pcm, where="hosted" if where == "hosted" else "local", model=model,
                                          fallback=False)
        return {"text": text, **info}

    # ---- the TV, for the panel's Overview ----

    @app.get("/v1/tv/status")
    async def tv_info(request: Request):
        svc.check(request, lan=False)
        adb = svc.panel_adb
        try:
            info = await asyncio.to_thread(tv_status.status, adb)
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"[:300]}, status_code=503)
        info["laptop"] = await asyncio.to_thread(tv_status.laptop_link, adb.host or "")
        info["ping_ms"] = await asyncio.to_thread(tv_status.ping, adb.host or "", 2)
        info["foreground_label"] = next((a.label for a in svc.agent.rules.apps.values()
                                         if a.package == info.get("foreground")), None)
        return info

    @app.get("/v1/tv/packages")
    async def tv_packages(request: Request):
        """The TV app and the players: installed, version, enabled (the panel's Settings and About)."""
        svc.check(request, lan=False)
        try:
            return await asyncio.to_thread(tv_status.packages, svc.panel_adb)
        except TvUnreachable as e:
            return JSONResponse({"error": str(e)}, status_code=503)

    @app.get("/v1/about")
    async def about(request: Request):
        """This running agent: its version, Python, since when, and the models it uses."""
        svc.check(request, lan=False)
        c = svc.cfg
        return {"version": __version__, "python": platform.python_version(), "started": STARTED,
                "pid": os.getpid(), "stt_model": c.stt_model, "stt_where": c.stt_where,
                "stt_hosted_model": c.stt_hosted_model, "d1_model": str(c.d1_model), "d1_where": c.d1_where,
                "hosted_d1_model": c.hosted_d1_model, "llm_model": c.llm_model, "player": c.player}

    @app.post("/v1/tv/speedtest")
    async def tv_speed(request: Request):
        svc.check(request, lan=False)
        try:
            return {"mbps": await asyncio.to_thread(tv_status.speedtest, svc.panel_adb)}
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}"}, status_code=503)

    @app.post("/v1/tv/diagnose")
    async def tv_diagnose(request: Request):
        svc.check(request, lan=False)
        try:
            return {"steps": await asyncio.to_thread(tv_status.diagnose, svc.cfg, svc.panel_adb)}
        except TvUnreachable as e:
            return {"steps": [{"step": "ADB", "ok": False, "detail": str(e), "hint": "Reconnect, or Diagnose again"}]}

    @app.post("/v1/tv/reconnect")
    async def tv_reconnect(request: Request):
        svc.check(request, lan=False)
        await asyncio.to_thread(svc.agent.adb.reconnect)
        await asyncio.to_thread(svc.panel_adb.reconnect)
        try:
            await svc.agent.tv(lambda adb: adb.run(["true"], timeout=5))
            return {"ok": True, "host": svc.agent.adb.host}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__}

    @app.post("/v1/tv/key")
    async def tv_key(request: Request):
        svc.check(request, lan=False)
        key = tv_status.KEYS.get(str((await request.json()).get("key", "")))
        if not key:
            raise HTTPException(422, f"key: one of {', '.join(tv_status.KEYS)}")
        await svc.agent.tv(lambda adb: adb.run(["input", "keyevent", key], timeout=5))
        return {"ok": True}

    @app.get("/v1/tv/screenshot")
    async def tv_screenshot(request: Request):
        svc.check(request, lan=False)
        try:
            png = await asyncio.to_thread(svc.panel_adb.run_bytes, ["screencap", "-p"], 20)
        except TvUnreachable as e:
            raise HTTPException(503, str(e)) from None
        return Response(png, media_type="image/png")

    @app.get("/v1/now-playing/link")
    async def now_playing_link(request: Request):
        """The stream link of what the agent plays: the panel shows it (localhost and the token only; it carries
        the debrid key, so it's never logged)."""
        svc.check(request, lan=False)
        return svc.agent.now_playing_link() or {}

    @app.post("/v1/tv/play-link")
    async def play_link(request: Request):
        svc.check(request, lan=False)
        url = str((await request.json()).get("url", "")).strip()
        out = await svc.agent.play_link(url)
        return {"ok": out.ok, "say": out.say}

    @app.post("/v1/eval")
    async def run_eval(request: Request):
        svc.check(request, lan=False)
        body = await request.body()
        raw = json.loads(body).get("rules") if body else None
        try:
            rules = R.build(raw, "unsaved") if raw else svc.agent.rules
        except R.RulesError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
        return await evaluate.run(Router(svc.agent.d1, rules))

    return app


async def serve(cfg: Config):
    if not cfg.secret("agent_token"):
        raise SystemExit("no agent token: run `tv-agent setup token` and rebuild the TV app")
    svc = Service(cfg)
    if cfg.stt_where != "hosted" or not cfg.secret("openrouter_api_key"):
        await svc.local_stt()               # hosted only: loaded if ever needed, ~1 GB of memory saved
    try:
        await svc.agent.restore()
        asyncio.create_task(svc.agent.warm())
    except Exception as e:
        log.warning("couldn't pick up the last session: %s", type(e).__name__)
    lan_host = lan_address(cfg)
    stream = await asyncio.start_server(lambda r, w: stream_client(svc, r, w), lan_host, cfg.stream_port)
    servers = [
        uvicorn.Server(uvicorn.Config(lan_app(svc), host=lan_host, port=cfg.lan_port, access_log=False,
                                      log_level="warning")),
        uvicorn.Server(uvicorn.Config(local_app(svc), host="127.0.0.1", port=cfg.local_port, access_log=False,
                                      log_level="warning")),
    ]

    async def keepalive():
        while True:
            await asyncio.sleep(50)
            await asyncio.to_thread(svc.agent.adb.keepalive)

    log.info("listening on %s:%d and :%d (TV) and 127.0.0.1:%d (local)", lan_host, cfg.lan_port, cfg.stream_port,
             cfg.local_port)
    ka = asyncio.create_task(keepalive())
    try:
        await asyncio.gather(*(s.serve() for s in servers), stream.serve_forever())
    finally:
        ka.cancel()
        await svc.agent.close()
