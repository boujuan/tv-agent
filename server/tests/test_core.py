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
    ("Please, please.", A.Transport(op="toggle")),                   # a misheard pause or play: whichever fits
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
    ("play youtube", A.OpenApp(app="tizentube")),                           # an app by its exact name
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
    ("play the bear season 3 episode 2", 0, False), ("play the bear season 3 episode 2", 1, True),   # "… and …"
    ("go to season 4 episode 2", 0, False), ("go to season 4 episode 2", 1, True),
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

    def ended(kind, last, after=None, binge=True, user_stopped=False, replaced=False):
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

        np = NowPlaying("tt1", kind, "tt1:1:3" if kind == "series" else "tt1", "X")
        me = types.SimpleNamespace(_user_stopped=user_stopped, dry_run=False, _after_this=after, tv=tv, notify=notify,
                                   _next_episode=next_episode, _turn_off=turn_off, cfg=types.SimpleNamespace(binge=binge),
                                   state=types.SimpleNamespace(now_playing=None if replaced else np))
        asyncio.run(Agent._ended(me, np, "stopped", last, 3_000_000))
        return done

    assert ended("movie", 2_995_000) == ["home"]
    assert ended("series", 2_995_000) == ["next"]
    assert ended("series", 2_995_000, binge=False) == ["home"]
    assert ended("series", 2_995_000, after="stop") == ["home"]          # "stop after this one" beats binge mode
    assert ended("series", 2_995_000, after="off") == ["off"]
    assert ended("series", 1_000_000) == ["home"]                        # stopped on the remote: Home, not Kodi's menus
    assert ended("series", 2_995_000, user_stopped=True) == []          # "stop" already went Home
    assert ended("series", 2_995_000, replaced=True) == []              # something else started meanwhile


def test_watched_marks_follow_stremios_order_and_never_wipe():
    from tv_agent.stremio import watched as W
    ep = lambda s, e: {"id": f"tt1:{s}:{e}", "season": s, "episode": e}               # noqa: E731
    videos = [ep(0, 1), ep(0, 2)] + [ep(1, e) for e in range(1, 9)]                    # specials first, as AIOMetadata
    assert W.stremio_order(videos + [ep(1, 1)]) == [f"tt1:1:{e}" for e in range(1, 9)] + ["tt1:0:1", "tt1:0:2"]
    ids = W.stremio_order(videos)
    # Stremio (stremio-core) watched E1-E5 and the first special: its anchor is that special, last in its order.
    stremio = W.encode([i < 5 or ids[i] == "tt1:0:1" for i in range(len(ids))], ids)
    assert stremio.startswith("tt1:0:1:9:")
    marks = W.mark(stremio, videos, ["tt1:1:6"])
    got = W.decode(marks, ids)
    assert {ids[i] for i, b in enumerate(got) if b} == {f"tt1:1:{e}" for e in range(1, 7)} | {"tt1:0:1"}  # none lost
    # An older client that wrote them in the addon's order (specials first), anchored on an episode: same reading.
    raw = [v["id"] for v in videos]
    older = W.encode([raw[i] in ("tt1:1:1", "tt1:1:2") for i in range(len(raw))], raw)
    assert {ids[i] for i, b in enumerate(W.decode(W.mark(older, videos, ["tt1:1:3"]), ids)) if b} == \
        {"tt1:1:1", "tt1:1:2", "tt1:1:3"}
    # Odd marks the two orders read differently, or an anchor not in the list: left alone.
    odd = "tt1:0:1:1:" + W.encode([False] * 3 + [True] + [False] * 6, raw).rsplit(":", 1)[1]
    assert W.mark(odd, videos, ["tt1:1:3"]) is None
    assert W.mark(W.encode([True], ["tt9:1:1"]), videos, ["tt1:1:3"]) is None
    assert W.decode("undefined:1:eJwDAAAAAAE=", ids) == [False] * len(ids)    # what Stremio writes for films


def test_progress_marks_the_episode_watched_at_90_percent():
    from tv_agent.stremio import watched as W
    videos = [{"id": "tt1:1:1", "season": 1, "episode": 1}, {"id": "tt1:1:2", "season": 1, "episode": 2}]
    np = NowPlaying("tt1", "series", "tt1:1:2", "X")
    item = updated(None, np, 950_000, 1_000_000, 60_000, {}, videos)
    assert W.decode(item["state"]["watched"], ["tt1:1:1", "tt1:1:2"]) == [False, True]
    assert "watched" not in updated(None, np, 500_000, 1_000_000, 60_000, {}, videos)["state"]


def test_a_start_that_never_played_writes_no_progress():
    """A file that sat buffering at 0:00 with no length known (2026-10-24: the TV couldn't look up the server) and was
    closed: nothing is written, so the resume point in Stremio stays."""
    import asyncio
    import types
    from tv_agent.stremio import progress as P
    puts = []

    def run(polls):
        async def poll():
            return polls.pop(0) if polls else None

        async def put(items):
            puts.append(items[0]["state"]["timeOffset"])

        async def get(imdb):
            return None
        account = types.SimpleNamespace(put=put)
        library = types.SimpleNamespace(get=get, invalidate=lambda: None)
        np = NowPlaying("tt1", "movie", "tt1", "X", "tvplayer")
        asyncio.run(P.track(poll, account, library, np, {}, every_s=0))
        return np

    run([(0, 0, 0.0), (0, 0, 0.0)])
    assert puts == []
    run([(60_000, 1_000_000, 1.0), (70_000, 1_000_000, 0.0)])         # one that played: written on pause and at the end
    assert puts == [70_000, 70_000]


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
                  {"_id": "tt4", "type": "series", "name": "Fundación"},
                  {"_id": "tt5", "type": "series", "name": "The Bear"}]
    find = lambda t: (asyncio.run(lib.find(t)) or {}).get("_id")   # noqa: E731
    assert find("dune parte dos") is None and find("dune") == "tt1"
    assert find("el señor de los anillos") == "tt2"                # the name before its subtitle
    assert find("office") == "tt3" and find("fundacion") == "tt4"
    assert find("the bare") == "tt5"                               # spelled otherwise, said the same
    assert find("dawn") is None                                    # sounds a bit like Dune, but isn't


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

    def watch(segments, start_ms, auto=True, also=None):
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
        kodi = types.SimpleNamespace(position=position, seek_to=seek_to)
        me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np), _player_id=player, segments=segs,
                                   _ctl=lambda pid: kodi, notify=notify, cfg=types.SimpleNamespace(auto_skip=auto))
        asyncio.run(Agent._skip_watch(me, np, sleep=sleep, also=also))
        return seeks

    sure = K.Segment("intro", 216_000, 233_000, "TheIntroDB", sure=True)
    unsure = K.Segment("intro", 216_000, 233_000, "IntroDB")
    assert watch([sure], 200_000) == [233_000]          # skipped once, as it came
    assert watch([unsure], 200_000) == []                # not sure: only when asked
    assert watch([sure], 400_000) == []                  # resumed after it
    # "play it and skip the intro": that intro, sure or not, even with auto skip off
    assert watch([unsure], 30_000, auto=False, also="intro") == [233_000]
    assert watch([sure], 30_000, auto=False) == []


