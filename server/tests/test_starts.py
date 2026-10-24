"""How fast a start gets to the player, and that it starts the right title: the title search racing the LLM's
sound-alikes, d1 left out when the answer is certain, stream lookups started ahead, slow addons not waited for."""
import asyncio
import time
import types

import httpx

from tv_agent import agent as agent_mod
from tv_agent import tracing
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
                library: list[dict] = (), slow: dict[str, float] = None, popularity: dict[str, float] = None,
                sure: float | None = 0.5):
    """An Agent with only what title resolution uses: a fake title search (catalog by query, `slow` seconds before
    some queries answer, unless the caller found the title another way), the LLM's sound-alikes, and d1 picking the
    first option named as in `prefer` (else the first option), `sure` of it (None: d1 unreachable)."""
    a = Agent.__new__(Agent)
    a.cfg = types.SimpleNamespace(decide_off=[])
    a.rules = types.SimpleNamespace(title_candidates=4, title_question="Which title?")
    a.account, a.dry_run, a.look_ahead, a.http = None, False, True, None
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
        model = "deepseek/deepseek-v4.1-flash-1017"

        async def titles(self, heard, sentence, names):
            return list(sounds_like)

    class D1:
        last_source, hosted_model = "hosted", "d1"

        async def choice(self, state, question, options):
            asked.append((state, dict(options)))
            if sure is None:
                raise httpx.HTTPStatusError("502", request=httpx.Request("POST", "https://d1"),
                                            response=httpx.Response(502))
            key = next((k for want in prefer for k, label in options.items() if label.startswith(want)), "c0")
            return Choice(key, sure, {k: sure if k == key else (1 - sure) / len(options) for k in options})

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


def test_a_starts_trace_says_how_its_title_was_found(monkeypatch):
    """For the activity history and the panel's diagram: each step after the decision, in order, with what it found
    and its time: the library, the searches, the LLM's sound-alikes, each choice (d1 with its model and confidence),
    the pick."""
    a, _ = title_agent(monkeypatch, {"rebel mung": [REBELDE, PART_TWO], "Rebel Moon": [PART_ONE, PREQUEL, PART_TWO]},
                       ["Rebel Moon"], prefer=["Rebelde", "Rebel Moon - Part One"])
    a.dry_run, a._closing, a.state = True, None, agent_mod.State()

    async def play_found(play, imdb, kind, name, playing_video=None):
        agent_mod._step("start", "streams", 2100, kept=11)
        return agent_mod.Outcome(True, f"Playing {name}", {"stages": {"streams_ms": 2100}})
    a._play_found = play_found

    async def go():
        run = tracing.Trace()
        tracing.TRACE.set(run)
        out = await a.execute(PlayTitle(title="rebel mung"), "Pon Rebel Mung")
        return out, [t for t in run.close()[0] if t["stage"] in ("title", "start")]
    out, trace = asyncio.run(go())
    steps = [(t["stage"], t["step"], t.get("query")) for t in trace]
    assert steps[0] == ("title", "library", "rebel mung") and trace[0]["matched"] is False
    assert ("title", "search", "rebel mung") in steps and ("title", "sound-alikes", "rebel mung") in steps
    assert ("title", "search", "Rebel Moon") in steps and steps[-2:] == [("title", "picked", None),
                                                                        ("start", "streams", None)]
    moon = next(t for t in trace if t["step"] == "choice" and t["query"] == "Rebel Moon")
    assert moon["by"] == "d1" and moon["model"] == "hosted: d1" and moon["confidence"] == 0.5 and moon["of"] == 3
    assert moon["picked"].startswith(PART_ONE.name) and len(moon["candidates"]) == 3
    assert next(t for t in trace if t["step"] == "sound-alikes")["names"] == ["Rebel Moon"]
    assert all(isinstance(t["at"], int) for t in trace) and [t["at"] for t in trace] == sorted(t["at"] for t in trace)
    assert out.details["title"] == {"said": "rebel mung", "picked": PART_ONE.name, "imdb": PART_ONE.id,
                                    "by": "sounds like 'Rebel Moon', in Cinemeta", "ms": out.details["title"]["ms"]}
    assert out.details["stages"] == {"streams_ms": 2100}


