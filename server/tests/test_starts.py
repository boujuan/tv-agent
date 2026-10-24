"""How fast a start gets to the player, and that it starts the right title: the title search racing the LLM's
sound-alikes, d1 left out when the answer is certain, stream lookups started ahead, slow addons not waited for."""
import asyncio
import time
import types

import httpx

from tv_agent import agent as agent_mod
from tv_agent.actions import PlayTitle
from tv_agent.agent import Agent
from tv_agent.decide.d1 import Choice
from tv_agent.stremio import cinemeta as C
from tv_agent.stremio import search as title_search
from tv_agent.stremio import streams as S
from tv_agent.stremio.account import Addon

PART_ONE = C.Meta("tt14998742", "movie", "Rebel Moon - Part One: A Child of Fire", "2023", 0)
PART_TWO = C.Meta("tt23137904", "movie", "Rebel Moon - Part Two: The Scargiver", "2024", 1)
PREQUEL = C.Meta("tt29282591", "series", "Untitled Rebel Moon Prequel", "", 2)
REBELDE = C.Meta("tt14153686", "series", "Rebelde (2022)", "2022", 0)
THE_BOYS = C.Meta("tt1190634", "series", "The Boys", "2019–2026", 0)
OLD_BOYS = C.Meta("tt0108713", "series", "The Boys", "1993–", 1)
POLO_BOYS = C.Meta("tt2372566", "series", "Polo Boys", "2013", 0)
SEASONS = {"tt1190634": 5, "tt0108713": 1, "tt2372566": 1, "tt2": 3}


def title_agent(monkeypatch, catalog: dict[str, list[C.Meta]], sounds_like: list[str], prefer: list[str] = (),
                library: list[dict] = (), slow: dict[str, float] = None, popularity: dict[str, float] = None):
    """An Agent with only what title resolution uses: a fake title search (catalog by query, `slow` seconds before
    some queries answer, unless the caller found the title another way), the LLM's sound-alikes, and d1 picking the
    first option named as in `prefer` (else the first option)."""
    a = Agent.__new__(Agent)
    a.cfg = types.SimpleNamespace(decide_off=[])
    a.rules = types.SimpleNamespace(title_candidates=4, title_question="Which title?")
    a.account, a.dry_run, a.http = None, False, None
    asked = []

    async def search(http, query, addons, patience=1.5, enough=None):
        if delay := (slow or {}).get(query):
            try:
                await asyncio.wait_for(enough.wait() if enough else asyncio.sleep(delay), delay)
            except asyncio.TimeoutError:
                pass
        return list(catalog.get(query, []))

    async def meta(http, kind, imdb):
        return {"popularity": (popularity or {}).get(imdb, 0),
                "videos": [{"id": f"{imdb}:{s}:{e}", "season": s, "episode": e}
                           for s in range(1, SEASONS.get(imdb, 0) + 1) for e in range(1, 9)]}
    monkeypatch.setattr(agent_mod.title_search, "search", search)
    monkeypatch.setattr(agent_mod.cinemeta, "meta", meta)

    class Library:
        async def find(self, title, kind=None):
            return next((i for i in library if title_search.norm(i["name"]) == title_search.norm(title)), None)

        async def items(self):
            return list(library)

    class Llm:
        async def titles(self, heard, sentence, names):
            return list(sounds_like)

    class D1:
        last_source = "hosted"

        async def choice(self, state, question, options):
            asked.append((state, dict(options)))
            key = next((k for want in prefer for k, label in options.items() if label.startswith(want)), "c0")
            return Choice(key, 0.5, {})

    async def addons(max_age=3600):
        return []
    a.library, a.llm, a.d1, a.addons = Library(), Llm(), D1(), addons
    return a, asked


def test_a_misheard_title_is_the_sound_alike_found_by_its_name_before_a_subtitle(monkeypatch):
    """2026-10-24: "Pon Rebel Mung" played Rebelde (2022). The LLM's "Rebel Moon" was found (Part One, by d1) and
    thrown away, because its full name has a subtitle; the search's pick for "rebel mung" won."""
    a, asked = title_agent(monkeypatch, {
        "rebel mung": [REBELDE, PART_TWO],
        "Rebel Moon": [PART_ONE, PREQUEL, PART_TWO]}, ["Rebel Moon", "Rebel Mon"],
        prefer=["Rebelde", "Rebel Moon - Part One"])

    async def run():
        agent_mod.HEARD.set("Pon Rebel Mung")
        return await a.resolve("rebel mung", None, None)
    assert asyncio.run(run()) == (PART_ONE.id, "movie", PART_ONE.name)
    assert [state for state, _ in asked if "Rebel Moon - Part One" in str(_)] == ["Pon Rebel Mung"]   # d1 sees it all