def test_speech_model_download_is_checked_and_unpacked(tmp_path, monkeypatch):
    import contextlib
    import hashlib
    import io
    import tarfile
    from tv_agent import stt

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name in stt.FILES:
            data = name.encode()
            info = tarfile.TarInfo(f"fake-model/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive = buf.getvalue()

    class Http:
        @contextlib.contextmanager
        def stream(self, method, url, **kw):
            assert url == stt.RELEASE + "fake-model.tar.bz2"
            yield type("R", (), {"raise_for_status": lambda self: None,
                                 "iter_bytes": lambda self, n: iter([archive[:100], archive[100:]])})()

    monkeypatch.setattr(stt, "DATA_DIR", tmp_path)
    monkeypatch.setitem(stt.MODELS, "fake", stt.Model("Fake", "none", "fake-model", "0" * 64, len(archive)))
    with pytest.raises(ValueError):
        stt.download("fake", Http())                                   # wrong SHA-256: nothing installed
    assert not stt.installed("fake") and not list((tmp_path / "models/fake").iterdir())
    monkeypatch.setitem(stt.MODELS, "fake", stt.Model("Fake", "none", "fake-model",
                                                      hashlib.sha256(archive).hexdigest(), len(archive)))
    assert stt.download("fake", Http()) == tmp_path / "models/fake/fake-model" and stt.installed("fake")


def test_d1_falls_back_to_the_hosted_one_only_with_a_key():
    import asyncio
    import httpx
    from tv_agent.decide.d1 import D1, HOSTED

    class Http:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None, timeout=None, headers=None):
            self.calls.append((url, json.get("model"), (headers or {}).get("Authorization")))
            if url != HOSTED:
                raise httpx.ConnectError("llama-d1 is down")
            return httpx.Response(200, request=httpx.Request("POST", url), json={"answers": {"q": {
                "choice": "pause", "confidence": 0.9, "probabilities": {"pause": 0.95, "none": 0.05}}}})

    http = Http()
    d1 = D1(http, "http://127.0.0.1:8091", hosted_key="k")
    pick = asyncio.run(d1.choice("hold it", "Which action?", {"pause": "pause", "none": "nothing"}))
    assert pick.key == "pause" and d1.last_source == "hosted"
    assert http.calls[-1] == (HOSTED, "d1", "Bearer k")
    with pytest.raises(httpx.ConnectError):                    # no key: the error surfaces as before
        asyncio.run(D1(Http(), "http://127.0.0.1:8091").choice("x", "?", {"a": "a"}))


def test_llm_answers_become_checked_actions():
    from tv_agent.decide.llm import to_action
    cands = ["The Bear", "Severance"]
    play = to_action({"action": "play_title", "title": "The Bear", "season": 3}, "put that bare show on", cands, RULES)
    assert play == A.PlayTitle(title="The Bear", season=3)
    described = to_action({"action": "play_title", "title": "La casa de papel"}, "the one with the dali masks", cands, RULES)
    assert described == A.PlayTitle(title="La casa de papel")          # its own knowledge: the search checks it
    assert to_action({"action": "volume_set", "number": 30}, "x", [], RULES) == A.Volume(op="set", amount=30)
    assert to_action({"action": "volume_set", "number": 300}, "x", [], RULES) is None          # out of range
    assert to_action({"action": "subtitles", "language": "Spanish"}, "x", [], RULES) == A.Tracks(subtitles="spa")
    assert to_action({"action": "open_app", "app": "YouTube"}, "x", [], RULES) == A.OpenApp(app="tizentube")
    assert to_action({"action": "pick", "number": 2}, "x", [], RULES) == A.Pick(number=2)
    assert to_action({"action": "none"}, "what's the weather", [], RULES) is None


def test_the_llm_only_sees_what_rules_and_d1_left_undecided():
    import asyncio
    from tv_agent.decide.d1 import Choice

    class D1:
        async def choice(self, state, question, options):
            return Choice("none", 0.9, {"none": 0.9, "pause": 0.1})

    class Llm:
        model = "test/model"

        def __init__(self, reached=True):
            self.asked, self.reached = [], reached

        async def answer(self, said, context):
            self.asked.append((said, context))
            return (True, A.Transport(op="pause")) if self.reached else (False, None)

    async def context(text):
        return {"candidates": []}

    for steps in (router.STEPS, ("phrases", "patterns", "d1", "llm")):     # the LLM before d1, and after
        llm = Llm()
        r = router.Router(D1(), RULES, llm, context, steps)
        assert asyncio.run(r.decide("pause")).via == "phrase" and not llm.asked          # rules first
        d = asyncio.run(r.decide("could you freeze the picture for me"))
        assert d.via == "llm" and d.action.op == "pause" and d.trace[-1]["stage"] == "llm"
        assert llm.asked[0][0] == "could you freeze the picture for me"
    unreachable = router.Router(D1(), RULES, Llm(reached=False), context)
    d = asyncio.run(unreachable.decide("could you freeze the picture for me"))
    assert d.via == "d1" and [s["stage"] for s in d.trace][-2:] == ["llm", "model"]    # d1 when the LLM is out


def test_thats_wrong_marks_the_last_command_and_teaching_makes_a_phrase(tmp_path):
    from tv_agent import activity
    from tv_agent.decide import learned
    log = tmp_path / "activity.jsonl"
    activity.append({"heard": "freeze it", "type": "none"}, log)
    activity.append({"heard": "that's wrong", "type": "wrong"}, log)
    marked = activity.mark_last_wrong(log)
    assert marked["heard"] == "freeze it"
    assert {e["heard"]: e.get("wrong") for e in activity.read(log)} == {"freeze it": True, "that's wrong": None}
    store = tmp_path / "learned.json"
    learned.add("Freeze it", {"type": "transport", "op": "pause"}, store)
    learned.add("freeze it", {"type": "transport", "op": "stop"}, store)            # taught again: replaced
    assert [(x["say"], x["do"]) for x in learned.load(store)] == [("freeze it", {"op": "stop", "type": "transport"})]
    with pytest.raises(ValueError):
        learned.add("blah", {"type": "none"}, store)
    assert learned.remove("FREEZE IT", store) and learned.load(store) == []
    for text in ("that's wrong", "no, that's not what i said", "wrong", "Eso está mal."):
        assert rule_action(text).type == "wrong", text


def test_tracebacks_lose_their_links_too():
    import io
    import logging
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(redact.RedactFilter())
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("test-redact-traceback")
    log.addHandler(handler)
    log.propagate = False
    try:
        raise RuntimeError("Server error '500' for url 'https://addon.example/abcdefsecret123/catalog/movie/x.json'")
    except RuntimeError:
        log.exception("films list failed")
    out = stream.getvalue()
    assert "abcdefsecret123" not in out and "https://addon.example/…" in out and "Traceback" in out


def test_a_start_whose_links_fail_leaves_what_plays_alone():
    import asyncio
    import types
    from tv_agent.agent import Agent
    from tv_agent.stremio import streams as S

    async def run():
        a = Agent.__new__(Agent)
        replaced = []

        async def nothing(*args, **kw):
            return None

        async def ready():
            return True

        async def found(kind, video, meta):
            return [S.Stream("AIOStreams", "https://x/1", "n", "", "f.mkv", 0)]

        async def tv(fn, *args):
            return ""
        a.account, a.dry_run, a.notify, a.tv = object(), False, nothing, tv
        a.cfg = types.SimpleNamespace(player="kodi", probe_streams=True)
        a._start_kodi, a.streams_for, a._first_playable = ready, found, nothing      # no link answers
        a.track_plan = lambda meta: languages.TrackPlan("eng", None)
        a.kodi = types.SimpleNamespace(prefer=nothing, allow_hdr=nothing)
        a._home_unless_playing = nothing
        a._replace_session = lambda *args: replaced.append(args)
        out = await Agent.play(a, "tt1", "movie", "X", "tt1", "X", {})
        return out, replaced

    out, replaced = asyncio.run(run())
    assert not out.ok and "didn't answer" in out.say and replaced == []      # the old session wasn't touched