def test_the_activity_entry_has_the_whole_trace_and_explain_finds_the_title(monkeypatch):
    """The decision's steps, then the action's own (finding the title, starting it), in one trace; and the panel's
    Explain gets the title steps too, without anything played."""
    from tv_agent import server
    from tv_agent.decide.router import Decision
    decided = [{"stage": "clean", "text": "pon rebel mung"}, {"stage": "llm", "action": "play_title", "reached": True}]
    after = [{"stage": "title", "step": "picked", "title": "Rebel Moon", "at": 900}]
    svc = server.Service.__new__(server.Service)
    svc.token, svc.cfg = "agent-token", types.SimpleNamespace(tv_hosts=[], log_transcripts=True)
    svc.clients = None                   # only read by the phone routes, which this test doesn't call
    recorded = []
    svc.settings = lambda: {"player": "tvplayer"}
    monkeypatch.setattr(server.activity, "append", recorded.append)
    monkeypatch.setattr(server.activity, "save_details", lambda t, data: f"{t:.3f}.json.gz")

    async def execute(action, heard=""):
        agent_mod._step("title", "picked", title="Rebel Moon")
        return agent_mod.Outcome(True, "Playing Rebel Moon", {"stages": {"streams_ms": 2}})

    async def trace_title(a, heard):
        return ("tt14998742", "movie", "Rebel Moon - Part One"), list(after)

    async def decide(text):
        return Decision(PlayTitle(title="rebel mung"), "llm", 0.9, "llm", list(decided))
    svc.agent = types.SimpleNamespace(execute=execute, trace_title=trace_title, state=agent_mod.State(),
                                      adb=types.SimpleNamespace(host=None))
    svc.router = types.SimpleNamespace(decide=decide)
    svc.maybe_reload = lambda: None
    d = asyncio.run(decide("pon rebel mung"))
    asyncio.run(svc.execute(d, "Pon Rebel Mung", "tv"))
    entry = recorded[0]
    assert entry["trace"][:2] == decided and entry["stages"] == {"streams_ms": 2}
    assert [(t["stage"], t["step"]) for t in entry["trace"][2:]] == [("title", "picked"), ("done", "outcome")]
    assert entry["settings"] == {"player": "tvplayer"}
    assert d.trace == decided                           # the decision itself is left as it was

    async def explain(title: bool):
        transport = httpx.ASGITransport(app=server.local_app(svc), client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as http:
            return (await http.post("/v1/decide", json={"text": "pon rebel mung", "title": title},
                                    headers={"authorization": "Bearer agent-token"})).json()
    plain, found = asyncio.run(explain(False)), asyncio.run(explain(True))
    assert plain["trace"] == decided and "title" not in plain
    assert found["trace"] == decided + after and found["title"]["name"] == "Rebel Moon - Part One"


def test_a_sound_alike_request_not_waited_for_is_in_the_trace(monkeypatch):
    rebel_music = C.Meta("tt3281470", "series", "Rebel Music", "2013", 0)
    a, _ = title_agent(monkeypatch, {"rebel music": [rebel_music]}, ["Rebel Moon"])

    async def slow(heard, sentence, names):
        await asyncio.sleep(5)
        return ["Rebel Moon"]
    a.llm.titles = slow

    async def run():
        agent_mod.START.set({"_t0": time.perf_counter()})
        tracing.TRACE.set(trace := tracing.Trace())
        found = await a.resolve("rebel music", None, None)
        return found, trace.close()[0]
    found, trace = asyncio.run(run())
    asked = next(t for t in trace if t["step"] == "sound-alikes")
    assert found[2] == "Rebel Music" and asked["answered"] is False and asked["model"].startswith("deepseek")


DUNE_2000 = C.Meta("tt0142032", "series", "Dune", "2000", 0)
PROPHECY = C.Meta("tt10466872", "series", "Dune: Prophecy", "2024–", 2)
DUNE_DOCS = C.Meta("tt9", "series", "Dune", "2021", 1)


def unsure_agent(monkeypatch, sure):
    """"Play Dune episode 2" (2026-10-24): the search has two series, d1 picks one `sure` of it."""
    a, _ = title_agent(monkeypatch, {"dune": [DUNE_2000, DUNE_DOCS, PROPHECY]}, [], prefer=["Dune: Prophecy"],
                       sure=sure)
    a.state = types.SimpleNamespace(timings=types.SimpleNamespace(add=lambda *args: None))
    a._shown, shown, played = None, [], []

    async def show_on_tv(s):
        shown.append(s)

    async def play_found(ask, imdb, kind, name, playing_video=None):
        played.append((ask, imdb))
        return agent_mod.Outcome(True, f"Playing {name}")
    a._show_on_tv, a._play_found = show_on_tv, play_found
    return a, shown, played


def test_a_title_d1_isnt_sure_of_is_listed_to_pick_not_guessed(monkeypatch):
    """2026-10-24: "Play Dune episode 2" played the 2000 Dune miniseries, d1 0.14 sure of it; Dune: Prophecy 0.25."""
    a, shown, played = unsure_agent(monkeypatch, 0.3)
    ask = PlayTitle(title="dune", episode=2, kind="series")
    out = asyncio.run(a._play_title(ask))
    assert out.ok and "say a number" in out.say and played == []
    assert [i.label for i in shown[0].items] == ["Dune: Prophecy (2024–, series)", "Dune (2000, series)",
                                                 "Dune (2021, series)"]               # d1's likeliest first
    asyncio.run(a.play_item(shown[0].items[1]))                                      # "number 2"
    assert played == [(ask.model_copy(update={"title": "Dune"}), DUNE_2000.id)]     # episode 2 of that one


def test_a_sure_pick_plays_and_no_d1_is_a_list_too(monkeypatch):
    a, shown, played = unsure_agent(monkeypatch, 0.8)
    asyncio.run(a._play_title(PlayTitle(title="dune", episode=2)))
    assert shown == [] and played[0][1] == PROPHECY.id
    a, shown, played = unsure_agent(monkeypatch, None)                               # d1 unreachable (502)
    out = asyncio.run(a._play_title(PlayTitle(title="dune", episode=2)))
    assert played == [] and len(shown[0].items) == 3 and out.ok


def test_in_a_chain_a_title_asked_about_stops_it(monkeypatch):
    a, shown, played = unsure_agent(monkeypatch, 0.1)

    async def run():
        agent_mod.chain.PLAN.set("go to 30:00")
        return await a._play_title(PlayTitle(title="dune", episode=2))
    out = asyncio.run(run())
    assert not out.ok and shown and played == []


def stream_agent(answers: list[list[S.Stream]], fits: bool = True):
    """An Agent with only what the stream lookups use: each lookup answers the next of `answers`; `fits`: whether
    an answer's best stream fits the rules (Agent._fits)."""
    a = Agent.__new__(Agent)
    a._streams, a._stream_jobs, a._originals = {}, {}, {}
    asked = []

    async def fetch(http, addons, kind, video, enough=None):
        asked.append(video)
        await asyncio.sleep(0.05)
        return answers[min(len(asked), len(answers)) - 1]

    async def addons(max_age=3600):
        return []
    a.http, a.addons, a._fits = None, addons, lambda meta: (lambda found: fits)
    a.fetch = fetch
    return a, asked


def test_streams_kept_past_their_fresh_window_are_used_at_once_when_they_fit_and_asked_again(monkeypatch):
    old = [S.Stream("AIOStreams", "https://x/old", "4K", "", "old.mkv", 0, height=2160)]
    new = [S.Stream("AIOStreams", "https://x/new", "4K", "", "new.mkv", 0, height=2160)]

    async def run(fits: bool, fresh: bool = False):
        a, asked = stream_agent([new], fits)
        monkeypatch.setattr(agent_mod.streams, "fetch", a.fetch)
        a._streams[("movie", "tt1")] = (time.monotonic() - agent_mod.STREAMS_TTL - 60, old)
        got = await a.fetch_streams("movie", "tt1", {}, fresh=fresh)
        await asyncio.sleep(0.1)                      # the lookup made meanwhile
        return [s.url for s in got], asked, [s.url for s in a._streams[("movie", "tt1")][1]]
    assert asyncio.run(run(True)) == (["https://x/old"], ["tt1"], ["https://x/new"])      # at once, then refreshed
    assert asyncio.run(run(False)) == (["https://x/new"], ["tt1"], ["https://x/new"])     # doesn't fit: waited for
    assert asyncio.run(run(True, fresh=True)) == (["https://x/new"], ["tt1"], ["https://x/new"])

    async def too_old():
        a, asked = stream_agent([new])
        monkeypatch.setattr(agent_mod.streams, "fetch", a.fetch)
        a._streams[("movie", "tt1")] = (time.monotonic() - agent_mod.STREAMS_STALE - 1, old)
        return [s.url for s in await a.fetch_streams("movie", "tt1", {})]
    assert asyncio.run(too_old()) == ["https://x/new"]

    async def settled(released: str):
        a, asked = stream_agent([new], fits=False)
        monkeypatch.setattr(agent_mod.streams, "fetch", a.fetch)
        a._streams[("series", "tt1:6:25")] = (time.monotonic() - agent_mod.STREAMS_TTL - 60, old)
        meta = {"videos": [{"id": "tt1:6:25", "season": 6, "episode": 25, "released": released}]}
        return [s.url for s in await a.fetch_streams("series", "tt1:6:25", meta)]
    # The best is only 1080p, but the episode is from 1995: no better copy is coming
    assert asyncio.run(settled("1995-05-21T00:00:00.000Z")) == ["https://x/old"]
    today = time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime())
    assert asyncio.run(settled(today)) == ["https://x/new"]            # out today: the new answer is waited for

    async def addons_fail():
        a, asked = stream_agent([[]], fits=False)
        monkeypatch.setattr(agent_mod.streams, "fetch", a.fetch)
        a._streams[("movie", "tt1")] = (time.monotonic() - agent_mod.STREAMS_TTL - 60, old)
        return [s.url for s in await a.fetch_streams("movie", "tt1", {})]
    assert asyncio.run(addons_fail()) == ["https://x/old"]           # better than nothing