def test_the_search_for_a_misheard_name_stops_waiting_once_a_sound_alike_is_found(monkeypatch):
    """A misheard name is a search nobody has made (Cinemeta: up to 8 s); the real title's answer comes first."""
    a, _ = title_agent(monkeypatch, {"paul the boys": [POLO_BOYS], "The Boys": [THE_BOYS]}, ["The Boys"],
                       slow={"paul the boys": 5.0})
    t = time.perf_counter()
    assert asyncio.run(a.resolve("paul the boys", None, 4)) == (THE_BOYS.id, "series", "The Boys")
    assert time.perf_counter() - t < 1


def test_a_sound_alike_without_the_season_asked_for_isnt_taken(monkeypatch):
    """"Paul the Boys, season four": Polo Boys (one season) sounds closer than The Boys, and the LLM may say it first."""
    library = [{"_id": THE_BOYS.id, "type": "series", "name": "The Boys"}]
    a, _ = title_agent(monkeypatch, {"paul the boys": [POLO_BOYS, THE_BOYS, OLD_BOYS], "Polo Boys": [POLO_BOYS]},
                       ["Polo Boys", "The Boys"], library=library)
    assert asyncio.run(a.resolve("paul the boys", None, 4)) == (THE_BOYS.id, "series", "The Boys")


def test_a_title_said_exactly_still_wins_over_the_sound_alikes(monkeypatch):
    rebel_music = C.Meta("tt3281470", "series", "Rebel Music", "2013", 0)
    a, _ = title_agent(monkeypatch, {"rebel music": [rebel_music], "Rebel Moon": [PART_ONE]}, ["Rebel Moon"])
    assert asyncio.run(a.resolve("rebel music", None, None)) == (rebel_music.id, "series", "Rebel Music")


def test_no_d1_when_the_leader_is_certain_once_popularity_counts(monkeypatch):
    """Oppenheimer (2023) against the 1980 series, both said exactly: 19.6 ahead after popularity, and d1 always
    picked it (0.97). A closer call still goes to d1, with the leader's streams looked up meanwhile."""
    film, series = C.Meta("tt1", "movie", "X", "1985", 0), C.Meta("tt2", "series", "X", "1991", 0)

    def choose(film_pop: float, series_pop: float):
        a, asked = title_agent(monkeypatch, {}, [], popularity={"tt1": film_pop, "tt2": series_pop})
        expected = []
        hits = C.ranked("x", [film, series])
        assert not a._decisive("x", hits)                      # too close without popularity
        got = asyncio.run(a._choose_title("x", None, hits, "play x", lambda imdb, kind: expected.append(imdb)))
        gap = (lambda h: h[0][0] - h[1][0])(C.ranked("x", [film, series], None, {"tt1": film_pop, "tt2": series_pop}))
        return got, len(asked), expected, gap
    got, asked, expected, gap = choose(3.0, 0.1)
    assert got is film and asked == 0 and expected == [] and agent_mod.CERTAIN_GAP <= gap < 20
    got, asked, expected, gap = choose(1.0, 0.5)
    assert got is film and asked == 1 and expected == ["tt1"] and gap < agent_mod.CERTAIN_GAP


def test_the_likely_title_has_its_streams_looked_up_while_the_rest_decides(monkeypatch):
    a, _ = title_agent(monkeypatch, {}, [])
    a.account = object()
    looked = []
    a.prefetch = lambda kind, video: looked.append(video)

    async def get(imdb):
        return {"state": {"video_id": "tt2:3:4"}} if imdb == "tt2" else None
    a.library.get = get

    async def run():
        expect = a._expecting(PlayTitle(title="x"))
        for imdb, kind in (("tt1", "movie"), ("tt2", "series"), ("tt3", "movie")):
            expect(imdb, kind)
        expect("tt1", "movie")
        await asyncio.sleep(0.01)
        series = a._expecting(PlayTitle(title="the boys", season=4, episode=1))
        series(THE_BOYS.id, "series")
        await asyncio.sleep(0.01)
    asyncio.run(run())
    # the film; the series where you were; no third guess; then the episode asked for
    assert looked == ["tt1", "tt2:3:4", "tt1190634:4:1"]