def test_every_command_in_the_list_is_understood_in_english_and_spanish():
    """Each pattern of commands.py, expanded and filled in, gets its action from the rules alone (no model)."""
    import asyncio
    from tv_agent.commands import SECTIONS, expand

    class NoD1:
        async def choice(self, *args):
            raise router.httpx.ConnectError("off")

    rules = R.build(R.defaults_raw(), "defaults")
    rules.fallback_intent = "none"
    r = router.Router(NoD1(), rules)
    wrong = []
    for section in SECTIONS:
        for c in section.commands:
            for lang in ("en", "es"):
                for text in expand(c.say(lang), lang):
                    got = asyncio.run(r.decide(text)).action
                    if got.type != c.type:
                        wrong.append((text, c.type, got.type))
    assert not wrong, wrong




@pytest.mark.parametrize("text,expect", [
    ("Subtitles of", A.Tracks(subtitles="off")),                  # misheard "off" at the end of a switch
    ("Sops off.", A.Tracks(subtitles="off")),
    ("Subtitles all.", A.Tracks(subtitles="toggle")),             # on or off unclear: switched over
    ("subtitles", A.Tracks(subtitles="toggle")),
    ("Turn the subtitles on.", A.Tracks(subtitles="on")),         # any switch, either word order
    ("turn the binge mode off", A.Binge(on=False)),
    ("Beach mode Oh.", A.Binge(on=True)),
    ("Pinch Mode On", A.Binge(on=True)),
    ("auto skip of", A.AutoSkip(on=False)),
    ("put severance on", A.PlayTitle(title="severance")),
    ("Turn down the volume to the minimum.", A.Volume(op="set", amount=1)),
    ("volume to the max", A.Volume(op="set", amount=100)),
    ("What am I watching right now?", A.Info()),
    ("¿qué estoy viendo?", A.Info()),
    ("where am I in severance", A.Info(title="severance")),
    ("What is the last episode of Cebruns that I watched?", A.Info(title="cebruns")),
    ("skip thirty seconds", A.Transport(op="forward", seconds=30)),
    ("skip purpose all", None),                                   # "skip" + no duration: not a jump
    ("skip a bit", A.Transport(op="forward", seconds=30)), ("skip thirty", A.Transport(op="forward", seconds=30)),
    ("skip it", A.Transport(op="forward")),
    ("turn the sound back on", A.Volume(op="unmute")),            # the word-order swap leaves "back" alone
    ("put youtube on", A.OpenApp(app="tizentube")),               # not a YouTube search for "put"
    ("marca esto como visto", A.LibraryEdit(op="watched")),       # "esto": what's on, not a title
    ("añade esto a mi lista", A.LibraryEdit(op="add")),
])
def test_misheard_switches_and_new_commands(text, expect):
    assert rule_action(text) == expect


def _router(**kw):
    class NoD1:
        where = "local"

        async def choice(self, *args):
            raise router.httpx.ConnectError("off")
    rules = R.build(R.defaults_raw(), "defaults")
    rules.fallback_intent = "none"
    return router.Router(NoD1(), rules, **kw)


@pytest.mark.parametrize("text,steps", [
    ("go to the second episode of sevence and go to the middle of it", ["play_title", "transport"]),
    ("turn the volume down and play the bear", ["volume", "play_title"]),
    ("pon la casa de papel y salta la intro", ["play_title", "skip"]),
    ("play pride and prejudice", None),                           # a title with "and" stays one title
    ("play tom and jerry", None),
    ("pon romeo y julieta", None),
    ("skip the intro and the recap", None),                       # a bare name isn't a title to play
    ("pon el volumen al treinta y cinco", None),                  # numbers: one command (volume 35)
    ("ve al minuto treinta y cinco", None),
    ("skip to an hour and a half", None),
    ("pausa y luego", None),                                      # the offline guess isn't a second command
    ("salta la intro y pon friends", ["skip", "play_title"]),     # "pon" is a play verb once cleaned up
    ("pon the bear y apaga la tele cuando acabe", ["play_title", "timer"]),
    ("para cuando acabe este, que estoy cansado", None),
])
def test_two_commands_in_one_sentence(text, steps):
    import asyncio
    d = asyncio.run(_router().decide(text))
    if steps is None:
        assert d.action.type != "sequence", d.action
    else:
        assert d.action.type == "sequence" and [s.type for s in d.action.steps] == steps


def test_help_is_shown_in_the_language_it_was_asked_in():
    import asyncio
    r = _router()
    assert asyncio.run(r.decide("what can you do")).action == A.Help(lang="en")
    assert asyncio.run(r.decide("¿Qué puedes hacer?")).action == A.Help(lang="es")
    assert asyncio.run(r.decide("ayuda")).action == A.Help(lang="es")


def test_steps_follow_their_order_and_can_be_switched_off():
    import asyncio

    class Llm:
        model = "test/model"

        def __init__(self):
            self.asked = []

        async def answer(self, said, context):
            self.asked.append(said)
            return True, A.Transport(op="stop")

    llm = Llm()
    r = _router(llm=llm)
    assert asyncio.run(r.decide("pause")).via == "phrase" and not llm.asked
    r.steps = ["llm", "phrases", "patterns", "d1"]                 # the LLM first: it decides everything
    assert asyncio.run(r.decide("pause")).action == A.Transport(op="stop") and llm.asked == ["pause"]
    r.steps, r.off = list(router.STEPS), {"phrases", "patterns"}
    d = asyncio.run(r.decide("skip the intro"))
    assert d.via == "llm" and [t["stage"] for t in d.trace[1:3]] == ["phrase", "pattern"]
    assert d.trace[1].get("off") and d.trace[2].get("off")
    r.off = {"phrases", "patterns", "llm", "d1"}                   # nothing on: nothing understood
    assert asyncio.run(r.decide("pause")).action.type == "none"


def test_old_settings_keep_their_meaning(tmp_path, monkeypatch):
    from tv_agent import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.toml")
    tmp_path.chmod(0o700)
    (tmp_path / "config.toml").write_text("llm_fallback = false\nllm_first = false\nhosted_d1 = false\n")
    cfg = config.load()
    assert cfg.decide_off == ["llm"] and cfg.decide_steps == ["phrases", "patterns", "d1", "llm"]
    assert cfg.d1_where == "local"
    (tmp_path / "config.toml").write_text('decide_steps = ["d1"]\nd1_where = "hosted"\nllm_first = true\n')
    cfg = config.load()
    assert cfg.decide_steps == ["d1", "phrases", "patterns", "llm"] and cfg.d1_where == "hosted"
    (tmp_path / "config.toml").write_text('decide_off = ["rules"]\n')
    with pytest.raises(SystemExit):
        config.load()


def test_d1_asks_local_or_hosted_in_the_chosen_order():
    import asyncio
    import httpx
    from tv_agent.decide.d1 import D1, HOSTED

    class Http:
        def __init__(self, down=()):
            self.calls, self.down = [], down

        async def post(self, url, json=None, timeout=None, headers=None):
            where = "hosted" if url == HOSTED else "local"
            self.calls.append(where)
            if where in self.down:
                raise httpx.ConnectError(f"{where} down")
            return httpx.Response(200, request=httpx.Request("POST", url), json={"answers": {"q": {
                "choice": "pause", "confidence": 0.9, "probabilities": {"pause": 0.95, "none": 0.05}}}})

    def ask(where, key="k", down=()):
        http = Http(down)
        d1 = D1(http, "http://127.0.0.1:8091", hosted_key=key, where=where)
        asyncio.run(d1.choice("x", "?", {"pause": "p", "none": "n"}))
        return http.calls, d1.last_source

    assert ask("hosted-first") == (["hosted"], "hosted")
    assert ask("hosted-first", down=("hosted",)) == (["hosted", "local"], "local")
    assert ask("local-first", down=("local",)) == (["local", "hosted"], "hosted")
    assert ask("local") == (["local"], "local")
    with pytest.raises(httpx.TransportError):                      # hosted only, no key: nothing to ask
        ask("hosted", key=None)


