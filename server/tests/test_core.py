"""Offline tests: nothing here talks to the TV, Stremio, Kodi or the models."""
import json
import shlex

import pytest
from pydantic import ValidationError

from tv_agent import actions as A
from tv_agent import languages, redact
from tv_agent.decide import router
from tv_agent.decide import rules as R
from tv_agent.decide import text as T
from tv_agent.decide.numbers import season_episode, seconds_in, to_int
from tv_agent.state import NowPlaying
from tv_agent.stremio import streams
from tv_agent.stremio.progress import updated
from tv_agent.tv.adb import quote

RULES = R.build(R.defaults_raw(), "defaults")


def rule_action(text, rules=RULES):
    hit = router.match_rules(router.normalize(text, rules), rules, [])
    return hit[0] if hit else None


NASTY = ['"; reboot; "', "$(rm -rf /)", "`id`", "a'b", "x && am force-stop com.android.systemui", "\n reboot",
         "tt1:2:3 | sh", "*", "~root"]


@pytest.mark.parametrize("value", NASTY)
def test_quote_keeps_every_argument_one_word(value):
    argv = ["am", "start", "-d", value]
    assert shlex.split(quote(argv)) == argv


def test_quote_rejects_non_strings():
    with pytest.raises(TypeError):
        quote(["am", 3])
    with pytest.raises(TypeError):
        quote([])


def test_actions_reject_unknown_apps_and_fields():
    with pytest.raises(ValidationError):
        A.parse({"type": "open_app", "app": "com.evil.app"})
    with pytest.raises(ValidationError):
        A.parse({"type": "home", "shell": "reboot"})
    with pytest.raises(ValidationError):
        A.parse({"type": "volume", "op": "set", "amount": 500})
    assert A.parse('{"type":"play_title","title":"The Bear","season":3}').season == 3


def test_numbers():
    assert to_int("twenty one") == 21 and to_int("third") == 3 and to_int("banana") is None
    assert seconds_in("go back a minute and a half") == 90
    assert seconds_in("jump to one hour five minutes") == 3900
    assert seconds_in("my phone minutes") is None
    assert season_episode("the bear s3e2")[:2] == (3, 2)
    assert season_episode("episode 4 of season 2 of severance")[:2] == (2, 4)


@pytest.mark.parametrize("text,expect", [
    ("Pause.", A.Transport(op="pause")),
    ("skip forward thirty seconds", A.Transport(op="forward", seconds=30)),
    ("go to twelve minutes", A.Transport(op="seek_to", seconds=720)),
    ("set the volume to 15", A.Volume(op="set", amount=15)),
    ("turn it down a bit", A.Volume(op="down", amount=3)),
    ("next episode", A.NextEpisode()),
    ("Please, please.", A.Transport(op="pause")),
])
def test_grammar(text, expect):
    assert rule_action(text) == expect


@pytest.mark.parametrize("text,expect", [
    ("play lofi hip hop on youtube", A.YouTube(query="lofi hip hop")),
    ("put ibai on twitch", A.Twitch(channel="ibai")),
    ("Go back to YouTube.", A.OpenApp(app="tizentube")),
    ("open mobistar plus", A.OpenApp(app="movistar")),
    ("continue the bear", A.ContinueWatching(title="the bear")),
    ("keep watching what i was watching", A.ContinueWatching()),
    ("show me the office in stremio", A.OpenInStremio(title="the office")),
])
def test_cues(text, expect):
    assert rule_action(text) == expect


def test_title_slots():
    s = T.title_slots("start the first episode of the last of us")
    assert (s["title"], s["episode"], s["kind"]) == ("the last of us", 1, "series")
    assert T.title_slots("i want to watch severance from the start")["restart"] is True
    assert T.title_slots("put on the movie oppenheimer")["kind"] == "movie"


def test_language_plan():
    spain = languages.plan({"country": "Spain"}, "eng", ["eng", "spa"], "spa")
    usa = languages.plan({"country": "United States"}, "eng", ["eng", "spa"], "spa")
    assert spain == languages.TrackPlan("spa", None)
    assert usa == languages.TrackPlan("eng", ["eng", "spa"])
    audio = [{"index": 0, "language": "fre"}, {"index": 1, "language": "eng"}]
    subs = [{"index": 0, "language": "fre"}, {"index": 1, "language": "eng", "name": "SDH"},
            {"index": 2, "language": "eng", "name": "English"}]
    assert languages.pick_audio(audio, "eng")["index"] == 1
    assert languages.pick_subtitle(subs, ["eng", "spa"])["index"] == 2       # plain before SDH
    spanish = [{"index": 0, "language": "spa", "name": "Latino"}, {"index": 1, "language": "spa", "name": "Castellano"}]
    assert languages.pick_audio(spanish, "spa")["index"] == 1                # Castilian first


