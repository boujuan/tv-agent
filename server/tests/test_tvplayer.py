"""TV Agent Player's connection (tv/tvplayer.py) and the agent driving it. A fake player speaks the protocol over real
TLS on localhost; nothing reaches the TV."""
import asyncio
import hashlib
import json
import ssl
import types

import pytest

from tv_agent import clients, languages
from tv_agent import agent as agent_mod
from tv_agent.actions import Transport
from tv_agent.agent import Agent
from tv_agent.state import NowPlaying
from tv_agent.stremio import streams as S
from tv_agent.stremio.subtitles import Subtitle
from tv_agent.tv import tvplayer as T

TOKEN = "player-token-" + "x" * 32


@pytest.fixture
def tls(tmp_path):
    """A server context with a throwaway certificate, and that certificate's SHA-256 (what the app pins)."""
    cert, key = tmp_path / "c.crt", tmp_path / "c.key"
    clients._make_certificate(cert, key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx, clients.fingerprint(cert.read_text())


class FakePlayer:
    """The app's side: connects, checks the pin, says hello, answers commands, sends events."""

    def __init__(self, port: int, pin: str):
        self.port, self.pin, self.got = port, pin, []

    async def connect(self, token=TOKEN, version=T.PROTOCOL) -> dict:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE         # the pin below is the check
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port, ssl=ctx)
        der = self.writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        assert hashlib.sha256(der).hexdigest() == self.pin
        await self.send({"t": "hello", "v": version, "token": token, "app": "0.1.0", "build": 1, "sdk": 34})
        return await self.read()

    async def send(self, msg: dict):
        self.writer.write(json.dumps(msg).encode() + b"\n")
        await self.writer.drain()

    async def read(self) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), 3)
        return json.loads(line) if line else {}

    async def answer(self, **result) -> dict:
        """The next command, answered."""
        while (cmd := await self.read()).get("t") == "ping":
            pass
        self.got.append(cmd)
        await self.send({"t": "ack", "id": cmd["id"], **result})
        return cmd


async def server(tls, allowed=frozenset({"127.0.0.1"})):
    ctx, pin = tls
    p = T.TvPlayer(lambda: TOKEN, lambda: set(allowed), "9.9.9")
    srv = await p.serve("127.0.0.1", 0, ctx)
    return p, srv, FakePlayer(srv.sockets[0].getsockname()[1], pin)


def test_only_the_paired_player_from_the_tv_gets_in(tls):
    async def run():
        p, srv, fake = await server(tls)
        assert (await fake.connect(token="wrong")) == {"t": "bye", "error": "unauthorized"}
        assert (await fake.connect(version=99)) == {"t": "bye", "error": "version"}
        assert not p.up
        welcome = await fake.connect()
        assert welcome == {"t": "welcome", "v": T.PROTOCOL, "agent": "9.9.9"} and p.up and p.app == "0.1.0"
        srv.close()
        p2, srv2, other = await server(tls, allowed={"192.168.1.40"})       # not from the TV's address
        assert await other.connect() == {} and not p2.up
        srv2.close()
    asyncio.run(run())


def test_a_load_goes_over_the_connection_and_the_player_reports_back(tls):
    async def run():
        p, srv, fake = await server(tls)
        await fake.connect()

        async def player():
            cmd = await fake.answer()
            item = cmd["item"]
            await fake.send({"t": "state", "state": "ready", "playing": True, "item": item})
            await fake.send({"t": "pos", "item": item, "pos": 60_000, "dur": 3_000_000, "buf": 40_000})
            return cmd
        cmd, _ = await asyncio.gather(player(), p.open("https://debrid/f.mkv", 60_000, item="tt1:1:2", title="X S1E2",
                                                       headers={"Referer": "r"}, audio="eng", subs=["eng", "spa"]))
        assert cmd["t"] == "load" and cmd["url"] == "https://debrid/f.mkv" and cmd["start_ms"] == 60_000
        assert cmd["headers"] == {"Referer": "r"} and cmd["audio"] == "eng" and cmd["subs"] == ["eng", "spa"]
        assert cmd["item"].startswith("tt1:1:2#")
        await asyncio.sleep(0.05)
        pos, dur, speed = p.now()
        assert 60_000 <= pos < 61_000 and dur == 3_000_000 and speed == 1.0      # brought up to now
        assert await p.player() == T.PID
        # The one before closing late can't end this one; its own `closed` does, with the exact position.
        await fake.send({"t": "closed", "item": "tt1:1:1#0", "why": "replaced", "pos": 5, "dur": 9})
        await asyncio.sleep(0.05)
        assert p.now() is not None
        await fake.send({"t": "closed", "item": cmd["item"], "why": "home", "pos": 61_234, "dur": 3_000_000})
        await asyncio.sleep(0.05)
        assert p.now() is None and p.last["closed"]["pos"] == 61_234
        srv.close()
    asyncio.run(run())