def test_llm_requests_adapt_to_what_a_model_takes():
    import asyncio
    import httpx
    from tv_agent.decide.llm import Llm

    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((body.get("temperature"), (body.get("reasoning") or {}).get("effort")))
        if "temperature" in body:
            return httpx.Response(404, json={"error": {"message": "No endpoints found that can handle the requested "
                                                                  "parameters."}})
        if (body.get("reasoning") or {}).get("effort") == "none":
            return httpx.Response(400, json={"error": {"message": "Reasoning is mandatory for this endpoint"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action": "pause"}'}}], "usage": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            llm = Llm(http, "k", "some/model", RULES, "none")
            first = await llm.ask("pause", {})
            again = await llm.ask("pause", {})
            return first, again

    first, again = asyncio.run(run())
    assert first == again == {"action": "pause"}
    assert sent == [(0, "none"), (None, "none"), (None, "minimal"), (None, "minimal")]    # learned, then remembered


def test_two_commands_run_one_after_the_other():
    import asyncio
    import types
    from tv_agent.agent import Agent, Outcome

    done = []

    async def execute(a):
        done.append(a.type)
        return Outcome(True, a.type)

    async def until_playing():
        done.append("wait")

    a = types.SimpleNamespace(execute=execute, _until_playing=until_playing)
    seq = A.Sequence(steps=[A.PlayTitle(title="severance", episode=2), A.Transport(op="seek_to", percent=50)])
    out = asyncio.run(Agent._sequence(a, seq))
    assert done == ["play_title", "wait", "transport"] and out.ok and out.say == "play_title · transport"


def test_subtitles_switch_over_from_what_kodi_shows():
    import asyncio
    import types
    from tv_agent.agent import Agent

    set_to = []

    async def player():
        return 1

    async def tracks(pid):
        return {"subtitleenabled": True, "subtitles": [], "audiostreams": []}

    async def set_subtitle(pid, index):
        set_to.append(index)

    kodi = types.SimpleNamespace(tracks=tracks, set_subtitle=set_subtitle)
    a = types.SimpleNamespace(_player_id=player, _ctl=lambda pid: kodi)
    out = asyncio.run(Agent._tracks(a, A.Tracks(subtitles="toggle")))
    assert set_to == [None] and out.say == "Subtitles off"


def test_a_hosted_decision_model_can_be_any_openrouter_one():
    import asyncio
    import httpx
    from tv_agent.decide.d1 import D1, HOSTED, OPENROUTER

    sent = []

    class Http:
        async def post(self, url, json=None, timeout=None, headers=None):
            sent.append((url, json.get("model"), headers["Authorization"]))
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "answers": {"q": {"choice": "pause", "confidence": 0.9, "probabilities": {"pause": 0.95, "none": 0.05}}},
                "usage": {"input_tokens": 50, "cost": 1e-6}})

    def ask(model, liquid="lk", openrouter="ok"):
        d1 = D1(Http(), "http://127.0.0.1:8091", liquid, model, "hosted", openrouter)
        asyncio.run(d1.choice("x", "?", {"pause": "p", "none": "n"}))
        return d1

    assert ask("d1-ultra") and sent[-1] == (HOSTED, "d1-ultra", "Bearer lk")             # Liquid's own API
    d1 = ask("typesafe/jev-1.14")
    assert sent[-1] == (OPENROUTER, "typesafe/jev-1.14", "Bearer ok") and d1.last_usage["cost"] == 1e-6
    assert D1(Http(), "u", "lk", "cloudflare/clef", "hosted", None).sources() == ()      # no OpenRouter key


@pytest.mark.parametrize("text,expect", [
    ("start it", None), ("go to the movies", None),               # not apps that merely sound alike
    ("ok tv pon pausa", A.Transport(op="pause")),                 # fillers go before the fixes that need ^
    ("subtitles off please", A.Tracks(subtitles="off")),
    ("skip the intro", A.Skip(what="intro")), ("skip ahead", A.Transport(op="forward")),
    ("go back an episode", A.NextEpisode(offset=-1)),
    ("go to sleep in 20 minutes", A.Timer(minutes=20, then="off")),
    ("continuous", None), ("continue the movie", A.Transport(op="resume")),
    ("put on youtube", A.OpenApp(app="tizentube")), ("pon twitch", A.OpenApp(app="twitch")),
    ("pon la temporada dos de the bear", A.PlayTitle(title="the bear", season=2, kind="series")),
    ("pon el capítulo cinco", A.PlayTitle(episode=5, kind="series")),
    ("pon el capítulo siguiente", A.NextEpisode(offset=1)), ("pon el anterior", A.NextEpisode(offset=-1)),
    ("pon el audio en inglés", A.Tracks(audio="eng")),
    ("apaga la tele en una hora y media", A.Timer(minutes=90, then="off")),
    ("volume up to 40", A.Volume(op="set", amount=40)), ("turn it up to 30", A.Volume(op="set", amount=30)),
    ("increase the volume", A.Volume(op="up")), ("too loud", A.Volume(op="down")),
    ("sube un poco el volumen", A.Volume(op="up", amount=3)), ("volumen a tope", A.Volume(op="set", amount=100)),
    ("go back 10", A.Transport(op="back", seconds=10)), ("fast forward 90", A.Transport(op="forward", seconds=90)),
    ("pon el segundo", A.Pick(number=2)), ("el número tres", A.Pick(number=3)), ("pick two", A.Pick(number=2)),
    ("now you see me", None), ("right play the bear", A.PlayTitle(title="the bear")),   # titles keep their first word
    ("vuelve a empezar", A.Transport(op="seek_to", seconds=0)),
    ("pon a ibai en twitch", A.Twitch(channel="ibai")),
    ("set sleep timer 30", A.Timer(minutes=30, then="off")), ("cancel timer", A.Timer(then="stop", cancel=True)),
    ("abre ajustes", A.OpenApp(app="settings")), ("youtube music", A.OpenApp(app="youtube_music")),
    ("pon rtve", A.OpenApp(app="rtve")), ("play tv", A.PlayTitle(title="tv")),          # "tv" isn't RTVE
])
def test_rules_review_2026_10(text, expect):
    assert rule_action(text) == expect


def test_saving_unchanged_rules_keeps_following_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "USER_FILE", tmp_path / "rules.json")
    assert R.save(R.defaults_raw()).source == "defaults" and not R.USER_FILE.exists()
    changed = R.defaults_raw()
    changed["phrases"][0]["say"].append("hold it right there")
    R.save(changed)
    assert R.USER_FILE.exists()


def test_no_model_answering_plays_a_title_only_if_found_exactly():
    import asyncio
    r = _router()
    r.rules.fallback_intent = "play_title"
    d = asyncio.run(r.decide("the bear"))
    assert d.action.type == "play_title" and d.action.guess and d.action.title == "the bear"
    assert asyncio.run(r.decide("play the bear")).action.guess is False      # a rule understood it


def _bare_service(monkeypatch, tmp_path):
    """A Service without __init__ (no TV, no models): just what transcribe and local_stt use."""
    import asyncio
    import types
    from tv_agent import server, stt
    svc = server.Service.__new__(server.Service)
    svc.cfg = types.SimpleNamespace(stt_where="hosted", stt_model="a", stt_hosted_model="x/y", keep_recordings=True,
                                    log_transcripts=True, secret=lambda k: "key", stt_language="native",
                                    native_language="spa", stt_vocabulary=["Stremio"], stt_vocabulary_library=False,
                                    twitch_favorites=[])
    svc._library_names, svc._library_names_at, svc._other_names = {}, 0.0, {}
    svc.agent = types.SimpleNamespace(http=None, state=types.SimpleNamespace(
        timings=types.SimpleNamespace(add=lambda *a: None), now_playing=None))
    svc.stt, svc._stt_name, svc._stt_gen, svc._stt_loading = None, "", 0, asyncio.Lock()
    monkeypatch.setattr(stt, "RECORDINGS", tmp_path / "recordings")
    return svc, server, stt


