"""Languages: the plan for a title (settings, the original language, what was asked by voice), what each stream says
about its audio and subtitles, reading a file's tracks from its header, switching what plays to a copy that has the
language asked for, and "from now on" as settings. Nothing reaches the TV, an addon or a debrid link."""
import asyncio
import types

import httpx
import pytest
from fastapi import HTTPException

from tv_agent import actions as A
from tv_agent import agent as agent_mod
from tv_agent import config, languages, remote
from tv_agent.agent import Agent, Outcome
from tv_agent.decide import llm, router
from tv_agent.decide import rules as R
from tv_agent.state import NowPlaying
from tv_agent.stremio import streams as S
from tv_agent.tv import thumbs
from tv_agent.tv import tvplayer as T

RULES = R.build(R.defaults_raw(), "defaults")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")
    return tmp_path


# ---- the plan ----

def test_the_plan_follows_your_settings_the_original_language_and_what_was_asked():
    usa, spain, japan = {"country": "United States"}, {"country": "Spain"}, {"country": "Japan"}
    def plan(meta, audio="eng", subs="foreign", **kw):
        return languages.plan(meta, audio, ["eng", "spa"], "spa", subs, **kw)
    assert plan(usa).audio == "eng" and plan(usa).subs_for("en") == ["eng", "spa"]
    assert plan(spain, "eng").audio == "spa" and plan(spain).subs_for("spa") is None    # made in yours: no subtitles
    assert plan(usa, "spa").audio == "spa" and plan(usa, "spa").subs_for("es") is None  # a dub in yours: none either
    assert plan(usa, "spa").subs_for("eng") == ["eng", "spa"]          # the file had no Spanish: English with them
    assert plan(japan, "original").audio == "jpn"                       # the language it was made in, by country
    assert plan(usa, "original", original="kor").audio == "kor"         # the files' own original track wins
    assert plan({}, "original").audio is None                           # unknown: the file's own default
    assert plan(spain, asked="eng").audio == "eng"                      # asked for this title: over the native rule
    assert plan(usa, subs="always").subs_for("spa") == ["eng", "spa"]
    assert plan(usa, subs="off").subtitles is None and plan(usa, subs="off").subs_for("eng") is None
    assert languages.code("Latino") == "spa" and languages.code("es") == "spa" and languages.code("zh") == "chi"
    assert languages.code("Portuguese (Brazil)") == "por" and languages.code("klingon") is None


def test_settings_check_the_audio_and_when_subtitles_show(paths):
    config.CONFIG_FILE.write_text('audio_language = "original"\nsubtitles = "always"\n')
    cfg = config.load()
    assert cfg.audio_language == "original" and cfg.subtitles == "always"
    for line in ('audio_language = "english"', 'subtitles = "sometimes"'):
        config.CONFIG_FILE.write_text(line + "\n")
        with pytest.raises(SystemExit):
            config.load()
    assert config.Config().subtitles == "foreign"                       # as before: Spanish-made titles without


# ---- what streams say ----

def _aio(name, langs=(), subs=(), tracks=None, probe=False, resolution="1080p", score=100):
    pf = {"resolution": resolution, "quality": "WEB-DL", "languages": list(langs), "subtitles": list(subs)}
    if tracks is not None:
        pf["audioTracks"] = tracks
    if probe:
        pf["mediaInfoQuality"] = "probe"
    return S.parse("AIOStreams", {"name": name, "description": "", "url": f"https://debrid/{name}",
                                  "behaviorHints": {"filename": f"{name}.mkv"},
                                  "streamData": {"type": "debrid", "service": {"id": "premiumize", "cached": True},
                                                 "streamExpressionScore": score, "parsedFile": pf}}, 0)


