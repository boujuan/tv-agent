"""The apps on the TV (tv_apps.py) and what phones get of them, of the help and of the history (remote.py)."""
import asyncio
import json
import time
import types

from tv_agent import activity, server, tv_apps, voicetests
from tv_agent import clients as C
from tv_agent.decide import rules as R

from test_clients import call, paired, paths, service  # noqa: F401  (paths is a fixture)

PNG = tv_apps.PNG + b"rest of a png"


class FakeAdb:
    """The TV as ADB sees it: the TV app's receiver (or none, an older TV app) and its files."""
    def __init__(self, receiver=True, apps=None):
        self.receiver = receiver
        self.apps = apps if apps is not None else [
            {"package": "com.netflix.ninja", "activity": "com.netflix.ninja.MainActivity", "label": "Netflix",
             "icon": "com.netflix.ninja.png"},
            {"package": "com.stremio.one", "activity": "com.stremio.tv.MainActivity", "label": "Stremio",
             "icon": "com.stremio.one.png"},
            {"package": "com.boujuan.tvagent", "activity": "com.boujuan.tvagent.ListenActivity", "label": "TV agent"},
            {"package": "com.boujuan.tvagent.player", "activity": "com.boujuan.tvagent.player.SettingsActivity",
             "label": "TV Agent Player"},
            {"package": "bad package", "activity": "x", "label": "Nope"},
        ]

    def run(self, argv, timeout=10.0):
        if argv[:2] == ["am", "broadcast"]:
            return 'Broadcast completed: result=-1, data="4"' if self.receiver else "Broadcast completed: result=0"
        if argv[0] == "cat":
            return json.dumps(self.apps)
        if argv[:3] == ["cmd", "package", "query-activities"]:
            return ("2 activities found:\n  Activity #0:\n    com.stremio.one/com.stremio.tv.MainActivity\n"
                    "  Activity #1:\n    com.rtve.androidtv/.Activity.SplashActivity\n")
        raise AssertionError(argv)

    def run_bytes(self, argv, timeout=15.0):
        return PNG


def fake_svc(adb, rules_apps):
    async def tv(fn, *args):
        return fn(adb, *args)
    rules = types.SimpleNamespace(apps=dict(rules_apps))
    return types.SimpleNamespace(agent=types.SimpleNamespace(tv=tv, rules=rules))


STREMIO = R.App("stremio", "Stremio", "com.stremio.one", "com.stremio.tv.MainActivity", ("stremio",))


def test_names_keys_and_the_tv_s_list():
    assert tv_apps.key_for("Movistar+", "com.movistarplus.androidtv", set()) == "movistar"
    assert tv_apps.key_for("Pluto TV", "tv.pluto.android", {"pluto_tv"}) == "pluto_tv_2"
    assert tv_apps.key_for("ラジオ", "jp.radio.app", set()) == "app"
    assert tv_apps.aliases_for("Pluto TV") == ("pluto tv", "pluto")
    assert tv_apps.aliases_for("Movistar+") == ("movistar+", "movistar")
    assert tv_apps.parse_query("    com.stremio.one/com.stremio.tv.MainActivity\n    com.rtve.androidtv/.Activity.Splash\n"
                               "    com.stremio.one/other.Activity\n    nonsense\n") == [
        ("com.stremio.one", "com.stremio.tv.MainActivity"), ("com.rtve.androidtv", "com.rtve.androidtv.Activity.Splash")]