def test_stream_parse_and_rank():
    raw = [
        {"name": "4K ⚡", "description": "The Bear 3.2 GB", "url": "https://debrid/1",
         "behaviorHints": {"filename": "The.Bear.S03E02.FRENCH.2160p.WEB.mkv"}},
        {"name": "4K ⚡", "description": "The Bear 3.2 GB", "url": "https://debrid/2",
         "behaviorHints": {"filename": "The.Bear.S03E02.2160p.WEB.mkv"}},
        {"name": "1080p ⏳", "description": "uncached", "url": "https://debrid/3"},
        {"name": "CAM", "description": "HDCAM 1080p ⚡", "url": "https://debrid/4"},
        {"name": "torrent", "infoHash": "abc"},
    ]
    parsed = [s for i, r in enumerate(raw) if (s := streams.parse("AIO", r, i))]
    assert len(parsed) == 4                                                    # the torrent is skipped
    ranked = streams.rank(parsed, 2160, 60, 30)
    assert ranked[0].url == "https://debrid/2"                                  # dub-only French marked down
    assert {s.why_dropped for s in ranked[2:]} == {"not cached", "cam/ts"}


def _aio(url, resolution, score, languages, cached=True, size=3_200_000_000, quality="WEB-DL", visual=()):
    return {"name": "x", "description": "y", "url": url, "behaviorHints": {"filename": f"{url}.mkv"},
            "streamData": {"type": "debrid", "service": {"id": "premiumize", "cached": cached}, "size": size,
                           "duration": 1_630_000, "streamExpressionScore": score,
                           "parsedFile": {"resolution": resolution, "quality": quality, "visualTags": list(visual),
                                          "languages": languages}}}


def test_streams_ranked_by_language_resolution_score_and_bitrate():
    raw = [
        _aio("https://debrid/italian-only", "2160p", 9000, ["Italian"]),
        _aio("https://debrid/1080-best", "1080p", 5000, ["English"]),
        _aio("https://debrid/4k-ok", "2160p", 3000, ["English"]),
        _aio("https://debrid/4k-better", "2160p", 3600, ["French", "English"]),
        _aio("https://debrid/uncached", "2160p", 9999, ["English"], cached=False),
        _aio("https://debrid/too-fast", "2160p", 9999, ["English"], size=60_000_000_000),   # ~290 Mbit/s
        _aio("https://debrid/cam", "1080p", 9999, ["English"], quality="CAM"),
        {"name": "error", "externalUrl": "https://aio/error", "streamData": {"type": "error"}},
    ]
    parsed = [s for i, r in enumerate(raw) if (s := streams.parse("AIOStreams", r, i))]
    ranked = streams.rank(parsed, 2160, 60, 27, audio="eng")
    assert [s.url for s in ranked[:4]] == ["https://debrid/4k-better", "https://debrid/4k-ok",
                                           "https://debrid/1080-best", "https://debrid/italian-only"]
    assert {s.why_dropped.split()[-1] for s in ranked[4:]} == {"cached", "Mbit/s", "cam/ts"}
    parsed += [streams.parse("AIOStreams", _aio("https://debrid/castellano", "1080p", 100, ["Spanish"]), 9),
               streams.parse("AIOStreams", _aio("https://debrid/latino", "2160p", 100, ["Latino"]), 10)]
    spanish = streams.rank(parsed, 2160, 60, 27, native_original=True, audio="spa")
    assert [s.url for s in spanish[:2]] == ["https://debrid/castellano", "https://debrid/latino"]   # Spanish-made


def test_playable_follows_redirects_but_not_to_a_problem_video():
    import asyncio
    import httpx

    def handler(request):
        if request.url.path == "/play/ok":
            return httpx.Response(307, headers={"location": "https://cdn.example/file.mkv"})
        if request.url.path == "/play/uncached":
            return httpx.Response(307, headers={"location": "/static/downloading.mp4"})
        return httpx.Response(206, headers={"content-type": "application/force-download"}, content=b"\0")

    async def check(path):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            s = streams.Stream("AIO", f"https://aio.example{path}", "n", "d", "f", 0)
            return await streams.playable(http, s)

    assert asyncio.run(check("/play/ok")) is True
    assert asyncio.run(check("/play/uncached")) is False


