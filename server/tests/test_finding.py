"""Finding an episode or a scene from a description (finding.py): the words, the ranking, the plots from Wikipedia,
the choices instead of a guess, the subtitles in the language asked in, the passages sent, the answers compared, the
episode next door; and the agent's lists to pick from (a chain going on from the pick)."""
import asyncio
import json
import types

import httpx
import pytest

from tv_agent import actions as A
from tv_agent import chain, finding
from tv_agent.agent import Agent, ListItem, Outcome
from tv_agent.state import NowPlaying, State


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(finding, "CACHE", tmp_path / "finding")
    finding.forget()
    yield
    finding.forget()


class Llm:
    """Answers each of finding.py's questions (by its schema's name) with what the test says."""
    model = "fake"

    def __init__(self, **answers):
        self.answers, self.asked = answers, []

    async def _post(self, body, timeout):
        name = body["response_format"]["json_schema"]["name"]
        self.asked.append(name)
        answer = self.answers[name]
        if isinstance(answer, list):                  # one answer per time it's asked
            answer = answer[min(self.asked.count(name), len(answer)) - 1]
        return {"choices": [{"message": {"content": json.dumps(answer(body) if callable(answer) else answer)}}]}


def agent_with(llm, lines: dict | None = None):
    """An agent whose subtitles are `lines` (video -> [(ms, words)]), plots patched by the test."""
    async def addons():
        return []
    me = types.SimpleNamespace(llm=llm, http=None, addons=addons, looked=[])
    me.prefetch = lambda kind, video: me.looked.append(video)

    async def track(http, addons_, video, lang, wait_s=0, sdh_wanted=False, stream=None):
        got = (lines or {}).get((video, lang)) or ((lines or {}).get(video) if lang == "eng" else None) or []
        return got, False
    return me, track


# ---- words ----

def test_words_are_folded_and_cut_to_their_stem():
    assert finding.keys("Paul RIDES the worm") == ["paul", "rid", "worm"]
    assert finding.keys("dances") == finding.keys("dancing") == finding.keys("dance") == ["danc"]
    assert finding.keys("¿Cuándo comen carne humana?") == ["comen", "carn", "human"]
    assert finding.keys("the episode where it is") == []                # nothing that says anything


def test_a_rare_word_and_a_whole_phrase_rank_higher():
    docs = ["Homer eats a donut", "Homer at work", "Homer dreams of the land of chocolate", "Homer and chocolate"]
    scores = finding.rank(docs, ["land of chocolate", "chocolate"])
    assert max(range(4), key=lambda i: scores[i]) == 2 and scores[1] == 0


def test_passages_with_several_of_the_words_beat_one_word_repeated():
    cues = [(i * 1000, "[worm chittering]" if i % 20 == 0 else f"line {i}") for i in range(400)]
    cues[300] = (300_000, "Shai-Hulud decides today if you become Fremen, Paul")
    cues[302] = (302_000, "Ride the worm!")
    hits, keep, _ = finding.windows(cues, ["paul rides the worm", "Shai-Hulud", "Fremen"])
    assert 300 in keep and 302 in keep and keep == sorted(keep)
    assert len(keep) <= finding.MAX_LINES and set(hits) >= {300, 302}
    finding_first = finding.windows(cues, ["worm"])[1]
    assert finding_first                                 # a lone word still finds its lines


def test_a_passage_reaches_further_back_than_forward_and_short_gaps_are_filled():
    """A moment often comes just before the lines that talk about it ("what was that?"); two passages a few lines
    apart are one scene, and the lines between them are sent too."""
    cues = [(i * 1000, f"line {i}") for i in range(300)]
    cues[100] = (100_000, "what was that crystal")
    _, keep, _ = finding.windows(cues, ["crystal"])
    assert keep == list(range(100 - finding.BEFORE, 100 + finding.WINDOW + 1))
    cues[125] = (125_000, "the crystal again")             # its passage starts 125 - BEFORE = 115: a gap of 8
    _, keep, _ = finding.windows(cues, ["crystal"])
    assert keep == list(range(100 - finding.BEFORE, 125 + finding.WINDOW + 1))
    cues[160] = (160_000, "more crystal")                  # far enough away: a passage of its own
    _, keep, _ = finding.windows(cues, ["crystal"])
    assert 140 not in keep and 150 in keep


# ---- plots ----

