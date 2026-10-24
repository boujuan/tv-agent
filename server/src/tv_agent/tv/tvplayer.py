"""TV Agent Player (com.boujuan.tvagent.player, github.com/boujuan/tv-agent-player) as the player: Media3 in an app
of its own on the TV, driven over one TLS connection it opens to this agent when its screen comes up. Its protocol
is that repository's PROTOCOL.md.

The link and its headers only travel inside that connection: never in an ADB command line or a log. ADB only
brings the screen up (`am start`, no extras) and pairs the app (the player's own token, this agent's address and
certificate). The agent listens on its LAN address at player_port, with the phones' certificate (clients.py), which
the player pins; only the TV's addresses may connect, with the token. One player at a time.

Where Kodi is polled, this one tells: state, position every second while playing, tracks, the decoder, errors,
and the position it closed at. `now()` answers from the last of those, without asking the TV.

Its methods have kodi.py's surface (a player id first, Kodi-shaped track lists), so the agent drives either the
same way; PID is this player's id where Kodi's is a number.
"""
import asyncio
import hmac
import itertools
import json
import logging
import secrets
import time
from collections.abc import Callable

from ..config import Config, lan_address, tailscale_address
from .adb import Adb

log = logging.getLogger(__name__)

PACKAGE = "com.boujuan.tvagent.player"
ACTIVITY = f"{PACKAGE}/.PlayerActivity"
PID = "tvplayer"
PROTOCOL = 1
MAX_LINE = 4 << 20
PING_EVERY = 10.0           # the player treats 30 s of silence as a dead connection
SILENCE = 30.0


class PlayerError(RuntimeError):
    pass


def addresses(cfg: Config) -> list[str]:
    """Where the agent listens for the player, in the order the player tries them: the LAN address, phone_hosts, then
    this computer's Tailscale address while Tailscale runs, so the player finds the agent when the computer is away
    from the TV's network too."""
    return list(dict.fromkeys([lan_address(cfg), *cfg.phone_hosts, *filter(None, [tailscale_address()])]))


def kodi_tracks(t: dict) -> dict:
    """The player's track lists (PROTOCOL.md, Track) in Kodi's shape (Player.GetProperties), for languages.py. A
    forced track shown with subtitles off doesn't count as subtitles on."""
    audio = [{"index": a["i"], "language": a.get("lang") or "", "name": a.get("label") or "", "codec": a.get("codec"),
              "channels": a.get("ch"), "isdefault": bool(a.get("default")), "isimpaired": False, "isoriginal": False}
             for a in t.get("audio") or []]
    subs = [{"index": x["i"], "language": x.get("lang") or "",
             "name": (x.get("label") or "") + (" (external)" if x.get("external") else ""),
             "isforced": bool(x.get("forced")), "isimpaired": bool(x.get("sdh")), "isdefault": bool(x.get("default"))}
            for x in t.get("text") or []]
    current_audio = next((k for k, a in zip(audio, t.get("audio") or []) if a.get("selected")), {})
    shown = [k for k, x in zip(subs, t.get("text") or []) if x.get("selected") and not k["isforced"]]
    return {"audiostreams": audio, "currentaudiostream": current_audio, "subtitles": subs,
            "currentsubtitle": shown[0] if shown else {}, "subtitleenabled": bool(shown)}


