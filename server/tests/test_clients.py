"""Paired phones (clients.py) and the endpoints they use (remote.py): pairing, tokens, the phone listener's gate, the
TV's access unchanged, the certificate, and the settings and tracks the phone sees."""
import asyncio
import json
import os
import ssl
import stat
import time
import types
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException

from tv_agent import clients as C
from tv_agent import config, remote, server
from tv_agent.state import State

AGENT_TOKEN = "agent-token-for-tests-0123456789"
TV = "192.168.1.40"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(config, "CLIENTS_FILE", tmp_path / "clients.json")
    monkeypatch.setattr(config, "TLS_CERT", tmp_path / "phone-tls.crt")
    monkeypatch.setattr(config, "TLS_KEY", tmp_path / "phone-tls.key")
    return tmp_path


def service(paths) -> server.Service:
    """A Service without __init__ (no TV, no models): the token, the phones, a stand-in agent."""
    svc = server.Service.__new__(server.Service)
    svc.cfg = config.Config(tv_hosts=[TV], lan_host="192.168.1.51")
    svc.token = AGENT_TOKEN
    svc.clients = C.Clients()
    svc.clients.fingerprint = "ab" * 32
    svc.agent = types.SimpleNamespace(
        state=State(), adb=types.SimpleNamespace(host=TV),
        now_playing_link=lambda: {"url": "https://debrid.example/secret-key/file.mkv"})
    return svc


def call(app, method: str, path: str, token: str | None = None, client=("192.168.1.77", 50000), **kw):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="https://agent") as http:
            headers = {"authorization": f"Bearer {token}"} if token else {}
            return await http.request(method, path, headers=headers, **kw)
    return asyncio.run(go())


def paired(svc, name="Pixel") -> str:
    p = svc.clients.start_pairing()
    _, token = svc.clients.pair(p.code, name, "192.168.1.77")
    return token


# ---- pairing and tokens ----

def test_a_pairing_code_works_once_and_typos_are_forgiven(paths):
    clients = C.Clients()
    p = clients.start_pairing()
    assert len(p.code) == 8 and set(p.code) <= set(C.ALPHABET)
    typed = C.shown_code(p.code).lower().replace("0", "o").replace("1", "l")
    device, token = clients.pair(typed, "  Juan's\n Pixel  ", "192.168.1.77")
    assert device.name == "Juan's Pixel" and token.startswith("tvp_") and len(token) > 40
    assert clients.paired is device and clients.open_pairing() is None
    with pytest.raises(C.PairingError) as e:
        clients.pair(p.code, "again")
    assert e.value.status == 410


def test_wrong_codes_shut_out_their_address_not_the_phone_and_a_pairing_expires(paths, monkeypatch):
    clients = C.Clients()
    p = clients.start_pairing()
    wrong = "ZZZZZZZZ" if p.code != "ZZZZZZZZ" else "YYYYYYYY"
    for left in (4, 3, 2, 1):
        with pytest.raises(C.PairingError, match=f"{left} tries left"):
            clients.pair(wrong, "x", "10.0.0.9")
    with pytest.raises(C.PairingError, match="no more tries from here"):
        clients.pair(wrong, "x", "10.0.0.9")
    with pytest.raises(C.PairingError, match="from here"):
        clients.pair(p.code, "x", "10.0.0.9")            # even the right code, from that address
    device, _ = clients.pair(p.code, "Pixel", "192.168.1.77")   # the phone still pairs
    assert device.name == "Pixel"
    p = clients.start_pairing()
    monkeypatch.setattr(C.time, "time", lambda: p.expires + 1)
    with pytest.raises(C.PairingError) as e:
        clients.pair(p.code, "x")
    assert e.value.status == 410 and len(clients.devices) == 1


def test_many_addresses_guessing_close_the_pairing(paths):
    clients = C.Clients()
    p = clients.start_pairing()
    wrong = "ZZZZZZZZ" if p.code != "ZZZZZZZZ" else "YYYYYYYY"
    for n in range(C.PAIRING_TRIES_ALL - 1):
        with pytest.raises(C.PairingError):
            clients.pair(wrong, "x", f"10.0.{n // 4}.{n % 4}")
    with pytest.raises(C.PairingError, match="closed now"):
        clients.pair(wrong, "x", "10.9.9.9")
    with pytest.raises(C.PairingError) as e:
        clients.pair(p.code, "x", "192.168.1.77")
    assert e.value.status == 410