def test_progress_update_marks_watched_at_90_percent():
    np = NowPlaying("tt14452776", "series", "tt14452776:3:2", "The Bear")
    item = updated(None, np, 950_000, 1_000_000, 60_000, {})
    assert item["temp"] and item["state"]["timeOffset"] == 0 and item["state"]["flaggedWatched"] == 1
    again = updated(item, NowPlaying("tt14452776", "series", "tt14452776:3:3", "The Bear"), 10_000, 1_000_000, 10_000, {})
    assert again["state"]["video_id"] == "tt14452776:3:3" and again["state"]["timeOffset"] == 10_000


def test_redaction():
    redact.remember("supersecretauthkey123")
    text = "key supersecretauthkey123 url https://aio.example/abc123config/stream/series/tt1.json"
    out = redact.scrub(text)
    assert "supersecretauthkey123" not in out and "abc123config" not in out and "aio.example" in out


@pytest.mark.parametrize("text,expect", [
    ("go to season 4 episode 2", A.PlayTitle(season=4, episode=2, kind="series")),
    ("play episode five", A.PlayTitle(episode=5, kind="series")),
    ("previous episode", A.NextEpisode(offset=-1)),
    ("got to the beginning", A.Transport(op="seek_to", seconds=0)),
    ("go to the middle of this show", A.Transport(op="seek_to", percent=50)),
])
def test_current_show_navigation(text, expect):
    assert rule_action(text) == expect


def test_a_named_show_is_not_the_current_one():
    assert rule_action("go to stranger things season 2") == A.PlayTitle(title="stranger things", season=2,
                                                                          kind="series")


@pytest.mark.parametrize("text,expect", [
    ("watch stop making sense", A.PlayTitle(title="stop making sense")),     # titles made of command words
    ("put on the movie pause", A.PlayTitle(title="pause", kind="movie")),
    ("play up", A.PlayTitle(title="up")),
    ("watch next of kin", A.PlayTitle(title="next of kin")),
    ("put arrival on", A.PlayTitle(title="arrival")),
    ("start the expanse the tenth episode of season five",
     A.PlayTitle(title="the expanse", season=5, episode=10, kind="series")),
    ("community season two episode two", A.PlayTitle(title="community", season=2, episode=2, kind="series")),
    ("okay let's watch the godfather", A.PlayTitle(title="the godfather")),
    ("play youtube", None),                                                  # an app: left for d1
    ("play a game", None),
    ("skip back two minutes", A.Transport(op="back", seconds=120)),
    ("skip to an hour and a half", A.Transport(op="seek_to", seconds=5400)),
    ("a bit louder", A.Volume(op="up", amount=3)),
    ("un pause", A.Transport(op="resume")),
])
def test_play_rules_and_edge_cases(text, expect):
    assert rule_action(text) == expect


def test_rules_reject_mistakes_with_a_reason():
    raw = R.defaults_raw()
    bad = json.loads(json.dumps(raw))
    bad["patterns"][0]["match"] = "(unclosed"
    with pytest.raises(R.RulesError, match="doesn't compile"):
        R.build(bad, "test")
    bad = json.loads(json.dumps(raw))
    bad["patterns"][0]["do"] = {"type": "transport", "op": "pause", "shell": "reboot"}
    with pytest.raises(R.RulesError, match="no field"):
        R.build(bad, "test")
    bad = json.loads(json.dumps(raw))
    bad["hooks"]["youtube"]["app"] = "nope"
    with pytest.raises(R.RulesError, match="hook youtube"):
        R.build(bad, "test")


def test_custom_phrase_and_intent():
    raw = R.defaults_raw()
    raw["phrases"].append({"id": "night", "say": ["night mode"], "do": {"type": "volume", "op": "set", "amount": 8}})
    raw["model"]["intents"]["bedtime"] = {"describe": "make it quiet for the night",
                                          "do": {"type": "volume", "op": "set", "amount": 6}}
    rules = R.build(raw, "test")
    assert rule_action("night mode", rules) == A.Volume(op="set", amount=8)
    assert router.from_intent("bedtime", "make it quiet", rules) == A.Volume(op="set", amount=6)