def link_agent(monkeypatch, resolves: dict[str, str | None], rechecks: bool = True):
    """An Agent with only what link checks use: streams.resolve answers from `resolves` (stream url -> where it
    leads) after 50 ms; streams.recheck answers `rechecks`."""
    a = Agent.__new__(Agent)
    a._links, a._link_jobs, a.http = {}, {}, None
    calls = []

    async def resolve(http, s, hops=None):
        calls.append(("resolve", s.url))
        await asyncio.sleep(0.05)
        if hops is not None:
            hops.append({"host": "addon.example", "status": 302, "ms": 50})
        return resolves.get(s.url)

    async def recheck(http, link, headers=None):
        calls.append(("recheck", link))
        return rechecks
    monkeypatch.setattr(agent_mod.streams, "resolve", resolve)
    monkeypatch.setattr(agent_mod.streams, "recheck", recheck)
    return a, calls


def test_a_link_is_found_once_and_reused_checked_at_its_own_address(monkeypatch):
    s = S.Stream("AIOStreams", "https://addon/play/1", "n", "", "f.mkv", 0)

    async def run():
        a, calls = link_agent(monkeypatch, {s.url: "https://cdn/f.mkv"})
        first, second = await asyncio.gather(a.link(s), a.link(s))          # one request for both
        about = {}
        third = await a.link(s, about)                                       # a moment later: as it is
        assert about == {"how": "answered a moment ago"}
        a_while = time.monotonic() - agent_mod.LINK_TRUSTED - 1
        a._links[s.url] = (a_while, "https://cdn/f.mkv", a_while)
        fourth = await a.link(s)                                             # later: checked at its address
        fifth = await a.link(s)                                              # it just answered: as it is
        too_old = time.monotonic() - agent_mod.LINK_TTL - 1
        a._links[s.url] = (too_old, "https://cdn/f.mkv", time.monotonic() - agent_mod.LINK_TRUSTED - 1)
        sixth = await a.link(s)                                              # too old: the addon again
        return [first, second, third, fourth, fifth, sixth], calls
    links, calls = asyncio.run(run())
    assert links == ["https://cdn/f.mkv"] * 6
    assert calls == [("resolve", s.url), ("recheck", "https://cdn/f.mkv"), ("resolve", s.url)]