def test_commands_have_kodis_surface_and_errors_come_back(tls):
    async def run():
        p, srv, fake = await server(tls)
        await fake.connect()

        async def one(coro, **result):
            cmd, out = await asyncio.gather(fake.answer(**result), coro)
            return cmd, out
        cmd, _ = await one(p.seek_to(T.PID, 90_500))
        assert cmd == {"t": "seek", "id": cmd["id"], "ms": 90_500, "mode": "exact"}
        cmd, _ = await one(p.seek_by(T.PID, -10))
        assert cmd["t"] == "seek_by" and cmd["ms"] == -10_000
        cmd, _ = await one(p.play(T.PID, False))
        assert cmd["t"] == "pause"
        cmd, _ = await one(p.set_subtitle(T.PID, None))
        assert cmd["t"] == "select" and cmd["text"] is None
        cmd, n = await one(p.add_subtitle(T.PID, "https://subs/x.srt", lang="spa", label="Spanish"), text=7)
        assert n == 7 and cmd["url"] == "https://subs/x.srt" and cmd["lang"] == "spa"
        with pytest.raises(T.PlayerError, match="no such text track"):
            await one(p.set_subtitle(T.PID, 99), error="no such text track")
        srv.close()
    asyncio.run(run())


def test_a_new_player_connection_replaces_the_old_and_a_lost_one_fails_its_commands(tls):
    async def run():
        p, srv, first = await server(tls)
        await first.connect()
        second = FakePlayer(first.port, first.pin)
        await second.connect()
        assert await first.read() == {"t": "bye", "error": "replaced"}
        call = asyncio.create_task(p.call("status"))
        await second.read()                     # the command arrives, and the player goes away before answering
        second.writer.close()
        with pytest.raises(T.PlayerError, match="went away"):
            await call
        assert not p.up
        srv.close()
    asyncio.run(run())


def test_tracks_in_kodis_shape_with_forced_subtitles_not_counted_as_on():
    t = {"audio": [{"i": 0, "lang": "en", "label": "Commentary", "codec": "eac3", "ch": 2, "selected": False},
                   {"i": 1, "lang": "en", "label": "", "codec": "truehd", "ch": 8, "default": True, "selected": True}],
         "text": [{"i": 0, "lang": "en", "label": "Forced", "forced": True, "selected": True},
                  {"i": 1, "lang": "en", "label": "SDH", "sdh": True},
                  {"i": 2, "lang": "es", "label": "Spanish (SubSense)", "external": True}]}
    k = T.kodi_tracks(t)
    assert k["currentaudiostream"]["index"] == 1 and k["audiostreams"][1]["channels"] == 8
    assert not k["subtitleenabled"] and k["currentsubtitle"] == {}       # only the forced lines show: "off"
    assert k["subtitles"][2]["name"] == "Spanish (SubSense) (external)"
    # the agent's own rules work on them as on Kodi's (never the forced track; "en" is English)
    assert languages.pick_subtitle(k["subtitles"], ["eng"])["index"] == 1
    assert languages.pick_subtitle(k["subtitles"], ["spa"])["index"] == 2
    t["text"][1]["selected"] = True
    assert T.kodi_tracks(t)["subtitleenabled"] and T.kodi_tracks(t)["currentsubtitle"]["index"] == 1