def test_hook_links_are_encoded():
    app, link = RULES.link("twitch", channel="ibai")
    assert (app.key, link) == ("twitch", "twitch://stream/ibai")
    assert RULES.link("stremio", type="series", id="tt1", video="tt1:3:2")[1] == "stremio:///detail/series/tt1/tt1:3:2"


class FakeAdb:
    def __init__(self, out):
        self.out, self.sent = out, []

    def run(self, argv, timeout=10.0):
        self.sent.append(argv)
        return self.out


def test_just_player_status_brings_position_up_to_date():
    from tv_agent.tv import justplayer
    dump = ("  package=com.brouken.player\n      active=true\n      state=PlaybackState {state=PLAYING(3), "
            "position=62821, buffered position=69236, speed=1.0, updated=1000000, actions=7339995}\n"
            "nowELAPSED=1004000\n")
    assert justplayer.status(FakeAdb(dump)) == ("PLAYING", 66821, 1.0)
    assert justplayer.status(FakeAdb("package=org.xbmc.kodi\n")) is None
    assert justplayer.presses(30, True) == 2 and justplayer.presses(10, False) == 2


def test_just_player_start_reopens_at_the_position():
    from tv_agent.tv import justplayer
    adb = FakeAdb("")
    justplayer.start(adb, "https://debrid/x.mkv", "The Bear · S3E2", 180_000)
    argv = adb.sent[0]
    assert argv[:3] == ["am", "start", "-S"] and argv[argv.index("--ei") + 2] == "180000"


def test_subtitle_languages_and_request_path():
    from tv_agent.stremio import subtitles as S
    assert S.language("eng") == "eng" and S.language("Spanish", "es") == "spa"
    assert S.language("Chinese bilingual", "ze") == "chi" and S.language("chi") == "chi"
    sub = S.parse("SubSense", {"id": "1", "url": "https://subs/1.srt", "lang": "eng",
                               "label": "OpenSubtitles · [SRT] · x · HI"})
    assert sub.language == "eng" and sub.impaired
    assert S.parse("x", {"lang": "eng"}) is None                               # no link, nothing to load
    s = streams.Stream("AIO", "https://debrid/1", "n", "d", "The Bear S03E02.mkv", 0, video_hash="abc", size=123)
    assert S.query_path("series", "tt14452776:3:2", s) == \
        "/subtitles/series/tt14452776:3:2/videoHash=abc&videoSize=123&filename=The%20Bear%20S03E02.mkv.json"
    assert S.query_path("movie", "tt1", None) == "/subtitles/movie/tt1.json"


def test_subtitles_chosen_per_language_in_your_order():
    from tv_agent.stremio import subtitles as S
    subs = [S.Subtitle("https://s/eng-hi", "eng", "x HI", "SubSense", impaired=True),
            S.Subtitle("https://s/eng", "eng", "x", "SubSense"),
            S.Subtitle("https://s/zh", "chi", "Chinese bilingual", "opensubtitles PRO", ai=True),
            S.Subtitle("https://s/spa", "spa", "x", "SubSense")]
    picked = S.choose(subs, ["eng", "cht", "chi", "chs", "spa"])
    assert [(lang, s.url) for lang, s in picked] == [("eng", "https://s/eng"), ("chi", "https://s/zh"),
                                                     ("spa", "https://s/spa")]   # no script hint: counted once


class _FakeKodi:
    def __init__(self, subtitles):
        self.subs, self.added, self.shown, self.ms, self.external = list(subtitles), [], None, 0, []

    async def position(self, pid):
        self.ms += 400                  # playing: the picture moves
        return self.ms, 1_600_000, 1.0

    async def tracks(self, pid):
        return {"audiostreams": [{"index": 0, "language": "eng"}], "currentaudiostream": {"index": 0},
                "subtitles": self.subs + self.external, "subtitleenabled": False}

    async def set_audio(self, pid, index):
        pass

    async def add_subtitle(self, pid, url):          # Kodi lists an added file as "<id> (External)"
        self.added.append(url)
        self.external.append({"index": len(self.subs) + len(self.external), "language": "",
                              "name": f"{len(self.added)} (External)"})
        self.shown = self.external[-1]["index"]

    async def set_subtitle(self, pid, index):
        self.shown = index


