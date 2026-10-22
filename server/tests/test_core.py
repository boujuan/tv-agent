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
        me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np), _kodi_player=player, segments=segs,
                                   kodi=types.SimpleNamespace(position=position, seek_to=seek_to), notify=notify,
                                   cfg=types.SimpleNamespace(auto_skip=auto))
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
        a.kodi = types.SimpleNamespace(prefer=nothing)
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
    ("put youtube on", None),                                     # not a YouTube search for "put"
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

    a = types.SimpleNamespace(_kodi_player=player, kodi=types.SimpleNamespace(tracks=tracks, set_subtitle=set_subtitle))
    out = asyncio.run(Agent._tracks(a, A.Tracks(subtitles="toggle")))
    assert set_to == [None] and out.say == "Subtitles off"