def test_streams_say_their_audio_subtitles_and_original_language():
    dual = _aio("dual", tracks=[{"lang": "English", "original": True}, {"lang": "Spanish"}], subs=["English"],
                probe=True)
    english = _aio("english", langs=["English"], tracks=[{"lang": "English", "original": True}])
    latino = _aio("latino", langs=["Latino", "English"])
    multi = _aio("multi", langs=["Multi"])
    assert S.has_audio(dual, "spa") and dual.tracks_read and dual.original == "English"
    assert S.has_audio(english, "spa") is False and S.has_audio(multi, "spa") is None
    assert S.has_subtitles(dual, "eng") and S.has_subtitles(dual, "spa") is False
    assert S.has_subtitles(english, "eng") is None                      # the addon didn't say
    assert S.original_language([dual, english, latino]) == "eng"
    ranked = S.rank([english, multi, latino, dual], 2160, 0, 120, audio="spa")
    assert [s.name for s in ranked] == ["dual", "latino", "multi", "english"]     # Castilian, Latino, maybe, none
    assert [s.rank_key[1] for s in ranked] == [2, 1.5, 1, 0]
    assert [s.rank_key[1] for s in S.rank([english, dual], 2160, 0, 120, audio=None)] == [1, 1]   # no preference
    # other addons: flags and words in the filename
    torrentio = S.parse("Torrentio", {"name": "RD+ 1080p", "description": "Dune 🇪🇸 / 🇲🇽\n👤 12", "url": "https://x/1",
                                      "behaviorHints": {"filename": "Dune.2021.MULTi.1080p.mkv"}}, 0)
    french = S.parse("Torrentio", {"name": "RD+", "description": "", "url": "https://x/2",
                                   "behaviorHints": {"filename": "Dune.2021.FRENCH.1080p.mkv"}}, 1)
    assert S.has_audio(torrentio, "spa") and S.has_audio(torrentio, "eng") is None
    assert S.has_audio(french, "eng") is False and S.has_audio(french, "fre")


def _element(eid: int, data: bytes) -> bytes:
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big") + thumbs.size_vint(len(data)) + data


def _mkv() -> bytes:
    entry = lambda n, kind, *more: _element(0xAE, _element(0xD7, bytes([n])) + _element(0x83, bytes([kind])) +  # noqa: E731
                                            b"".join(more))
    tracks = _element(0x1654AE6B, entry(1, 1) + entry(2, 2, _element(0x22B59C, b"spa")) + entry(3, 2) +
                      entry(4, 17, _element(0x22B59C, b"und"), _element(0x22B59D, b"es-ES")))
    return (_element(0x1A45DFA3, _element(0x4282, b"matroska")) +
            _element(0x18538067, tracks + _element(0x1F43B675, b"\x00" * 64)))


def test_a_files_track_languages_come_from_its_header_in_one_read():
    data, reads = _mkv(), []

    def handler(request):
        start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        reads.append((start, end))
        return httpx.Response(206, content=data[start:end + 1])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await S.probe(http, "https://cdn/file.mkv")
    assert asyncio.run(run()) == ({"spa", "eng"}, {"spa"})              # a track without a language is English
    assert reads == [(0, thumbs.HEAD - 1)]

    async def not_mkv():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(206, content=b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100))) as http:
            return await S.probe(http, "https://cdn/file.mp4")
    assert asyncio.run(not_mkv()) is None


# ---- switching what plays ----

class Ctl:
    """A player's track surface (Kodi's shape, as TV Agent Player's tracks become)."""

    def __init__(self, audio: list[dict], text: list[dict]):
        self.audio, self.text, self.did = audio, text, []

    async def tracks(self, pid=None):
        return T.kodi_tracks({"audio": self.audio, "text": self.text})

    async def set_audio(self, pid, index):
        self.did.append(("audio", index))
        for t in self.audio:
            t["selected"] = t["i"] == index

    async def set_subtitle(self, pid, index):
        self.did.append(("text", index))
        for t in self.text:
            t["selected"] = t["i"] == index

    async def position(self, pid=None):
        return 1_234_000, 9_000_000, 1.0


DUNE = {"id": "tt1160419", "name": "Dune", "country": "Canada, United States", "runtime": "155 min"}