def _apply(kodi, offered, langs):
    import asyncio
    import types
    from tv_agent.agent import Agent
    from tv_agent.stremio import subtitles as S

    async def run():
        async def player():
            return 1
        async def recheck(*args):
            pass
        me = types.SimpleNamespace(kodi=kodi, _kodi_player=player, _recheck_tracks=recheck)
        task = asyncio.create_task(asyncio.sleep(0, result=offered))
        return await Agent.apply_tracks(me, languages.TrackPlan("eng", langs), addon_subs=task, settle_s=0)
    return asyncio.run(run())


def test_kodi_gets_addon_subtitles_only_for_missing_languages():
    from tv_agent.stremio import subtitles as S
    offered = [S.Subtitle("https://s/eng", "eng", "x", "SubSense"), S.Subtitle("https://s/spa", "spa", "x", "SubSense")]
    inside_english = _FakeKodi([{"index": 0, "language": "eng", "name": "English"}])
    note = _apply(inside_english, offered, ["eng", "spa"])
    assert inside_english.added == ["https://s/spa"] and inside_english.shown == 0 and note == "eng subs"
    nothing_inside = _FakeKodi([])
    note = _apply(nothing_inside, offered, ["eng", "spa"])
    assert nothing_inside.added == ["https://s/spa", "https://s/eng"]           # the one to show goes last
    assert nothing_inside.shown == 1 and note == "eng subs from SubSense"       # and it's the one selected


@pytest.mark.parametrize("text,stage,expect", [
    ("pause", 0, True), ("next episode", 0, True), ("go home", 0, True), ("mute", 0, True),
    ("play", 0, False), ("play", 1, False), ("play", 2, True),                  # "play …" may go on
    ("play the bear", 1, False), ("play the bear", 2, True),                    # a title alone waits for "season 3"
    ("play the bear season 3 episode 2", 0, True), ("go to season 4 episode 2", 0, True),
    ("volume twenty", 0, False), ("volume twenty", 1, True), ("volume fifteen", 0, True),
    ("lofi on youtube", 1, False), ("skip 30 seconds", 0, True), ("continue the bear", 1, True),
])
def test_when_a_streamed_command_is_acted_on(text, stage, expect):
    from tv_agent.listen_stream import ready
    t = router.normalize(text, RULES)
    hit = router.match_rules(t, RULES, [])
    assert hit, text
    assert ready(hit[0], hit[1], t, stage) is expect


def test_d1_answers_wait_for_the_second_checkpoint():
    from tv_agent.listen_stream import ready
    assert not ready(A.Transport(op="pause"), "d1", "could you hold it there", 0)
    assert ready(A.Transport(op="pause"), "d1", "could you hold it there", 1)
    assert not ready(A.Unclear(), "d1", "what time is it", 1)


def test_endpointer_counts_quiet_after_speech():
    import numpy as np
    from tv_agent.listen_stream import FRAME, Endpointer
    room = (np.random.default_rng(1).normal(0, 30, FRAME)).astype("<i2")       # ~-60 dB background
    voice = (np.sin(np.arange(FRAME) / 5) * 8000).astype("<i2")                 # ~-15 dB
    ep = Endpointer()
    for _ in range(12):
        ep.feed(room, 0)
    assert not ep.speaking and ep.stage == -1
    for _ in range(20):
        ep.feed(voice, 0)
    assert ep.speaking and ep.stage == -1
    stages = []
    for _ in range(40):
        ep.feed(room, 0)
        stages.append(ep.stage)
    assert stages.index(0) == 7 and stages.index(1) == 22 and stages.index(2) == 35   # 160, 460, 720 ms


def test_stream_session_drops_an_early_result_when_you_keep_talking():
    import asyncio
    import numpy as np
    from tv_agent.decide.router import Decision
    from tv_agent.listen_stream import FRAME, StreamSession

    room = (np.random.default_rng(2).normal(0, 30, FRAME)).astype("<i2").tobytes()
    voice = (np.sin(np.arange(FRAME) / 5) * 8000).astype("<i2").tobytes()
    said = iter(["play", "play the bear season 3 episode 2"])

    async def run():
        async def transcribe(audio):
            return next(said)

        async def decide(text):
            hit = router.match_rules(router.normalize(text, RULES), RULES, [])
            return Decision(hit[0], hit[1], 1.0, hit[2]) if hit else Decision(A.Unclear(), "none", 0.0)

        sess = StreamSession(transcribe, decide, lambda t: router.normalize(t, RULES), lambda m: None)
        for chunk in [room] * 12 + [voice] * 20 + [room] * 10 + [voice] * 30 + [room] * 12:
            sess.feed(chunk)
            await asyncio.sleep(0)
        return await sess.finish()

    d, text = asyncio.run(run())
    assert text == "play the bear season 3 episode 2" and d.action.episode == 2   # "play" alone never ran