LIST_PAGE = """==Series overview==
===Season 1 (2009–10)===
{{Episode list/sublist|List of Show episodes
|EpisodeNumber=1
|EpisodeNumber2=1
|Title=[[Pilot (Show)|Pilot]]
|ShortSummary=Jeff enrolls at [[Greendale]].<ref>x</ref> ''Chang'' teaches Spanish.
}}
"""
SEASON_PAGE = """{{#invoke:Episode list|sublist|Show season 2
|EpisodeNumber=26
|EpisodeNumber2=6
|Title=[[Epidemiology (Show)|Epidemiology]]
|ShortSummary=A Halloween party turns into a [[zombie]] outbreak; {{abbr|Chang|x}} dances.
}}
"""


def test_wikipedia_episode_summaries_both_template_forms():
    assert finding._wiki_episodes(LIST_PAGE, None) == {(1, 1): "Pilot: Jeff enrolls at Greendale. Chang teaches Spanish."}
    assert finding._wiki_episodes(SEASON_PAGE, 2) == {(2, 6): "Epidemiology: A Halloween party turns into a zombie "
                                                              "outbreak; dances."}


def test_plots_come_from_tvmaze_and_wikipedia_and_are_kept():
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        q = dict(request.url.params)
        if request.url.host == "api.tvmaze.com":
            if request.url.path == "/lookup/shows":
                return httpx.Response(200, json={"id": 318})
            return httpx.Response(200, json=[{"season": 2, "number": 6, "summary": "<p>Zombies at Greendale.</p>"}])
        if q.get("action") == "query":
            titles = q["titles"].split("|")
            return httpx.Response(200, json={"query": {"pages": [
                {"title": t} if t in ("List of Show episodes", "Show season 2") else {"title": t, "missing": True}
                for t in titles]}})
        page = q["page"]
        text = LIST_PAGE if page.startswith("List") else SEASON_PAGE
        return httpx.Response(200, json={"parse": {"title": page, "wikitext": text}})

    eps = [{"id": "tt1:1:1", "season": 1, "episode": 1, "name": "Pilot", "overview": "The study group forms."},
           {"id": "tt1:2:6", "season": 2, "episode": 6, "name": "Epidemiology", "overview": ""}]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            first = await finding.plots(http, "tt1", "Show", eps)
            finding.forget()                             # the disk keeps them: no asking again
            n = len(asked)
            again = await finding.plots(http, "tt1", "Show", eps)
            return first, again, n
    first, again, n = asyncio.run(go())
    assert first == again and len(asked) == n
    assert "Zombies at Greendale." in first["tt1:2:6"] and "zombie outbreak" in first["tt1:2:6"]
    assert first["tt1:1:1"].startswith("Pilot The study group forms. Pilot: Jeff enrolls")


# ---- episodes ----

EPS = [{"id": f"tt1:{s}:{e}", "season": s, "episode": e, "name": n, "overview": o} for s, e, n, o in [
    (1, 7, "Introduction to Statistics", "A Halloween party at Greendale."),
    (2, 6, "Epidemiology", "A Halloween party turns into a zombie outbreak."),
    (2, 14, "Advanced Dungeons & Dragons", "The group plays Dungeons & Dragons with Neil."),
    (3, 5, "Horror Fiction in Seven Spooky Steps", "Britta's Halloween test."),
    (1, 10, "Environmental Science", "Chang is sad about his divorce; Jeff takes him out dancing.")]]
LOOK = {"en": ["Chang", "dance", "ex-wife"], "es": ["Chang", "baile"], "episodes": []}


def finder(monkeypatch, pick, lines=None, look=LOOK):
    llm = Llm(look_for=look, episodes=pick)
    me, track = agent_with(llm, lines)

    async def plots(http, imdb, name, eps, wait_s=0):
        return {v["id"]: f"{v['name']} {v['overview']}" for v in eps}
    monkeypatch.setattr(finding, "plots", plots)
    monkeypatch.setattr(finding, "track_", track)
    return me, llm


def test_an_episode_by_its_name_needs_no_model(monkeypatch):
    me, llm = finder(monkeypatch, {"fits": []})
    got = asyncio.run(finding.episode(me, "tt1", "Community", EPS, "Advanced Dungeons and Dragons"))
    assert got.episode["id"] == "tt1:2:14" and llm.asked == []