def test_tries_without_a_pairing_open_use_up_nothing(paths):
    clients = C.Clients()
    for _ in range(3 * C.PAIR_PER_MINUTE):
        with pytest.raises(C.PairingError) as e:
            clients.pair("00000000", "x", "192.168.1.77")
        assert e.value.status == 410                       # not 429: nothing to guess, nothing counted
    p = clients.start_pairing()
    assert clients.pair(p.code, "Pixel", "192.168.1.77")[0].name == "Pixel"


def test_pairing_answers_ten_tries_a_minute(paths):
    clients = C.Clients()
    for _ in range(10):
        clients.start_pairing()
        with pytest.raises(C.PairingError) as e:
            clients.pair("00000000" if clients.pairing.code != "00000000" else "11111111", "x")
        assert e.value.status == 403
    p = clients.start_pairing()
    with pytest.raises(C.PairingError) as e:
        clients.pair(p.code, "x")
    assert e.value.status == 429


def test_tokens_are_kept_only_as_hashes_in_a_private_file(paths):
    clients = C.Clients()
    token = paired(types.SimpleNamespace(clients=clients))
    text = (paths / "clients.json").read_text()
    assert token not in text and C.digest(token) in text
    assert stat.S_IMODE(os.stat(paths / "clients.json").st_mode) == 0o600
    again = C.Clients()                     # read back as the daemon would after a restart
    assert again.verify(token, "192.168.1.78").name == "Pixel"
    assert again.verify(token + "x") is None and again.verify("") is None
    assert again.revoke(again.devices[0].id) and again.verify(token) is None
    assert C.Clients().verify(token) is None
    config.check_permissions()              # 600: accepted
    os.chmod(paths / "clients.json", 0o644)
    with pytest.raises(SystemExit, match="clients.json must be private"):
        config.check_permissions()


def test_the_certificate_is_made_once_privately_and_its_digest_is_what_tls_presents(paths):
    fp = C.certificate()
    assert fp == C.certificate() and len(fp) == 64
    assert stat.S_IMODE(os.stat(config.TLS_KEY).st_mode) == 0o600
    der = ssl.PEM_cert_to_DER_cert(config.TLS_CERT.read_text())
    import hashlib
    assert hashlib.sha256(der).hexdigest() == fp
    C.tls_context()                         # key and certificate belong together


# ---- the phone listener ----

def test_the_phone_listener_lets_a_paired_phone_use_only_the_panels_endpoints(paths):
    svc = service(paths)
    token = paired(svc)
    gate = C.gateway(svc, server.local_app(svc))
    assert call(gate, "GET", "/v1/stats", token).status_code == 200
    assert call(gate, "GET", "/v1/stats").status_code == 401
    assert call(gate, "GET", "/v1/stats", AGENT_TOKEN).status_code == 401      # the agent's token isn't a phone's
    for method, path in (("GET", "/v1/now-playing/link"), ("POST", "/v1/rules"), ("POST", "/v1/rules/reset"),
                         ("POST", "/v1/eval"), ("GET", "/v1/clients"), ("POST", "/v1/clients/pairing"),
                         ("POST", "/v1/clients/revoke"), ("GET", "/v1/nope")):
        r = call(gate, method, path, token, json={})
        assert r.status_code == 404, path
        assert "debrid" not in r.text
    assert all(path.startswith("/v1/") for _, path in C.PHONE_ROUTES)
    assert not {p for _, p in C.PHONE_ROUTES} & {"/v1/now-playing/link", "/v1/eval", "/v1/clients",
                                                 "/v1/clients/pairing", "/v1/rules/reset", "/v1/rules/reload"}
    assert ("POST", "/v1/rules") not in C.PHONE_ROUTES


def test_phone_tokens_open_nothing_on_the_tv_or_local_listeners(paths):
    svc = service(paths)
    token = paired(svc)
    local = server.local_app(svc)
    assert call(local, "GET", "/v1/stats", token, client=("127.0.0.1", 1)).status_code == 401
    assert call(local, "GET", "/v1/stats", AGENT_TOKEN, client=("127.0.0.1", 1)).status_code == 200
    assert call(local, "GET", "/v1/now-playing/link", AGENT_TOKEN, client=("127.0.0.1", 1)).json()["url"]
    lan = server.lan_app(svc)
    assert call(lan, "GET", "/v1/health", token, client=(TV, 1)).status_code == 401
    assert call(lan, "GET", "/v1/health", AGENT_TOKEN, client=(TV, 1)).status_code == 200
    assert call(lan, "GET", "/v1/health", AGENT_TOKEN, client=("192.168.1.77", 1)).status_code == 403


