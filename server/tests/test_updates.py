"""Updates from GitHub releases (updates.py) and the phone app's version as each phone reports it (clients.py)."""
import asyncio
import hashlib
import json
import types

import httpx
import pytest
from fastapi import HTTPException

from tv_agent import __version__
from tv_agent import clients as C
from tv_agent import config, remote, server
from tv_agent import updates as U
from tv_agent.state import State

APK = b"PK\x03\x04 the TV app, release 0.9.0"


def release(tag, assets=(), draft=False, prerelease=False):
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "html_url": f"https://github.com/x/{tag}",
            "published_at": "2026-10-23T10:00:00Z", "body": f"notes for {tag}",
            "assets": [{"name": n, "browser_download_url": f"https://dl.example/{n}", "size": len(APK),
                        "digest": d} for n, d in assets]}


MAIN = [release("server-v0.3.0"), release("server-v0.10.0", draft=True), release("panel-v0.3.0"),
        release("android-v0.9.0", [("SHA256SUMS", "sha256:00"),
                                   ("tv-agent-android-0.9.0.apk", "sha256:" + hashlib.sha256(APK).hexdigest())]),
        release("android-v0.8.0", [("tv-agent-android-0.8.0.apk", "sha256:11")]),
        release("android-v1.0.0", [("tv-agent-android-1.0.0.apk", "sha256:22")], prerelease=True)]
PHONE = [release("v0.1.0", [("tv-agent-phone-0.1.0.apk", "sha256:33")])]


def test_versions_compare_as_numbers_and_never_downgrade():
    assert U.version("0.10.0") > U.version("0.9.1") and U.version("v1.2") == U.version("1.2.0")
    assert U.version("0.2.9") < U.version("0.3.0-dev") < U.version("0.3.0") and U.version(None) is None
    assert U.newer("0.9.0", "0.9.0-dev") and not U.newer("0.1.1", "0.2.0-test")     # a test build: never downgraded
    assert U.newer("0.10.0", "0.9.0") and not U.newer("0.8.0", "0.9.0") and not U.newer("0.9.0", "0.9.0")
    assert U.newer("0.9.0", None) and not U.newer(None, "0.1.0")


def test_the_latest_release_of_each_part_skips_drafts_and_prereleases():
    assert U.latest(MAIN, "server-v")["version"] == "0.3.0"         # the 0.10.0 draft doesn't count
    tv = U.latest(MAIN, "android-v", ".apk")
    assert tv["version"] == "0.9.0" and tv["apk"].endswith("tv-agent-android-0.9.0.apk")
    assert tv["sha256"] == hashlib.sha256(APK).hexdigest() and tv["notes"] == "notes for android-v0.9.0"
    assert U.latest(PHONE, "v", ".apk")["version"] == "0.1.0"
    assert U.latest(MAIN, "v") is None                               # "server-v" isn't the phone app's "v"
    assert U.latest(MAIN, "panel-v", ".apk") is None and U.latest({"message": "rate limit"}, "v") is None


def fake_service(tmp_path, monkeypatch, tv_version="0.8.0", apk=APK):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    installed = []

    def handler(request: httpx.Request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=PHONE if "tv-agent-phone" in request.url.path else MAIN)
        return httpx.Response(200, content=apk)

    svc = types.SimpleNamespace(cfg=config.Config(tv_hosts=["192.168.1.40"]), _busy=asyncio.Lock(),
                                agent=types.SimpleNamespace(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                                            adb=object()))
    svc.updates = U.Updates(svc)
    version = {"now": tv_version}

    def packages(adb):
        return {"tvagent": {"version": version["now"]}}

    def install(cfg, adb, path):
        installed.append(open(path, "rb").read())
        version["now"] = "0.9.0"
        return "http://192.168.1.51:8765"

    monkeypatch.setattr(U.tv_status, "packages", packages)
    monkeypatch.setattr(U, "install_app", install)
    return svc, installed