def test_one_episode_that_fits_exactly_and_is_said_in_its_subtitles_plays(monkeypatch):
    lines = {"tt1:1:10": [(1000, "Chang, you dance like my ex-wife")]}
    me, llm = finder(monkeypatch, {"fits": [{"id": "tt1:1:10", "match": "exactly"}, {"id": "tt1:2:6", "match":
                                                                                       "partly"}]}, lines)
    got = asyncio.run(finding.episode(me, "tt1", "Community", EPS, "Mr. Chang dances with his ex-wife"))
    assert got.episode["id"] == "tt1:1:10" and got.why == "its plot and subtitles"
    assert llm.asked == ["look_for", "episodes"]
    assert me.looked[0] == "tt1:1:10"          # its streams looked up from the plots' ranking on, before the answer


def test_several_that_fit_are_listed_not_guessed(monkeypatch):
    me, _ = finder(monkeypatch, {"fits": [{"id": i, "match": "exactly"} for i in ("tt1:2:6", "tt1:1:7", "tt1:3:5")]})
    got = asyncio.run(finding.episode(me, "tt1", "Community", EPS, "the Halloween episode"))
    assert got.episode is None and [v["id"] for v in got.choices] == ["tt1:2:6", "tt1:1:7", "tt1:3:5"]


def test_a_sure_pick_whose_subtitles_say_none_of_it_is_listed(monkeypatch):
    lines = {"tt1:1:10": [(1000, "Nobody says anything about it here")]}
    me, _ = finder(monkeypatch, {"fits": [{"id": "tt1:1:10", "match": "exactly"}]}, lines)
    got = asyncio.run(finding.episode(me, "tt1", "Community", EPS, "Mr. Chang dances with his ex-wife"))
    assert got.episode is None and got.why == "its subtitles say nothing of it" and got.choices[0]["id"] == "tt1:1:10"


def test_none_fitting_searches_the_subtitles_then_lists_the_closest(monkeypatch):
    lines = {"tt1:2:14": [(1000, "Chang dances"), (2000, "with his ex-wife at the dance")]}
    me, _ = finder(monkeypatch, {"fits": []}, lines)
    got = asyncio.run(finding.episode(me, "tt1", "Community", EPS, "Mr. Chang goes dancing with his ex-wife"))
    assert got.episode is None and got.why == "found in the subtitles" and got.choices[0]["id"] == "tt1:2:14"


# ---- scenes ----

CUES = [(i * 1000, f"line {i}") for i in range(600)]
CUES[300] = (300_000, "Shai-Hulud decides today if you become Fremen.")
CUES[303] = (303_000, "[worm roaring]")
SCENE_LOOK = {"en": ["worm", "Shai-Hulud"], "es": ["gusano"], "episodes": []}


def place(line, at, how="words", shown=None):
    return {"line": line, "at": at, "how": how, "shown": shown or line}


def scener(monkeypatch, answers, lines=None, look=SCENE_LOOK):
    llm = Llm(look_for=look, moment=answers)
    me, track = agent_with(llm, lines or {"tt2": CUES})
    monkeypatch.setattr(finding, "track_", track)
    return me, llm


def test_a_moment_both_answers_agree_on(monkeypatch):
    me, llm = scener(monkeypatch, place("Shai-Hulud decides today if you become Fremen.", "5:00"))
    got = asyncio.run(finding.scene(me, "tt2", "Dune: Part Two", "paul rides the worm"))
    assert got.ms == 300_000 and not got.others and llm.asked.count("moment") == 2
    assert got.lines_sent <= finding.MAX_LINES


def test_two_answers_apart_a_third_settles_it(monkeypatch):
    right, wrong = place("Shai-Hulud decides today if you become Fremen.", "5:00"), place("[worm roaring]", "5:03")
    far = place("line 590", "9:50")                     # nothing of what was said around it
    me, llm = scener(monkeypatch, [right, far, right])
    got = asyncio.run(finding.scene(me, "tt2", "Dune: Part Two", "paul rides the worm"))
    assert got.ms == 300_000 and llm.asked.count("moment") == 3
    me, llm = scener(monkeypatch, [right, wrong])      # 3 s apart: the same moment
    assert asyncio.run(finding.scene(me, "tt2", "Dune: Part Two", "paul rides the worm")).ms == 300_000