def test_d1_needs_a_clear_lead_to_act():
    import asyncio
    from tv_agent.decide.d1 import Choice

    class FakeD1:
        def __init__(self, probs):
            self.probs = probs

        async def choice(self, state, question, options):
            key = max(self.probs, key=self.probs.get)
            n = len(options)
            return Choice(key, (self.probs[key] - 1 / n) / (1 - 1 / n), self.probs)

    def decide(probs, text="one two three"):
        return asyncio.run(router.Router(FakeD1(probs), RULES).decide(text))

    assert decide({"next_episode": 0.36, "resume": 0.33, "none": 0.31}).action.type == "none"    # no clear winner
    assert decide({"unmute": 0.63, "none": 0.20, "volume_up": 0.17}).action.type == "none"       # below 0.65
    assert decide({"volume_down": 0.92, "none": 0.05, "mute": 0.03}, "it is way too loud").action.type == "volume"


def test_every_action_has_a_handler_no_attribute_hides():
    import re
    from pathlib import Path
    from tv_agent.agent import Agent
    src = (Path(__file__).parents[1] / "src/tv_agent/agent.py").read_text()
    attributes = set(re.findall(r"self\.(_\w+)\s*(?::[^=\n]+)?=", src))
    for kind in A.ACTION_TYPES:
        name = "_" + kind.replace("none", "unclear")
        assert callable(getattr(Agent, name, None)), f"no handler for {kind}"
        assert name not in attributes, f"self.{name} would hide the {kind} handler"


def test_end_of_playback_starts_the_next_episode_goes_home_or_turns_off():
    import asyncio
    import types
    from tv_agent.agent import Agent, Outcome
    from tv_agent.tv import apps

    def ended(kind, last, after=None, binge=True, user_stopped=False):
        done = []

        async def tv(fn, *args):
            done.append("home" if fn is apps.home else fn)

        async def next_episode(a):
            done.append("next")
            return Outcome(True, "Playing S1E4")

        async def turn_off():
            done.append("off")

        async def notify(text):
            pass

        me = types.SimpleNamespace(_user_stopped=user_stopped, dry_run=False, _after_this=after, tv=tv, notify=notify,
                                   _next_episode=next_episode, _turn_off=turn_off, cfg=types.SimpleNamespace(binge=binge))
        np = NowPlaying("tt1", kind, "tt1:1:3" if kind == "series" else "tt1", "X")
        asyncio.run(Agent._ended(me, np, "stopped", last, 3_000_000))
        return done

    assert ended("movie", 2_995_000) == ["home"]
    assert ended("series", 2_995_000) == ["next"]
    assert ended("series", 2_995_000, binge=False) == ["home"]
    assert ended("series", 2_995_000, after="stop") == ["home"]          # "stop after this one" beats binge mode
    assert ended("series", 2_995_000, after="off") == ["off"]
    assert ended("series", 1_000_000) == ["home"]                        # stopped on the remote: Home, not Kodi's menus
    assert ended("series", 2_995_000, user_stopped=True) == []          # "stop" already went Home


def test_watched_marks_round_trip_realign_and_never_wipe():
    from tv_agent.stremio import watched as W
    ids = ["tt1:0:1", "tt1:1:1", "tt1:1:2", "tt1:1:3", "tt1:2:1"]
    marks = W.mark(None, ids, ["tt1:1:1", "tt1:1:2"])
    assert marks.startswith("tt1:1:2:3:") and W.decode(marks, ids) == [False, True, True, False, False]
    longer = ["tt1:0:1", "tt1:0:2"] + ids[1:]                  # a special added in front: lined up on the anchor
    assert W.decode(marks, longer) == [False, False, True, True, False, False]
    assert W.decode(marks, ["tt1:3:1", "tt1:3:2"]) is None      # anchor missing: can't be lined up
    assert W.mark(marks, ["tt1:3:1", "tt1:3:2"], ["tt1:3:1"]) is None    # so nothing is rewritten
    assert W.decode(W.mark(marks, ids, ["tt1:1:1"], on=False), ids) == [False, False, True, False, False]
    assert W.decode("undefined:1:eJwDAAAAAAE=", ids) == [False] * 5   # what Stremio writes for films