def test_a_link_that_stops_answering_at_its_address_is_found_again(monkeypatch):
    s = S.Stream("AIOStreams", "https://addon/play/1", "n", "", "f.mkv", 0)

    async def run():
        a, calls = link_agent(monkeypatch, {s.url: "https://cdn/new.mkv"}, rechecks=False)
        a_while = time.monotonic() - agent_mod.LINK_TRUSTED - 1
        a._links[s.url] = (a_while, "https://cdn/old.mkv", a_while)
        return await a.link(s), calls
    link, calls = asyncio.run(run())
    assert link == "https://cdn/new.mkv" and calls == [("recheck", "https://cdn/old.mkv"), ("resolve", s.url)]


def test_the_best_link_found_a_moment_ago_starts_without_asking_anything(monkeypatch):
    best, other = (S.Stream("AIOStreams", f"https://addon/play/{n}", "n", "", f"{n}.mkv", 0) for n in (1, 2))

    async def run():
        a, calls = link_agent(monkeypatch, {best.url: "https://cdn/1.mkv", other.url: "https://cdn/2.mkv"})
        a._links[best.url] = (time.monotonic(), "https://cdn/1.mkv", time.monotonic())
        about = {}
        return await a._first_playable([best, other], None, about), about, calls
    got, about, calls = asyncio.run(run())
    assert got == (best, "https://cdn/1.mkv") and about["how"] == "answered a moment ago" and calls == []