def test_three_different_answers_are_listed(monkeypatch):
    cues = list(CUES)
    cues[100] = (100_000, "The worm comes. Shai-Hulud!")
    a, b = place("Shai-Hulud decides today if you become Fremen.", "5:00"), place("The worm comes. Shai-Hulud!", "1:40")
    me, _ = scener(monkeypatch, [a, b, place("x", None, "guess")], {"tt2": cues})
    got = asyncio.run(finding.scene(me, "tt2", "Dune: Part Two", "paul rides the worm"))
    assert got.ms == 300_000 and [f.ms for f in got.others] == [100_000]


def test_a_line_with_nothing_of_the_request_around_it_is_refused(monkeypatch):
    cues = list(CUES)
    cues[500] = (500_000, "I love you.")
    me, _ = scener(monkeypatch, place("I love you.", "8:20"), {"tt2": cues})
    got = asyncio.run(finding.scene(me, "tt2", "Dune: Part Two", "paul rides the worm"))
    assert got.ms is None and got.why in ("the line it gave isn't in the subtitles",
                                          "nothing of what you said is near the line it found")


def test_spanish_asks_search_the_spanish_subtitles_first_and_show_the_line_in_spanish(monkeypatch):
    spa = [(i * 1000, f"línea {i}") for i in range(600)]
    spa[300] = (300_000, "Shai-Hulud decidirá hoy si te vuelves un Fremen.")
    seen = []

    def answer(body):
        seen.append(body["messages"][1]["content"])
        return place("Shai-Hulud decidirá hoy si te vuelves un Fremen.", "5:00")
    me, _ = scener(monkeypatch, answer, {("tt2", "spa"): spa, ("tt2", "eng"): CUES})
    got = asyncio.run(finding.scene(me, "tt2", "Dune: Part Two", "Paul monta el gusano"))
    assert got.ms == 300_000 and got.lang == "spa" and "decidirá" in got.shown and "línea" in seen[0]


def test_not_in_this_episode_but_in_the_next_is_offered(monkeypatch):
    here = [(i * 1000, f"line {i}") for i in range(300)]
    there = [(i * 1000, f"line {i}") for i in range(300)]
    there[200] = (200_000, "Carol learns what's in the food: human remains.")
    me, _ = scener(monkeypatch, place("Carol learns what's in the food: human remains.", "3:20"),
                   {"tt3:1:2": here, "tt3:1:3": there}, {"en": ["human remains", "Carol"], "es": [], "episodes": []})

    async def meta(http, kind, imdb):
        return {"videos": [{"id": f"tt3:1:{n}", "season": 1, "episode": n} for n in (1, 2, 3)]}
    monkeypatch.setattr(finding.cinemeta, "meta", meta)
    here[10] = (10_000, "Hi")
    got = asyncio.run(finding.scene(me, "tt3:1:2", "Pluribus S1E2", "Carol finds out they eat people"))
    assert got.video == "tt3:1:3" and got.ms == 200_000


# ---- the agent's lists ----

def test_an_episode_not_sure_of_is_listed_and_the_pick_goes_on_with_the_chain(monkeypatch):
    shown, played = [], []
    me = types.SimpleNamespace(_shown=None, notify=None)

    async def show(s):
        shown.append(s)
    me._show_on_tv = show
    found = finding.EpisodeFind(choices=[EPS[1], EPS[0]], why="more than one fits")
    a = A.PlayTitle(title="community", episode_name="the Halloween one")
    chain.PLAN.set("the scene: “zombies”")
    chain.REST.set((A.FindScene(what="zombies"),))
    try:
        out = asyncio.run(Agent._ask_episode(me, a, "tt1", "Community", found))
    finally:
        chain.PLAN.set("")
        chain.REST.set(())
    assert not out.ok and out.say == "Not sure which episode: say a number"     # a chain stops at the list
    items = shown[0].items
    assert [i.video for i in items] == ["tt1:2:6", "tt1:1:7"] and items[0].then == (A.FindScene(what="zombies"),)

    async def sequence(seq):
        played.append(seq)
        return Outcome(True, "ok")
    me2 = types.SimpleNamespace(_sequence=sequence, state=State())
    asyncio.run(Agent.play_item(me2, items[0]))
    assert played == [A.Sequence(steps=[A.PlayTitle(title="Community", season=2, episode=6, kind="series"),
                                        A.FindScene(what="zombies")])]