def test_a_check_finds_whats_newer_than_what_runs(tmp_path, monkeypatch):
    svc, _ = fake_service(tmp_path, monkeypatch)
    asyncio.run(svc.updates.check())
    s = svc.updates.state()
    assert s["error"] is None and s["mode"] == "notify" and s["checked"]
    assert s["components"]["server"] == {"latest": "0.3.0", "url": "https://github.com/x/server-v0.3.0",
                                         "published": "2026-10-23T10:00:00Z", "notes": "notes for server-v0.3.0",
                                         "current": __version__, "newer": U.newer("0.3.0", __version__),
                                         "ahead": U.newer(__version__, "0.3.0")}     # this copy is past 0.3.0
    assert s["components"]["tv"]["current"] == "0.8.0" and s["components"]["tv"]["newer"]
    assert s["components"]["phone"]["latest"] == "0.1.0" and "current" not in s["components"]["phone"]


def test_the_tv_app_is_installed_only_with_the_releases_checksum(tmp_path, monkeypatch):
    svc, installed = fake_service(tmp_path, monkeypatch)
    asyncio.run(svc.updates.check())
    out = asyncio.run(svc.updates.install_tv())
    assert out["installed"] and out["version"] == "0.9.0" and installed == [APK]
    assert not list(tmp_path.glob("*.apk"))                          # the download doesn't stay
    assert asyncio.run(svc.updates.install_tv())["installed"] is False     # up to date now: nothing done

    svc, installed = fake_service(tmp_path, monkeypatch, apk=b"something else")
    asyncio.run(svc.updates.check())
    with pytest.raises(RuntimeError, match="SHA-256"):
        asyncio.run(svc.updates.install_tv())
    assert installed == [] and not svc.updates.installing