def playing(ctl: Ctl, copies: list[S.Stream] | None = None, probed: dict | None = None):
    """An agent with Dune playing in TV Agent Player (its English file), these copies from the addons, and these
    files' tracks when read."""
    a = Agent.__new__(Agent)
    a.cfg = config.Config(player="tvplayer", probe_streams=True)
    a.state, a.dry_run, a.account, a._progress = agent_mod.State(), False, object(), None
    a._asked, a._originals, a._probed = {}, {}, {}
    a.state.now_playing = NowPlaying("tt1160419", "movie", "tt1160419", "Dune", "tvplayer", "https://debrid/playing",
                                     filename="playing.mkv", size=1)
    a.said, a.played = [], []

    async def player_id():
        return T.PID

    async def notify(text):
        a.said.append(text)

    async def meta(np):
        return DUNE

    async def streams_for(kind, video, meta=None):
        a.ranked_for = a.track_plan(meta).audio
        return S.rank([S.Stream(**{k: getattr(s, k) for k in S.Stream.__dataclass_fields__}) for s in copies or []],
                      2160, 0, 155, a.ranked_for)

    async def probe(s):
        return (probed or {}).get(s.name)

    async def play(imdb, kind, name, video, label, meta, resume_ms=0, prepared=None, jump=False):
        a.played.append((label, prepared.name, resume_ms, jump, a.track_plan(meta).audio))
        return Outcome(True, f"Playing {label}")
    async def addons(max_age=3600):
        return []                                   # no subtitle addons
    a._player_id, a._ctl, a.notify, a._meta_of = player_id, lambda pid: ctl, notify, meta
    a.http, a.addons = None, addons
    a.streams_for, a._probe, a.play = streams_for, probe, play
    return a


def english_file() -> Ctl:
    return Ctl([{"i": 0, "lang": "en", "label": "", "selected": True}],
               [{"i": 0, "lang": "en", "label": "", "selected": True}, {"i": 1, "lang": "es", "label": ""}])


def test_a_language_the_file_has_is_a_track_switch_and_subtitles_follow_the_audio():
    ctl = Ctl([{"i": 0, "lang": "en", "label": "", "selected": True}, {"i": 1, "lang": "es", "label": "Castellano"}],
              [{"i": 0, "lang": "en", "label": "", "selected": True}])
    a = playing(ctl)
    out = asyncio.run(Agent._tracks(a, A.Tracks(audio="spa")))
    assert ctl.did == [("audio", 1), ("text", None)] and out.say == "Spanish audio, subtitles off"
    assert a.track_plan(DUNE).audio == "spa"                    # kept for the title: its next start, another copy
    out = asyncio.run(Agent._tracks(a, A.Tracks(audio="eng")))
    assert ctl.did[2:] == [("audio", 0), ("text", 0)] and out.say == "English audio, English subtitles"
    assert a.played == [] and a.said == []


def test_a_language_the_file_lacks_switches_to_the_best_copy_that_has_it_at_the_same_moment():
    copies = [_aio("english-4k", langs=["English"], resolution="2160p", score=900),
              _aio("unknown", resolution="2160p", score=500),
              _aio("castellano", langs=["Spanish", "English"], score=300),
              S.Stream("AIOStreams", "https://debrid/playing", "playing", "", "playing.mkv", 0, size=1,
                       languages=("Spanish",), service="premiumize")]          # the one on now: never again
    a = playing(english_file(), copies, probed={"castellano": ({"spa", "eng"}, set()), "unknown": ({"eng"}, set())})
    out = asyncio.run(Agent._tracks(a, A.Tracks(audio="spa")))
    assert a.played == [("Dune", "castellano", 1_234_000, True, "spa")]       # from where it was, Spanish first
    assert a.ranked_for == "spa" and out.ok and out.say == "Spanish audio: another copy · 1080p, from 20:34"
    assert a.said == ["No Spanish audio in this file: looking for a copy with it…",
                      "Switching to a copy with Spanish audio…"]


def test_copies_that_say_nothing_are_read_and_a_file_that_says_wrong_is_passed_over():
    named = _aio("named-english", langs=["English"], score=950)              # by its name only: read last
    read = _aio("read-english", tracks=[{"lang": "English"}], probe=True, score=960)   # its tracks: never read
    a = playing(english_file(), [read, named], probed={"named-english": ({"eng", "spa"}, set()),
                                                     "read-english": ({"spa"}, set())})
    asyncio.run(Agent._tracks(a, A.Tracks(audio="spa")))
    assert [p[1] for p in a.played] == ["named-english"]
    copies = [_aio("says-spanish", langs=["Spanish"], score=900), _aio("silent", score=500)]
    a = playing(english_file(), copies, probed={"says-spanish": ({"eng"}, set()), "silent": ({"eng", "spa"}, set())})
    asyncio.run(Agent._tracks(a, A.Tracks(audio="spa")))
    assert [p[1] for p in a.played] == ["silent"]
    a = playing(english_file(), copies, probed={"says-spanish": None})       # unreadable: the addon's word goes
    asyncio.run(Agent._tracks(a, A.Tracks(audio="spa")))
    assert [p[1] for p in a.played] == ["says-spanish"]
    a = playing(english_file(), copies, probed={"says-spanish": False, "silent": ({"spa"}, set())})   # dead link
    asyncio.run(Agent._tracks(a, A.Tracks(audio="spa")))
    assert [p[1] for p in a.played] == ["silent"]
    a = playing(english_file(), copies)
    a.cfg.probe_streams = False                                       # no link checks: only what addons say
    asyncio.run(Agent._tracks(a, A.Tracks(audio="spa")))
    assert [p[1] for p in a.played] == ["says-spanish"]


