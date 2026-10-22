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
    assert rule_action("go to stranger things season 2") is None   # left for d1 + title rules


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