def test_the_phone_listener_limits_bodies(paths):
    svc = service(paths)
    token = paired(svc)
    gate = C.gateway(svc, server.local_app(svc))
    r = call(gate, "POST", "/v1/text", token, content=b"x" * (C.MAX_JSON + 1))
    assert r.status_code == 413

    async def chunks():
        yield b'{"text": "pause"}'
    assert call(gate, "POST", "/v1/text", token, content=chunks()).status_code == 411
    assert call(gate, "POST", "/v1/pair", content=b"x" * 5000).status_code == 413


def test_pairing_from_the_panel_to_the_phone_and_revoking_it(paths):
    svc = service(paths)
    local, gate = server.local_app(svc), C.gateway(svc, server.local_app(svc))
    here = ("127.0.0.1", 1)
    started = call(local, "POST", "/v1/clients/pairing", AGENT_TOKEN, client=here).json()
    link = urlparse(started["link"])
    q = parse_qs(link.query)
    assert link.scheme == "tvagent" and q["u"] == ["https://192.168.1.51:8768"] and q["s"] == ["ab" * 32]
    assert C.shown_code(q["c"][0]) == started["code"]
    assert call(gate, "POST", "/v1/pair", json={"code": "WRONGCDE", "name": "x"}).status_code == 403
    r = call(gate, "POST", "/v1/pair", json={"code": started["code"], "name": "Pixel 9"})
    assert r.status_code == 200
    reply = r.json()
    assert reply["stream_port"] == 8769 and reply["device"]["name"] == "Pixel 9" and "token_sha256" not in r.text
    token = reply["token"]
    status = call(local, "GET", "/v1/clients/pairing", AGENT_TOKEN, client=here).json()
    assert status["paired"]["name"] == "Pixel 9" and not status["open"]
    me = call(gate, "GET", "/v1/clients/me", token).json()
    assert me["device"]["last_ip"] == "192.168.1.77"
    listed = call(local, "GET", "/v1/clients", AGENT_TOKEN, client=here).json()
    assert [d["name"] for d in listed["devices"]] == ["Pixel 9"] and "token_sha256" not in json.dumps(listed)
    assert call(local, "GET", "/v1/clients/me", AGENT_TOKEN, client=here).status_code == 404
    device_id = listed["devices"][0]["id"]
    assert call(local, "POST", "/v1/clients/revoke", AGENT_TOKEN, client=here, json={"id": device_id}).json()["ok"]
    assert call(gate, "GET", "/v1/clients/me", token).status_code == 401


def test_a_phone_can_forget_this_agent(paths):
    svc = service(paths)
    token = paired(svc)
    gate = C.gateway(svc, server.local_app(svc))
    assert call(gate, "POST", "/v1/clients/me/forget", token).json()["ok"]
    assert call(gate, "GET", "/v1/stats", token).status_code == 401 and not svc.clients.devices


def test_no_pairing_without_the_phone_listener(paths):
    svc = service(paths)
    svc.clients.fingerprint = ""
    r = call(server.local_app(svc), "POST", "/v1/clients/pairing", AGENT_TOKEN, client=("127.0.0.1", 1))
    assert r.status_code == 409


class _Writer:
    def __init__(self, peer):
        self.peer, self.out, self.closed = peer, b"", False

    def get_extra_info(self, name):
        return (self.peer, 5000) if name == "peername" else None

    def write(self, data):
        self.out += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def _stream(svc, hello: dict, peer: str, phone: bool) -> list[dict]:
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps(hello).encode() + b"\n")
        reader.feed_eof()
        w = _Writer(peer)
        await server.stream_client(svc, reader, w, phone=phone)
        return [json.loads(line) for line in w.out.splitlines()]
    return asyncio.run(go())


def test_the_phones_audio_stream_takes_a_phones_token_only(paths):
    svc = service(paths)
    svc._busy = asyncio.Lock()
    token = paired(svc)
    assert _stream(svc, {"token": AGENT_TOKEN}, "192.168.1.77", phone=True) == [{"error": "unauthorized"}]
    assert _stream(svc, {"token": "tvp_nope"}, "192.168.1.77", phone=True) == [{"error": "unauthorized"}]
    assert _stream(svc, {"token": token}, TV, phone=False) == [{"error": "unauthorized"}]   # nor the TV's port
    assert _stream(svc, {"token": AGENT_TOKEN}, "192.168.1.77", phone=False) == []          # not the TV: silence

    async def busy():
        async with svc._busy:
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps({"token": token}).encode() + b"\n")
            reader.feed_eof()
            w = _Writer("192.168.1.77")
            await server.stream_client(svc, reader, w, phone=True)
            return json.loads(w.out)
    assert asyncio.run(busy()) == {"error": "busy"}      # past the token: one command at a time, as for the TV