def test_a_model_changed_or_unloaded_while_loading_isnt_kept(monkeypatch, tmp_path):
    import asyncio
    svc, server, _ = _bare_service(monkeypatch, tmp_path)

    class FakeStt:
        def __init__(self, path):
            self.path = path
            if path.endswith("a"):
                svc.cfg.stt_model = "b"          # the panel picked another model meanwhile
    monkeypatch.setattr(server, "Stt", FakeStt)
    monkeypatch.setattr(server, "stt_model_dir", lambda name: f"/models/{name}")
    got = asyncio.run(svc.local_stt())
    assert got.path == "/models/b" and svc._stt_name == "b"

    class SlowStt(FakeStt):
        def __init__(self, path):
            self.path = path
            svc.unload_stt()                     # "unload" arrived while it loaded
    monkeypatch.setattr(server, "Stt", SlowStt)
    svc.unload_stt()
    assert asyncio.run(svc.local_stt()) is not None and svc.stt is None


def test_transcribing_again_never_falls_back_quietly(monkeypatch, tmp_path):
    import asyncio
    import httpx
    svc, server, stt = _bare_service(monkeypatch, tmp_path)

    async def down(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(stt, "hosted_transcribe", down)
    svc.stt, svc._stt_name = types_stt("local words"), "a"
    text, info = asyncio.run(svc.transcribe(b"\0" * 32000, where="hosted", fallback=False))
    assert text == "" and info["stt"] == "x/y" and "ConnectError" in info["stt_error"]
    text, info = asyncio.run(svc.transcribe(b"\0" * 32000, where="hosted"))        # a voice command: the backup
    assert text == "local words" and info["stt"] == "local" and info["stt_error"]


def types_stt(text):
    import types
    return types.SimpleNamespace(transcribe=lambda pcm: text)


def test_a_recording_that_cant_be_saved_loses_only_the_recording(monkeypatch, tmp_path):
    import asyncio
    svc, server, stt = _bare_service(monkeypatch, tmp_path)
    name = asyncio.run(svc.keep_recording(b"\1\0" * 16000))
    assert name and oct(stt.recording(name).stat().st_mode & 0o777) == "0o600"

    svc.cfg.log_transcripts = False                 # no history: nothing would point at it, so none kept
    assert asyncio.run(svc.keep_recording(b"\1\0" * 16000)) is None
    svc.cfg.log_transcripts = True

    def full(pcm):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(stt, "save_recording", full)
    assert asyncio.run(svc.keep_recording(b"\1\0" * 16000)) is None
    assert stt.delete_recordings() == 1 and not stt.recordings()         # the temporary folder's one


def test_this_laptop_first_asks_the_hosted_model_when_it_hears_nothing(monkeypatch, tmp_path):
    import asyncio
    svc, server, stt = _bare_service(monkeypatch, tmp_path)
    svc.cfg.stt_where = "local-first"
    svc.stt, svc._stt_name = types_stt(""), "a"            # the laptop's model heard nothing

    async def hosted(*a, **k):
        return "pause", 0.0001
    monkeypatch.setattr(stt, "hosted_transcribe", hosted)
    text, info = asyncio.run(svc.transcribe(b"\0" * 32000))
    assert text == "pause" and info["stt"] == "x/y" and info["stt_error"] == "this laptop heard nothing"
    text, info = asyncio.run(svc.transcribe(b"\0" * 8000))   # a quarter second: nothing to hear, no hosted call
    assert text == "" and info["stt"] == "local"
    svc.stt = types_stt("volume up")
    text, info = asyncio.run(svc.transcribe(b"\0" * 32000))
    assert text == "volume up" and info["stt"] == "local" and "stt_error" not in info


@pytest.mark.parametrize("text,recommends", [
    ("recommend something like dark", True), ("what should we watch tonight", True), ("play a comedy", True),
    ("put on a horror movie from the 80s", True), ("something like breaking bad", True), ("any suggestions", True),
    ("recomiéndame algo de miedo", True), ("¿qué vemos hoy?", True), ("algo como la casa de papel", True),
    ("pon una peli de miedo", True),
    ("play a series of unfortunate events", False), ("play a quiet place", False), ("play the office", False),
    ("pon la casa de papel", False), ("play something", False), ("¿qué estoy viendo?", False),
])
def test_asking_for_suggestions_isnt_a_title(text, recommends):
    got = rule_action(text)
    assert (got is not None and got.type == "recommend") == recommends, got


def test_the_llm_can_ask_for_suggestions():
    from tv_agent.decide.llm import to_action
    assert to_action({"action": "recommend", "query": "algo de miedo"}, "x", [], RULES) == A.Recommend(query="algo de miedo")
    assert to_action({"action": "recommend", "query": None}, "something light", [], RULES) == \
        A.Recommend(query="something light")


def test_suggestions_are_real_titles_that_fit_and_play_from_the_list(monkeypatch):
    import asyncio
    import types
    from tv_agent import agent as agent_mod
    from tv_agent.agent import Agent, Outcome
    from tv_agent.stremio import cinemeta as C
    catalog = {"1899": C.Meta("tt1", "series", "1899", "2022", 0), "Coherence": C.Meta("tt2", "movie", "Coherence",
                                                                                         "2013", 0),
               "Long Film": C.Meta("tt3", "movie", "Long Film", "2010", 0),
               "Severance": C.Meta("tt4", "series", "Severance", "2022", 0)}
    runtimes = {"tt1": "", "tt2": "89 min", "tt3": "2h 10min", "tt4": ""}

    async def search(http, title, addons):
        return [catalog[title]] if title in catalog else []

    async def meta(http, kind, imdb):
        return {"runtime": runtimes[imdb], "imdbRating": "7.5", "poster": "p"}
    monkeypatch.setattr(agent_mod.title_search, "search", search)
    monkeypatch.setattr(agent_mod.cinemeta, "meta", meta)

    class Llm:
        seen = []

        async def recommend(self, request, kind, taste, seen, language):
            self.seen.append(list(seen))
            return [{"title": t, "year": None, "kind": k, "why": "because"} for t, k in
                    (("1899", "series"), ("Coherence", "movie"), ("Long Film", "movie"), ("Severance", "series"),
                     ("Made Up", "movie"))], 100

    class Library:
        async def items(self):
            return [{"_id": "tt4", "type": "series", "name": "Severance", "state": {"video_id": "tt4:1:1"}}]

        async def get(self, imdb):
            return None

    async def addons():
        return []
    played = []

    async def show(shown):
        pass

    async def play_found(a, imdb, kind, name, playing_video=None):
        played.append((imdb, kind))
        return Outcome(True, "ok")
    me = types.SimpleNamespace(llm=Llm(), library=Library(), http=None, _shown=None, addons=addons, _show_on_tv=show,
                               _play_found=play_found)
    me._recommended = lambda *a: Agent._recommended(me, *a)
    me.play_item = lambda it: Agent.play_item(me, it)
    out = asyncio.run(Agent._recommend(me, A.Recommend(query="something like dark")))
    # not the 130-minute film (over the 100 asked), not the series you're watching, not one the search doesn't know
    assert out.ok and [i.name for i in me._shown.items] == ["1899", "Coherence"]
    assert me.llm.seen[0] == ["Severance"]
    asyncio.run(Agent._pick(me, A.Pick(number=1)))
    assert played == [("tt1", "series")]                # a series from the list: where you were, or its first episode



def test_the_hosted_model_gets_a_language_hint_and_the_vocabulary(monkeypatch, tmp_path):
    import asyncio
    svc, server, stt = _bare_service(monkeypatch, tmp_path)
    sent = {}

    async def hosted(http, key, model, pcm, timeout=6.0, language="", vocabulary=None):
        sent.update(language=language, vocabulary=vocabulary)
        return "Abre Stremio", 0.0002
    monkeypatch.setattr(stt, "hosted_transcribe", hosted)
    svc.cfg.twitch_favorites = ["Ibai"]
    svc.cfg.stt_vocabulary_library = True
    svc._library_names, svc._library_names_at = {"recent": ["The Bear", "stremio"]}, 1e12   # fresh: no refresh
    asyncio.run(svc.transcribe(b"\0" * 32000))
    assert sent == {"language": "es", "vocabulary": ["Stremio", "The Bear", "Ibai"]}   # native Spanish; no repeats
    svc.cfg.stt_language = ""
    asyncio.run(svc.transcribe(b"\0" * 32000))
    assert sent["language"] == ""                    # the model guesses


def test_the_vocabulary_sends_the_likeliest_names_first_in_the_hints_language(monkeypatch, tmp_path):
    """What's playing and Continue Watching, then yours, what you watched (newest first), Twitch and the rest of
    the library (newest added first). Stremio's names with a Spanish hint, Cinemeta's with an English one, both
    without. Only the first 100 go (AssemblyAI refuses more), so the least likely are the ones left out."""
    import asyncio
    import types
    svc, server, stt = _bare_service(monkeypatch, tmp_path)
    svc.cfg.stt_vocabulary_library, svc.cfg.twitch_favorites = True, ["Ibai"]
    svc.agent.state.now_playing = types.SimpleNamespace(name="Severance")
    item = lambda i, name, last="", offset=0, ctime="": {"_id": f"tt{i}", "type": "movie", "name": name,
                                                       "_ctime": ctime,
                                                       "state": {"lastWatched": last, "timeOffset": offset}}
    items = [item(1, "Old Film", last="2026-01-01"), item(2, "El señor de los anillos", last="2026-10-01"),
             item(3, "Half Watched", last="2026-09-01", offset=600), item(4, "Never Watched", ctime="2026-05-01"),
             item(5, "Added Lately", ctime="2026-10-20"), item(6, "Severance", last="2026-10-23", offset=60)]

    async def library_items():
        return items
    svc.agent.library = types.SimpleNamespace(items=library_items)
    asked = []

    async def meta(http, kind, imdb_id):
        asked.append(imdb_id)
        return {"name": "The Lord of the Rings" if imdb_id == "tt2" else
                next(i["name"] for i in items if i["_id"] == imdb_id)}
    monkeypatch.setattr(server.cinemeta, "meta", meta)
    asyncio.run(svc._refresh_library_names())
    svc._library_names_at = 1e12
    assert asked == []                                                      # a Spanish hint: Stremio's names only
    assert svc.stt_vocabulary() == ["Severance", "Half Watched", "Stremio", "El señor de los anillos", "Old Film",
                                    "Ibai", "Added Lately", "Never Watched"]
    asyncio.run(svc._refresh_library_names(other_names=True))
    asyncio.run(svc._refresh_library_names(other_names=True))
    assert sorted(asked) == ["tt1", "tt2", "tt3", "tt4", "tt5", "tt6"]       # each title's name asked once
    svc.cfg.stt_language = "en"
    assert svc.stt_vocabulary()[3] == "The Lord of the Rings"
    svc.cfg.stt_language = ""
    assert svc.stt_vocabulary()[3:5] == ["El señor de los anillos", "The Lord of the Rings"]
    svc._library_names = {"rest": [f"Film {i}" for i in range(150)]}
    assert len(svc.stt_vocabulary()) == stt.VOCABULARY_MAX == 100


def test_another_script_from_this_laptop_asks_the_hosted_model(monkeypatch, tmp_path):
    import asyncio
    svc, server, stt = _bare_service(monkeypatch, tmp_path)
    svc.cfg.stt_where = "local-first"
    svc.stt, svc._stt_name = types_stt("Пауса."), "a"

    async def hosted(*a, **k):
        return "Pausa.", 0.0002
    monkeypatch.setattr(stt, "hosted_transcribe", hosted)
    text, info = asyncio.run(svc.transcribe(b"\0" * 32000))
    assert text == "Pausa." and info["stt_error"] == "this laptop heard another script"


def test_hosted_requests_carry_the_hint_and_assemblyai_the_keyterms():
    import asyncio
    from tv_agent import stt

    class Http:
        async def post(self, url, timeout, headers, json):
            self.body = json

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"text": "Abre Stremio.", "usage": {"cost": 0.0002}}
            return R()
    http = Http()
    asyncio.run(stt.hosted_transcribe(http, "k", "assemblyai/universal-3-6-pro", b"\0" * 3200, language="es",
                                      vocabulary=["Stremio"]))
    assert http.body["language"] == "es"
    assert http.body["provider"] == {"options": {"assemblyai": {"keyterms_prompt": ["Stremio"]}}}
    asyncio.run(stt.hosted_transcribe(http, "k", "mistralai/voxtral-mini-transcribe-3-0", b"\0" * 3200,
                                      vocabulary=["Stremio"]))
    assert "provider" not in http.body and "language" not in http.body