def test_a_list_kept_from_earlier_whose_links_fail_gives_way_to_the_new_answer(monkeypatch):
    old = [S.Stream("AIOStreams", f"https://addon/old/{n}", "n", "", f"{n}.mkv", 0) for n in (1, 2)]
    new = S.Stream("AIOStreams", "https://addon/new/1", "n", "", "n1.mkv", 0)

    async def again():
        return [old[0], new]                             # what was tried already isn't tried again

    async def run():
        a, calls = link_agent(monkeypatch, {new.url: "https://cdn/n1.mkv"})
        about = {}
        return await a._first_playable(old, again, about), about, calls
    got, about, calls = asyncio.run(run())
    assert got == (new, "https://cdn/n1.mkv") and about["asked_again"] is True
    assert [url for _, url in calls] == [old[0].url, old[1].url, new.url]


def test_a_video_looked_up_ahead_has_its_best_link_checked_too(monkeypatch):
    best = S.Stream("AIOStreams", "https://addon/play/1", "4K", "", "f.mkv", 0, height=2160)
    dropped = S.Stream("AIOStreams", "https://addon/play/0", "cam", "", "cam.mkv", 0, why_dropped="cam/ts")

    async def run():
        a, calls = link_agent(monkeypatch, {best.url: "https://cdn/f.mkv"})
        a.account, a.look_ahead = object(), True
        a.cfg = types.SimpleNamespace(player="tvplayer", probe_streams=True)

        async def fetch_streams(kind, video, meta=None, fresh=False):
            return [dropped, best]

        async def streams_for(kind, video, meta=None, fresh=False):
            return [dropped, best]

        async def meta(http, kind, imdb):
            return {}
        monkeypatch.setattr(agent_mod.cinemeta, "meta", meta)
        a.fetch_streams, a.streams_for = fetch_streams, streams_for
        a.prefetch("movie", "tt1")
        await asyncio.sleep(0.2)
        return a._links, calls
    links, calls = asyncio.run(run())
    assert calls == [("resolve", best.url)] and links[best.url][1] == "https://cdn/f.mkv"


def test_an_addon_that_times_out_is_asked_again_at_once_and_each_answer_is_traced():
    tries = []

    async def handler(request: httpx.Request) -> httpx.Response:
        tries.append(request.url.host)
        if len(tries) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"streams": [{"url": "https://debrid/f.mkv", "name": "4K ⚡"}]})
    manifest = {"resources": ["stream"], "types": ["movie"], "idPrefixes": ["tt"]}

    async def run():
        trace = tracing.Trace()
        tracing.TRACE.set(trace)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            found = await S.fetch(http, [Addon("AIOStreams", "https://aio.example/manifest.json", manifest)],
                                  "movie", "tt1")
        return found, trace.close()[0]
    found, steps = asyncio.run(run())
    assert len(found) == 1 and tries == ["aio.example", "aio.example"]
    step = next(s for s in steps if s["step"] == "addons")
    assert step["addons"][0]["addon"] == "AIOStreams" and step["addons"][0]["tries"] == 2 and step["playable"] == 1


def test_a_link_checks_requests_are_timed_by_host_without_their_paths():
    def handler(request):
        if request.url.host == "aio.example":
            return httpx.Response(302, headers={"location": "https://cdn.example/key123/file.mkv"})
        return httpx.Response(206, headers={"content-type": "video/x-matroska"}, content=b"\0")

    async def run():
        hops = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            s = S.Stream("AIO", "https://aio.example/play/secret", "n", "d", "f", 0)
            link = await S.resolve(http, s, hops)
            again = await S.recheck(http, link)
        return link, again, hops
    link, again, hops = asyncio.run(run())
    assert link == "https://cdn.example/key123/file.mkv" and again is True
    assert [(h["host"], h["status"]) for h in hops] == [("aio.example", 302), ("cdn.example", 206)]
    assert "secret" not in str(hops) and "key123" not in str(hops)


def test_an_episode_said_by_number_has_its_streams_looked_up_before_the_details_come(monkeypatch):
    a, _ = title_agent(monkeypatch, {}, [])
    a.account = object()
    looked = []
    a.prefetch = lambda kind, video: looked.append(video)

    async def get(imdb):
        return {"state": {"video_id": "tt2:3:4"}}
    a.library.get = get

    async def meta(http, kind, imdb):
        looked.append("meta")
        return {"videos": []}
    monkeypatch.setattr(agent_mod.cinemeta, "meta", meta)

    def asked(**fields):
        looked.clear()
        asyncio.run(a._play_found(PlayTitle(title="x", **fields), "tt2", "series", "X"))
        return looked[:2]
    assert asked(episode=6) == ["tt2:3:6", "meta"]                  # "episode 6": of the season you're on
    assert asked(season=2) == ["tt2:2:1", "meta"]                   # "season 2": its first episode
    assert asked(season=2, latest=True) == ["meta"]
