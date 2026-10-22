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
"""
import asyncio
import hmac
import json
import logging
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import actions as A
from . import activity
from .agent import Agent
from .config import Config, lan_address
from .decide import evaluate
from .decide import rules as R
from .decide.router import Decision, Router, normalize
from .languages import name as language_name
from .listen_stream import StreamSession
from .stt import Stt

log = logging.getLogger("tv_agent")
MAX_BODY = 400_000          # 12 s of 16 kHz PCM16
SOURCES = ("tv", "laptop", "text", "mcp")


def describe(a: A.Action) -> str:
    """What the TV pill shows right after you stop talking."""
    match a:
        case A.PlayTitle():
            ep = f" S{a.season}" if a.season is not None else ""
            ep += f"E{a.episode}" if a.episode is not None else ""
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
                on_off = {"on": "Subtitles on", "off": "Subtitles off"}
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
        case A.LibraryEdit():
            what = a.title.title() if a.title else "this"
            return {"add": f"Adding {what} to your library", "remove": f"Removing {what} from your library",
                    "watched": f"Marking {what} as watched", "unwatched": f"Marking {what} as not watched"}[a.op]
        case _:
            return "Didn't get that"


class Service:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.agent = Agent(cfg)
        self.router = Router(self.agent.d1, self.agent.rules)
        self.stt: Stt | None = None
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

    def use_rules(self, rules: R.Rules, error: str | None = None):
        self.agent.rules = self.router.rules = rules
        self.agent.rules_error = error
        self._rules_mtime = self._mtime()
        log.info("rules: %s (%s)", rules.source, ", ".join(f"{v} {k}" for k, v in rules.counts.items()))

    def maybe_reload(self):
        """Pick up a hand-edited rules.json; a broken one is reported and the current rules stay."""
        if self._mtime() == self._rules_mtime:
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

    def record(self, source: str, heard: str, d: Decision, ok, say: str, ms):
        if self.cfg.log_transcripts:
            try:
                activity.append(activity.entry(source, heard, d, ok, say, ms))
            except OSError as e:
                log.warning("activity log: %s", e)

    async def execute(self, d: Decision, heard: str, source: str):
        t = time.perf_counter()
        out = await self.agent.execute(d.action)
        ms = (time.perf_counter() - t) * 1000
        self.agent.state.timings.add(f"execute:{d.action.type}", ms)
        log.info("%s -> %s (%.0f ms)", d.action.type, out.say, ms)
        self.record(source, heard, d, out.ok, out.say, ms)
        return out

    async def run(self, d: Decision, heard: str, source: str):
        """Execute in the background so the pill can close; the result arrives on the TV as a toast."""
        out = await self.execute(d, heard, source)
        if d.action.type in ("play_title", "continue_watching", "next_episode", "youtube", "twitch", "pick",
                             "library", "skip") or not out.ok:
            await self.agent.notify(out.say)

    def start(self, d: Decision, heard: str, source: str):
        if self._running and not self._running.done():
            self._running.cancel()    # a new request replaces one still looking for a stream
        self._running = asyncio.create_task(self.run(d, heard, source))

    async def decide(self, text: str) -> Decision:
        self.maybe_reload()
        t = time.perf_counter()
        d = await self.router.decide(text)
        self.agent.state.timings.add(f"decide:{d.via}", (time.perf_counter() - t) * 1000)
        if self.cfg.log_transcripts:
            log.info("heard %r -> %s via %s (%.2f)", text, d.action.type, d.via, d.confidence)
        return d

    async def hear(self, request: Request) -> str:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            raise HTTPException(413)
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413)
        t = time.perf_counter()
        text = await asyncio.to_thread(self.stt.transcribe, body)
        self.agent.state.timings.add("stt", (time.perf_counter() - t) * 1000)
        return text


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

    async def transcribe(audio: bytes) -> str:
        t = time.perf_counter()
        text = await asyncio.to_thread(svc.stt.transcribe, audio)
        svc.agent.state.timings.add("stt", (time.perf_counter() - t) * 1000)
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
    if sess.endpoint_ms is not None:
        svc.agent.state.timings.add("endpoint", sess.endpoint_ms)
        log.info("decided %.0f ms after the last word", sess.endpoint_ms)
    if d.action.type != "none":
        svc.start(d, text, "tv")
    else:
        svc.record("tv", text, d, False, unclear_say(text), None)
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
            text = await svc.hear(request)
            d = await svc.decide(text)
            await svc.agent.unduck(d.action)
            if d.action.type != "none":
                svc.start(d, text, "tv")
            else:
                svc.record("tv", text, d, False, unclear_say(text), None)
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
            heard = await svc.hear(request)
            d = await svc.decide(heard)
            if d.action.type == "none":
                svc.record("laptop", heard, d, False, unclear_say(heard), None)
                return {"heard": heard, **decision_json(d), "ok": False, "say": unclear_say(heard)}
        out = await svc.execute(d, heard, "laptop")
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

    @app.post("/v1/rules/reset")
    async def reset_rules(request: Request):
        svc.check(request, lan=False)
        return rules_reply(R.reset)

    @app.post("/v1/rules/reload")
    async def reload_rules(request: Request):
        svc.check(request, lan=False)
        return rules_reply(R.load)

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
    log.info("loading speech model")
    svc.stt = await asyncio.to_thread(Stt, cfg.stt_model)
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
