"""The listeners following this computer's addresses (server.Listeners) and the TV's apps paired again (install.py).
Real sockets on loopback addresses (127.0.0.2, then .3) stand for the LAN address; a fake ADB for the TV."""
import asyncio
import hashlib
import json
import os
import signal
import socket
import ssl
import types

import httpx
import pytest

from tv_agent import clients as C
from tv_agent import config, install, server
from tv_agent.state import State
from tv_agent.tv import tvplayer as T
from tv_agent.tv.adb import TvUnreachable

AGENT_TOKEN = "agent-token-for-tests-0123456789"
PLAYER_TOKEN = "player-token-for-tests-0123456789"
OLD, NEW = "127.0.0.2", "127.0.0.3"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name, file in (("CONFIG_FILE", "config.toml"), ("SECRETS_FILE", "secrets.toml"), ("CLIENTS_FILE", "clients.json"),
                       ("TLS_CERT", "phone-tls.crt"), ("TLS_KEY", "phone-tls.key")):
        monkeypatch.setattr(config, name, tmp_path / file)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(install, "PAIRED_FILE", tmp_path / "paired.json")
    monkeypatch.setattr(T, "tailscale_address", lambda: None)
    config.save_secret("player_token", PLAYER_TOKEN)
    return tmp_path


@pytest.fixture
def route(monkeypatch):
    """Where the route to the TV goes from: set route.host (None: no route at all). lan_host still wins."""
    r = types.SimpleNamespace(host=OLD)

    def lan_address(cfg):
        if cfg.lan_host:
            return cfg.lan_host
        if r.host is None:
            raise SystemExit("no route")
        return r.host

    for module in (server, C, T, install):
        monkeypatch.setattr(module, "lan_address", lan_address)
    return r


class FakeAdb:
    """The TV's shell as pairing sees it: these packages installed, every broadcast taken; `down` calls fail first."""

    def __init__(self, packages=(install.APP, install.PLAYER), down=0):
        self.sent, self.packages, self.down = [], set(packages), down

    def run(self, argv, timeout=10.0, quiet_s=None):
        if self.down:
            self.down -= 1
            raise TvUnreachable("TV not reachable over ADB (is it on, with USB debugging enabled?)")
        self.sent.append(list(argv))
        if argv[:2] == ["pm", "path"]:
            return f"package:/data/app/{argv[2]}/base.apk" if argv[2] in self.packages else ""
        return "Broadcast completed: result=-1"

    def broadcasts(self) -> list[list[str]]:
        return [a for a in self.sent if a[:2] == ["am", "broadcast"]]


def free_ports(n: int) -> list[int]:
    out = []
    for _ in range(n):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            out.append(s.getsockname()[1])
    return out


def service(adb: FakeAdb, **settings) -> server.Service:
    """A Service without __init__ (no TV, no models): the token, the phones, TV Agent Player's connection, a TV that
    records what it's told and shown."""
    ports = dict(zip(("lan_port", "stream_port", "player_port", "phone_port", "phone_stream_port"), free_ports(5)))
    svc = server.Service.__new__(server.Service)
    svc.cfg = config.Config(tv_hosts=["127.0.0.1"], **ports, **settings)
    svc.cfg.secrets["agent_token"] = AGENT_TOKEN
    svc.token = AGENT_TOKEN
    svc.clients = C.Clients()
    svc._busy = asyncio.Lock()
    svc._panel_adb = adb
    shown = []

    async def notify(text):
        shown.append(text)

    svc.agent = types.SimpleNamespace(state=State(), adb=types.SimpleNamespace(host="127.0.0.1"), notify=notify,
                                      tvplayer=T.TvPlayer(lambda: PLAYER_TOKEN, lambda: {"127.0.0.1"}),
                                      shown=shown)
    return svc


async def started(svc) -> server.Listeners:
    listeners = server.Listeners(svc, server.local_app(svc))
    await listeners.load_certificate()
    await listeners.update()
    return listeners