class FakeTvPlayer:
    """TvPlayer's surface without a connection: records what the agent asks."""
    item = "tt1:1:2#1"
    up = False

    def __init__(self, pos=(600_000, 2_700_000, 1.0)):
        self.did, self.pos, self.last = [], pos, {}

    def now(self):
        return self.pos

    async def ready(self, adb, wait_s=10.0):
        self.did.append(("ready",))
        return True

    async def open(self, url, resume_ms=0, **kw):
        self.did.append(("open", url, resume_ms, kw))

    async def position(self, pid=T.PID):
        return self.pos

    def __getattr__(self, name):
        async def record(*args, **kw):
            self.did.append((name, *args[1:]))
        return record


def bare_agent(player: FakeTvPlayer) -> Agent:
    a = Agent.__new__(Agent)
    a.tvplayer, a.dry_run = player, False
    a.state = agent_mod.State()
    a.cfg = types.SimpleNamespace(player="tvplayer", probe_streams=False, auto_skip=False, check_episodes=False,
                                  binge=True)
    a._progress, a._starting, a._user_stopped = None, asyncio.Lock(), False
    return a


def test_play_brings_the_player_up_during_the_search_and_loads_in_place():
    async def run():
        player = FakeTvPlayer(pos=None)
        a = bare_agent(player)
        seen = []

        async def nothing(*args, **kw):
            return None

        async def found(kind, video, meta):
            await asyncio.sleep(0)
            seen.append(player.did[:])            # the player is already being brought up meanwhile
            return [S.Stream("AIOStreams", "https://x/1", "n", "", "f.mkv", 0, library=True,
                             headers={"Authorization": "Bearer t"})]
        a.account, a.notify, a.streams_for, a._kodi_player = object(), nothing, found, nothing
        a.adb = object()
        a.track_plan = lambda meta: languages.TrackPlan("eng", None)
        a._replace_session = lambda video, resume_ms, exact=False: resume_ms
        a._player_subs, a._started, a._preloaded = nothing, lambda *args: None, None
        a.state.timings = types.SimpleNamespace(add=lambda *args: None)
        out = await Agent.play(a, "tt1", "series", "X", "tt1:1:2", "X S1E2", {}, resume_ms=90_000)
        return out, player.did, seen, a.state.now_playing

    out, did, seen, np = asyncio.run(run())
    assert out.ok and seen == [[("ready",)]]
    assert did[1] == ("open", "https://x/1", 90_000, {"item": "tt1:1:2", "title": "X S1E2",
                                                    "headers": {"Authorization": "Bearer t"}, "audio": "eng",
                                                    "subs": None})
    assert np.player == "tvplayer" and np.video_id == "tt1:1:2"


def test_a_preloaded_next_episode_opens_with_the_link_it_was_preloaded_with():
    async def run():
        player = FakeTvPlayer(pos=None)
        a = bare_agent(player)
        a.cfg.probe_streams = True                  # a fresh check would resolve a different link: not done

        async def nothing(*args, **kw):
            return None
        nxt = S.Stream("AIOStreams", "https://addon/play/2", "n", "", "e2.mkv", 0)
        a.account, a.notify, a._kodi_player, a.adb = object(), nothing, nothing, object()
        a.track_plan = lambda meta: languages.TrackPlan("eng", None)
        a._replace_session = lambda video, resume_ms, exact=False: resume_ms
        a._player_subs, a._started = nothing, lambda *args: None
        a._first_playable = None                   # would fail if called
        a._preloaded = ("tt1:1:3", "https://addon/play/2", "https://cdn/resolved-2")
        a.state.timings = types.SimpleNamespace(add=lambda *args: None)
        await Agent.play(a, "tt1", "series", "X", "tt1:1:3", "X S1E3", {}, prepared=nxt)
        return player.did

    assert asyncio.run(run())[1][1] == "https://cdn/resolved-2"