def test_the_phone_listener_speaks_tls_with_the_pinned_certificate(paths, monkeypatch):
    """The real listener: uvicorn over TLS on a free port, the certificate's digest what the phone pins."""
    import socket
    from tv_agent.tv import tvplayer
    monkeypatch.setattr(tvplayer, "tailscale_address", lambda: None)
    svc = service(paths)
    ports = []
    for _ in range(2):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
    svc.cfg = config.Config(tv_hosts=[TV], lan_host="127.0.0.1", phone_port=ports[0], phone_stream_port=ports[1])
    svc._busy = asyncio.Lock()
    p = svc.clients.start_pairing()

    async def go():
        listeners = server.Listeners(svc, server.local_app(svc))
        await listeners.load_certificate()
        await listeners.update(only=("phones", "phone_stream"))
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE      # pinned instead, as the phone does
            for _ in range(50):
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", ports[0], ssl=ctx)
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            writer.close()
            async with httpx.AsyncClient(verify=ctx) as http:
                r = await http.post(f"https://127.0.0.1:{ports[0]}/v1/pair", json={"code": p.code, "name": "TLS"})
            return der, r
        finally:
            for key in list(listeners.bound):
                listeners._stop(key)
            await asyncio.sleep(0.2)

    der, r = asyncio.run(go())
    import hashlib
    assert hashlib.sha256(der).hexdigest() == svc.clients.fingerprint == C.certificate()
    assert r.status_code == 200 and r.json()["device"]["name"] == "TLS"


# ---- remote.py ----

def test_settings_take_only_values_the_agent_understands():
    assert remote.setting("player", "just") == "just"
    assert remote.setting("max_height", 1080) == 1080 and remote.setting("max_mbps", 0) == 0.0
    assert remote.setting("subtitle_languages", ["spa", "eng"]) == ["spa", "eng"]
    assert remote.setting("twitch_favorites", ["ibai", "auron_play"]) == ["ibai", "auron_play"]
    for key, value in (("player", "vlc"), ("max_height", True), ("max_height", 10), ("max_mbps", "60"),
                       ("binge", "yes"), ("audio_language", "english"), ("subtitle_languages", []),
                       ("subtitle_languages", ["eng", "eng"]), ("twitch_favorites", ["a b"]),
                       ("twitch_favorites", ["ñandú"])):
        with pytest.raises(HTTPException):
            remote.setting(key, value)
    assert "tv_hosts" not in remote.SETTINGS and "lan_host" not in remote.SETTINGS


def test_settings_are_saved_and_live(paths):
    svc = service(paths)
    token = paired(svc)
    gate = C.gateway(svc, server.local_app(svc))
    r = call(gate, "POST", "/v1/settings", token, json={"player": "just", "binge": False})
    assert r.status_code == 200 and r.json()["settings"]["player"] == "just"
    assert svc.cfg.player == "just" and svc.cfg.binge is False
    assert 'player = "just"' in config.CONFIG_FILE.read_text()
    assert call(gate, "POST", "/v1/settings", token, json={"tv_hosts": ["1.2.3.4"]}).status_code == 422
    assert call(gate, "POST", "/v1/settings", token, json={"player": "just", "max_mbps": -1}).status_code == 422
    assert svc.cfg.tv_hosts == [TV]


def test_tracks_lose_links_in_their_names():
    t = remote.tracks_json({
        "audiostreams": [{"index": 0, "language": "eng", "name": "Surround 5.1", "codec": "eac3", "channels": 6}],
        "currentaudiostream": {"index": 0},
        "subtitles": [{"index": 1, "language": "spa",
                       "name": "https://subs.example/eyJrZXkiOiJzZWNyZXQifQ/subtitles/tt1.srt"}],
        "currentsubtitle": {"index": 1}, "subtitleenabled": True})
    assert t["audio"][0]["label"] == "English" and t["subtitle_current"] == 1 and t["subtitles_on"]
    assert "eyJ" not in json.dumps(t) and t["subtitles"][0]["name"].startswith("https://subs.example/")