def test_the_tv_app_s_list_with_names_and_icons_joins_what_voice_can_open(tmp_path):
    svc = fake_svc(FakeAdb(), {"stremio": STREMIO})
    apps = tv_apps.TvApps(svc, tmp_path)
    assert apps.installed() is None                        # never read: nothing is left out
    state = asyncio.run(apps.refresh())
    assert state["source"] == "tv app" and state["count"] == 4 and state["error"] is None
    assert [a["label"] for a in apps.apps] == ["Netflix", "Stremio", "TV agent", "TV Agent Player"]
    assert apps.icon("com.netflix.ninja") == PNG and apps.icon("com.boujuan.tvagent.player") is None
    rules = svc.agent.rules.apps
    assert rules["netflix"].package == "com.netflix.ninja" and rules["netflix"].aliases == ("netflix",)
    assert rules["tv_agent_player"].activity == "com.boujuan.tvagent.player.SettingsActivity"
    assert rules["stremio"] is STREMIO                     # the rules' own stays as written
    assert not any(a.package == "com.boujuan.tvagent" for a in rules.values())     # the agent's listening app

    # read again later: Netflix gone, so it leaves; a rules app with a merged key is never taken out
    again = tv_apps.TvApps(svc, tmp_path)                  # from the cache, as after a restart
    assert again.checked and len(again.apps) == 4
    adb = FakeAdb(apps=[a for a in FakeAdb().apps if a["label"] != "Netflix"])
    svc2 = fake_svc(adb, {"stremio": STREMIO})
    apps2 = tv_apps.TvApps(svc2, tmp_path)
    apps2.merge(svc2.agent.rules)
    assert "netflix" in svc2.agent.rules.apps
    asyncio.run(apps2.refresh())
    assert "netflix" not in svc2.agent.rules.apps and "tv_agent_player" in svc2.agent.rules.apps
    svc2.agent.rules.apps["tv_agent_player"] = R.App("tv_agent_player", "Mine", "my.own.app", ".Main", ("mine",))
    apps2.merge(svc2.agent.rules)
    assert svc2.agent.rules.apps["tv_agent_player"].package == "my.own.app"


def test_an_older_tv_app_gives_the_packages_over_adb(tmp_path):
    svc = fake_svc(FakeAdb(receiver=False), {"stremio": STREMIO})
    apps = tv_apps.TvApps(svc, tmp_path)
    asyncio.run(apps.refresh())
    assert apps.source == "adb"
    assert apps.apps == [
        {"package": "com.stremio.one", "activity": "com.stremio.tv.MainActivity", "label": "Stremio", "icon": False},
        {"package": "com.rtve.androidtv", "activity": "com.rtve.androidtv.Activity.SplashActivity",
         "label": "Androidtv", "icon": False}]


def test_a_tv_that_doesn_t_answer_keeps_the_last_list(tmp_path):
    class Away(FakeAdb):
        def run(self, argv, timeout=10.0):
            raise tv_apps.TvUnreachable("no TV")
    svc = fake_svc(Away(), {})
    apps = tv_apps.TvApps(svc, tmp_path)
    state = asyncio.run(apps.refresh())
    assert state["error"].startswith("TvUnreachable") and apps.checked is None


def test_a_phone_gets_the_tv_s_apps_their_icons_and_the_help(paths, tmp_path):
    svc = service(paths)
    token = paired(svc)
    svc.agent.rules = types.SimpleNamespace(apps={"stremio": STREMIO, "kodi": R.App(
        "kodi", "Kodi", "org.xbmc.kodi", ".Splash", ("kodi",))})
    svc.agent.tv = fake_svc(FakeAdb(), {}).agent.tv
    svc.tv_apps = tv_apps.TvApps(svc, tmp_path / "apps")
    gate = C.gateway(svc, server.local_app(svc))
    before = call(gate, "GET", "/v1/apps", token).json()
    assert [a["key"] for a in before["apps"]] == ["stremio", "kodi"] and before["checked"] is None
    after = call(gate, "POST", "/v1/apps/refresh", token).json()
    assert [a["label"] for a in after["apps"]] == ["Netflix", "Stremio", "TV Agent Player"]     # Kodi isn't on it
    assert after["apps"][0] == {"key": "netflix", "label": "Netflix", "package": "com.netflix.ninja", "icon": True}
    r = call(gate, "GET", "/v1/apps/icon?package=com.netflix.ninja", token)
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert call(gate, "GET", "/v1/apps/icon?package=../../etc", token).status_code == 404

    # after a restart: the list kept, joined at the start (Service.__init__), before the TV is read again
    svc.agent.rules = types.SimpleNamespace(apps={"stremio": STREMIO})
    svc.tv_apps = tv_apps.TvApps(svc, tmp_path / "apps")
    svc.tv_apps.merge(svc.agent.rules)
    restarted = call(gate, "GET", "/v1/apps", token).json()
    assert restarted["count"] == 4 and [a["label"] for a in restarted["apps"]] == ["Netflix", "Stremio", "TV Agent Player"]

    es = call(gate, "GET", "/v1/help?lang=es", token).json()
    assert es["lang"] == "es" and es["sections"][0]["name"] == "Películas y series"
    first = es["sections"][0]["commands"][0]
    assert first["say"] == "pon ‹título›" and first["example"] == "pon ‹título›" and first["type"] == "play_title"
    chains = next(s for s in es["sections"] if s["name"] == "Varias de una vez")["commands"]
    assert chains[0]["example"] == "pon ‹título› y ve al minuto tres"
    en = call(gate, "GET", "/v1/help", token).json()
    assert en["lang"] == "en" and any(c["type"] == "pick" for s in en["sections"] for c in s["commands"])