async def stop_all(listeners: server.Listeners):
    tasks = [s.task for s in listeners.bound.values() if isinstance(s, server.Listener)]
    for key in list(listeners.bound):
        listeners._stop(key)
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


async def answers(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection(host, port)
    except OSError:
        return False
    writer.close()
    return True


def ports(cfg) -> list[int]:
    return [getattr(cfg, setting) for _, setting in server.LISTENERS.values()]


def test_the_listeners_move_with_the_lan_address_and_the_tvs_apps_are_told(paths, route):
    adb = FakeAdb()
    svc = service(adb)

    async def go():
        listeners = await started(svc)
        try:
            assert {host for _, host in listeners.bound} == {OLD} and len(listeners.bound) == 5
            await listeners.check()                 # no pairing remembered yet (the first start): both told
            assert len(adb.broadcasts()) == 2
            adb.sent.clear()
            svc.agent.shown.clear()

            route.host = NEW                        # Ethernet unplugged: the route to the TV is Wi-Fi's now
            await listeners.check()
            await asyncio.sleep(0.3)                # uvicorn closes its socket on its next tick
            assert {host for _, host in listeners.bound} == {NEW}
            assert [await answers(OLD, p) for p in ports(svc.cfg)] == [False] * 5
            assert [await answers(NEW, p) for p in ports(svc.cfg)] == [True] * 5
            async with httpx.AsyncClient() as http:
                r = await http.get(f"http://{NEW}:{svc.cfg.lan_port}/v1/health",
                                   headers={"authorization": f"Bearer {AGENT_TOKEN}"})
            assert r.json() == {"ok": True}

            # The phones' listener on the new address has the certificate they pinned
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            _, writer = await asyncio.open_connection(NEW, svc.cfg.phone_port, ssl=ctx)
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            writer.close()
            assert hashlib.sha256(der).hexdigest() == C.certificate() == svc.clients.fingerprint
        finally:
            await stop_all(listeners)

    asyncio.run(go())
    app, player = adb.broadcasts()
    assert app[app.index("url") + 1] == f"http://{NEW}:{svc.cfg.lan_port}" and AGENT_TOKEN in app
    assert player[player.index("hosts") + 1] == NEW and player[player.index("cert") + 1] == C.certificate()
    assert svc.agent.shown == [f"Voice agent now at {NEW}: the TV app and TV Agent Player paired again"]
    kept = (paths / "paired.json").read_text()
    assert json.loads(kept)["app"]["url"] == f"http://{NEW}:{svc.cfg.lan_port}"
    assert AGENT_TOKEN not in kept and PLAYER_TOKEN not in kept


def test_an_address_that_stays_asks_nothing_of_the_tv_after_a_restart(paths, route):
    adb = FakeAdb()
    svc = service(adb)

    async def go():
        listeners = await started(svc)
        await listeners.check()
        assert len(adb.broadcasts()) == 2
        adb.sent.clear()
        await listeners.check()
        await stop_all(listeners)
        await asyncio.sleep(0.2)
        again = await started(svc)                  # the agent restarted on the same address
        await again.check()
        await stop_all(again)

    asyncio.run(go())
    assert adb.sent == [] and len(svc.agent.shown) == 1


def test_a_new_dhcp_address_at_the_start_pairs_the_apps_again(paths, route):
    adb = FakeAdb()
    svc = service(adb)

    async def go():
        listeners = await started(svc)
        await listeners.check()
        await stop_all(listeners)
        await asyncio.sleep(0.2)
        adb.sent.clear()
        route.host = NEW                            # restarted (a reboot) with another lease
        again = await started(svc)
        await again.check()
        await stop_all(again)

    asyncio.run(go())
    assert [b[b.index("url") + 1] for b in adb.broadcasts() if "url" in b] == [f"http://{NEW}:{svc.cfg.lan_port}"]


def test_lan_host_stays_fixed_until_config_toml_changes_it(paths, route):
    adb = FakeAdb()
    svc = service(adb, lan_host=OLD)
    route.host = NEW                                # the route has moved, but lan_host wins

    def write(lan_host: str):
        cfg = svc.cfg
        lines = [f"lan_host = {json.dumps(lan_host)}", f"tv_hosts = {json.dumps(cfg.tv_hosts)}"]
        lines += [f"{setting} = {getattr(cfg, setting)}" for _, setting in server.LISTENERS.values()]
        config.CONFIG_FILE.write_text("\n".join(lines) + "\n")
        st = config.CONFIG_FILE.stat()
        os.utime(config.CONFIG_FILE, (st.st_atime, st.st_mtime + 1))

    async def go():
        write(OLD)
        listeners = await started(svc)
        await listeners.check()
        await listeners.check()
        assert {host for _, host in listeners.bound} == {OLD}
        assert [b[b.index("url") + 1] for b in adb.broadcasts() if "url" in b] == [f"http://{OLD}:{svc.cfg.lan_port}"]
        adb.sent.clear()
        write(NEW)                                  # edited by hand while the agent runs
        await listeners.check()
        await asyncio.sleep(0.3)
        assert {host for _, host in listeners.bound} == {NEW} and svc.cfg.lan_host == NEW
        assert await answers(NEW, svc.cfg.lan_port) and not await answers(OLD, svc.cfg.lan_port)
        await stop_all(listeners)

    asyncio.run(go())
    assert [b[b.index("url") + 1] for b in adb.broadcasts() if "url" in b] == [f"http://{NEW}:{svc.cfg.lan_port}"]


def test_a_tv_that_doesnt_answer_is_paired_once_it_does(paths, route):
    adb = FakeAdb(down=1)                           # in standby when the address changes
    svc = service(adb)

    async def go():
        listeners = await started(svc)
        route.host = NEW
        await listeners.check()
        assert listeners.pair_due and adb.broadcasts() == [] and svc.agent.shown == []
        await listeners.check()                     # too soon: not asked again yet
        assert adb.sent == []
        listeners._retry_at = 0.0                   # RETRY_EVERY later
        await listeners.check()
        await stop_all(listeners)
        return listeners.pair_due

    assert asyncio.run(go()) is False
    assert len(adb.broadcasts()) == 2 and svc.agent.shown == [
        f"Voice agent now at {NEW}: the TV app and TV Agent Player paired again"]


def test_with_no_route_to_the_tv_everything_stays(paths, route):
    adb = FakeAdb()
    svc = service(adb)

    async def go():
        listeners = await started(svc)
        await listeners.check()
        before, adb.sent = dict(listeners.bound), []
        route.host = None                           # the network is down
        listeners._retry_at = 0.0
        await listeners.check()
        assert listeners.bound == before and listeners.lan == OLD
        await stop_all(listeners)

    asyncio.run(go())
    assert adb.sent == []


def test_an_app_that_isnt_installed_isnt_asked_about_again(paths, route):
    adb = FakeAdb(packages=(install.APP,))          # no TV Agent Player
    svc = service(adb)

    async def go():
        listeners = await started(svc)
        await listeners.check()
        await stop_all(listeners)
        await asyncio.sleep(0.2)
        adb.sent.clear()
        again = await started(svc)
        await again.check()
        await stop_all(again)

    asyncio.run(go())
    assert adb.sent == [] and svc.agent.shown == [f"Voice agent now at {OLD}: the TV app paired again"]


def test_a_listener_that_stops_leaves_the_signal_handlers_alone():
    """uvicorn puts back the handlers it found when a server stops: one stopped out of order would undo those of the
    servers started after it, and SIGTERM would no longer stop the agent gracefully."""
    port, = free_ports(1)

    async def go():
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        listener = server.Listener(server.uvicorn.Config(server.FastAPI(), log_level="warning")).run(
            C.listen_socket(OLD, port))
        await asyncio.sleep(0.2)
        during = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        listener.should_exit = True
        await asyncio.wait_for(listener.task, 5)
        return before, during, (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))

    before, during, after = asyncio.run(go())
    assert before == during == after