def test_play_and_pause_go_to_the_app_in_front_not_to_kodi_behind_it():
    import asyncio
    import types
    from tv_agent.agent import Agent
    from tv_agent.tv import media
    tasks = ("  * Task{6a #1270 type=standard A=10200:com.boujuan.tvagent U=0 visible=true}\n"
             "  * Task{6b #1254 type=standard A=10099:io.gh.reisxd.tizentube.cobalt U=0 visible=true}\n"
             "  * Task{9b #1263 type=standard A=10135:org.xbmc.kodi:2055 U=0 visible=false}\n")
    sent = []

    async def tv(fn, *args):
        if fn is media.dispatch:
            sent.append(args[0])
            return None
        return fn(types.SimpleNamespace(run=lambda argv, timeout=10: tasks))

    async def no_kodi():
        return None

    async def started():
        raise AssertionError("Kodi was brought up over the app in front")
    me = types.SimpleNamespace(tv=tv, _kodi_player=no_kodi, kodi=types.SimpleNamespace(start=started),
                               state=types.SimpleNamespace(now_playing=None), tvplayer=types.SimpleNamespace(now=lambda: None))
    me.front_app = lambda: Agent.front_app(me)
    assert asyncio.run(me.front_app()) == "io.gh.reisxd.tizentube.cobalt"      # under the TV app's pill
    out = asyncio.run(Agent._transport(me, A.Transport(op="resume")))
    assert out.ok and sent == ["resume"]
    assert not asyncio.run(Agent._transport(me, A.Transport(op="seek_to", seconds=60))).ok