def test_remote_keys_include_the_d_pad_and_numbers():
    assert {"up", "down", "left", "right", "ok", "back", "home", "0", "9"} <= set(remote.KEYS)
    assert all(v.startswith("KEYCODE_") for v in remote.KEYS.values())
    # the keys the TCL remote's gear and input buttons send (the launcher answers these, not SETTINGS or MENU)
    assert remote.KEYS["gear"] == "KEYCODE_NOTIFICATION" and remote.KEYS["input"] == "KEYCODE_TV_INPUT"


def test_activity_for_the_phone_pages_back_in_time(paths, monkeypatch):
    from tv_agent import activity
    log = paths / "activity.jsonl"
    monkeypatch.setattr(activity, "FILE", log)
    monkeypatch.setattr(activity.read, "__defaults__", (log, None))
    monkeypatch.setattr(activity.mark, "__defaults__", (log, None))
    now = time.time()
    with log.open("w") as f:
        for i in range(5):
            f.write(json.dumps({"t": now - 100 + i, "heard": f"command {i}", "type": "home"}) + "\n")
    svc = service(paths)
    token = paired(svc)
    gate = C.gateway(svc, server.local_app(svc))
    page = call(gate, "GET", "/v1/activity?limit=2", token).json()
    assert [e["heard"] for e in page["entries"]] == ["command 4", "command 3"] and page["more"]
    older = call(gate, "GET", f"/v1/activity?limit=10&before={page['entries'][-1]['t']}", token).json()
    assert [e["heard"] for e in older["entries"]] == ["command 2", "command 1", "command 0"] and not older["more"]
    t = page["entries"][0]["t"]
    assert call(gate, "POST", "/v1/activity/mark", token, json={"t": t, "wrong": True}).json()["entry"]["wrong"]
    assert call(gate, "POST", "/v1/activity/mark", token, json={"t": 1.0}).status_code == 404


def test_junk_on_the_phone_audio_port_is_unauthorized(paths):
    svc = service(paths)
    svc._busy = asyncio.Lock()
    for junk in ([1], "token", 7):
        assert _stream(svc, junk, "192.168.1.77", phone=True) == [{"error": "unauthorized"}]


def test_phones_cant_point_the_agent_at_files_on_this_laptop(paths):
    svc = service(paths)
    token = paired(svc)
    app = C.gateway(svc, server.local_app(svc))
    for body in ({"d1_model": "/etc/passwd.gguf"}, {"stt_model": "/tmp"}):
        r = call(app, "POST", "/v1/pipeline", token, json=body)
        assert r.status_code == 403 and "only from this laptop" in r.text


def test_a_phones_list_leaves_the_one_on_the_tv_alone(paths):
    import time as _time
    from tv_agent.agent import ListItem, Shown
    svc = service(paths)
    token = paired(svc)
    on_tv = Shown("For “algo de miedo”", [ListItem("tt9", "movie", "Scream", "Scream (1996)", "")], _time.monotonic())
    played = []

    async def recent():
        return [ListItem("tt1", "series", "The Bear", "The Bear · S3E2", "", video="tt1:3:2")]

    async def play_item(it):
        played.append(it.imdb)
        return types.SimpleNamespace(ok=True, say="Playing The Bear")
    svc.agent._shown, svc.agent._recent_items, svc.agent.play_item = on_tv, recent, play_item
    svc.record = lambda *a, **k: None
    gate = C.gateway(svc, server.local_app(svc))
    r = call(gate, "GET", "/v1/lists?list=recent", token)
    assert r.status_code == 200 and r.json()["items"][0]["name"] == "The Bear"
    assert svc.agent._shown is on_tv                     # "number 1" on the remote still picks the suggestion
    r = call(gate, "POST", "/v1/lists/pick", token, json={"list": "recent", "imdb": "tt1", "video": "tt1:3:2"})
    assert r.json()["ok"] and played == ["tt1"] and svc.agent._shown is on_tv