def test_a_phone_says_a_command_was_right_or_wrong(paths, monkeypatch):
    log = paths / "activity.jsonl"
    monkeypatch.setattr(activity, "FILE", log)
    monkeypatch.setattr(activity.read, "__defaults__", (log, None))
    monkeypatch.setattr(activity.mark, "__defaults__", (log, None))
    monkeypatch.setattr(activity, "TRACES", paths / "traces")
    for name in ("LABELS", "SENTENCES", "FINDING"):
        monkeypatch.setattr(voicetests, name, paths / f"{name.lower()}.jsonl")
    monkeypatch.setattr(voicetests, "LEARNED", paths / "learned.json")
    now = time.time()
    with log.open("w") as f:
        f.write(json.dumps({"t": now - 5, "heard": "pause", "type": "transport",
                            "action": {"type": "transport", "op": "pause"}, "ok": True}) + "\n")
        f.write(json.dumps({"t": now - 2, "heard": "play the bare", "type": "none", "action": {"type": "none"},
                            "ok": False, "details": f"{now - 2}.json.gz"}) + "\n")
    import gzip
    (paths / "traces").mkdir()
    (paths / "traces" / f"{now - 2}.json.gz").write_bytes(gzip.compress(json.dumps({"llm": "the prompt"}).encode()))
    svc = service(paths)
    token = paired(svc)
    gate = C.gateway(svc, server.local_app(svc))

    r = call(gate, "POST", "/v1/activity/right", token, json={"t": now - 5, "words": "pause"})
    assert r.status_code == 200 and r.json()["kept"] == ["one of your sentences"] and "pause" in r.json()["does"]
    assert json.loads((paths / "sentences.jsonl").read_text().splitlines()[0])["text"] == "pause"

    bad = call(gate, "POST", "/v1/activity/wrong", token, json={"t": now - 2, "should": {"type": "nonsense"}})
    assert bad.status_code == 422
    assert call(gate, "POST", "/v1/activity/wrong", token, json={"t": now - 2}).status_code == 422
    r = call(gate, "POST", "/v1/activity/wrong", token, json={
        "t": now - 2, "words": "play the bear", "should": {"type": "play_title", "title": "The Bear"}})
    assert r.status_code == 200 and r.json()["kept"] == ["one of your sentences"]
    wrong = next(e for e in activity.read() if abs(e["t"] - (now - 2)) < 0.001)
    assert wrong["wrong"] is True

    assert call(gate, "GET", f"/v1/activity/details?t={now - 2}", token).json() == {"details": {"llm": "the prompt"}}
    assert call(gate, "GET", f"/v1/activity/details?t={now - 5}", token).json() == {"details": {}}
    assert call(gate, "GET", "/v1/activity/details?t=1.0", token).status_code == 404