def test_progress_marks_the_episode_watched_at_90_percent():
    from tv_agent.stremio import watched as W
    ids = ["tt1:1:1", "tt1:1:2"]
    np = NowPlaying("tt1", "series", "tt1:1:2", "X")
    item = updated(None, np, 950_000, 1_000_000, 60_000, {}, ids)
    assert W.decode(item["state"]["watched"], ids) == [False, True]
    assert "watched" not in updated(None, np, 500_000, 1_000_000, 60_000, {}, ids)["state"]


def test_search_merges_names_and_popularity_settles_namesakes():
    from tv_agent.stremio import cinemeta as C
    from tv_agent.stremio.search import merge, said_exactly
    cine = [C.Meta("tt1", "movie", "Back to the Future", "1985", 0), C.Meta("tt2", "series", "Back to the Future", "1991", 0)]
    aio = [C.Meta("tt1", "movie", "Regreso al futuro", "1985", C.NOT_RANKED, extra_rank=0,
                  names=("Regreso al futuro",)),
           C.Meta("tt2", "series", "Regreso al futuro", "1991", C.NOT_RANKED, extra_rank=0, names=("Regreso al futuro",))]
    merged = merge([cine, aio])
    assert [m.names for m in merged] == [("Back to the Future", "Regreso al futuro")] * 2
    assert said_exactly("regreso al futuro", merged[0]) and merged[0].label == \
        "Back to the Future / Regreso al futuro (1985, movie)"
    tie = C.ranked("regreso al futuro", merged)
    assert tie[0][0] - tie[1][0] < 20                              # a close call...
    settled = C.ranked("regreso al futuro", merged, popularity={"tt1": 2.27, "tt2": 0.39})
    assert settled[0][1].id == "tt1"                               # ...settled by popularity


def test_library_match_needs_the_whole_name():
    import asyncio
    from tv_agent.stremio.library import Library
    lib = Library(None)
    lib._items = [{"_id": "tt1", "type": "movie", "name": "Dune"},
                  {"_id": "tt2", "type": "movie", "name": "El señor de los anillos: La comunidad del anillo"},
                  {"_id": "tt3", "type": "series", "name": "The Office"},
                  {"_id": "tt4", "type": "series", "name": "Fundación"}]
    find = lambda t: (asyncio.run(lib.find(t)) or {}).get("_id")   # noqa: E731
    assert find("dune parte dos") is None and find("dune") == "tt1"
    assert find("el señor de los anillos") == "tt2"                # the name before its subtitle
    assert find("office") == "tt3" and find("fundacion") == "tt4"


def test_pick_needs_a_list_on_screen():
    import asyncio
    import time
    import types
    from tv_agent.agent import Agent, ListItem, Shown
    me = types.SimpleNamespace(_shown=None)
    assert asyncio.run(Agent._pick(me, A.Pick(number=2))).say == "No list on screen"
    me._shown = Shown("Continue watching", [ListItem("tt1", "movie", "Up", "Up", "")], time.monotonic())
    assert asyncio.run(Agent._pick(me, A.Pick(number=2))).say == "The list goes up to 1"
    me._shown = Shown("Continue watching", [ListItem("tt1", "movie", "Up", "Up", "")], time.monotonic() - 400)
    assert asyncio.run(Agent._pick(me, A.Pick(number=1))).say == "No list on screen"   # too old


@pytest.mark.parametrize("text,expect", [
    ("number two", {"type": "pick", "number": 2}), ("the third one", {"type": "pick", "number": 3}),
    ("no one", None), ("what's new", {"type": "show_list", "list": "new"}),
    ("play the latest episode of the bear", {"type": "play_title", "title": "the bear", "latest": True}),
    ("mark as watched", {"type": "library", "op": "watched", "title": None}),
    ("add spanish subtitles", None),
])
def test_list_and_library_rules(text, expect):
    got = rule_action(text)
    if expect is None:
        assert got is None or got.type not in ("pick", "library")
    else:
        assert got is not None and all(getattr(got, k) == v for k, v in expect.items())