def test_no_copy_with_the_language_leaves_what_plays_alone():
    a = playing(english_file(), [_aio("english", langs=["English"])], probed={})
    out = asyncio.run(Agent._tracks(a, A.Tracks(audio="jpn")))
    assert not out.ok and out.say == "No copy of Dune has Japanese audio: it goes on as it was" and a.played == []


def test_subtitles_the_file_and_addons_lack_come_from_a_copy_that_has_them():
    a = playing(english_file(), [_aio("chinese-subs", langs=["English"], subs=["English", "Chinese"])])

    async def no_addon_subtitles(pid, ctl, info, langs, np):
        return None
    a._show_subtitles = no_addon_subtitles
    out = asyncio.run(Agent._tracks(a, A.Tracks(subtitles="cht")))
    assert [p[1] for p in a.played] == ["chinese-subs"] and out.say.startswith("Chinese subtitles: another copy")


def test_original_audio_is_the_language_the_title_was_made_in():
    ctl = Ctl([{"i": 0, "lang": "es", "label": "", "selected": True, "default": True},
               {"i": 1, "lang": "en", "label": ""}], [])
    a = playing(ctl)
    out = asyncio.run(Agent._tracks(a, A.Tracks(audio="original")))
    assert ctl.did[0] == ("audio", 1) and out.say.startswith("English audio")


def test_from_now_on_changes_your_settings_and_what_plays_follows(paths):
    ctl = Ctl([{"i": 0, "lang": "en", "label": "", "selected": True}, {"i": 1, "lang": "es", "label": ""}],
              [{"i": 0, "lang": "en", "label": "", "selected": True}])
    a = playing(ctl)
    a._ask("tt1160419", audio="eng")
    out = asyncio.run(Agent._tracks(a, A.Tracks(audio="spa", keep=True)))
    assert a.cfg.audio_language == "spa" and 'audio_language = "spa"' in config.CONFIG_FILE.read_text()
    assert a._asked == {} and ctl.did == [("audio", 1), ("text", None)]
    assert out.say == "From now on: Spanish audio · Spanish audio, subtitles off"
    out = asyncio.run(Agent._tracks(a, A.Tracks(subtitles="off", keep=True)))
    assert a.cfg.subtitles == "off" and out.say.startswith("From now on: no subtitles")
    asyncio.run(Agent._tracks(a, A.Tracks(subtitles="cht", keep=True)))
    assert a.cfg.subtitles == "foreign" and a.cfg.subtitle_languages == ["cht", "eng", "spa"]
    a.state.now_playing = None                                        # nothing of the agent's on: settings only
    out = asyncio.run(Agent._tracks(a, A.Tracks(subtitles="foreign")))
    assert out.say == "From now on: subtitles only when the audio isn't Spanish"


def test_player_subtitles_follow_the_audio_it_chose():
    """Asked for Spanish audio and no subtitles; a file without Spanish plays English: subtitles come on."""
    async def run(audio_lang: str, shown: bool) -> list:
        player = types.SimpleNamespace(item="tt1#1", last={"tracks": {}}, did=[])
        text = [{"i": 0, "lang": "en", "label": "", "selected": shown}]

        async def tracks(pid=T.PID):
            return T.kodi_tracks({"audio": [{"i": 0, "lang": audio_lang, "selected": True}], "text": text})

        async def set_subtitle(pid, index):
            player.did.append(index)
        player.tracks, player.set_subtitle = tracks, set_subtitle
        a = Agent.__new__(Agent)
        a.tvplayer = player
        await Agent._player_subs(a, languages.TrackPlan("spa", ["eng"], "spa"), None, "X")
        return player.did
    assert asyncio.run(run("en", False)) == [0]
    assert asyncio.run(run("es", True)) == [None]
    assert asyncio.run(run("es", False)) == []