def test_auto_installs_the_tv_app_and_notify_only_checks(tmp_path, monkeypatch):
    async def run(mode):
        svc, installed = fake_service(tmp_path, monkeypatch)
        svc.cfg.updates = mode
        slept = []

        async def sleep(s):
            slept.append(s)
            if len(slept) > 1:
                raise asyncio.CancelledError
        monkeypatch.setattr(U.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await U.loop(svc)
        return svc, installed
    svc, installed = asyncio.run(run("auto"))
    assert installed == [APK] and svc.updates.tv_version == "0.9.0"
    svc, installed = asyncio.run(run("notify"))
    assert installed == [] and svc.updates.checked
    svc, installed = asyncio.run(run("off"))
    assert installed == [] and svc.updates.checked is None          # GitHub never asked


def test_the_updates_setting_is_checked(paths):
    assert remote.setting("updates", "auto") == "auto"
    with pytest.raises(HTTPException):
        remote.setting("updates", True)
    (paths / "config.toml").write_text('tv_hosts = ["192.168.1.40"]\nupdates = "always"\n')
    with pytest.raises(SystemExit, match="updates must be"):
        config.load()


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.toml")
    monkeypatch.setattr(config, "CLIENTS_FILE", tmp_path / "clients.json")
    return tmp_path


def test_a_phone_says_its_app_version_and_the_panel_sees_it(paths):
    clients = C.Clients()
    p = clients.start_pairing()
    device, token = clients.pair(p.code, "Pixel", "192.168.1.77")
    assert clients.verify(token, "192.168.1.77", "0.1.0") is device and device.public()["app_version"] == "0.1.0"
    clients.verify(token, "192.168.1.77", "<script>")                 # not a version: the last one stays
    clients.verify(token, "192.168.1.77", "")
    assert device.app_version == "0.1.0"
    assert json.loads((paths / "clients.json").read_text())["devices"][0]["app_version"] == "0.1.0"
    assert C.Clients().devices[0].app_version == "0.1.0"                # kept across restarts


def test_through_the_phone_listener_the_header_counts_and_updates_are_readable(paths):
    svc = server.Service.__new__(server.Service)
    svc.cfg = config.Config(tv_hosts=["192.168.1.40"], lan_host="192.168.1.51")
    svc.token, svc.clients = "agent-token-for-tests-0123456789", C.Clients()
    svc.agent = types.SimpleNamespace(state=State(), adb=types.SimpleNamespace(host="192.168.1.40"))
    svc.updates = U.Updates(svc)
    svc._busy = asyncio.Lock()
    p = svc.clients.start_pairing()
    device, token = svc.clients.pair(p.code, "Pixel", "192.168.1.77")
    gate = C.gateway(svc, server.local_app(svc))

    async def go(method, path, **headers):
        transport = httpx.ASGITransport(app=gate, client=("192.168.1.77", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="https://agent") as http:
            return await http.request(method, path, headers={"authorization": f"Bearer {token}", **headers})
    r = asyncio.run(go("GET", "/v1/clients/me", **{"X-App-Version": "0.2.0"}))
    assert r.status_code == 200 and r.json()["device"]["app_version"] == "0.2.0" and device.app_version == "0.2.0"
    r = asyncio.run(go("GET", "/v1/updates"))
    assert r.status_code == 200 and r.json()["components"]["server"]["current"] == __version__
    assert asyncio.run(go("POST", "/v1/updates/tv")).status_code == 404    # installing on the TV: the panel's only


PLAYER_APK = b"PK\x03\x04 TV Agent Player 0.2.0"
PLAYER = [release("v0.2.0", [("SHA256SUMS", "sha256:00"),
                             ("tv-agent-player-0.2.0.apk", "sha256:" + hashlib.sha256(PLAYER_APK).hexdigest())]),
          release("v0.1.0", [("tv-agent-player-0.1.0.apk", "sha256:44")])]


def player_service(tmp_path, monkeypatch, player_version="0.1.0", playing=False):
    """The TV app up to date; TV Agent Player installed (or not: None) and behind its repo's latest release."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    installed = []

    def handler(request: httpx.Request):
        path = request.url.path
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=PLAYER if "tv-agent-player" in path else
                                  PHONE if "tv-agent-phone" in path else MAIN)
        return httpx.Response(200, content=PLAYER_APK if "player" in path else APK)

    agent = types.SimpleNamespace(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), adb=object(),
                                  tvplayer=types.SimpleNamespace(now=lambda: (60_000, 1_200_000, 1.0) if playing else None))
    svc = types.SimpleNamespace(cfg=config.Config(tv_hosts=["192.168.1.40"]), _busy=asyncio.Lock(), agent=agent)
    svc.updates = U.Updates(svc)
    versions = {"tvagent": "0.9.0", "tvplayer": player_version}

    def install(cfg, adb, path):
        data = open(path, "rb").read()
        installed.append(data)
        versions["tvplayer" if data == PLAYER_APK else "tvagent"] = "0.2.0" if data == PLAYER_APK else "0.9.0"
        return "ok"
    monkeypatch.setattr(U.tv_status, "packages", lambda adb: {k: {"version": v} for k, v in versions.items()})
    monkeypatch.setattr(U, "install_app", install)
    return svc, installed


def test_tv_agent_players_releases_are_checked_and_installed(tmp_path, monkeypatch):
    svc, installed = player_service(tmp_path, monkeypatch)
    asyncio.run(svc.updates.check())
    player = svc.updates.state()["components"]["tvplayer"]
    assert player["current"] == "0.1.0" and player["latest"] == "0.2.0" and player["newer"]
    assert player["apk"].endswith("tv-agent-player-0.2.0.apk")
    out = asyncio.run(svc.updates.install("tvplayer"))
    assert out["installed"] and out["say"] == "TV Agent Player is now 0.2.0" and installed == [PLAYER_APK]


def test_the_player_isnt_replaced_while_it_plays(tmp_path, monkeypatch):
    svc, installed = player_service(tmp_path, monkeypatch, playing=True)
    asyncio.run(svc.updates.check())
    with pytest.raises(RuntimeError, match="playing"):
        asyncio.run(svc.updates.install("tvplayer"))
    assert installed == []


def test_auto_updates_the_player_when_idle_and_installed_only(tmp_path, monkeypatch):
    async def run(**kw):
        svc, installed = player_service(tmp_path, monkeypatch, **kw)
        svc.cfg.updates = "auto"
        slept = []

        async def sleep(s):
            slept.append(s)
            if len(slept) > 1:
                raise asyncio.CancelledError
        monkeypatch.setattr(U.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await U.loop(svc)
        return installed
    assert asyncio.run(run()) == [PLAYER_APK]
    assert asyncio.run(run(playing=True)) == []
    assert asyncio.run(run(player_version=None)) == []           # not installed: not put on the TV by itself
