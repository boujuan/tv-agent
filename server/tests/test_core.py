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
    sub = S.parse("SubSense", {"id": "1", "url": "https://subs/1.srt", "lang": "eng", "label": "OpenSubtitles · [SRT] · x · HI"})
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
        self.subs, self.added, self.shown = list(subtitles), [], None

    async def tracks(self, pid):
        return {"audiostreams": [{"index": 0, "language": "eng"}], "currentaudiostream": {"index": 0},
                "subtitles": self.subs, "subtitleenabled": False}

    async def set_audio(self, pid, index):
        pass

    async def add_subtitle(self, pid, url):
        self.added.append(url)
        self.shown = url

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
        me = types.SimpleNamespace(kodi=kodi, _kodi_player=player)
        task = asyncio.create_task(asyncio.sleep(0, result=offered))
        return await Agent.apply_tracks(me, languages.TrackPlan("eng", langs), addon_subs=task)
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
    assert nothing_inside.shown == "https://s/eng" and note == "eng subs from SubSense"