class TvPlayer:
    def __init__(self, token: Callable[[], str | None], allowed: Callable[[], set[str]], version: str = ""):
        self.token = token                      # read at each hello: a new pairing needs no restart
        self.allowed = allowed                  # the TV's addresses, as of now
        self.version = version
        self.app: str | None = None             # the connected player's version
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._acks: dict[int, asyncio.Future] = {}
        self._waits: list[tuple[Callable[[dict], bool], asyncio.Future]] = []
        self._sent = 0.0
        self.connected = asyncio.Event()
        self.listeners: list[Callable[[dict], None]] = []
        self.item: str | None = None            # what the last load was, as named to the player
        self.last: dict[str, dict] = {}         # the latest event of each type about self.item
        self._pos_at = 0.0
        self._loads = itertools.count(1)
        self._run = secrets.token_hex(3)        # an agent restarted meanwhile names its loads differently
        self._adopt: str | None = None          # after a restart: the first item the player reports that starts so
        self._coming: tuple[str, str] | None = None    # (video, item id) of the start the loading screen is for
        self._no_loading: set[str | None] = set()      # player versions without the loading screen

    # ---- the connection ----

    async def serve(self, host: str, port: int, ssl_ctx) -> asyncio.Server:
        return await asyncio.start_server(self._client, host, port, ssl=ssl_ctx, ssl_handshake_timeout=5,
                                          limit=MAX_LINE)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = (writer.get_extra_info("peername") or ("",))[0]
        try:
            if peer not in self.allowed():
                log.info("player connection from %s refused: not the TV", peer)
                return
            try:
                hello = json.loads(await asyncio.wait_for(reader.readline(), 3) or b"{}")
            except ValueError:
                hello = {}
            hello = hello if isinstance(hello, dict) else {}
            given, token = str(hello.get("token") or ""), self.token() or ""
            if hello.get("t") != "hello" or not token or not hmac.compare_digest(given.encode(), token.encode()):
                await self._write(writer, {"t": "bye", "error": "unauthorized"})
                return
            if hello.get("v") != PROTOCOL:
                await self._write(writer, {"t": "bye", "error": "version"})
                return
            old, self._writer = self._writer, writer
            if old is not None:
                await self._write(old, {"t": "bye", "error": "replaced"})
                old.close()
            self.app = str(hello.get("app") or "")
            await self._write(writer, {"t": "welcome", "v": PROTOCOL, "agent": self.version})
            self.connected.set()
            log.info("player %s connected from %s", self.app, peer)
            pinger = asyncio.create_task(self._ping(writer))
            try:
                while True:
                    line = await asyncio.wait_for(reader.readline(), SILENCE)
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        self._received(msg)
            finally:
                pinger.cancel()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError) as e:
            log.info("player connection from %s ended: %s", peer, type(e).__name__)
        finally:
            if self._writer is writer:
                self._writer = None
                self.connected.clear()
                for fut in self._acks.values():
                    if not fut.done():
                        fut.set_exception(PlayerError("the player went away"))
                self._acks.clear()
                log.info("player disconnected")
            writer.close()

    async def _write(self, writer: asyncio.StreamWriter, msg: dict):
        writer.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
        await writer.drain()
        self._sent = time.monotonic()

    async def _ping(self, writer: asyncio.StreamWriter):
        while True:
            await asyncio.sleep(PING_EVERY)
            if time.monotonic() - self._sent >= PING_EVERY - 0.5:
                await self._write(writer, {"t": "ping"})

    def _received(self, msg: dict):
        t = msg.get("t")
        if t == "ping":
            return
        if t == "ack":
            fut = self._acks.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if self.item is None and self._adopt and str(msg.get("item") or "").startswith(self._adopt):
            self.item, self._adopt = msg["item"], None
        if "item" not in msg or msg.get("item") == self.item:
            self.last[t] = msg
            if t == "pos":
                self._pos_at = time.monotonic()
        for pred, fut in list(self._waits):
            if not fut.done() and pred(msg):
                fut.set_result(msg)
        for listener in self.listeners:
            try:
                listener(msg)
            except Exception:
                log.exception("player event listener")

    @property
    def up(self) -> bool:
        return self._writer is not None

    def _send(self, t: str, **fields) -> tuple[int, asyncio.Future]:
        """A command written at once, in order with the ones after it; (its id, the future of its answer)."""
        writer = self._writer
        if writer is None:
            raise PlayerError("the player isn't open")
        cid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._acks[cid] = fut
        writer.write(json.dumps({"t": t, "id": cid, **fields}, ensure_ascii=False).encode() + b"\n")
        self._sent = time.monotonic()
        return cid, fut

    async def call(self, t: str, timeout: float = 4.0, **fields) -> dict:
        """One command; its answer (with its results), or PlayerError with the player's reason."""
        writer = self._writer
        cid, fut = self._send(t, **fields)
        try:
            await writer.drain()
            answer = await asyncio.wait_for(fut, timeout)
        finally:
            self._acks.pop(cid, None)
        if answer.get("error"):
            raise PlayerError(str(answer["error"]))
        return answer

    async def wait_for(self, pred: Callable[[dict], bool], timeout: float) -> dict | None:
        """The next event that pred accepts, or None after timeout."""
        fut = asyncio.get_running_loop().create_future()
        entry = (pred, fut)
        self._waits.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waits.remove(entry)

    # ---- the screen ----

    @staticmethod
    def launch(adb: Adb):
        """The player's screen up (the link comes over the connection it then opens). A no-op when it's up."""
        adb.run(["am", "start", "-n", ACTIVITY])

    async def ready(self, adb: Adb, wait_s: float = 10.0) -> bool:
        """The screen up and connected to this agent (brought up if it isn't)."""
        if self.up:
            return True
        await asyncio.to_thread(self.launch, adb)
        try:
            await asyncio.wait_for(self.connected.wait(), wait_s)
        except asyncio.TimeoutError:
            return False
        return True

    # ---- what the agent asks of a player (the surface kodi.py has) ----

    def adopt(self, video: str):
        """After the agent restarts: what the player has open, if it's this video, is followed again."""
        self.item, self.last, self._adopt = None, {}, f"{video}#"

    def loading(self, video: str, **fields) -> bool:
        """The loading screen for the start of `video` (PROTOCOL.md, `loading`): what was heard, the title, its
        artwork, the step it's at. Written at once and answered in the background, so a start never waits for it. A
        later call for the same video updates what it gives; the load that follows has the same item id, and its
        first frame takes the screen away. False with no player screen up, or a player older than this screen (it
        answered "unknown command" once: not asked again)."""
        if self._writer is None or self.app in self._no_loading:
            return False
        if self._coming is None or self._coming[0] != video:
            self._coming = (video, f"{video}#{self._run}-{next(self._loads)}")
        cid, fut = self._send("loading", item=self._coming[1], **{k: v for k, v in fields.items() if v})
        asyncio.create_task(self._loading_answered(cid, fut, self.app))
        return True

    @property
    def has_loading(self) -> bool:
        """Whether the connected player has the loading screen, as far as it has said (an older one answers the first
        `loading` with "unknown command", within milliseconds)."""
        return self.up and self.app not in self._no_loading

    async def clear_loading(self):
        """The loading screen taken away while what was playing goes on (`osd` with no text)."""
        await self.call("osd", text="", ms=0)

    async def _loading_answered(self, cid: int, fut: asyncio.Future, app: str | None):
        try:
            answer = await asyncio.wait_for(fut, 4)
        except (asyncio.TimeoutError, PlayerError):
            return
        finally:
            self._acks.pop(cid, None)
        error = str(answer.get("error") or "")
        if error.startswith("unknown command"):
            log.info("player %s has no loading screen", app)
            self._no_loading.add(app)
        elif error:
            log.info("loading screen: %s", error)

    async def open(self, url: str, resume_ms: int = 0, *, item: str, title: str = "", headers: dict | None = None,
                   audio: str | None = None, subs: list[str] | None = None, bitrate: int = 0, play: bool = True,
                   http: str | None = None, info: dict | None = None):
        """Loads it in place (the screen stays: no relaunch). `audio` and `subs` are the track plan
        (languages.TrackPlan), applied in the player's own first choice. Each load gets its own id (`item`#run-n,
        the one its loading screen had), so an event about the one before, even one this agent opened before a
        restart, can't be taken for this one's. `info` is what the pause screen shows (PROTOCOL.md, `info`)."""
        coming, self._coming = self._coming, None
        self.item = coming[1] if coming and coming[0] == item else f"{item}#{self._run}-{next(self._loads)}"
        self.last, self._adopt = {}, None
        fields = {"url": url, "item": self.item, "title": title, "start_ms": max(0, int(resume_ms)), "audio": audio,
                  "subs": subs, "play": play}
        if headers:
            fields["headers"] = headers
        if bitrate:
            fields["bitrate"] = int(bitrate)
        if http:
            fields["http"] = http
        if info:
            fields["info"] = info
        await self.call("load", timeout=6, **fields)

    async def preload(self, url: str, start_ms: int = 0, *, item: str, headers: dict | None = None,
                      audio: str | None = None, subs: list[str] | None = None, bitrate: int = 0,
                      info: dict | None = None):
        fields = {"url": url, "item": f"{item}#preload", "start_ms": max(0, int(start_ms)), "audio": audio, "subs": subs}
        if headers:
            fields["headers"] = headers
        if bitrate:
            fields["bitrate"] = int(bitrate)
        if info:
            fields["info"] = info
        await self.call("preload", **fields)

    def now(self) -> tuple[int, int, float] | None:
        """(position ms now, length ms, speed) of what the last load opened, from the player's last report; None once
        it has closed or ended, or the player is gone."""
        if self.item is None or not self.up or "closed" in self.last or "ended" in self.last:
            return None
        pos, state = self.last.get("pos"), self.last.get("state") or {}
        if pos is None:
            return 0, 0, 0.0
        playing = bool(state.get("playing"))
        at = int(pos.get("pos", 0)) + (int((time.monotonic() - self._pos_at) * 1000) if playing else 0)
        return at, int(pos.get("dur", 0)), 1.0 if playing else 0.0

    async def player(self) -> str | None:
        """PID while something is open in it (as Kodi's player id), else None."""
        return PID if self.now() is not None else None

    async def position(self, pid: str = PID) -> tuple[int, int, float]:
        now = self.now()
        if now is None:
            raise PlayerError("nothing is open in the player")
        return now

    async def play(self, pid: str, on: bool | str):
        """on: True plays, False pauses, "toggle" switches."""
        await self.call("toggle" if on == "toggle" else "play" if on else "pause")

    async def stop(self, pid: str):
        """Stops and closes the screen (`closed` comes first, with the position)."""
        await self.call("stop")

    async def seek_by(self, pid: str, seconds: int):
        await self.call("seek_by", ms=int(seconds) * 1000)

    async def seek_percent(self, pid: str, percent: float):
        _, total, _ = await self.position(pid)
        await self.seek_to(pid, int(total * float(percent) / 100))

    async def seek_to(self, pid: str, ms: int):
        await self.call("seek", ms=max(0, int(ms)), mode="exact")

    async def tracks(self, pid: str = PID) -> dict:
        return kodi_tracks(await self.call("tracks"))

    async def set_audio(self, pid: str, index: int):
        await self.call("select", audio=index)

    async def set_subtitle(self, pid: str, index: int | None):
        """Show text track `index`, or turn subtitles off with None (a forced track in the audio's language stays)."""
        await self.call("select", text=index)

    async def add_subtitle(self, pid: str, url: str, *, lang: str = "und", label: str = "", show: bool = True) -> int:
        """A subtitle file by link (the player fetches and draws it, no rebuffering); it becomes the shown one."""
        return int((await self.call("add_sub", timeout=20, url=url, lang=lang, label=label or lang, show=show))["text"])

    async def info(self, *labels: str) -> dict:
        return {}                               # no info labels (Kodi's chapter names): callers fall back

    async def action(self, name: str):
        raise PlayerError(f"no {name} in this player")

    async def notify(self, title: str, message: str):
        await self.call("osd", text=f"{title}: {message}" if title else message, ms=4000)

    async def volume(self, level: float):
        """The player's own output (0-1): ducking while the pill listens, without the TV's volume bar."""
        await self.call("volume", level=max(0.0, min(1.0, float(level))))

    async def status(self) -> dict:
        return await self.call("status")