def test_an_episode_by_its_name_or_by_what_happens_in_it():
    import asyncio
    import types
    from tv_agent.agent import Agent
    eps = [{"id": "tt1:2:13", "season": 2, "episode": 13, "name": "Celebrity Pharmacology", "overview": "A play."},
           {"id": "tt1:2:14", "season": 2, "episode": 14, "name": "Advanced Dungeons & Dragons",
            "overview": "Abed invites Fat Neil to play Dungeons & Dragons."},
           {"id": "tt1:6:25", "season": 6, "episode": 25, "name": "Who Shot Mr. Burns? (1)",
            "overview": "When Burns winds up shot, everyone is a suspect."}]

    class Llm:
        asked = []

        async def pick_episode(self, series, wanted, candidates):
            self.asked.append([c[0] for c in candidates])
            return "tt1:6:25"
    me = types.SimpleNamespace(llm=None)
    # by name: matched here, no model needed ("and" for "&")
    assert asyncio.run(Agent._find_episode(me, "Community", eps, "Advanced Dungeons and Dragons"))["id"] == "tt1:2:14"
    assert asyncio.run(Agent._find_episode(me, "Community", eps, "Mr. Burns gets shot")) is None   # no LLM to ask
    me.llm = Llm()
    assert asyncio.run(Agent._find_episode(me, "The Simpsons", eps, "Mr. Burns gets shot"))["id"] == "tt1:6:25"
    assert me.llm.asked[0][0] == "tt1:6:25"            # the one whose summary shares the words comes first


@pytest.mark.parametrize("text", [
    "Can you play The Simpsons, um, that episode where Mr. Burns is killed?",
    "Can you play the scenes um in that episode where Mr. Burns is killed?",
    "pon el capítulo de los simpson en el que homer va al espacio",
])
def test_a_described_episode_is_for_the_llm_not_the_title_search(text):
    got = rule_action(text)
    assert got is None or got.type != "play_title", got


def test_the_llm_names_an_episode():
    from tv_agent.decide.llm import to_action
    got = to_action({"action": "play_title", "title": "Community", "episode_name": "Advanced Dungeons and Dragons"},
                    "abre community capítulo advanced dungeons and dragons", [], RULES)
    assert got == A.PlayTitle(title="Community", episode_name="Advanced Dungeons and Dragons")


@pytest.mark.parametrize("text,what", [
    ("go to the part where they kiss", "they kiss"),
    ("skip to the scene where Walter meets Gus", "walter meets gus"),
    ("Vete al minuto en el cual Mr. Burns le dispara.", "mr. burns le dispara"),
    ("ve a la parte en la que se besan", "se besan"),
    ("go to minute 12", None),                                    # a time, not a moment
])
def test_a_moment_described_is_searched_for(text, what):
    got = rule_action(text)
    if what is None:
        assert got == A.Transport(op="seek_to", seconds=720)
    else:
        assert got == A.FindScene(what=what), got


def test_subtitle_cues_are_read_with_their_times():
    from tv_agent.stremio import subtitles as S
    srt = "1\n00:00:01,500 --> 00:00:03,000\n<i>Hello</i> there\n\n2\n00:21:05,040 --> 00:21:07,000\n{\\an8}Who shot me?\n"
    assert S.cues(srt) == [(1500, "Hello there"), (1265040, "Who shot me?")]
    assert S.cues("WEBVTT\n\n01:02:03.250 --> 01:02:04.000\nLong &amp; form\n") == [(3723250, "Long & form")]


def test_the_scene_search_jumps_to_where_the_llm_found_the_moment(monkeypatch):
    import asyncio
    import types
    from tv_agent import agent as agent_mod
    from tv_agent.agent import Agent, Outcome
    from tv_agent.stremio import subtitles as S
    lines = "".join(f"{n}\n00:{n // 60:02d}:{n % 60:02d},000 --> 00:{n // 60:02d}:{n % 60:02d},900\nline {n}\n\n"
                    for n in range(1, 40))

    async def fetch(*a, **k):
        return [S.Subtitle("u", "eng", "x", "SubSense")]

    async def download(http, url):
        return lines
    monkeypatch.setattr(agent_mod.subtitles, "fetch", fetch)
    monkeypatch.setattr(agent_mod.subtitles, "download", download)
    seen, jumped = {}, []

    class Llm:
        async def find_moment(self, title, wanted, text):
            seen.update(title=title, wanted=wanted, first=text.splitlines()[0])
            return 30, "line 30"

    async def addons():
        return []

    async def transport(a):
        jumped.append(a)
        return Outcome(True, "To 0:28")
    from tv_agent.episode_check import Verdict

    async def no_subs(video, lang="eng"):
        return []                               # nothing kept from a check: the addons are asked as before
    np = NowPlaying("tt1", "series", "tt1:6:25", "The Simpsons")
    me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np), llm=Llm(), http=None, addons=addons,
                               cfg=types.SimpleNamespace(subtitle_languages=["eng"]), _transport=transport,
                               checks=types.SimpleNamespace(get=lambda *a: None), episode_subs=no_subs)
    out = asyncio.run(Agent._find_scene(me, A.FindScene(what="Mr. Burns gets shot")))
    assert out.ok and jumped == [A.Transport(op="seek_to", seconds=28)]      # a moment before it
    assert seen == {"title": "The Simpsons S6E25", "wanted": "Mr. Burns gets shot", "first": "0:01 line 1"}

    async def kept(video, lang="eng"):          # the episode's subtitles from checking the file, 3 s later than it
        return S.cues(lines)
    me.episode_subs = kept
    me.checks = types.SimpleNamespace(get=lambda *a: Verdict("tt1:6:25", True, shift=3.0))
    jumped.clear()
    asyncio.run(Agent._find_scene(me, A.FindScene(what="Mr. Burns gets shot")))
    assert jumped == [A.Transport(op="seek_to", seconds=25)]
    me.state.now_playing = None
    assert asyncio.run(Agent._find_scene(me, A.FindScene(what="x"))).say == "Nothing of yours is playing to search"


def test_a_stuck_kodi_is_restarted_and_the_file_opened_again_once():
    """Kodi's player hung in the TV's Dolby Vision decoder: it took files without playing them (its log stopped
    mid-line). A stop at 0:00 right after opening, with nothing in Kodi's log since, restarts Kodi and replays."""
    import asyncio
    import time
    import types
    from tv_agent.agent import Agent

    async def run(log_quiet_s: float, opened_ago: float = 10, again=True):
        a = Agent.__new__(Agent)
        did, said = [], []
        np = NowPlaying("tt1439629", "series", "tt1439629:6:8", "Community", "kodi", "https://x/1")

        async def tv(fn, *args):
            adb = types.SimpleNamespace(run=lambda argv: did.append(argv) or "1700000100 %d" % (1700000100 - log_quiet_s))
            return fn(adb, *args)

        async def notify(text):
            said.append(text)

        async def start():
            did.append("kodi start")

        async def replay():
            did.append("replay")
            return types.SimpleNamespace(say="Playing Community · S6E8")

        async def nothing(*args):
            return None
        a.tv, a.notify, a._home_unless_playing = tv, notify, nothing
        a.kodi = types.SimpleNamespace(start=start)
        a._again = (np, time.monotonic() - opened_ago, replay if again else None)
        return await Agent._restart_stuck_kodi(a, np), did, said

    restarted, did, said = asyncio.run(run(log_quiet_s=60))         # nothing written since well before the open
    assert restarted and ["am", "force-stop", "org.xbmc.kodi"] in did and did[-2:] == ["kodi start", "replay"]
    assert said == ["Kodi was stuck: restarting it", "Playing Community · S6E8"]
    restarted, did, _ = asyncio.run(run(log_quiet_s=2))             # Kodi wrote about the file: a real failure
    assert not restarted and "replay" not in did
    assert not asyncio.run(run(log_quiet_s=60, again=False))[0]     # the retry itself isn't retried
    assert not asyncio.run(run(log_quiet_s=600, opened_ago=300))[0]     # a stop long after the open: not this