def test_a_slow_addon_isnt_waited_for_once_a_stream_that_fits_is_in():
    async def handler(request: httpx.Request) -> httpx.Response:
        if "slow" in request.url.host:
            await asyncio.sleep(1)
            return httpx.Response(200, json={"streams": []})
        return httpx.Response(200, json={"streams": [{"url": "https://debrid/f.mkv", "name": "4K ⚡",
                                                      "description": "Movie 2160p"}]})
    manifest = {"resources": ["stream"], "types": ["movie"], "idPrefixes": ["tt"]}
    addons = [Addon("Slow", "https://slow.example/manifest.json", manifest),
              Addon("AIOStreams", "https://fast.example/manifest.json", manifest)]

    async def run(enough):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            t = time.perf_counter()
            found = await S.fetch(http, addons, "movie", "tt1", enough=enough, grace=0.1)
            return found, time.perf_counter() - t
    found, took = asyncio.run(run(lambda got: any(s.height == 2160 for s in got)))
    assert [s.addon for s in found] == ["AIOStreams"] and took < 0.5
    found, took = asyncio.run(run(lambda got: False))            # nothing fits: everyone is waited for
    assert took >= 1


def test_continue_watchings_first_items_have_their_streams_looked_up_when_the_mic_opens():
    a = Agent.__new__(Agent)
    looked = []

    async def addons(max_age=3600):
        return []

    async def continue_watching():
        return [{"_id": "tt1", "type": "series"}, {"_id": "tt2", "type": "movie"}, {"_id": "tt3", "type": "movie"}]

    async def target(item):
        return {"tt1": ("tt1:2:5", 0, "X · S2E5", {}), "tt2": ("tt2", 600_000, "Y", {})}.get(item["_id"])
    a.addons, a._continue_target = addons, target
    a.library = types.SimpleNamespace(_at=time.monotonic(), continue_watching=continue_watching)
    a.prefetch = lambda kind, video: looked.append((kind, video))
    asyncio.run(a.warm())
    assert looked == [("series", "tt1:2:5"), ("movie", "tt2")]           # the next episode, where you stopped


def test_the_episode_check_waits_for_the_file_to_open_and_reads_the_link_found(monkeypatch):
    a = Agent.__new__(Agent)
    did = []
    a.checks = types.SimpleNamespace(get=lambda *args: None)
    a.state = types.SimpleNamespace(now_playing=None)

    def verify(s, video, meta, link=None):
        did.append(("check", link))

        async def verdict():
            return types.SimpleNamespace(ok=True)
        return asyncio.ensure_future(verdict())
    a._verify = verify
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        did.append(("wait", seconds))
        await real_sleep(0)
    s = S.Stream("AIOStreams", "https://addon/play/1", "n", "", "X.S01E01.mkv", 0)

    monkeypatch.setattr(agent_mod.asyncio, "sleep", sleep)
    asyncio.run(a._switch_if_wrong(object(), s, None, ("tt1", "series", "X", "tt1:1:1", "X · S1E1", {}),
                                   "https://cdn/resolved"))
    assert did == [("wait", agent_mod.CHECK_AFTER), ("check", "https://cdn/resolved")]


def test_a_starts_record_says_how_its_title_was_found(monkeypatch):
    """For the activity history: the candidates, how each choice was made (d1 with its confidence), the LLM's
    sound-alikes and which one won, and the times."""
    a, _ = title_agent(monkeypatch, {"rebel mung": [REBELDE, PART_TWO], "Rebel Moon": [PART_ONE, PREQUEL, PART_TWO]},
                       ["Rebel Moon"], prefer=["Rebelde", "Rebel Moon - Part One"])
    a.dry_run, a._closing, a.state = True, None, agent_mod.State()

    async def play_found(play, imdb, kind, name, playing_video=None):
        return agent_mod.Outcome(True, f"Playing {name}", {"stages": {"streams_ms": 2100}})
    a._play_found = play_found
    out = asyncio.run(a.execute(PlayTitle(title="rebel mung"), "Pon Rebel Mung"))
    title = out.details["title"]
    assert title["said"] == "rebel mung" and title["picked"] == PART_ONE.name and title["imdb"] == PART_ONE.id
    assert title["by"] == "sounds like 'Rebel Moon', in Cinemeta" and title["sounds_like"] == ["Rebel Moon"]
    assert any(c.startswith("Rebelde (2022)") for c in title["candidates"])
    moon = next(c for c in title["choices"] if c["for"] == "Rebel Moon")
    assert moon["by"] == "d1" and moon["confidence"] == 0.5 and moon["picked"].startswith(PART_ONE.name)
    assert {"ms", "library_ms", "search_ms", "llm_ms"} <= set(title) and out.details["stages"] == {"streams_ms": 2100}