def test_kodi_shows_subtitles_only_for_audio_that_isnt_yours():
    class Kodi:
        def __init__(self, audio):
            self.audio, self.ms, self.did = audio, 0, []

        async def position(self, pid):
            self.ms += 400
            return self.ms, 1_600_000, 1.0

        async def tracks(self, pid):
            track = {"index": 0, "language": self.audio}
            return {"audiostreams": [track], "currentaudiostream": track,
                    "subtitles": [{"index": 0, "language": "eng", "name": ""}], "subtitleenabled": True}

        async def set_audio(self, pid, index):
            pass

        async def set_subtitle(self, pid, index):
            self.did.append(index)

    def apply(audio: str) -> list:
        async def run():
            async def player():
                return 1

            async def recheck(*args):
                pass
            kodi = Kodi(audio)
            me = types.SimpleNamespace(kodi=kodi, _kodi_player=player, _recheck_tracks=recheck)
            await Agent.apply_tracks(me, languages.TrackPlan("spa", ["eng"], "spa"), settle_s=0)
            return kodi.did
        return asyncio.run(run())
    assert apply("spa") == [None]                       # Spanish audio: off
    assert apply("eng") == [0]                          # the file had no Spanish: English subtitles on


# ---- what you say ----

def _router():
    class NoD1:
        where = "local"

        async def choice(self, *args):
            raise router.httpx.ConnectError("off")
    rules = R.build(R.defaults_raw(), "defaults")
    rules.fallback_intent = "none"
    return router.Router(NoD1(), rules)


@pytest.mark.parametrize("text,expect", [
    ("ponlo en español", A.Tracks(audio="spa")),
    ("switch to English audio", A.Tracks(audio="eng")),
    ("subtítulos en inglés", A.Tracks(subtitles="eng")),
    ("quita los subtítulos", A.Tracks(subtitles="off")),
    ("a partir de ahora, audio en español", A.Tracks(audio="spa", keep=True)),
    ("From now on, audio in Spanish.", A.Tracks(audio="spa", keep=True)),
    ("from now on, no subtitles", A.Tracks(subtitles="off", keep=True)),
    ("a partir de ahora sin subtítulos", A.Tracks(subtitles="off", keep=True)),
    ("siempre subtítulos en inglés", A.Tracks(subtitles="eng", keep=True)),
    ("ponlo en español a partir de ahora", A.Tracks(audio="spa", keep=True)),
    ("a partir de ahora en versión original", A.Tracks(audio="original", keep=True)),
    ("by default, English subtitles", A.Tracks(subtitles="eng", keep=True)),
    ("subtítulos solo cuando no sea en español", A.Tracks(subtitles="foreign")),
    ("subtitles only when it's not in Spanish", A.Tracks(subtitles="foreign")),
    ("always skip the intro", A.AutoSkip(on=True)),                   # "always" alone isn't about languages
])
def test_language_commands_now_and_from_now_on(text, expect):
    assert asyncio.run(_router().decide(text)).action == expect


def test_the_models_say_from_now_on_too():
    assert router.from_intent("audio_language", "from now on audio in spanish", RULES) == A.Tracks(audio="spa",
                                                                                                   keep=True)
    assert router.from_intent("subtitles", "a partir de ahora sin subtitulos", RULES) == A.Tracks(subtitles="off",
                                                                                                  keep=True)
    to = lambda answer: llm.to_action(answer, "x", [], RULES)          # noqa: E731
    assert to({"action": "default_audio", "language": "original"}) == A.Tracks(audio="original", keep=True)
    assert to({"action": "default_subtitles", "language": "foreign"}) == A.Tracks(subtitles="foreign", keep=True)
    assert to({"action": "default_subtitles", "language": "english"}) == A.Tracks(subtitles="eng", keep=True)


# ---- the phone and the panel ----

def test_the_phone_sets_the_audio_and_when_subtitles_show():
    assert remote.setting("audio_language", "original") == "original"
    assert remote.setting("subtitles", "always") == "always"
    for key, value in (("native_language", "original"), ("subtitles", "never"), ("subtitles", True)):
        with pytest.raises(HTTPException):
            remote.setting(key, value)
    assert "subtitles" in remote.SETTINGS