def test_a_file_opens_in_kodi_only_after_the_playing_one_has_let_go(monkeypatch):
    """Opened over a playing file, this TV's decoder isn't free yet ("InstanceGuard locked"): no picture, seeks
    running to the end. Kodi is stopped first, and the next file opens once its player is gone."""
    import asyncio
    import types
    from tv_agent import agent as agent_mod
    from tv_agent.agent import Agent
    from tv_agent.stremio import streams as S

    async def run():
        a = Agent.__new__(Agent)
        did, players = [], [1, 1, None]           # Kodi's player: there, still there, gone

        async def nothing(*args, **kw):
            return None

        async def ready():
            return True

        async def found(kind, video, meta):
            return [S.Stream("AIOStreams", "https://x/1", "n", "", "f.mkv", 0, library=True)]

        async def tv(fn, *args):
            return ""

        async def player():
            return players.pop(0) if players else None

        async def stop(pid):
            did.append(("stop", pid))

        async def open_(url, resume_ms=0):
            did.append(("open", url))

        async def position(pid):
            return 168_000, 1_277_000, 1
        real_sleep = asyncio.sleep

        async def sleep(s):
            did.append(("wait", s))
            await real_sleep(0)
        monkeypatch.setattr(agent_mod.asyncio, "sleep", sleep)
        a.account, a.dry_run, a.notify, a.tv, a.state = object(), False, nothing, tv, agent_mod.State()
        a.cfg = types.SimpleNamespace(player="kodi", probe_streams=False, auto_skip=False)
        a._start_kodi, a.streams_for, a._kodi_player = ready, found, player
        a.track_plan = lambda meta: languages.TrackPlan("eng", None)
        a.kodi = types.SimpleNamespace(prefer=nothing, allow_hdr=nothing, stop=stop, open=open_, position=position)
        a._progress, a._again, a._starting = None, None, asyncio.Lock()
        a._watch_picture = nothing
        a._replace_session = lambda video, resume_ms, exact=False: resume_ms
        a._set_tracks, a._started, a.timings = nothing, lambda *args: None, None
        a.state.timings = types.SimpleNamespace(add=lambda *args: None)
        out = await Agent.play(a, "tt1", "movie", "X", "tt1", "X", {})
        return out, did

    out, did = asyncio.run(run())
    assert out.ok and did[0] == ("stop", 1)
    assert did.index(("wait", agent_mod.KODI_FREE)) < did.index(("open", "https://x/1"))


def test_dolby_vision_and_hdr10_plus_are_told_apart_by_tags_or_name():
    from tv_agent.stremio import streams as S
    dv = S.Stream("AIOStreams", "u", "n", "", "Community.S02E15.2160p.NF.WEB-DL.DDP5.1.HDR.DV.HEVC-X.mkv", 0)
    assert dv.dolby_vision and not dv.hdr10_plus
    both = S.Stream("AIOStreams", "u", "n", "", "f.mkv", 0, visual=("DV", "HDR10+"))
    assert both.dolby_vision and both.hdr10_plus
    plain = S.Stream("AIOStreams", "u", "n", "", "Breaking.Bad.S01E07.2160p.NF.WEB-DL.DTS-HD.MA.5.1.HEVC-CRFW.mkv", 0)
    assert not plain.dolby_vision and not S.Stream("A", "u", "n", "", "Movie.DVDRip.x264.mkv", 0).dolby_vision


def test_jumps_in_dolby_vision_or_behind_another_screen_reopen_the_file():
    """This TV's Dolby Vision decoder can fail a seek's flush (black picture, or time running to the end), and a
    Kodi behind the home screen has no window for its decoder: those jumps reopen the file at the time instead."""
    import asyncio
    import types
    from tv_agent.actions import Transport
    from tv_agent.agent import Agent

    async def run(dolby_vision: bool, front: str, op: Transport):
        a = Agent.__new__(Agent)
        did = []
        np = NowPlaying("tt1", "series", "tt1:2:14", "Community", "kodi", "https://x/1", dolby_vision=dolby_vision)

        async def front_app():
            return front

        async def player():
            return 1

        async def seek_to(pid, ms):
            did.append(("seek", ms))

        async def seek_by(pid, s):
            did.append(("seek by", s))

        async def position(pid):
            return 600_000, 1_277_000, 1

        async def reopen(ms):
            did.append(("reopen", ms))
            return types.SimpleNamespace(ok=True, say="Playing")
        a.front_app, a._kodi_player = front_app, player
        a.kodi = types.SimpleNamespace(seek_to=seek_to, seek_by=seek_by, position=position)
        a.tvplayer = types.SimpleNamespace(now=lambda: None)
        a.state = types.SimpleNamespace(now_playing=np)
        a._reopen = (np, reopen)
        out = await Agent._transport(a, op)
        return out, did

    out, did = asyncio.run(run(True, "org.xbmc.kodi", Transport(op="seek_to", seconds=168)))
    assert did == [("reopen", 168_000)] and out.say == "To 2:48"
    assert asyncio.run(run(True, "org.xbmc.kodi", Transport(op="back")))[1] == [("reopen", 590_000)]
    assert asyncio.run(run(True, "org.xbmc.kodi", Transport(op="seek_to", percent=50)))[1] == [("reopen", 638_500)]
    assert asyncio.run(run(False, "org.xbmc.kodi", Transport(op="seek_to", seconds=168)))[1] == [("seek", 168_000)]
    assert asyncio.run(run(False, "home", Transport(op="forward", seconds=60)))[1] == [("reopen", 660_000)]


def test_a_lost_picture_restarts_kodi_where_the_file_was(monkeypatch):
    import asyncio
    import types
    from tv_agent import agent as agent_mod
    from tv_agent.agent import Agent

    async def run(tail: str):
        a = Agent.__new__(Agent)
        did = []
        np = NowPlaying("tt1", "series", "tt1:2:14", "Community", "kodi", "https://x/1")
        polls = [1, 1]

        async def player():
            return polls.pop(0) if polls else None

        async def tv(fn, *args):
            return fn(types.SimpleNamespace(run=lambda argv, timeout=None: tail))

        async def position(pid):
            return 206_000, 1_277_000, 1

        async def recover(np_, at, say):
            did.append((at, say))
            return True

        async def sleep(s):
            return None
        monkeypatch.setattr(agent_mod.asyncio, "sleep", sleep)
        a.state = types.SimpleNamespace(now_playing=np)
        a._kodi_player, a.tv, a._recover_kodi = player, tv, recover
        a.kodi = types.SimpleNamespace(position=position)
        await Agent._watch_picture(a, np)
        return did

    failing = "\n".join(["2026-10-23 13:14:09.914 T:10605   error <general>: CDVDVideoCodecAndroidMediaCodec:"
                         "GetOutputPicture dequeueOutputBuffer failed"] * 30)
    assert asyncio.run(run(failing)) == [(206_000, "The picture was lost: restarting Kodi")]
    assert asyncio.run(run("2026-10-23 13:08:25 info <general>: Using codec: c2.mtk.avc.decoder\n" * 30)) == []


def test_the_tvs_own_apps_and_players_get_cpu_and_memory_rows():
    from tv_agent.tv import status as S
    stat = ["cpu  1000 0 500 8000 100 0 0 0 0 0",
            "15395 (com.boujuan.tva) S 1 2 3 4 5 6 7 8 9 10 200 50 0 0",
            "cpu  1100 0 550 8300 100 0 0 0 0 0",
            "15395 (com.boujuan.tva) S 1 2 3 4 5 6 7 8 9 10 240 60 0 0"]
    apps = ["APP 15395 com.boujuan.tvagent.player 132948", "APP 15435 com.boujuan.tvagent 109312", "APP 9 other.app 5"]
    got = S.apps(stat, apps, {k: S.PACKAGES[k] for k in S.WATCHED})
    assert [a["name"] for a in got] == ["TV Agent Player", "TV app"]       # in WATCHED's order; unknown ones left out
    assert got[0]["cpu"] == 11.1 and got[0]["rss_kb"] == 132948           # 50 of 450 ticks
    assert got[1]["cpu"] == 0.0                                            # no stat lines for it: idle