def test_a_moment_of_what_plays_picked_from_a_list_is_a_jump():
    jumped = []
    np = NowPlaying("tt2", "movie", "tt2", "Dune", "tvplayer")

    async def player_id():
        return "tvplayer"

    async def transport(a):
        jumped.append(a)
        return Outcome(True, "To 5:00")
    me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np), _player_id=player_id, _transport=transport)
    item = ListItem("tt2", "movie", "Dune", "5:00", "“Shai-Hulud…”", video="tt2", at_ms=300_000)
    assert asyncio.run(Agent.play_item(me, item)).ok
    assert jumped == [A.Transport(op="seek_to", seconds=300 - chain.SCENE_BEFORE)]


def test_a_moment_of_another_episode_picked_from_a_list_opens_it_exactly_there(monkeypatch):
    """TV Agent Player starts a resume at the keyframe before it (up to 10 s earlier); a scene asks for the moment."""
    played = []

    async def play(*args, **kw):
        played.append((args, kw))
        return Outcome(True, "Playing")

    async def get(imdb):
        return {"state": {"video_id": "tt1:2:6", "timeOffset": 600_000}}

    async def meta(http, kind, imdb):
        return {}
    monkeypatch.setattr("tv_agent.agent.cinemeta.meta", meta)
    me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=None), play=play, prefetch=lambda *a: None,
                               http=None, library=types.SimpleNamespace(get=get))
    item = ListItem("tt1", "series", "Community", "S2E6", "“Zombies…”", video="tt1:2:6", at_ms=300_000)
    assert asyncio.run(Agent.play_item(me, item)).ok
    (args, kw), = played
    assert args[6] == 300_000 - chain.SCENE_BEFORE * 1000 and kw == {"exact": True}
    me.state.now_playing, played[:] = None, []
    asyncio.run(Agent.play_item(me, ListItem("tt1", "series", "Community", "S2E6", "", video="tt1:2:6")))
    assert played[0][0][6] == 600_000 and played[0][1] == {"exact": False}      # where you were: a resume


def test_subtitles_are_matched_to_the_ones_the_episode_check_measured():
    from tv_agent.episode_check import Verdict
    kept = [(i * 1000 + 2000, f"a line long enough to match number {i}") for i in range(50)]   # 2 s later
    ours = [(i * 1000, f"A line, long enough to match: number {i}!") for i in range(50)]
    np = NowPlaying("tt1", "series", "tt1:1:1", "X", "tvplayer", filename="f.mkv", size=1)
    me = types.SimpleNamespace(checks=types.SimpleNamespace(get=lambda *a: Verdict("tt1:1:1", True, shift=3.0)),
                               _episode_cues={("tt1:1:1", "eng"): kept})
    assert Agent._subtitle_shift(me, np, ours) == 2000 - 3000      # to theirs, then to the file's
    assert Agent._subtitle_shift(me, np, [(0, "otra cosa")] * 10) == 0


def test_yes_picks_the_one_offered():
    from test_chain import decide
    assert decide("yes").action == A.Pick(number=1)
    assert decide("sí").action == A.Pick(number=1)


def test_subtitles_offered_whose_downloads_fail_are_told_apart_from_none(monkeypatch):
    """The trace says the addon failed (and how), so a bench doesn't count it against the agent."""
    from tv_agent import tracing
    from tv_agent.stremio import subtitles
    offered = [subtitles.Subtitle("https://subs.example/1.srt", "eng", "x", "Subs", impaired=True)]

    async def listing(*args):
        return offered

    async def download(http, url, timeout):
        raise httpx.HTTPStatusError("down", request=httpx.Request("GET", url),
                                    response=httpx.Response(503, request=httpx.Request("GET", url)))
    monkeypatch.setattr(finding, "_listing", listing)
    monkeypatch.setattr(subtitles, "download", download)
    trace = tracing.Trace()
    token = tracing.TRACE.set(trace)
    try:
        lines, _ = asyncio.run(finding.track(None, [], "tt1:1:1", "eng", label="Subtitles"))
    finally:
        tracing.TRACE.reset(token)
    step = trace.close()[0][-1]
    assert lines == [] and step["verdict"] == "failed" and "Subs: HTTP 503" in step["why"]

    offered.clear()                                  # none offered at all: just none
    trace = tracing.Trace()
    token = tracing.TRACE.set(trace)
    try:
        asyncio.run(finding.track(None, [], "tt1:1:2", "eng", label="Subtitles"))
    finally:
        tracing.TRACE.reset(token)
    assert trace.close()[0][-1]["verdict"] == "none"