def test_transport_goes_straight_to_the_open_player_without_asking_the_tv():
    async def run():
        player = FakeTvPlayer()
        a = bare_agent(player)

        async def no(*args, **kw):
            raise AssertionError("asked the TV which app is in front")
        a.front_app = no
        a.state.now_playing = NowPlaying("tt1", "series", "tt1:1:2", "X", "tvplayer")
        out = await Agent._transport(a, Transport(op="forward", seconds=30))
        out2 = await Agent._transport(a, Transport(op="seek_to", percent=50))
        out3 = await Agent._transport(a, Transport(op="pause"))
        return player.did, out, out2, out3

    did, out, out2, out3 = asyncio.run(run())
    assert did == [("seek_by", 30), ("seek_percent", 50), ("play", False)]
    assert out.ok and out2.say == "To 50%" and out3.ok


def test_addon_subtitles_only_for_a_language_the_file_lacks():
    def run(text_tracks: list[dict], langs: list[str]) -> list:
        async def go():
            player = FakeTvPlayer()
            player.last = {"tracks": {}}
            a = bare_agent(player)

            async def tracks(pid=T.PID):
                return T.kodi_tracks({"audio": [], "text": text_tracks})
            player.tracks = tracks
            subs = asyncio.get_running_loop().create_future()
            subs.set_result([Subtitle("https://subs/en.srt", "eng", "English", "SubSense"),
                             Subtitle("https://subs/es.srt", "spa", "Spanish", "SubSense")])
            a.notify = lambda text: asyncio.sleep(0)
            await Agent._player_subs(a, languages.TrackPlan("eng", langs), subs, "X")
            return [d for d in player.did if d[0] == "add_subtitle"]
        return asyncio.run(go())

    spanish_only = [{"i": 0, "lang": "es", "label": ""}]
    assert run(spanish_only, ["eng", "spa"]) == [("add_subtitle", "https://subs/en.srt")]     # English first: added
    assert run(spanish_only, ["spa", "eng"]) == []                    # the file has the first one: shown already
    assert run([{"i": 0, "lang": "en", "label": "Forced", "forced": True}], ["eng"]) == \
        [("add_subtitle", "https://subs/en.srt")]                     # a forced track isn't English subtitles


def test_the_players_events_reach_the_session():
    async def run():
        player = FakeTvPlayer()
        a = bare_agent(player)
        np = a.state.now_playing = NowPlaying("tt1", "series", "tt1:1:2", "X", "tvplayer")
        nexts, others = [], []

        async def next_episode(n):
            nexts.append(n.offset)
        a._next_episode = next_episode

        async def try_another(np_, msg):
            others.append(msg["code"])
        a._try_another = try_another
        Agent._player_event(a, {"t": "closed", "item": "tt1:1:2#1", "why": "home", "pos": 1_234_567, "dur": 9})
        Agent._player_event(a, {"t": "closed", "item": "tt1:1:1#0", "why": "replaced", "pos": 5, "dur": 9})
        Agent._player_event(a, {"t": "key", "key": "next"})
        Agent._player_event(a, {"t": "error", "item": "tt1:1:2#1", "code": "ERROR_CODE_IO_BAD_HTTP_STATUS",
                                "http": 403, "fatal": True})
        await asyncio.sleep(0)
        return np.stopped_at, nexts, others

    stopped_at, nexts, others = asyncio.run(run())
    assert stopped_at == 1_234_567 and nexts == [1] and others == ["ERROR_CODE_IO_BAD_HTTP_STATUS"]


def test_the_player_is_told_the_lan_address_then_tailscales(monkeypatch):
    cfg = types.SimpleNamespace(phone_hosts=["192.168.1.52"])
    monkeypatch.setattr(T, "lan_address", lambda c: "192.168.1.52")
    monkeypatch.setattr(T, "tailscale_address", lambda: "100.125.125.38")
    assert T.addresses(cfg) == ["192.168.1.52", "100.125.125.38"]        # once each, in the order to try
    monkeypatch.setattr(T, "tailscale_address", lambda: None)              # Tailscale not running
    assert T.addresses(cfg) == ["192.168.1.52"]