def test_a_phone_sees_the_whole_vocabulary_sent_with_each_command(paths, monkeypatch):
    """Yours, the library's titles and the Twitch favourites (when on), each once: what the app lists."""
    import types
    from tv_agent.decide import learned
    monkeypatch.setattr(learned, "load", lambda: [])
    svc = service(paths)
    svc.router = types.SimpleNamespace(steps=["phrases", "patterns", "llm", "d1"], off=set())
    svc.agent.d1 = types.SimpleNamespace(where="hosted", hosted_model="d1", hosted=lambda: None)
    svc.agent.rules = types.SimpleNamespace(fallback_intent=None, counts={})
    svc.stt = None
    svc.cfg.stt_vocabulary = ["Stremio", "Kodi"]
    svc.cfg.twitch_favorites = ["Ibai"]
    svc.cfg.stt_vocabulary_library = True
    svc._library_names, svc._library_names_at = {"recent": ["The Bear", "kodi"]}, 1e12     # fresh: no refresh
    svc._other_names = {"The Bear": "The Bear (FX)"}
    gate = C.gateway(svc, server.local_app(svc))
    p = call(gate, "GET", "/v1/pipeline", paired(svc)).json()
    assert p["stt_vocabulary"] == ["Stremio", "Kodi"]
    assert p["stt_vocabulary_sent"] == ["Stremio", "Kodi", "The Bear", "Ibai"] and p["stt_vocabulary_count"] == 4
    assert p["stt_vocabulary_language"] == "es" and p["stt_vocabularies"] == {}
    assert p["stt_vocabulary_sent_by_language"] == {"es": ["Stremio", "Kodi", "The Bear", "Ibai"],
                                                    "en": ["Stremio", "Kodi", "The Bear (FX)", "Ibai"],
                                                    "": ["Stremio", "Kodi", "The Bear", "The Bear (FX)", "Ibai"]}


def test_each_language_hint_can_have_its_own_names(paths, monkeypatch):
    """Saved one language at a time, merged with the others, written to config.toml as a table it reads back;
    null removes a language's list (it uses the names for no hint again)."""
    import tomllib
    import types
    from tv_agent.decide import learned
    monkeypatch.setattr(learned, "load", lambda: [])
    svc = service(paths)
    svc.router = types.SimpleNamespace(steps=["phrases", "patterns", "llm", "d1"], off=set())
    svc.agent.d1 = types.SimpleNamespace(where="hosted", hosted_model="d1", hosted=lambda: None)
    svc.agent.rules = types.SimpleNamespace(fallback_intent=None, counts={})
    svc.agent.llm = None
    svc.stt = None
    svc.cfg.stt_vocabulary, svc.cfg.stt_vocabulary_library = ["Stremio"], False
    gate = C.gateway(svc, server.local_app(svc))
    token = paired(svc)
    call(gate, "POST", "/v1/pipeline", token, json={"stt_vocabularies": {"es": ["Movistar  Plus", "movistar plus"]}})
    p = call(gate, "POST", "/v1/pipeline", token, json={"stt_vocabularies": {"en": ["Peacock"]}}).json()
    assert p["stt_vocabularies"] == {"es": ["Movistar Plus"], "en": ["Peacock"]}
    assert p["stt_vocabulary_sent"] == ["Movistar Plus"]                          # the hint is Spanish
    assert p["stt_vocabulary_sent_by_language"] == {"es": ["Movistar Plus"], "en": ["Peacock"], "": ["Stremio"]}
    written = tomllib.loads((paths / "config.toml").read_text())
    assert written["stt_vocabularies"] == {"es": ["Movistar Plus"], "en": ["Peacock"]}
    p = call(gate, "POST", "/v1/pipeline", token, json={"stt_vocabularies": {"es": None}}).json()
    assert p["stt_vocabularies"] == {"en": ["Peacock"]} and p["stt_vocabulary_sent"] == ["Stremio"]
    for bad in ({"spanish": []}, ["es"], {"es": "Stremio"}):
        assert call(gate, "POST", "/v1/pipeline", token, json={"stt_vocabularies": bad}).status_code == 422


def test_phones_are_offered_the_tailscale_address_and_told_the_current_ones(paths, monkeypatch):
    """Nothing set by hand: Tailscale's address comes after the LAN's (and phone_hosts'), and a connected phone
    learns where the agent listens now, so an address change doesn't need pairing again."""
    monkeypatch.setattr(C, "tailscale_address", lambda: "100.125.125.38")
    svc = service(paths)
    assert C.addresses(svc.cfg) == ["192.168.1.51", "100.125.125.38"]
    svc.listeners = types.SimpleNamespace(bound={("phones", "100.125.125.38"): 1, ("phones", "192.168.1.52"): 1,
                                                 ("tv", "192.168.1.52"): 1})
    gate = C.gateway(svc, server.local_app(svc))
    me = call(gate, "GET", "/v1/clients/me", paired(svc)).json()
    assert me["addresses"] == ["https://192.168.1.52:8768", "https://100.125.125.38:8768"]