def test_a_replaced_follower_saves_its_position_and_polls_no_more():
    import asyncio
    from tv_agent.stremio import progress as P

    class Account:
        def __init__(self):
            self.puts = []

        async def put(self, items):
            self.puts.append(items[0]["state"]["timeOffset"])

    class Library:
        async def get(self, _):
            return None

        def invalidate(self):
            pass

    async def run():
        account, polls, stop = Account(), [], asyncio.Event()

        async def poll():
            polls.append(1)
            return 60_000 * len(polls), 3_000_000, 1.0
        task = asyncio.create_task(P.track(poll, account, Library(), NowPlaying("tt1", "movie", "tt1", "X"), {},
                                           every_s=0.01, stop=stop))
        while len(polls) < 3:
            await asyncio.sleep(0.005)
        stop.set()
        how, last, _ = await task
        return how, last, account.puts, len(polls)

    how, last, puts, polls = asyncio.run(run())
    assert how == "replaced" and puts[-1] == last and polls in (3, 4)    # the last write: the position it had


def test_a_write_without_a_length_keeps_the_known_one():
    np = NowPlaying("tt1", "series", "tt1:1:2", "X")
    item = {"_id": "tt1", "state": {"video_id": "tt1:1:2", "duration": 3_000_000, "timeOffset": 100_000}}
    assert updated(item, np, 120_000, 0, 20_000, {})["state"]["duration"] == 3_000_000


def test_skip_timestamps_fit_the_file_and_need_more_to_go_by_themselves():
    from tv_agent import skips as K
    assert K._length_fit(2_831_000, 2_831_500)[1:] == (True, True)          # same file length
    assert K._length_fit(2_831_000, 2_845_000)[1:] == (False, True)         # near: only when asked
    assert K._length_fit(4_059_000, 3_795_000) is None                      # another cut: ignored
    scale, _, near = K._length_fit(round(3_695_000 / K.PAL), 3_695_000)     # PAL speed-up: scaled
    assert near and abs(scale - 1 / K.PAL) < 1e-3
    intro = K.Segment("intro", 216_000, 233_000, "TheIntroDB")
    assert K.around([intro], "intro", 220_000) is intro
    assert K.around([intro], "intro", 205_000) is intro                     # just before it
    assert K.around([intro], "intro", 60_000) is None                       # nowhere near: no jump
    assert K.around([intro], "recap", 220_000) is None


def test_two_sources_agreeing_make_a_segment_sure_unless_copied():
    import asyncio
    from tv_agent import skips as K

    def run(tidb_intro, idb_intro):
        async def tidb(*_):
            return [K.Segment("intro", *tidb_intro, "TheIntroDB", False, True)]

        async def idb(*_):
            return [K.Segment("intro", *idb_intro, "IntroDB")]
        K._theintrodb, K._introdb = tidb, idb
        return asyncio.run(K.fetch(None, "tt1", 1, 1, 2_000_000))[0].sure

    real_t, real_i = K._theintrodb, K._introdb
    try:
        assert run((216_218, 233_207), (216_000, 232_000))          # independent and within 2 s
        assert not run((216_000, 233_000), (216_000, 233_000))      # identical: likely copied
        assert not run((216_000, 233_000), (220_000, 233_000))      # 4 s apart
    finally:
        K._theintrodb, K._introdb = real_t, real_i


def test_auto_skip_jumps_a_sure_intro_as_it_comes_and_leaves_the_rest():
    import asyncio
    import types
    from tv_agent import skips as K
    from tv_agent.agent import Agent

    def watch(segments, start_ms):
        clock = {"t": 0.0, "base": start_ms}
        seeks = []

        async def sleep(d):
            clock["t"] += d

        async def position(pid):
            return round(clock["base"] + clock["t"] * 1000), 2_831_000, 1.0

        async def seek_to(pid, ms):
            seeks.append(ms)
            clock["base"] = ms - clock["t"] * 1000

        async def player():
            return 1 if clock["t"] < 600 else None

        async def segs(np, total):
            return segments

        async def notify(text):
            pass
        np = NowPlaying("tt1", "series", "tt1:2:3", "X")
        me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np), _kodi_player=player, segments=segs,
                                   kodi=types.SimpleNamespace(position=position, seek_to=seek_to), notify=notify)
        asyncio.run(Agent._skip_watch(me, np, sleep=sleep))
        return seeks

    sure = K.Segment("intro", 216_000, 233_000, "TheIntroDB", sure=True)
    unsure = K.Segment("intro", 216_000, 233_000, "IntroDB")
    assert watch([sure], 200_000) == [233_000]          # skipped once, as it came
    assert watch([unsure], 200_000) == []                # not sure: only when asked
    assert watch([sure], 400_000) == []                  # resumed after it
